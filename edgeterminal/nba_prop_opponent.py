"""Opponent-allowed production factor for NBA player props.

The frozen prop model uses a player's own per-minute rate with no opponent
context. This module measures how much of each stat a team has allowed per
opposing player-minute over its recent finished games, relative to the league
at the same moment, using only already-archived player game logs.

Per-minute allowance folds the opponent's defence and its pace into one number.
ponytail: one combined factor; split defence from pace with possession data if
the combined factor earns a place and the split is shown to add accuracy.

Pure: no database, network, fitting or clock access. Unknown context is the
identity adjustment, never an invented value.
"""
from __future__ import annotations

import bisect
from collections import defaultdict

from . import nba_model as baseline

WINDOW = 15
MIN_GAMES = 5
STRENGTHS = (0., .25, .5, .75, 1.)
_FIELDS = ('points', 'rebounds', 'assists', 'threes')


def _stat(totals, market):
    return sum(totals[k] for k in ('points', 'rebounds', 'assists')) if market == 'pra' else totals[market]


def build(history):
    """Index finished games by team: what the opposing side produced, and in how many minutes."""
    games, seen = defaultdict(dict), set()
    for raw in history or []:
        when = baseline.parse_time(raw.get('played_at', raw.get('date')))
        values = [baseline.valid_number(raw.get(k), nonnegative=True) for k in ('minutes',) + _FIELDS]
        key = (str(raw.get('player_id')), str(raw.get('game_id')))
        if when is None or not raw.get('team') or any(v is None for v in values) or values[0] <= 0 or key in seen:
            continue
        seen.add(key)
        side = games[key[1]].setdefault(raw['team'], dict(ts=when, minutes=0., **{k: 0. for k in _FIELDS}))
        side['ts'] = min(side['ts'], when)
        for name, value in zip(('minutes',) + _FIELDS, values):
            side[name] += value
    teams, opponents = defaultdict(list), {}
    for game_id, sides in games.items():
        if len(sides) != 2:
            continue
        (a, a_totals), (b, b_totals) = sorted(sides.items())
        opponents[(game_id, a)], opponents[(game_id, b)] = b, a
        teams[a].append((a_totals['ts'], game_id, b_totals))
        teams[b].append((b_totals['ts'], game_id, a_totals))
    for rows in teams.values():
        rows.sort(key=lambda row: row[:2])
    return dict(teams={team: dict(ts=[r[0] for r in rows], allowed=[r[2] for r in rows]) for team, rows in teams.items()},
                opponents=opponents, league={})


def opposing(table, game_id, team):
    return table['opponents'].get((str(game_id), team))


def allowed(table, team, market, tipoff):
    """Stat allowed per opposing player-minute over the team's last WINDOW eligible games."""
    rows = table['teams'].get(team)
    if not rows:
        return None
    end = bisect.bisect_right(rows['ts'], tipoff - baseline.FEATURE_LAG_S)
    window = rows['allowed'][max(0, end - WINDOW):end]
    minutes = sum(g['minutes'] for g in window)
    if len(window) < MIN_GAMES or minutes <= 0:
        return None
    return sum(_stat(g, market) for g in window) / minutes


def ratio(table, team, market, tipoff):
    """Team allowance relative to the mean of every team with enough games at the same moment."""
    own = allowed(table, team, market, tipoff)
    if own is None:
        return None
    key = (market, tipoff)
    if key not in table['league']:
        values = [v for v in (allowed(table, t, market, tipoff) for t in sorted(table['teams'])) if v is not None]
        table['league'][key] = sum(values) / len(values)
    league = table['league'][key]
    return own / league if league > 0 else None


def adjust(mean, opponent_ratio, strength):
    """Scale a projection toward the opponent's allowance; strength 0 or unknown context changes nothing."""
    if strength not in STRENGTHS:
        raise ValueError('opponent strength is outside the declared grid')
    if opponent_ratio is None:
        return mean
    return mean * (1. + strength * (opponent_ratio - 1.))
