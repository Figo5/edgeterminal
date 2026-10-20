"""One bounded NBA main-line batch, sharing the existing SportsGameOdds budget."""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone

from . import db, nba_sources
from .config import settings
from .integrity import parse_time
from .providers.base import get_raw_meta

URL = 'https://api.sportsgameodds.com/v2/events'
NBA_MONTH_REQUESTS = 48
NBA_DAY_REQUESTS = 2
NBA_MONTH_OBJECTS = 480
MARKETS = {'points': 'points', 'rebounds': 'rebounds', 'assists': 'assists',
           'threePointersMade': 'threes', 'points+rebounds+assists': 'pra'}
TEAM_NAMES = dict(zip(
    ['Atlanta Hawks','Boston Celtics','Brooklyn Nets','Charlotte Hornets','Chicago Bulls',
     'Cleveland Cavaliers','Dallas Mavericks','Denver Nuggets','Detroit Pistons','Golden State Warriors',
     'Houston Rockets','Indiana Pacers','Los Angeles Clippers','Los Angeles Lakers','Memphis Grizzlies',
     'Miami Heat','Milwaukee Bucks','Minnesota Timberwolves','New Orleans Pelicans','New York Knicks',
     'Oklahoma City Thunder','Orlando Magic','Philadelphia 76ers','Phoenix Suns','Portland Trail Blazers',
     'Sacramento Kings','San Antonio Spurs','Toronto Raptors','Utah Jazz','Washington Wizards'],
    ['ATL','BOS','BKN','CHA','CHI','CLE','DAL','DEN','DET','GSW','HOU','IND','LAC','LAL','MEM',
     'MIA','MIL','MIN','NOP','NYK','OKC','ORL','PHI','PHX','POR','SAC','SAS','TOR','UTA','WAS']))


def ensure_schema():
    db.ex('CREATE TABLE IF NOT EXISTS nba_prop_requests ('
          'request_id TEXT PRIMARY KEY, attempted_at REAL NOT NULL, completed_at REAL, '
          'http_status INTEGER, events_returned INTEGER NOT NULL DEFAULT 0, '
          'quota_json TEXT NOT NULL DEFAULT "{}", error TEXT, request_params TEXT, '
          'raw_sha256 TEXT, raw_body BLOB, target_game_ids TEXT NOT NULL DEFAULT "[]")')
    if 'target_game_ids' not in {r['name'] for r in db.q('PRAGMA table_info(nba_prop_requests)')}:
        db.ex('ALTER TABLE nba_prop_requests ADD COLUMN target_game_ids TEXT NOT NULL DEFAULT "[]"')
    db.ex("CREATE TRIGGER IF NOT EXISTS nba_prop_response_fixed BEFORE UPDATE ON nba_prop_requests "
          "WHEN OLD.raw_body IS NOT NULL AND (NEW.raw_body IS NOT OLD.raw_body OR NEW.raw_sha256 IS NOT OLD.raw_sha256 "
          "OR NEW.request_params IS NOT OLD.request_params OR NEW.attempted_at IS NOT OLD.attempted_at) "
          "BEGIN SELECT RAISE(ABORT, 'NBA raw prop evidence is immutable'); END")
    db.ex("CREATE TRIGGER IF NOT EXISTS nba_prop_request_no_delete BEFORE DELETE ON nba_prop_requests "
          "BEGIN SELECT RAISE(ABORT, 'NBA prop attempts cannot be deleted'); END")


def shared_rows(now):
    """Adapter consumed by the NFL guard; includes attempts that never received a response."""
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nba_prop_requests'"):
        return []
    return [dict(row, requests_made=1, target_game_ids=row.get('target_game_ids') or '[]', success=not bool(row['error']),
                 quota_json=json.dumps([json.loads(row['quota_json'])])) for row in
            db.q('SELECT * FROM nba_prop_requests WHERE attempted_at>=? ORDER BY attempted_at DESC',
                 (now-30*86400,))]


