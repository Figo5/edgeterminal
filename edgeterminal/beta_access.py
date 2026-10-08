"""Optional hosted preview boundaries, enforced on the server, not in CSS."""
from __future__ import annotations

import copy
import time

from . import db, members
from .config import settings
from .integrity import parse_time


def featured_game() -> str | None:
    current = db.kv_get('nfl_current_schedule') or {}
    locked = {r['game_id'] for r in db.q('SELECT game_id FROM nfl_top_five_selections')}
    # One global example for the current week; a week query cannot choose a new one.
    return next((game for game in current.get('game_ids', []) if game in locked), None)


def allowed(game_id: str, kickoff, request) -> bool:
    stamp = parse_time(kickoff)
    return members.full_access(request) or bool(stamp and stamp <= time.time()) or game_id == featured_game()


def board(payload: dict, request) -> dict:
    if not settings.hosted_mode or members.full_access(request):
        return payload
    payload = copy.deepcopy(payload)
    for game in payload['games']:
        if not allowed(game['id'], game['kickoff_utc'], request):
            game['top_props'] = []
            game['props_state'] = 'member_preview'
    payload['preview'] = True
    return payload


def matchup(payload: dict, request) -> dict:
    game = payload['game']
    if allowed(game['id'], game['kickoff_utc'], request):
        return payload
    payload = copy.deepcopy(payload)
    payload['game']['top_props'] = []
    payload['game']['props_state'] = 'member_preview'
    payload['props'] = {'rows': [], 'count': 0}
    payload['top_five'] = {'games': []}
    return payload


def performance(payload: dict, request) -> dict:
    if not settings.hosted_mode:
        return payload
    # Public performance is a curated ledger. Raw research/PAPER endpoints stay private.
    payload = {'readiness': 'RESEARCH_ONLY', 'designation': 'PASS',
               'season_record': copy.deepcopy(payload['season_record'])}
    if members.full_access(request):
        return payload
    for game in payload['season_record']['props']['games']:
        if game['rows'] and not allowed(game['game_id'], game['rows'][0]['kickoff_utc'], request):
            game['rows'] = []
            game['preview'] = True
    return payload
