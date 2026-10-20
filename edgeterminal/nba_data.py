"""ESPN public NBA sources: fetch-and-archive plus pure parsers.

`fetch` returns an artifact (exact body bytes, URL, receipt time, sha256) for
`nba_store.save_artifact`; parsers take the decoded JSON and never fetch.
Response shapes were checked against live ESPN responses on 2026-10-20 --
see docs/research/nba_data_contract.md. No odds endpoints are called here.

Participation is explicit: a player absent from a box score is `not_listed`,
never zero and never inactive.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import nba_identity as ident

SITE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
WEB = 'https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba'
PROPS = ('points', 'rebounds', 'assists', 'threes', 'pra')
_STATUS = {'pre': 'scheduled', 'in': 'in_progress', 'post': 'final'}
_ODD_STATUS = {'STATUS_POSTPONED': 'postponed', 'STATUS_CANCELED': 'canceled', 'STATUS_CANCELLED': 'canceled',
               'STATUS_SUSPENDED': 'suspended', 'STATUS_DELAYED': 'delayed'}

Getter = Callable[[str], Tuple[int, bytes]]


# ---------------------------------------------------------------- urls
def scoreboard_url(game_date_et: str) -> str:
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])', game_date_et or ''):
        raise ValueError(f'invalid date: {game_date_et!r}')
    return f"{SITE}/scoreboard?dates={game_date_et.replace('-', '')}&limit=200"


def summary_url(espn_event_id: str) -> str:
    return f'{SITE}/summary?event={_digits(espn_event_id)}'


def roster_url(team: str) -> str:
    return f'{SITE}/teams/{ident.espn_team_id(team)}/roster'


def team_stats_url(team: str, espn_season_year: Optional[int] = None) -> str:
    url = f'{SITE}/teams/{ident.espn_team_id(team)}/statistics'
    return f'{url}?season={int(espn_season_year)}' if espn_season_year else url


def gamelog_url(espn_player_id: str, espn_season_year: Optional[int] = None) -> str:
    url = f'{WEB}/athletes/{_digits(espn_player_id)}/gamelog'
    return f'{url}?season={int(espn_season_year)}' if espn_season_year else url


def _digits(value) -> str:
    if not re.fullmatch(r'\d{1,12}', str(value)):
        raise ValueError(f'invalid ESPN id: {value!r}')
    return str(value)


# ---------------------------------------------------------------- fetch / archive
def _http_get(url: str) -> Tuple[int, bytes]:
    from .providers.base import get_raw_meta
    status, body, _headers = get_raw_meta(url, provider='espn-nba')
    return status, body


def fetch(url: str, kind: str, getter: Optional[Getter] = None, now: Optional[float] = None) -> Dict:
    """One GET, archived exactly as received (including non-200 bodies)."""
    status, body = (getter or _http_get)(url)
    received = time.time() if now is None else float(now)
    return {'kind': kind, 'url': url, 'received_at': received, 'http_status': int(status),
            'sha256': hashlib.sha256(body).hexdigest(), 'body': body}


def payload(artifact: Dict):
    """Decoded JSON of a successful, hash-intact artifact."""
    if artifact.get('http_status') != 200:
        raise ValueError(f"source returned HTTP {artifact.get('http_status')}")
    if hashlib.sha256(artifact['body']).hexdigest() != artifact.get('sha256'):
        raise ValueError('artifact body does not match its sha256')
    return json.loads(artifact['body'])


# ---------------------------------------------------------------- strict numbers
def _int(value) -> int:
    text = str(value).strip()
    if not re.fullmatch(r'[+-]?\d+', text):
        raise ValueError(f'not an integer stat: {value!r}')
    return int(text)


def _made(value) -> int:
    match = re.fullmatch(r'(\d+)-(\d+)', str(value).strip())
    if not match or int(match.group(1)) > int(match.group(2)):
        raise ValueError(f'not a made-attempted stat: {value!r}')
    return int(match.group(1))


def _score(value) -> int:
    score = _int(value)
    if score < 0 or score > 250:
        raise ValueError(f'implausible score: {value!r}')
    return score


def _props(minutes: int, points: int, rebounds: int, assists: int, threes: int) -> Dict:
    return {'minutes': minutes, 'points': points, 'rebounds': rebounds, 'assists': assists,
            'threes': threes, 'pra': points + rebounds + assists}


# ---------------------------------------------------------------- games
def parse_event(ev: Dict) -> Dict:
    """One ESPN event/header into a game dict. Raises ValueError on any refusal."""
    season = ev.get('season') or {}
    season_type = ident.SEASON_TYPES.get(season.get('type'))
    if not season_type:
        raise ValueError(f"season type {season.get('type')} is not an NBA regular/play-in/postseason game")
    comp = (ev.get('competitions') or [{}])[0]
    competition = str((comp.get('type') or {}).get('abbreviation') or '')
    if competition == 'ALLSTAR':
        raise ValueError('All-Star event')
    sides = {c.get('homeAway'): c for c in comp.get('competitors') or []}
    if set(sides) != {'home', 'away'} or len(comp.get('competitors')) != 2:
        raise ValueError('event must have exactly one home and one away competitor')
    home, away = ident.team_code(sides['home']['team']), ident.team_code(sides['away']['team'])
    if home == away:
        raise ValueError('team cannot play itself')
    tip = ident.parse_tipoff(ev.get('date') or comp.get('date'))
    st = ((ev.get('status') or comp.get('status') or {}).get('type')) or {}
    status = _ODD_STATUS.get(st.get('name')) or _STATUS.get(st.get('state'))
    if not status:
        raise ValueError(f"unknown status {st.get('name')}")
    home_score = away_score = None
    if status == 'final':
        if st.get('completed') is not True:
            raise ValueError('post state without completed flag')
        home_score, away_score = _score(sides['home'].get('score')), _score(sides['away'].get('score'))
        if home_score == away_score:
            raise ValueError('NBA final cannot be tied')
    year = season.get('year')
    return {'game_id': ident.game_id(year, str(ev.get('id'))), 'espn_event_id': str(ev['id']),
            'season_start_year': int(year) - 1, 'season_label': ident.season_label(year),
            'season_type': season_type, 'competition': competition, 'home_team': home,
            'away_team': away, 'tipoff_utc': ident.iso_utc(tip), 'game_date_et': ident.eastern_date(tip),
            'status': status, 'home_score': home_score, 'away_score': away_score}


def parse_scoreboard(data: Dict, game_date_et: str) -> Dict:
    """{'games': [game...], 'rejected': [{'espn_event_id', 'reason'}]} for one Eastern date."""
    games, rejected = [], []
    for ev in data.get('events') or []:
        try:
            game = parse_event(ev)
            if game['game_date_et'] != game_date_et:
                raise ValueError(f"tipoff is on {game['game_date_et']} Eastern")
            games.append(game)
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append({'espn_event_id': str(ev.get('id')), 'reason': str(exc)})
    return {'games': games, 'rejected': rejected}


# ---------------------------------------------------------------- box score
def parse_summary(data: Dict) -> Dict:
    """ESPN summary -> {'game', 'players': [...], 'box_complete': bool}.

    Player status: played (numeric minutes and stats), dnp (didNotPlay with
    coach's decision or no reason), inactive (didNotPlay with an explicit other
    ESPN reason, e.g. injury -- ESPN-derived, not the official NBA inactive list), unverified
    (listed as playing but minutes missing). `box_complete` requires each
    team's totals and summed player points to equal the final score.
    """
    game = parse_event(data['header'])
    by_team = {game['home_team']: game['home_score'], game['away_team']: game['away_score']}
    players, seen, complete = [], set(), game['status'] == 'final'
    box = (data.get('boxscore') or {}).get('players') or []
    for team_box in box:
        team = ident.team_code(team_box.get('team') or {})
        if team not in by_team:
            raise ValueError(f'box score team {team} is not in this game')
        tables = team_box.get('statistics') or []
        if len(tables) != 1:
            raise ValueError('expected exactly one player statistics table per team')
        table = tables[0]
        keys = table.get('keys') or []
        idx = {k: keys.index(k) for k in ('minutes', 'points', 'rebounds', 'assists',
                                          'threePointFieldGoalsMade-threePointFieldGoalsAttempted')}
        points_sum = 0
        for row in table.get('athletes') or []:
            pid = _digits((row.get('athlete') or {}).get('id'))
            if pid in seen:
                raise ValueError(f'player {pid} listed twice')
            seen.add(pid)
            stats, reason = row.get('stats') or [], row.get('reason')
            line = {'espn_player_id': pid, 'name': (row.get('athlete') or {}).get('displayName'),
                    'team': team, 'starter': bool(row.get('starter')), 'status': None,
                    'reason': None, 'stats': None}
            if row.get('didNotPlay') is True:
                if stats:
                    raise ValueError(f'player {pid} marked did-not-play but has stats')
                # Only an explicit non-coach reason is inactive; a missing reason stays plain dnp.
                reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
                line['status'] = 'inactive' if reason and reason != "COACH'S DECISION" else 'dnp'
                line['reason'] = reason
            elif row.get('didNotPlay') is False and len(stats) == len(keys):
                points = _int(stats[idx['points']])
                points_sum += points
                if str(stats[idx['minutes']]).strip() == '--':
                    line['status'] = 'unverified'
                else:
                    line['status'] = 'played'
                    line['stats'] = _props(_int(stats[idx['minutes']]), points, _int(stats[idx['rebounds']]),
                                           _int(stats[idx['assists']]), _made(stats[idx[
                                               'threePointFieldGoalsMade-threePointFieldGoalsAttempted']]))
            else:
                raise ValueError(f'player {pid} row has an unknown shape')
            players.append(line)
        if complete:
            totals = table.get('totals') or []
            total = _int(totals[idx['points']]) if len(totals) == len(keys) else None
            complete = total == by_team[team] == points_sum
    if complete and len(box) != 2:
        complete = False
    return {'game': game, 'players': players, 'box_complete': complete}


def player_line(summary: Dict, espn_player_id: str) -> Dict:
    for line in summary['players']:
        if line['espn_player_id'] == str(espn_player_id):
            return line
    return {'espn_player_id': str(espn_player_id), 'status': 'not_listed', 'stats': None}


# ---------------------------------------------------------------- roster / team stats
def parse_roster(data: Dict) -> Dict:
    """Current ESPN roster with ESPN-reported injury statuses (as reported, not verified)."""
    team = ident.team_code(data.get('team') or {})
    players = []
    for a in data.get('athletes') or []:
        players.append({'espn_player_id': _digits(a.get('id')), 'name': a.get('displayName'),
                        'jersey': a.get('jersey'), 'position': (a.get('position') or {}).get('abbreviation'),
                        'roster_status': (a.get('status') or {}).get('type'),
                        'injuries': [{'status': i.get('status'), 'date': i.get('date')}
                                     for i in a.get('injuries') or []]})
    return {'team': team, 'season_label': ident.season_label((data.get('season') or {})['year']),
            'as_of': data.get('timestamp'), 'players': players}


def parse_team_stats(data: Dict) -> Dict:
    """Team season stats. ESPN labels `season` as the current season even when
    the numbers are `requestedSeason`'s (2026-10-20 returned 82 games of 2025-26
    under a 2026-27 label), so the data season comes from requestedSeason."""
    team = ident.team_code(data.get('team') or {})
    stats = {}
    for cat in ((data.get('results') or {}).get('stats') or {}).get('categories') or []:
        for s in cat.get('stats') or []:
            value = s.get('value')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                raise ValueError(f"non-numeric team stat {cat.get('name')}.{s.get('name')}: {value!r}")
            stats[f"{cat['name']}.{s['name']}"] = float(value)
    games = stats.get('general.gamesPlayed')
    requested = (data.get('requestedSeason') or data.get('season') or {})['year']
    return {'team': team, 'data_season_label': ident.season_label(requested),
            'current_season_label': ident.season_label((data.get('season') or {})['year']),
            'games_played': int(games) if games is not None else None, 'stats': stats}


# ---------------------------------------------------------------- player game log
_GL_TYPES = {'Regular Season': 'regular', 'Postseason': 'postseason', 'Play In': 'play-in',
             'Play-In': 'play-in', 'Play In Tournament': 'play-in'}


def parse_gamelog(data: Dict, espn_player_id: str) -> Dict:
    """Games ESPN lists for one player (played games only; absence is not a zero)."""
    pid = _digits(espn_player_id)
    names = data.get('names') or []
    idx = {k: names.index(k) for k in ('minutes', 'points', 'totalRebounds', 'assists',
                                       'threePointFieldGoalsMade-threePointFieldGoalsAttempted')}
    meta = data.get('events') or {}
    games, rejected = [], []
    for block in data.get('seasonTypes') or []:
        match = re.fullmatch(r'(\d{4})-(\d{2}) (.+)', block.get('displayName') or '')
        kind = _GL_TYPES.get(match.group(3)) if match else None
        for cat in block.get('categories') or []:
            if cat.get('type') != 'event':
                continue
            for row in cat.get('events') or []:
                eid = str(row.get('eventId'))
                if not kind:
                    rejected.append({'espn_event_id': eid, 'reason': f"season type {match.group(3) if match else block.get('displayName')}"})
                    continue
                ev, stats = meta.get(eid) or {}, row.get('stats') or []
                if ev.get('leagueAbbreviation') != 'NBA' or len(stats) != len(names):
                    rejected.append({'espn_event_id': eid, 'reason': 'not an NBA event or malformed row'})
                    continue
                team, opp = ident.team_code(ev.get('team') or {}), ident.team_code(ev.get('opponent') or {})
                home_id = str(ev.get('homeTeamId'))
                if {home_id, str(ev.get('awayTeamId'))} != {ident.espn_team_id(team), ident.espn_team_id(opp)}:
                    raise ValueError(f'game log event {eid} team ids conflict')
                start = int(match.group(1))
                games.append({'espn_player_id': pid, 'espn_event_id': eid,
                              'game_id': ident.game_id(start + 1, eid),
                              'season_label': f'{start}-{match.group(2)}', 'season_type': kind,
                              'tipoff_utc': ident.iso_utc(ident.parse_tipoff(ev.get('gameDate'))),
                              'team': team, 'opponent': opp, 'home': home_id == ident.espn_team_id(team),
                              'stats': _props(_int(stats[idx['minutes']]), _int(stats[idx['points']]),
                                              _int(stats[idx['totalRebounds']]), _int(stats[idx['assists']]),
                                              _made(stats[idx['threePointFieldGoalsMade-threePointFieldGoalsAttempted']]))})
    return {'games': games, 'rejected': rejected}
