"""Pure, frozen NBA opportunity forecast and empirical predictive distribution.

No data fetching, fitting, database writes or outcomes from the running season.
The fitted JSON is produced separately by ``nba_model_fit``.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import statistics
from datetime import datetime

MINUTE_FEATURES = ("minutes_mean15", "minutes_mean5", "minutes_mean3",
                  "minutes_median5", "minutes_last", "minutes_sd15",
                  "minutes_change", "days_since_played", "history_n")
STAT_FEATURES = MINUTE_FEATURES + (
    "baseline", "stat_mean15", "stat_mean5", "stat_mean3", "stat_last",
    "stat_sd15", "rate15", "rate5", "rate3", "opportunity")


def load_parameters(path, expected_sha256=None, source_hashes=None):
    """Reject incomplete/nonfinite fitted artifacts before forecasting."""
    data = json.loads(path.read_text())
    if expected_sha256 is not None:
        actual = hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if actual != expected_sha256:
            raise ValueError('Frozen NBA model artifact hash mismatch')
    if source_hashes is not None:
        if data.get('serving_source_sha256') != source_hashes:
            raise ValueError('NBA serving source manifest mismatch')
        for name in ('nba_model.py','nba_prop_model.py','pricing.py'):
            if hashlib.sha256(path.with_name(name).read_bytes()).hexdigest() != source_hashes.get(name):
                raise ValueError('NBA serving implementation changed; create a new model version')
    finite = lambda v: isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
    when = lambda v: datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
    if (data.get('schema') != 1 or data.get('training_cutoff') != '2026-10-01T00:00:00Z'
            or data.get('window') != 15 or data.get('min_games') != 5
            or data.get('feature_names') != dict(minutes=list(MINUTE_FEATURES),stats=list(STAT_FEATURES))
            or not when(data['point_fit_end_exclusive']) < when(data['calibration_end_exclusive']) < when(data['training_cutoff'])
            or data.get('eligible_from') != when(data['calibration_end_exclusive'])):
        raise ValueError('Invalid frozen NBA prop model contract')
    for fitted,names in [(data['minutes_model'],MINUTE_FEATURES)] + [
            (v['point_model'],STAT_FEATURES) for v in data['markets'].values()]:
        if (not finite(fitted['intercept']) or len(fitted['coefficients']) != len(names)
                or not all(finite(v) for v in fitted['coefficients'])):
            raise ValueError('Invalid NBA fitted coefficients')
    if set(data['markets']) != {'points','rebounds','assists','threes','pra'}:
        raise ValueError('Incomplete NBA market fit')
    for v in data['markets'].values():
        residuals = v['standardized_residuals']
        if (v['point_method'] not in {'baseline','opportunity','ridge'}
                or v['probability_method'] not in {'empirical','parametric'}
                or len(residuals) < 100 or len(residuals) != v['calibration_n']
                or not all(finite(r) for r in residuals) or residuals != sorted(residuals)):
            raise ValueError('Invalid NBA residual calibration')
    return data


def features(rows, tipoff, minute_model=None):
    """Features from already validated, strictly earlier rows only."""
    minutes = [r['minutes'] for r in rows]
    stats = [r['stat'] for r in rows]
    mean = statistics.mean
    pooled = lambda n: sum(stats[-n:]) / sum(minutes[-n:])
    f = dict(zip(MINUTE_FEATURES, (
        mean(minutes), mean(minutes[-5:]), mean(minutes[-3:]),
        statistics.median(minutes[-5:]), minutes[-1], statistics.stdev(minutes),
        mean(minutes[-3:]) - mean(minutes),
        min(90., max(0., (tipoff - rows[-1]['ts']) / 86400)), len(rows))))
    predicted_minutes = (linear(minute_model, f, MINUTE_FEATURES)
                         if minute_model else mean(minutes))
    # Regulation plus ordinary overtime; data never invents negative exposure.
    predicted_minutes = min(48., max(1., predicted_minutes))
    baseline = mean(s / m for s, m in zip(stats, minutes)) * mean(minutes)
    f.update(baseline=baseline, stat_mean15=mean(stats),
             stat_mean5=mean(stats[-5:]), stat_mean3=mean(stats[-3:]),
             stat_last=stats[-1], stat_sd15=statistics.stdev(stats),
             rate15=pooled(15), rate5=pooled(5), rate3=pooled(3),
             opportunity=pooled(15) * predicted_minutes)
    return f, predicted_minutes


def linear(model, values, names):
    return model['intercept'] + sum(
        coefficient * values[name]
        for name, coefficient in zip(names, model['coefficients']))


def predict(rows, tipoff, market, artifact):
    settings = artifact['markets'][market]
    f, minutes = features(rows, tipoff, artifact['minutes_model'])
    raw = f['baseline']
    if settings['point_method'] == 'opportunity':
        raw = f['opportunity']
    elif settings['point_method'] == 'ridge':
        raw += linear(settings['point_model'], f, STAT_FEATURES)
    raw = max(0., raw)
    return raw, minutes


def residual_cdf(value, residuals):
    """Smoothed empirical CDF with finite tails and interpolation.

    One half pseudo-observation in each tail avoids claiming certainty beyond
    the calibration range. Residuals are sorted standardized forecast errors.
    """
    n = len(residuals)
    if not n:
        raise ValueError('NBA residual calibration is empty')
    left, right = bisect.bisect_left(residuals, value), bisect.bisect_right(residuals, value)
    if right != left:
        rank = (left + right) / 2.
    elif left == 0:
        rank = 0.
    elif left == n:
        rank = float(n)
    else:
        low, high = residuals[left - 1], residuals[left]
        rank = left - .5 + (value - low) / (high - low)
    return (rank + .5) / (n + 1.)


def probabilities(market, mu, sd, line, artifact):
    """Discrete predictive mass for all integer-valued NBA statistics.

    Empirical support is truncated at zero; the established normal fallback
    censors negative rounded outcomes into zero. Half-lines partition integer
    outcomes; an integer line reserves its own explicit push mass.
    """
    if artifact['markets'][market].get('probability_method') == 'parametric':
        return parametric_probabilities(market, mu, sd, line)
    residuals = artifact['markets'][market]['standardized_residuals']
    cdf = lambda x: residual_cdf((x - mu) / sd, residuals)
    lower = cdf(-.5)
    normalizer = max(1e-12, 1. - lower)
    bounded = lambda x: max(0., min(1., (cdf(x) - lower) / normalizer))
    if abs(line - round(line)) < 1e-9:
        low, high = bounded(line - .5), bounded(line + .5)
        return 1. - high, low, max(0., high - low)
    boundary = math.floor(line) + .5
    under = bounded(boundary)
    return 1. - under, under, 0.


def parametric_probabilities(market, mu, sd, line):
    """Count law or rounded normal with negative latent outcomes censored at 0."""
    from .pricing import norm_cdf, prob_over_count
    if line < 0:
        return 1.,0.,0.
    integer = abs(line - round(line)) < 1e-9
    if market in ('rebounds', 'assists', 'threes'):
        over = prob_over_count(mu, line, sd)
        under = (1. - prob_over_count(mu, line - 1., sd) if integer
                 else 1. - over)
    elif integer:
        over = 1. - norm_cdf((line + .5 - mu) / sd)
        under = 0. if line == 0 else norm_cdf((line - .5 - mu) / sd)
    else:
        over = 1. - norm_cdf((math.floor(line) + .5 - mu) / sd)
        under = 1. - over
    return over, under, max(0., 1. - over - under) if integer else 0.
