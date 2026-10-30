"""Pure, versioned NBA workload features and predictive minutes uncertainty.

These are observed-minute role proxies, never fabricated lineup/injury effects.
Fitting is explicit and offline. No source, database, registry or network access.
"""
import math
import statistics

from . import nba_prop_model

MINUTE_FEATURES = nba_prop_model.MINUTE_FEATURES + (
    'minutes_sd5', 'minutes_last_change', 'minutes_low_share15',
    'minutes_recent_shift', 'current_season_n', 'current_season_shift')


def _season(stamp):
    from datetime import datetime, timezone
    value = datetime.fromtimestamp(stamp, timezone.utc)
    return value.year if value.month >= 7 else value.year - 1


def features(rows, tipoff):
    if len(rows) < 5 or any(r['ts'] > tipoff - 14400 for r in rows):
        raise ValueError('workload features need five strictly prior games')
    values, _ = nba_prop_model.features(rows, tipoff)
    minutes = [r['minutes'] for r in rows]
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
               and math.isfinite(x) and 0 < x <= 65 for x in minutes):
        raise ValueError('invalid observed minutes')
    recent = minutes[-5:]
    current = [r['minutes'] for r in rows if _season(r['ts']) == _season(tipoff)]
    older = minutes[:-5] or minutes
    pooled = statistics.mean(minutes)
    values.update(minutes_sd5=statistics.stdev(recent),
                  minutes_last_change=abs(minutes[-1] - minutes[-2]),
                  minutes_low_share15=sum(x < .5 * statistics.median(minutes) for x in minutes) / len(minutes),
                  minutes_recent_shift=statistics.mean(recent) - statistics.mean(older),
                  current_season_n=len(current),
                  current_season_shift=statistics.mean(current) - pooled if current else 0.)
    return values


def fit_minutes(training, calibration):
    """Coefficients use the fit prefix; residual uncertainty uses a later block."""
    from .nba_model_fit import fit_linear
    if len(training) < 100 or len(calibration) < 30:
        raise ValueError('insufficient workload fitting/calibration rows')
    if max(r['ts'] for r in training) >= min(r['ts'] for r in calibration):
        raise ValueError('workload fitting must precede uncertainty calibration')
    fitted = fit_linear([features(r['rows'], r['ts']) for r in training], MINUTE_FEATURES,
                        [r['minutes'] for r in training])
    residuals = sorted(r['minutes'] - predict_minutes(r['rows'], r['ts'], fitted)
                       for r in calibration)
    return dict(schema=1, feature_names=list(MINUTE_FEATURES), minutes_model=fitted,
                minute_residuals=residuals, fit_n=len(training), calibration_n=len(calibration),
                fit_game_ids=sorted({r['game_id'] for r in training}),
                calibration_game_ids=sorted({r['game_id'] for r in calibration}),
                fit_label_max_at=max(r['ts'] + 14400 for r in training),
                calibration_label_max_at=max(r['ts'] + 14400 for r in calibration))


def predict_minutes(rows, tipoff, fitted):
    value = nba_prop_model.linear(fitted, features(rows, tipoff), MINUTE_FEATURES)
    if not math.isfinite(value):
        raise ValueError('nonfinite workload forecast')
    return min(48., max(1., value))


def minute_distribution(rows, tipoff, artifact):
    """Five fixed equal-mass residual quantiles retain role/minute uncertainty."""
    if artifact.get('schema') != 1 or artifact.get('feature_names') != list(MINUTE_FEATURES):
        raise ValueError('invalid workload artifact')
    residuals = artifact['minute_residuals']
    if len(residuals) < 30 or residuals != sorted(residuals) or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in residuals):
        raise ValueError('invalid workload residual distribution')
    center = predict_minutes(rows, tipoff, artifact['minutes_model'])
    values = []
    for q in (.1, .3, .5, .7, .9):
        index = q * (len(residuals) - 1)
        lo = int(index)
        error = residuals[lo] + (index - lo) * (residuals[min(lo + 1, len(residuals) - 1)] - residuals[lo])
        values.append((min(48., max(1., center + error)), .2))
    return values