def budget(now):
    from . import nfl_prop_capture_ops
    ensure_schema()
    rows = nfl_prop_capture_ops._attempts(now)
    constrained, reason = nfl_prop_capture_ops._constrained(rows, now)
    nba = shared_rows(now)
    if not constrained:
        if len(nba) >= NBA_MONTH_REQUESTS:
            reason = 'nba_monthly_budget'
        elif sum(r['attempted_at'] > now-86400 for r in nba) >= NBA_DAY_REQUESTS:
            reason = 'nba_daily_budget'
        elif sum(max(1,r['events_returned']) for r in nba) >= NBA_MONTH_OBJECTS:
            reason = 'nba_object_budget'
        elif sum(r['requests_made'] for r in rows) >= nfl_prop_capture_ops.MAX_LOCAL_REQUESTS_30D - 40:
            reason = 'nfl_capacity_reserved'
    return {'blocked_reason': reason,
            'requests_30d': sum(r['requests_made'] for r in rows), 'nba_requests_30d': len(nba),
            'nba_month_limit': NBA_MONTH_REQUESTS, 'nba_day_limit': NBA_DAY_REQUESTS,
            'shared_month_limit': nfl_prop_capture_ops.MAX_LOCAL_REQUESTS_30D}


def _team(item):
    names = item.get('names') or {}
    for candidate in [names.get('long'), names.get('short'), item.get('name'),
                      str(item.get('teamID','')).split('_')[0]]:
        code = nba_sources.team(candidate) or TEAM_NAMES.get(candidate)
        if code:
            return code
    return None


def _name(value):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', str(value)).encode('ascii','ignore').decode().lower())


def normalize(events, games, roster, observed_at):
    quotes, warnings = [], Counter()
    for event in events:
        status = event.get('status') or {}
        tip = parse_time(event.get('startTime') or status.get('startsAt'))
        teams = event.get('teams') or {}
        home, away = _team(teams.get('home') or {}), _team(teams.get('away') or {})
        matches = [g for g in games if g['home_team'] == home and g['away_team'] == away and tip and
                   abs((parse_time(g.get('tipoff') or g.get('kickoff_utc')) or 0)-tip) <= 300]
        if (event.get('leagueID') != 'NBA' or len(matches) != 1 or not tip or tip <= observed_at or
                status.get('started') is True or status.get('ended') is True or status.get('cancelled') is True):
            warnings['event_identity_or_tipoff'] += 1
            continue
        game = matches[0]
        team_ids = {str(teams[s].get('teamID')): _team(teams[s]) for s in ('home','away')}
        for key, odd in (event.get('odds') or {}).items():
            if not isinstance(odd, dict):
                continue
            stat, side, ref = odd.get('statID'), odd.get('sideID'), odd.get('statEntityID')
            if stat not in MARKETS or side not in {'over','under'} or odd.get('periodID') != 'game' or odd.get('betTypeID') != 'ou':
                continue
            if key != f'{stat}-{ref}-game-ou-{side}' or odd.get('oddID',key) != key:
                warnings['odd_identity'] += 1
                continue
            player = (event.get('players') or {}).get(ref) or {}
            code = team_ids.get(str(player.get('teamID')))
            name = player.get('name') or (player.get('names') or {}).get('long')
            candidates = [r for r in roster if r.get('team') == code and
                          _name(r.get('player') or r.get('full_name')) == _name(name)] if name and code else []
            if len(candidates) != 1 or player.get('playerID',ref) != ref:
                warnings['player_identity'] += 1
                continue
            matched = candidates[0]
            for book, offer in (odd.get('byBookmaker') or {}).items():
                if not isinstance(offer,dict) or offer.get('available') is not True:
                    continue
                line, price = nba_sources.number(offer.get('overUnder')), nba_sources.number(offer.get('odds'))
                updated = parse_time(offer.get('lastUpdatedAt'))
                if (line is None or not 0 <= line <= 150 or line*2 != int(line*2) or
                        price is None or abs(price) < 100 or abs(price) > 100000 or
                        updated is not None and updated > observed_at+60):
                    warnings['invalid_offer'] += 1
                    continue
                quotes.append({'game_id': game['id'], 'player_id': str(matched['player_id']),
                    'player': matched.get('player') or matched.get('full_name'), 'team': code,
                    'market': MARKETS[stat], 'line': line, 'side': side, 'price': int(price),
                    'sportsbook': book, 'observed_at': observed_at, 'identity_verified': True,
                    'line_type': 'main', 'is_main': True, 'provider_player_id': ref,
                    'provider_event_id': event.get('eventID'), 'source': URL,
                    'availability': matched.get('availability','unknown'),
                    'availability_observed_at': matched.get('observed_at')})
    return quotes, dict(warnings)


