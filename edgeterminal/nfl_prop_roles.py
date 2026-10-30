"""Receipt-backed NFL role features and an explicit JSON opportunity scorer.

Only columns in archived weekly player statistics are used. This module never
fetches, infers a starting lineup, redistributes injured players' work, reads a
target label while constructing its features, or changes the pinned baseline.
All rows from a week are committed together after every target in that week.
"""
from __future__ import annotations

from collections import defaultdict
import math
import statistics

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import nfl_prop_lab as lab, nfl_prop_model as baseline

SCHEMA = 'nfl-role-opportunity-json-v2'
ROLE_FEATURES = ('opportunity_last1', 'opportunity_sd3', 'opportunity_share_last1',
                 'opportunity_share_last3', 'opportunity_share_change',
                 'team_opportunity_last3', 'current_team_prior_games',
                 'role_share_observations', 'air_yards_per_opportunity_last3',
                 'air_yards_share_last3', 'air_yards_observations')
FEATURES = tuple(baseline.FEATURES) + ROLE_FEATURES
ALPHA = 100.
MIN_CALIBRATION = 30


def key(row):
    return str(row['game_id']), str(row['player_id']), row['market']


def _num(value, *, optional=False):
    if value is None or value == '':
        if optional:
            return None
        raise ValueError('NFL opportunity observations must be finite')
    if isinstance(value, bool):
        raise ValueError('NFL opportunity observations must be finite')
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError('NFL opportunity observations must be finite') from None
    if not math.isfinite(number):
        raise ValueError('NFL opportunity observations must be finite')
    return number


def _opportunity(market):
    if market not in lab.MARKETS:
        raise ValueError('unsupported NFL opportunity market')
    return ('attempts' if market == 'passing_yards' else 'carries'
            if market in {'rushing_yards', 'rushing_attempts'} else 'targets')


def _feature(prior, team_prior, team_games, target):
    market = target['market']
    unit = _opportunity(market)
    opportunities = [r[unit] for r in prior[-3:]]
    same_team = [r for r in prior if r['team'] == target['team']]
    shares = []
    for row in same_team[-3:]:
        total = team_games[(row['team'], row['game_id'])][unit]
        if total > 0:
            shares.append(row[unit] / total)
    air_column = 'passing_air_yards' if unit == 'attempts' else 'receiving_air_yards'
    air_rows = [r for r in prior[-3:] if r[air_column] is not None and r[unit] > 0]
    reported_shares = [r['air_yards_share'] for r in same_team[-3:]
                       if r['air_yards_share'] is not None]
    air_ratio = (sum(r[air_column] for r in air_rows) / sum(r[unit] for r in air_rows)
                 if air_rows and unit != 'carries' else 0.)
    mean = lambda values: statistics.mean(values) if values else 0.
    return dict(opportunity_last1=opportunities[-1] if opportunities else 0.,
                opportunity_sd3=statistics.pstdev(opportunities) if len(opportunities) > 1 else 0.,
                opportunity_share_last1=shares[-1] if shares else 0.,
                opportunity_share_last3=mean(shares),
                opportunity_share_change=(shares[-1] - mean(shares)) if shares else 0.,
                team_opportunity_last3=mean([r[unit] for r in team_prior[-3:]]),
                current_team_prior_games=float(len(same_team)),
                role_share_observations=float(len(shares)),
                air_yards_per_opportunity_last3=air_ratio,
                air_yards_share_last3=mean(reported_shares) if unit == 'targets' else 0.,
                air_yards_observations=float(len(air_rows)) if unit != 'carries' else 0.)


