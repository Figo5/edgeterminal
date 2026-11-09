"""Pure, finite NFL spread corrections; never changes the shared football model.

The incumbent is always a candidate. Only earlier fit data sets the two Ridge
models; development chooses once and calibration only sets residual uncertainty.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import re
import statistics

from .pricing import push_adjusted_cover

KIND = 'nfl-spread-quality-v1'
FORMULA_VERSION = 'et-model-0.4.0'
ALPHA = 100.
METHODS = ('incumbent', 'affine', 'market_residual')
FEATURES = {
    'incumbent': (),
    'affine': ('baseline_margin',),
    'market_residual': ('model_market_gap', 'market_margin', 'scoring_margin',
        'efficiency_points', 'home_games', 'away_games', 'home_plays', 'away_plays', 'advanced_available'),
}
MINIMUMS = {'fit': 40, 'development': 20, 'calibration': 20}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('NFL spread requires a finite number')
    return float(value)


def protocol():
    return dict(schema=1, family=KIND, alpha=ALPHA, methods=list(METHODS),
        inner_minimums=dict(MINIMUMS), excluded_weeks=[[2026, 8], [2026, 9]],
        season_type='REG', outer_games=60, outer_weeks=6, outer_one_season=True,
        min_mae_points=.25, min_mae_fraction=.02, min_brier_improvement=.002,
        bootstrap=dict(unit='NFL_week', replicates=2000, seed=20261109, confidence=.95),
        probability='conditional_home_cover_excluding_integer_push',
        incumbent_formula=FORMULA_VERSION, features={k:list(v) for k,v in FEATURES.items()},
        proposal_cadence_seconds=604800, max_margin=100., min_sd=1., max_sd=100., replay_float_tolerance=1e-12)


def validate_example(row, *, label=True):
    if not isinstance(row, dict):
        raise ValueError('NFL spread example is not a record')
    season, week = row.get('season'), row.get('week')
    if (isinstance(season, bool) or not isinstance(season, int) or not 2000 <= season <= 2200
            or isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 18
            or not re.fullmatch(fr'nfl-{season}-reg-{week:02d}-[a-z0-9]+-[a-z0-9]+', str(row.get('game_id')))):
        raise ValueError('NFL spread requires regular-season game identity')
    if (season, week) in {(2026, 8), (2026, 9)}:
        raise ValueError('NFL spread inspected week is excluded whole')
    at, kickoff, receipt = (number(row[k]) for k in ('as_of', 'kickoff', 'feature_received_at'))
    if not receipt <= at < kickoff:
        raise ValueError('NFL spread feature receipt/as-of chronology conflict')
    if row.get('formula_version') != FORMULA_VERSION or not isinstance(row.get('input_policy_id'), str) or not row['input_policy_id']:
        raise ValueError('NFL spread incumbent formula/input policy is missing')
    for name in ('baseline_margin', 'incumbent_margin', 'baseline_sd', 'home_spread', 'scoring_margin',
                 'efficiency_points', 'home_games', 'away_games', 'home_plays', 'away_plays', 'advanced_available'):
        number(row[name])
    if (abs(row['baseline_margin']) > 100 or abs(row['incumbent_margin']) > 100
            or not 1 <= row['baseline_sd'] <= 100 or abs(row['home_spread']) > 40
            or any(row[k] < 0 for k in ('home_games', 'away_games', 'home_plays', 'away_plays'))
            or row['advanced_available'] not in (0., 1.)):
        raise ValueError('NFL spread feature bounds conflict')
    for key in ('source_sha256', 'snapshot_sha256') + (('label_sha256',) if label else ()):
        if not re.fullmatch('[0-9a-f]{64}', str(row.get(key))):
            raise ValueError('NFL spread example seal is invalid')
    if label:
        actual, received = number(row['actual_margin']), number(row['label_received_at'])
        if abs(actual) > 100 or actual != int(actual) or not kickoff < received:
            raise ValueError('NFL spread final label receipt conflict')
    return row


def policy_identity(rows):
    identities = {(r['formula_version'], r['input_policy_id'],r.get('incumbent_version',FORMULA_VERSION)) for r in rows}
    if len(identities) != 1:
        raise ValueError('NFL spread inner/outer input policy mismatch')
    return next(iter(identities))


def partition(rows, *, minimums=None):
    """Earliest complete week prefixes; never split a week to clear a boundary."""
    floors = MINIMUMS if minimums is None else minimums
    if (not isinstance(floors, dict) or set(floors) != set(MINIMUMS)
            or any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in floors.values())):
        raise ValueError('NFL spread phase minimums require positive integers for every phase')
    groups = defaultdict(list)
    identities = set()
    for row in rows:
        validate_example(row)
        if row['game_id'] in identities:
            raise ValueError('NFL spread duplicate game identity')
        identities.add(row['game_id']); groups[(row['season'], row['week'])].append(row)
    if not rows:
        raise ValueError('NFL spread insufficient chronological coverage')
    policy_identity(rows)
    weeks = sorted(groups)
    result = {name: [] for name in MINIMUMS}
    offset = 0
    for name in result:
        while offset < len(weeks) and len(result[name]) < floors[name]:
            result[name].extend(sorted(groups[weeks[offset]], key=lambda r:r['game_id'])); offset += 1
        if len(result[name]) < floors[name]:
            raise ValueError('NFL spread insufficient chronological coverage')
    for before, after in (('fit','development'), ('development','calibration')):
        cutoff = max(max(r['label_received_at'], r['feature_received_at']) for r in result[before])
        if cutoff >= min(r['as_of'] for r in result[after]):
            raise ValueError('NFL spread phase receipt chronology conflict')
    return result


def feature_values(method, row):
    validate_example(row, label=False)
    values = dict(row, market_margin=-row['home_spread'],
                  model_market_gap=row['baseline_margin'] + row['home_spread'])
    return [number(values[k]) for k in FEATURES[method]]


def validate_member(member):
    if not isinstance(member, dict) or member.get('method') not in METHODS:
        raise ValueError('NFL spread member method invalid')
    method = member['method']; names = list(FEATURES[method])
    if member.get('feature_names') != names or member.get('alpha') != ALPHA:
        raise ValueError('NFL spread member feature contract invalid')
    for key in ('means','scales','coefficients'):
        if not isinstance(member.get(key), list) or len(member[key]) != len(names):
            raise ValueError('NFL spread member coefficient count invalid')
        for value in member[key]: number(value)
    if any(value <= 0 for value in member['scales']):
        raise ValueError('NFL spread feature scale invalid')
    number(member['intercept'])
    return member


def predict(member, row):
    validate_member(member)
    if member['method'] == 'incumbent':
        return number(row['incumbent_margin'])
    value = member['intercept'] + sum(c * (x-m)/s for c,x,m,s in zip(member['coefficients'],
        feature_values(member['method'], row), member['means'], member['scales']))
    if member['method'] == 'market_residual': value -= row['home_spread']
    return max(-100., min(100., number(value)))


def fit_bank(rows, *, cutoff):
    """Fit exactly the frozen family. Import numerical libraries only offline."""
    import numpy as np
    cutoff = number(cutoff)
    if len(rows) < MINIMUMS['fit']:
        raise ValueError('NFL spread insufficient fit coverage')
    for row in rows:
        validate_example(row)
        if max(row['label_received_at'], row['feature_received_at']) > cutoff:
            raise ValueError('NFL spread fit receipt exceeds cutoff')
    formula, policy, incumbent = policy_identity(rows)
    members = []
    for method in METHODS:
        member = dict(method=method, feature_names=list(FEATURES[method]), alpha=ALPHA,
                      means=[], scales=[], coefficients=[], intercept=0.)
        if method != 'incumbent':
            x = np.array([feature_values(method, row) for row in rows], dtype=float)
            means, scales = x.mean(axis=0), x.std(axis=0)
            scales[scales == 0.] = 1.
            z = (x-means)/scales
            y = np.array([row['actual_margin'] + (row['home_spread'] if method=='market_residual' else 0.) for row in rows])
            intercept = float(y.mean())
            coefficients = np.linalg.solve(z.T @ z + ALPHA*np.eye(x.shape[1]), z.T @ (y-intercept))
            member.update(means=means.tolist(), scales=scales.tolist(), coefficients=coefficients.tolist(), intercept=intercept)
        members.append(validate_member(member))
    return dict(kind=KIND, alpha=ALPHA, formula_version=formula, input_policy_id=policy,
                incumbent_version=incumbent, fit_cutoff=cutoff, members=members, fit_game_ids=sorted(r['game_id'] for r in rows),
                fit_sources_sha256=sha([{k:r[k] for k in ('game_id','snapshot_sha256','source_sha256','label_sha256')} for r in rows]))


def select(bank, rows, *, previous_receipt):
    if len(rows) < MINIMUMS['development']:
        raise ValueError('NFL spread insufficient development coverage')
    check_phase(bank, rows, previous_receipt)
    scored = [(statistics.mean(abs(predict(m,r)-r['actual_margin']) for r in rows), i)
              for i,m in enumerate(bank['members'])]
    _, index = min(scored)
    return dict(bank['members'][index])


def check_phase(bank, rows, previous_receipt):
    for row in rows: validate_example(row)
    if policy_identity(rows) != (bank['formula_version'], bank['input_policy_id'],bank['incumbent_version']):
        raise ValueError('NFL spread phase input policy mismatch')
    if min(r['as_of'] for r in rows) <= number(previous_receipt):
        raise ValueError('NFL spread phase receipt chronology conflict')


def calibrate(bank, member, rows, *, previous_receipt):
    if len(rows) < MINIMUMS['calibration']:
        raise ValueError('NFL spread insufficient calibration coverage')
    check_phase(bank, rows, previous_receipt)
    residuals = [r['actual_margin']-predict(member,r) for r in rows]
    return max(1., min(100., math.sqrt(statistics.mean(x*x for x in residuals))))


def probability(margin, sd, home_spread):
    margin,sd,line=number(margin),number(sd),number(home_spread)
    if not 1<=sd<=100 or abs(margin)>100 or abs(line)>40:
        raise ValueError('NFL spread probability bounds invalid')
    return push_adjusted_cover(margin,sd,line)
