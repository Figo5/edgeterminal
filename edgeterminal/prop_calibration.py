"""Pure actual-line calibration and pre-holdout market choice.

Calibration never fetches, selects live props, edits grades or activates models.
It maps conditional over probabilities and preserves the scorer's integer push
mass. A separate chronological inner cohort chooses which markets may change.
"""
from __future__ import annotations

import math
import statistics

import numpy as np

SCHEMA = 'actual-line-logit-1'
REGULARIZATION = 10.
MIN_BRIER_IMPROVEMENT = .002
EPSILON = 1e-9


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('calibration values must be finite numbers')
    return float(value)


def _probabilities(values):
    result = {key: _number(values[key]) for key in ('over', 'under', 'push')}
    # Saved six-decimal NFL masses can differ by one unit plus float error.
    if any(not 0 <= value <= 1 for value in result.values()) or abs(sum(result.values()) - 1.) > 1.01e-6:
        raise ValueError('calibration probability mass must sum to one')
    return result


def _logit(p):
    p = max(EPSILON, min(1. - EPSILON, p))
    return math.log(p / (1. - p))


def _sigmoid(value):
    if value >= 0:
        z = math.exp(-value)
        return 1. / (1. + z)
    z = math.exp(value)
    return z / (1. + z)


def _catalog(rows, cutoff):
    cutoff = _number(cutoff)
    ids = set()
    for row in rows:
        if not row.get('game_id') or not row.get('quote_id'):
            raise ValueError('calibration requires immutable game/quote identities')
        identity = (row['game_id'], row['quote_id'])
        if identity in ids:
            raise ValueError('duplicate calibration quote')
        ids.add(identity)
        if _number(row['result_observed_at']) >= cutoff:
            raise ValueError('calibration/market choice includes a post-cutoff label')
    return dict(selection_game_ids=sorted({r['game_id'] for r in rows}),
                selection_quote_ids=sorted({r['quote_id'] for r in rows}, key=str),
                selection_label_max_at=max((float(r['result_observed_at']) for r in rows), default=None),
                selection_cutoff=cutoff)


def fit_calibrator(rows, *, cutoff, min_rows=30, min_games=10):
    """Fit a deterministic logit map shrunk toward identity on earlier quotes.

    Push outcomes do not become binary losses. A market without both labels or
    enough independent game identities retains its existing probabilities.
    """
    rows = list(rows)
    catalog = _catalog(rows, cutoff)
    prepared = []
    for row in rows:
        probabilities = _probabilities({key: row['probability_' + key] for key in ('over', 'under', 'push')})
        actual, line = _number(row['actual']), _number(row['line'])
        if actual == line:
            continue
        decided_mass = 1. - probabilities['push']
        if decided_mass <= 0:
            raise ValueError('decided calibration observation has only push mass')
        prepared.append((row, _logit(probabilities['over'] / decided_mass), float(actual > line)))
    games = {row['game_id'] for row, _, _ in prepared}
    base = dict(schema=SCHEMA, method='identity', intercept=0., slope=1.,
                regularization=REGULARIZATION, fit_rows=len(prepared), fit_games=len(games),
                label_max_at=catalog['selection_label_max_at'], cutoff=float(cutoff),
                fit_game_ids=catalog['selection_game_ids'], fit_quote_ids=catalog['selection_quote_ids'])
    if len(prepared) < min_rows or len(games) < min_games:
        return dict(base, reason='insufficient_actual_line_coverage')
    if len({y for _, _, y in prepared}) < 2:
        return dict(base, reason='both_decided_outcomes_required')
    x = np.array([[1., value] for _, value, _ in prepared], dtype=float)
    y = np.array([target for _, _, target in prepared], dtype=float)
    prior = np.array([0., 1.])
    coefficients = prior.copy()
    for _ in range(40):
        predicted = np.array([_sigmoid(v) for v in x @ coefficients])
        gradient = x.T @ (predicted - y) + REGULARIZATION * (coefficients - prior)
        weights = np.maximum(predicted * (1. - predicted), 1e-9)
        hessian = x.T @ (weights[:, None] * x) + REGULARIZATION * np.eye(2)
        step = np.linalg.solve(hessian, gradient)
        # Fixed parameter bounds regularize the fitted map, not displayed scores.
        updated = coefficients - step
        updated[0] = np.clip(updated[0], -5., 5.)
        updated[1] = np.clip(updated[1], .05, 4.)
        if float(np.max(np.abs(updated - coefficients))) < 1e-9:
            coefficients = updated
            break
        coefficients = updated
    return dict(base, method='shrunk_logit', intercept=float(coefficients[0]),
                slope=float(coefficients[1]), reason=None)