def build_features(raw_rows, targets):
    """Map target game/player/market to strictly prior-week sourced features.

    Missing air-yard fields have explicit observation counts. Missing player rows
    are never invented as zero-opportunity participation. Within a completed week,
    game IDs provide a deterministic tie-break for the rare multi-game player.
    """
    targets = [dict(t, season=int(t['season']), week=int(t['week']), team=lab._team(t['team']))
               for t in targets]
    if not targets:
        return {}
    if len({key(t) for t in targets}) != len(targets):
        raise ValueError('duplicate NFL role target')
    for target in targets:
        _opportunity(target['market'])
        if not target['game_id'] or not target['player_id'] or not target['team']:
            raise ValueError('NFL role target identity is incomplete')
    maximum = max((t['season'], t['week']) for t in targets)
    by_week, target_weeks = defaultdict(list), defaultdict(list)
    unique = {}
    for original in raw_rows:
        if original.get('season_type') != 'REG':
            continue
        week = int(original['season']), int(original['week'])
        if week >= maximum:
            continue
        row = {name: _num(original.get(name), optional=name not in {'attempts', 'carries', 'targets'})
               for name in ('attempts', 'carries', 'targets', 'passing_air_yards',
                            'receiving_air_yards', 'air_yards_share')}
        row.update(player_id=str(original['player_id']), game_id=str(original['game_id']),
                   team=lab._team(original['team']), season=week[0], week=week[1])
        if any(row[name] < 0 for name in ('attempts', 'carries', 'targets')):
            raise ValueError('NFL opportunities must be nonnegative')
        identity = row['player_id'], row['game_id']
        if identity in unique:
            if unique[identity] != row:
                raise ValueError('conflicting NFL player-game opportunity rows')
            continue
        unique[identity] = row
        by_week[week].append(row)
    for target in targets:
        target_weeks[(target['season'], target['week'])].append(target)
    player_history, team_history, team_games = defaultdict(list), defaultdict(list), {}
    output = {}
    for week in sorted(set(by_week) | set(target_weeks)):
        for target in target_weeks[week]:
            output[key(target)] = _feature(player_history[target['player_id']],
                team_history[target['team']], team_games, target)
        for row in sorted(by_week[week], key=lambda r: (r['game_id'], r['player_id'])):
            player_history[row['player_id']].append(row)
            team_key = row['team'], row['game_id']
            if team_key not in team_games:
                team_games[team_key] = {name: 0. for name in ('attempts', 'carries', 'targets')}
                team_history[row['team']].append(team_games[team_key])
            for name in ('attempts', 'carries', 'targets'):
                team_games[team_key][name] += row[name]
    return output


def augment_datasets(datasets, raw_rows):
    targets = [dict(row, market=market) for market, rows in datasets.items() for row in rows]
    role = build_features(raw_rows, targets)
    return {market: [dict(row, features={**row['features'], **role[key(dict(row, market=market))]})
                      for row in rows] for market, rows in datasets.items()}


def current_features(inputs, raw_rows, season, week):
    targets = [dict(game_id=p['game']['game_id'], player_id=pid, team=team,
                    season=season, week=week, market=market)
               for (pid, team), p in inputs.items() for market in lab.MARKETS]
    values = build_features(raw_rows, targets)
    return {(t['player_id'], lab._team(t['team']), t['market']): values[key(t)] for t in targets}


def derived_features(features, market):
    values = baseline.derived_features({'features': features}, market)
    for name in ROLE_FEATURES:
        if name not in features:
            raise ValueError('NFL role features are incomplete')
        values[name] = _num(features[name])
    return values


def fit_market(fit, calibration, market):
    if not fit or len(calibration) < MIN_CALIBRATION:
        raise ValueError('insufficient NFL role fit/calibration coverage')
    def matrix(rows):
        derived = (derived_features(row['features'], market) for row in rows)
        return np.array([[values[name] for name in FEATURES] for values in derived], dtype=float)
    labels = np.array([_num(r['target']) for r in fit], dtype=float)
    model = make_pipeline(StandardScaler(), Ridge(alpha=ALPHA)).fit(matrix(fit), labels)
    scaler, ridge = model.steps[0][1], model.steps[1][1]
    coefficients = ridge.coef_ / scaler.scale_
    intercept = float(ridge.intercept_ - np.dot(coefficients, scaler.mean_))
    predicted = np.maximum(0., model.predict(matrix(calibration)))
    residuals = [_num(row['target']) - float(point) for row, point in zip(calibration, predicted)]
    return dict(kind=SCHEMA, features=list(FEATURES), alpha=ALPHA,
                coefficients=coefficients.tolist(), intercept=intercept,
                residuals=residuals, fit_rows=len(fit), calibration_rows=len(calibration))


def predict(config, features, market, line=None):
    if config.get('kind') != SCHEMA or config.get('features') != list(FEATURES):
        raise ValueError('NFL role scorer contract mismatch')
    coefficients = config.get('coefficients', [])
    if len(coefficients) != len(FEATURES):
        raise ValueError('NFL role coefficient contract mismatch')
    values = derived_features(features, market)
    point = _num(config.get('intercept')) + sum(_num(c) * values[n] for c, n in zip(coefficients, FEATURES))
    if not math.isfinite(point):
        raise ValueError('NFL role point must be finite')
    point = max(0., point)
    if line is None:
        return point, None
    line = _num(line)
    residuals = [_num(v) for v in config.get('residuals', [])]
    if len(residuals) < MIN_CALIBRATION:
        raise ValueError('NFL role residual calibration is incomplete')
    outcomes = np.rint(point + np.array(residuals))
    if market in {'receptions', 'rushing_attempts'}:
        outcomes = np.maximum(0., outcomes)
    denominator = len(outcomes) + 1.
    probability = dict(over=float((np.sum(outcomes > line) + .5) / denominator),
                       under=float((np.sum(outcomes < line) + .5) / denominator),
                       push=float(np.sum(outcomes == line) / denominator))
    if config.get('actual_line_calibration'):
        from .prop_calibration import apply_calibrator
        probability = apply_calibrator(probability, config['actual_line_calibration'])
    return point, probability
