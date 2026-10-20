"""Cached ESPN NBA observations, retaining exact response bytes and provenance."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlencode

from . import db
from .providers.base import get_raw_meta

SITE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/'
LOGS = 'https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/'


def ensure_schema():
    db.ex('CREATE TABLE IF NOT EXISTS nba_http_artifacts ('
          'sha256 TEXT PRIMARY KEY, body BLOB NOT NULL)')
    db.ex('CREATE TABLE IF NOT EXISTS nba_http_observations ('
          'id INTEGER PRIMARY KEY, url TEXT NOT NULL, observed_at REAL NOT NULL, '
          'http_status INTEGER NOT NULL, sha256 TEXT NOT NULL REFERENCES nba_http_artifacts(sha256))')
    db.ex('CREATE INDEX IF NOT EXISTS ix_nba_http_url ON nba_http_observations(url,observed_at)')
    for table in ('nba_http_artifacts', 'nba_http_observations'):
        for action in ('UPDATE', 'DELETE'):
            db.ex(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                  "BEGIN SELECT RAISE(ABORT, 'NBA source evidence is immutable'); END")


def route(path):
    if path in {'scoreboard', 'injuries', 'teams', 'summary'}:
        return SITE + path
    if re.fullmatch(r'teams/\d+/(roster|schedule|statistics)', path):
        return SITE + path
    if re.fullmatch(r'athletes/\d+/gamelog', path):
        return LOGS + path.split('/')[1] + '/gamelog'
    raise ValueError('Unsupported NBA source route')


def body(sha256):
    row = db.q1('SELECT body FROM nba_http_artifacts WHERE sha256=?', (sha256,))
    if not row or hashlib.sha256(row['body']).hexdigest() != sha256:
        raise ValueError('NBA source evidence missing or hash mismatch')
    return row['body']


def fetch(path, params=None, *, ttl=0, now=None, getter=None):
    ensure_schema()
    url = route(path)
    if params:
        if set(params) - {'dates', 'limit', 'season', 'seasontype', 'event'}:
            raise ValueError('Unsupported NBA source parameter')
        url += '?' + urlencode(sorted(params.items()))
    stamp = time.time() if now is None else now
    previous = db.q1('SELECT * FROM nba_http_observations WHERE url=? AND observed_at<=? '
                      'ORDER BY observed_at DESC,id DESC LIMIT 1', (url, stamp))
    if previous and previous['http_status'] == 200 and 0 <= stamp - previous['observed_at'] < ttl:
        try:
            cached=json.loads(body(previous['sha256']))
        except (ValueError,UnicodeDecodeError):
            cached=None
        if isinstance(cached,dict):
            return {'data':cached, 'sha256':previous['sha256'],
                    'url':url, 'observed_at':previous['observed_at'], 'cached':True}
    status, raw, _headers = (getter or get_raw_meta)(url, provider='espn-nba', timeout=15)
    receipt = stamp if getter is not None else time.time()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('NBA source response exceeds size limit')
    sha = hashlib.sha256(raw).hexdigest()
    with db.tx() as conn:
        conn.execute('INSERT OR IGNORE INTO nba_http_artifacts VALUES(?,?)', (sha, raw))
        conn.execute('INSERT INTO nba_http_observations(url,observed_at,http_status,sha256) VALUES(?,?,?,?)',
                     (url, receipt, status, sha))
    if status != 200:
        raise ValueError(f'NBA source HTTP {status}')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Invalid NBA source JSON')
    return {'data': data, 'sha256': sha, 'url': url, 'observed_at': receipt, 'cached': False}


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def team(value):
    # ESPN and SportsGameOdds use several different short names.
    aliases = {'NY': 'NYK', 'GS': 'GSW', 'SA': 'SAS', 'NO': 'NOP', 'UTAH': 'UTA',
               'WSH': 'WAS', 'PHO': 'PHX', 'BRK': 'BKN'}
    value = str(value or '').upper()
    value = aliases.get(value, value)
    return value if value in {'ATL','BOS','BKN','CHA','CHI','CLE','DAL','DEN','DET','GSW',
        'HOU','IND','LAC','LAL','MEM','MIA','MIL','MIN','NOP','NYK','OKC','ORL','PHI','PHX',
        'POR','SAC','SAS','TOR','UTA','WAS'} else None


def team_history(payload, source_sha):
    rows = []
    for event in payload.get('events', []):
        season_type = event.get('seasonType', {}).get('type', 2)
        if season_type != 2:
            continue
        comp = (event.get('competitions') or [{}])[0]
        if comp.get('status', {}).get('type', {}).get('completed') is not True:
            continue
        competitors = comp.get('competitors', [])
        sides = {c.get('homeAway'): c for c in competitors}
        if set(sides) != {'home', 'away'} or len(competitors) != 2:
            continue
        parsed = {}
        for side, item in sides.items():
            score = item.get('score')
            score = score.get('value') if isinstance(score, dict) else score
            score = number(score)
            code = team(item.get('team', {}).get('abbreviation'))
            if not code or score is None or not 1 <= score <= 250 or score != int(score):
                break
            parsed[side] = (code, score)
        if len(parsed) != 2 or parsed['home'][0] == parsed['away'][0]:
            continue
        for side, opposite in [('home', 'away'), ('away', 'home')]:
            rows.append({'game_id': str(event['id']), 'played_at': event['date'],
                         'date': event['date'], 'team': parsed[side][0], 'opponent': parsed[opposite][0],
                         'points': parsed[side][1], 'opponent_points': parsed[opposite][1],
                         'home': side == 'home', 'source_sha256': source_sha})
    return rows


def _minutes(value):
    if isinstance(value, str) and re.fullmatch(r'\d+:\d{2}', value):
        mins, secs = value.split(':')
        return int(mins) + int(secs) / 60 if int(secs) < 60 else None
    return number(value)


def player_history(payload, player_id, source_sha):
    rows, seen = [], set()
    names = payload.get('names', [])
    for season in payload.get('seasonTypes', []):
        if 'Regular Season' not in season.get('displayName', ''):
            continue
        for category in season.get('categories', []):
            if category.get('type') != 'event':
                continue
            for entry in category.get('events', []):
                event_id = str(entry.get('eventId', ''))
                event = payload.get('events', {}).get(event_id, {})
                stats = dict(zip(names, entry.get('stats', [])))
                minutes = _minutes(stats.get('minutes'))
                values = {key: number(stats.get(name)) for key, name in
                          [('points', 'points'), ('rebounds', 'totalRebounds'), ('assists', 'assists')]}
                threes = stats.get('threePointFieldGoalsMade-threePointFieldGoalsAttempted')
                values['threes'] = number(str(threes).split('-')[0]) if threes is not None else None
                code = team((event.get('team') or {}).get('abbreviation'))
                if (event_id in seen or event.get('leagueAbbreviation') != 'NBA' or not code or
                        not event.get('gameDate') or minutes is None or not 0 < minutes <= 65 or
                        any(v is None or v < 0 or v != int(v) for v in values.values())):
                    continue
                seen.add(event_id)
                rows.append({'player_id': str(player_id), 'team': code, 'game_id': event_id,
                             'date': event['gameDate'], 'played_at': event['gameDate'],
                             'minutes': minutes, 'participation': 'played', 'source_sha256': source_sha,
                             'pra': sum(values[k] for k in ('points', 'rebounds', 'assists')), **values})
    return rows
