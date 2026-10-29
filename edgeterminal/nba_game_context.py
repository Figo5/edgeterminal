"""Timestamped schedule and roster context for live NBA game projections.

Rest comes from a team's schedule, including intervening scheduled games, rather
than assuming the most recent scored row is its most recent game. Reads never
fetch sources or change a forecast. Missing context remains explicitly unknown.
"""
from __future__ import annotations

import hashlib
import json
import math

from . import db, nba_data, nba_identity, nba_store
from .integrity import parse_time


def source_ttl(code, now, kind='history'):
    row = db.q1("SELECT MIN(tipoff_ts) AS tipoff_ts FROM nba_games WHERE status='scheduled' "
                "AND tipoff_ts>? AND (home_team=? OR away_team=?)", (now, code, code))
    until = row['tipoff_ts']-now if row and row.get('tipoff_ts') is not None else math.inf
    if until <= 3600:
        return 300
    if until <= 6*3600:
        return 900
    return 3*3600 if kind == 'roster' else 6*3600


def parse_schedule(code, payload, source_artifact_id, observed_at):
    if code not in nba_identity.TEAMS or not isinstance(payload.get('events'), list):
        raise ValueError('NBA schedule context lacks a verified team or event list')
    owner = payload.get('team')
    if owner and nba_identity.team_code(owner) != code:
        raise ValueError('NBA schedule owner identity conflict')
    events = {}
    for event in payload['events']:
        if event.get('seasonType', {}).get('type', (event.get('season') or {}).get('type', 2)) != 2:
            continue
        comp = (event.get('competitions') or [{}])[0]
        competitors = comp.get('competitors') or []
        if len(competitors) != 2:
            raise ValueError('NBA schedule team identity is incomplete')
        teams = [nba_identity.team_code(item.get('team') or {}) for item in competitors]
        if code not in teams or teams[0] == teams[1]:
            raise ValueError('NBA schedule team identity conflict')
        tip = parse_time(event.get('date') or comp.get('date'))
        if tip is None:
            raise ValueError('NBA schedule context has no valid tipoff')
        status = (comp.get('status') or event.get('status') or {}).get('type') or {}
        item = {'game_id': str(event['id']), 'tipoff_ts': tip,
                'completed': status.get('completed') is True,
                'status': status.get('name'), 'state': status.get('state')}
        if item['game_id'] in events and events[item['game_id']] != item:
            raise ValueError('NBA schedule contains conflicting event identities')
        events[item['game_id']] = item
    return {'team': code, 'source_artifact_id': source_artifact_id,
            'observed_at': observed_at, 'events': sorted(events.values(), key=lambda e: (e['tipoff_ts'], e['game_id']))}


def team_context(schedule, code, tipoff, history, now):
    cutoff = min(now-4*3600, tipoff-4*3600)
    known = {str(r['game_id']) for r in history if r.get('team') == code}
    prior = [e for e in schedule.get('events', []) if e['tipoff_ts'] < tipoff
             and e.get('status') not in {'STATUS_CANCELED', 'STATUS_CANCELLED', 'STATUS_POSTPONED'}]
    missing = sorted({e['game_id'] for e in prior if e['tipoff_ts'] <= cutoff
                      and (not e['completed'] or e['game_id'] not in known)})
    last = prior[-1] if prior else {}
    observed = schedule.get('observed_at')
    limit = 300 if tipoff-now <= 3600 else 900 if tipoff-now <= 6*3600 else 6*3600
    stale = observed is None or not 0 <= now-observed <= limit
    return {'source_artifact_id': schedule.get('source_artifact_id'), 'observed_at': observed,
            'last_game_id': last.get('game_id'), 'last_game_tipoff': last.get('tipoff_ts'),
            'complete': bool(schedule.get('events')) and not missing and not stale,
            'missing_game_ids': missing, 'stale': stale,
            'rest_source': 'ESPN schedule' if last else 'unknown'}


def _verified(ident, now):
    row = nba_store.get_artifact(ident) if isinstance(ident, int) and not isinstance(ident, bool) else None
    if (not row or row['http_status'] != 200 or row['received_at'] > now or
            hashlib.sha256(row['body']).hexdigest() != row['sha256']):
        raise ValueError('NBA game context source is missing, altered, or from the future')
    return row


def schedules(game, history, now):
    result = {}
    for code in (game['home_team'], game['away_team']):
        saved = db.kv_get('nba_team_schedule_context_'+code) or {}
        if saved.get('source_artifact_id'):
            source = _verified(saved['source_artifact_id'], now)
            saved = parse_schedule(code, json.loads(source['body']), source['id'], source['received_at'])
        result[code] = team_context(saved, code, game['tipoff_ts'], history, now)
    return result


def availability(game, now):
    rows = []
    for code in (game['home_team'], game['away_team']):
        identifiers = {item.get('source_artifact_id') for item in db.kv_get('nba_roster_'+code) or []
                       if isinstance(item.get('source_artifact_id'), int)}
        for ident in sorted(identifiers):
            source = _verified(ident, now)
            parsed = nba_data.parse_roster(json.loads(source['body']))
            if parsed['team'] != code or parsed['season_label'] != game['season_label']:
                raise ValueError('NBA availability source team or season identity conflict')
            for player in parsed['players']:
                rows.append({'player_id': player['espn_player_id'], 'player': player['name'], 'team': code,
                    'availability': roster_availability(player), 'injuries': player['injuries'],
                    'source_artifact_id': ident, 'observed_at': source['received_at']})
    return rows


def roster_availability(player):
    statuses=[str(i.get('status') or '').lower() for i in player.get('injuries',[])]
    for status in ('out','doubtful','questionable','probable'):
        if any(status in value for value in statuses):return status
    if player.get('roster_status') not in {'active','Active'}:return 'inactive'
    return 'not_listed_on_roster_injury_feed'
