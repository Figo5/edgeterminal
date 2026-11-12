"""Opponent-adjusted NBA team ratings from sourced final scores.

Pure and not wired into any model or lane. One ridge regression per as-of
solves every team's offence and defence together, so a win over a strong
opponent counts for more than the same score against a weak one. Recent games
weigh more and earlier seasons are discounted.

The three constants were chosen on the archived 2024-25 season only and then
scored once on 2025-26 (margin MAE 11.38 against 11.73 for the last-20-game
baseline, paired standard error 0.09). They are not tuned on 2026-27 games.
"""
from collections import defaultdict
from datetime import datetime, timezone
import math

import numpy as np

from . import nba_model as baseline

HALF_LIFE_DAYS = 110.
RIDGE = 3.
SEASON_CARRY = .6
MIN_LEAGUE_GAMES = 150
MIN_TEAM_GAMES = baseline.MIN_TEAM_GAMES


def _season(stamp):
    when = datetime.fromtimestamp(stamp, timezone.utc)
    return when.year if when.month >= 7 else when.year - 1


def games(history, cutoff):
    """One row per finished game at or before cutoff; contradictory team rows fail closed."""
    found = {}
    for raw in history or []:
        ts = baseline.parse_time(raw.get('played_at', raw.get('date')))
        points = baseline.valid_number(raw.get('points'), nonnegative=True)
        allowed = baseline.valid_number(raw.get('opponent_points'), nonnegative=True)
        team, other, home = raw.get('team'), raw.get('opponent'), raw.get('home')
        if (ts is None or ts > cutoff or points is None or allowed is None or not raw.get('game_id')
                or team not in baseline.NBA_TEAMS or other not in baseline.NBA_TEAMS or team == other
                or not isinstance(home, bool)):
            continue
        game = (ts, team, other, points, allowed) if home else (ts, other, team, allowed, points)
        if found.setdefault(str(raw['game_id']), game) != game:
            raise ValueError('NBA ratings history has contradictory game rows')
    return sorted(found.values())


def fit(history, tipoff, *, as_of=None):
    """Ratings from games that could have finished before tipoff and by as_of."""
    tip = baseline.parse_time(tipoff)
    if tip is None:
        raise ValueError('NBA ratings need a valid tipoff')
    cutoff = tip - baseline.FEATURE_LAG_S
    if as_of is not None:
        if not isinstance(as_of, (int, float)) or isinstance(as_of, bool) or not math.isfinite(as_of) or as_of >= tip:
            raise ValueError('NBA ratings as_of must be before tipoff')
        cutoff = min(cutoff, as_of)
    rows = games(history, cutoff)
    if len(rows) < MIN_LEAGUE_GAMES:
        return dict(status='unavailable', reason='insufficient_league_games', games=len(rows))
    teams = sorted({t for r in rows for t in r[1:3]})
    index = {t: i for i, t in enumerate(teams)}
    n, size, season = len(rows), len(teams), _season(tip)
    x, y, w = np.zeros((2 * n, 2 * size + 2)), np.zeros(2 * n), np.zeros(2 * n)
    counts = defaultdict(int)
    for i, (ts, home, away, home_points, away_points) in enumerate(rows):
        weight = .5 ** ((cutoff - ts) / 86400. / HALF_LIFE_DAYS) * (1. if _season(ts) == season else SEASON_CARRY)
        h, a = index[home], index[away]
        # points = league mean + own offence - opponent defence +/- half the home edge
        x[i, [h, size + a, 2 * size, 2 * size + 1]] = 1., -1., 1., .5
        x[n + i, [a, size + h, 2 * size, 2 * size + 1]] = 1., -1., 1., -.5
        y[i], y[n + i], w[i], w[n + i] = home_points, away_points, weight, weight
        counts[home] += 1; counts[away] += 1
    penalty = np.full(2 * size + 2, RIDGE); penalty[2 * size:] = 1e-6
    weighted = x * w[:, None]
    beta = np.linalg.solve(x.T @ weighted + np.diag(penalty), weighted.T @ y)
    return dict(status='ok', games=n, cutoff=cutoff, mean=float(beta[2 * size]), home_edge=float(beta[2 * size + 1]),
                offense={t: float(beta[i]) for t, i in index.items()},
                defense={t: float(beta[size + i]) for t, i in index.items()}, team_games=dict(counts))


def project(ratings, home, away):
    """Neutral-rest home margin and total; None when either team is thinly observed."""
    if ratings.get('status') != 'ok' or min(ratings['team_games'].get(t, 0) for t in (home, away)) < MIN_TEAM_GAMES:
        return None
    edge = ratings['home_edge'] / 2
    home_points = ratings['mean'] + ratings['offense'][home] - ratings['defense'][away] + edge
    away_points = ratings['mean'] + ratings['offense'][away] - ratings['defense'][home] - edge
    return dict(margin=home_points - away_points, total=home_points + away_points)