def apply_calibrator(probabilities, config):
    """Calibrate over/under conditional on no push, retaining exact push mass."""
    values = _probabilities(probabilities)
    if config.get('schema') != SCHEMA or config.get('method') not in ('identity', 'shrunk_logit'):
        raise ValueError('unknown probability calibration contract')
    intercept, slope = _number(config['intercept']), _number(config['slope'])
    if not -5 <= intercept <= 5 or not .05 <= slope <= 4:
        raise ValueError('invalid calibration coefficients')
    if config['method'] == 'identity':
        if intercept != 0. or slope != 1.:
            raise ValueError('identity calibration coefficients changed')
        return values
    mass = 1. - values['push']
    if mass <= 0:
        return values
    conditional = _sigmoid(intercept + slope * _logit(values['over'] / mass))
    over = mass * conditional
    return dict(over=over, under=mass - over, push=values['push'])


def _metrics(rows, prefix):
    errors, briers, losses = [], [], []
    for row in rows:
        actual, line = _number(row['actual']), _number(row['line'])
        point = _number(row[prefix + '_prediction'])
        probability = _number(row[prefix + '_probability'])
        if not 0 <= probability <= 1 or row.get('side') not in ('over', 'under'):
            raise ValueError('invalid market-choice forecast')
        if actual == line:
            continue
        won = float((actual > line) == (row['side'] == 'over'))
        p = max(EPSILON, min(1. - EPSILON, probability))
        errors.append(abs(point - actual)); briers.append((probability - won) ** 2)
        losses.append(-math.log(p if won else 1. - p))
    if not errors:
        return {}
    return dict(mae=statistics.mean(errors), brier=statistics.mean(briers), log_loss=statistics.mean(losses))


def choose_market_updates(rows, markets, *, cutoff, min_rows=20, min_games=10):
    """Choose updates on an inner actual-line cohort, never on outer labels."""
    rows, markets = list(rows), tuple(markets)
    # Different markets may have their own numeric quote sequence in fixtures;
    # production identities remain unique within each independently chosen market.
    catalog = _catalog([dict(row, quote_id=str(row['market']) + ':' + str(row['quote_id'])) for row in rows], cutoff)
    if len(set(markets)) != len(markets) or any(r.get('market') not in markets for r in rows):
        raise ValueError('invalid market-choice catalog')
    chosen, details = [], {}
    for market in markets:
        subset = [r for r in rows if r['market'] == market and _number(r['actual']) != _number(r['line'])]
        games = {r['game_id'] for r in subset}
        reasons = []
        if len(subset) < min_rows: reasons.append('insufficient_rows')
        if len(games) < min_games: reasons.append('insufficient_games')
        before, after = _metrics(subset, 'baseline'), _metrics(subset, 'candidate')
        if subset:
            reasons.extend(metric + '_regression' for metric in ('mae', 'brier', 'log_loss')
                           if after[metric] > before[metric] + 1e-12)
            if before['brier'] - after['brier'] < MIN_BRIER_IMPROVEMENT:
                reasons.append('insufficient_probability_improvement')
        if not reasons:
            chosen.append(market)
        details[market] = dict(rows=len(subset), games=len(games), baseline=before,
                               candidate=after, reasons=reasons, selected=not reasons)
    return dict(changed_markets=sorted(chosen), markets=details, reasons=[] if chosen else ['no_qualified_market_update'], **catalog)