def capture(games, roster, now, *, getter=None, api_key=None):
    guard = budget(now)
    key = settings.sportsgameodds_api_key if api_key is None else api_key
    if guard['blocked_reason'] or not key:
        return {'status': guard['blocked_reason'] or 'missing_api_key', 'quotes': []}
    games = [g for g in games if (parse_time(g.get('tipoff') or g.get('kickoff_utc')) or 0) > now]
    if not games:
        return {'status': 'no_pregame_targets', 'quotes': []}
    times = [parse_time(g.get('tipoff') or g.get('kickoff_utc')) for g in games]
    utc = lambda t: datetime.fromtimestamp(t,timezone.utc).isoformat().replace('+00:00','Z')
    params = {'leagueID':'NBA','startsAfter':utc(min(times)-900),'startsBefore':utc(max(times)+900),
              'started':'false','cancelled':'false','oddsAvailable':'true','includeAltLines':'false',
              'bookmakerID':'draftkings','limit':100}
    request_id = uuid.uuid4().hex
    db.ex('INSERT INTO nba_prop_requests(request_id,attempted_at,request_params,target_game_ids) VALUES(?,?,?,?)',
          (request_id,now,json.dumps(params,sort_keys=True),json.dumps([g['id'] for g in games])))
    try:
        status, raw, headers = (getter or get_raw_meta)(URL, params=params, headers={'x-api-key':key},
                                                      provider='sportsgameodds-nba', timeout=20)
        receipt = now if getter is not None else time.time()
        quota = {k.lower():v for k,v in headers.items() if any(
            p in k.lower() for p in ('ratelimit','rate-limit','quota','remaining','retry-after','credits'))}
        error = None if status == 200 else f'HTTP {status}'
        data = json.loads(raw) if status == 200 else {}
        if status == 200 and (data.get('success') is not True or not isinstance(data.get('data'),list)):
            error = 'invalid_provider_payload'
        if data.get('nextCursor'):
            error = 'incomplete_slate_response'
        sha = hashlib.sha256(raw).hexdigest()
        db.ex('UPDATE nba_prop_requests SET completed_at=?,http_status=?,events_returned=?,quota_json=?,error=?,raw_sha256=?,raw_body=? WHERE request_id=?',
              (receipt,status,len(data.get('data',[])),json.dumps(quota),error,sha,raw,request_id))
        if error:
            return {'status':'source_error','error':error,'quotes':[],'request_id':request_id}
        quotes, warnings = normalize(data['data'],games,roster,receipt)
        return {'status':'captured','quotes':quotes,'warnings':warnings,'request_id':request_id,
                'source_sha256':sha,'observed_at':receipt,'events_returned':len(data['data'])}
    except Exception as exc:
        db.ex('UPDATE nba_prop_requests SET completed_at=?,error=? WHERE request_id=?',
              (time.time(),type(exc).__name__,request_id))
        return {'status':'source_error','error':type(exc).__name__,'quotes':[],'request_id':request_id}
