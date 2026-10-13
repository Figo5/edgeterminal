"""Read timestamped pregame availability; absence never proves availability."""
from __future__ import annotations
import json
import time
from . import db
from .nfl_prop_lab import _norm_name
from .paper_research import classify_availability


def view(player, game, kickoff, market=None, now=None):
    stamp = time.time() if now is None else now
    cutoff = min(stamp, kickoff)
    records = db.q("SELECT * FROM nfl_research_context_observations WHERE game_id=? "
                   "AND kind IN ('offensive_injury','qb_availability','qb_start') "
                   "AND fetched_at<=? AND fetched_at<? ORDER BY fetched_at DESC", (game, cutoff, kickoff))
    matching = [r for r in records if _norm_name(r['subject']) == _norm_name(player) and r['source_url']]
    injuries = [r for r in matching if r['kind'] != 'qb_start']
    latest = injuries[0] if injuries else None
    state = 'unresolved'
    if latest:
        state = 'stale' if cutoff-latest['fetched_at'] > 86400 else classify_availability(json.loads(latest['payload']).get('status'))
    starter = next((r for r in matching if r['kind']=='qb_start' and cutoff-r['fetched_at'] <= 86400), None)
    labels = {'unresolved':'Availability unconfirmed', 'stale':'Availability report is stale',
              'available':'Reported available', 'questionable_uncertain':'Questionable · check availability',
              'doubtful':'Doubtful', 'ruled_out':'Out / inactive'}
    note = labels[state]
    if market == 'passing_yards' and not starter:
        note += ' · starting QB unconfirmed'
    return {'status': state, 'label': note, 'blocked': state in {'ruled_out','doubtful'},
            'source_url': latest['source_url'] if latest else None,
            'observed_at': latest['fetched_at'] if latest else None,
            'starter_confirmed': bool(starter) if market=='passing_yards' else None}
