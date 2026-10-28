"""Explicit offline fitting/evaluation for the active NBA prop model.

Run manually: python -m edgeterminal.nba_model_fit. Reads existing sourced
histories; makes no network calls. Never invoked by the refresh worker.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import nba_prop_model as model

MARKETS = ('points', 'rebounds', 'assists', 'threes', 'pra')
FLOORS = dict(points=5.5, rebounds=2.2, assists=1.8, threes=1.3, pra=7.)
ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = Path(__file__).with_name('nba_prop_parameters.json')
FIT_END = '2026-02-01T00:00:00Z'
CALIBRATION_END = '2026-03-01T00:00:00Z'
CUTOFF = '2026-10-01T00:00:00Z'


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def examples(history):
    """All labels and features before the fixed cutoff, game by game.

    Conflicting duplicate identities are discarded, matching runtime policy.
    Every prediction uses only rows at least four hours earlier.
    """
    by_player = defaultdict(list)
    for raw in history:
        try:
            when = stamp(raw['played_at'])
            if any(isinstance(raw[k],bool) for k in ('minutes','points','rebounds','assists','threes')):
                continue
            nums = [float(raw[k]) for k in ('minutes', 'points', 'rebounds', 'assists', 'threes')]
            if when >= stamp(CUTOFF) or not all(math.isfinite(n) and n >= 0 for n in nums) or nums[0] <= 0:
                continue
            by_player[str(raw['player_id'])].append(dict(raw, ts=when))
        except (KeyError, ValueError, TypeError):
            continue
    output = {market: [] for market in MARKETS}
    for pid, rows in sorted(by_player.items()):
        grouped = defaultdict(list)
        for row in rows:
            grouped[str(row['game_id'])].append(row)
        rows = [group[0] for group in grouped.values() if len({
            tuple(float(r[k]) for k in ('minutes', 'points', 'rebounds', 'assists', 'threes'))
            for r in group}) == 1]
        rows.sort(key=lambda r: (r['ts'], str(r['game_id'])))
        for target in rows:
            prior = [r for r in rows if r['ts'] <= target['ts'] - 14400][-15:]
            if len(prior) < 5:
                continue
            for market in MARKETS:
                stat = lambda r: (sum(float(r[k]) for k in ('points', 'rebounds', 'assists'))
                                  if market == 'pra' else float(r[market]))
                normalized = [dict(ts=r['ts'], minutes=float(r['minutes']), stat=stat(r)) for r in prior]
                output[market].append(dict(rows=normalized, target=stat(target), minutes=float(target['minutes']),
                    ts=target['ts'], player_id=pid, game_id=str(target['game_id'])))
    return output


def fit_linear(rows, names, targets):
    x = np.array([[r[k] for k in names] for r in rows])
    fitted = make_pipeline(StandardScaler(), Ridge(alpha=100.)).fit(x, targets)
    scaler, ridge = fitted.steps[0][1], fitted.steps[1][1]
    coefficients = ridge.coef_ / scaler.scale_
    return dict(intercept=float(ridge.intercept_ - np.dot(coefficients, scaler.mean_)),
                coefficients=[float(v) for v in coefficients])


def minute_fit(rows):
    fs = [model.features(r['rows'], r['ts'])[0] for r in rows]
    return fit_linear(fs, model.MINUTE_FEATURES, [r['minutes'] for r in rows])


def stat_fit(rows, minute_model):
    fs = [model.features(r['rows'], r['ts'], minute_model)[0] for r in rows]
    return fit_linear(fs, model.STAT_FEATURES, [r['target'] - f['baseline'] for r, f in zip(rows, fs)])


def predict(row, method, fitted, minute_model):
    f, minutes = model.features(row['rows'], row['ts'], minute_model)
    mu = (f['opportunity'] if method == 'opportunity' else
          f['baseline'] + model.linear(fitted, f, model.STAT_FEATURES)
          if method == 'ridge' else f['baseline'])
    return max(0., mu), minutes


def mae(values):
    return float(np.mean(np.abs(values)))


def distribution_score(row, market, mu, sd, artifact=None):
    """CRPS and a common prior-only synthetic-threshold Brier score."""
    probability = (lambda line: model.probabilities(market,mu,sd,line,artifact)[0]) if artifact else (
        lambda line: model.parametric_probabilities(market,mu,sd,line)[0])
    baseline = model.features(row['rows'],row['ts'])[0]['baseline']
    line = math.floor(baseline)+.5
    brier = (probability(line)-float(row['target']>line))**2
    maximum = {'points':100, 'rebounds':40, 'assists':30, 'threes':20, 'pra':150}[market]
    crps = sum((1.-probability(k+.5)-float(row['target']<=k))**2 for k in range(maximum))
    return crps,brier


def select_distribution(market, calibration, point_method, fitted, minute_model):
    boundary = stamp('2026-02-15T00:00:00Z')
    initial = [r for r in calibration if r['ts']<boundary]
    validation = [r for r in calibration if r['ts']>=boundary]
    residuals = sorted((r['target']-predict(r,point_method,fitted,minute_model)[0]) /
        max(statistics.stdev(x['stat'] for x in r['rows']),FLOORS[market]) for r in initial)
    temporary = {'markets':{market:{'standardized_residuals':residuals}}}
    old,new = [],[]
    for row in validation:
        mu,_ = predict(row,point_method,fitted,minute_model)
        sd = max(statistics.stdev(x['stat'] for x in row['rows']),FLOORS[market])
        old.append(distribution_score(row,market,mu,sd))
        new.append(distribution_score(row,market,mu,sd,temporary))
    before,after = np.mean(old,axis=0),np.mean(new,axis=0)
    chosen = 'empirical' if all(after<=before) else 'parametric'
    return chosen,dict(fit_n=len(initial),validation_n=len(validation),
        parametric_crps=float(before[0]),empirical_crps=float(after[0]),
        parametric_brier=float(before[1]),empirical_brier=float(after[1]))


def fit(history, source_catalog):
    datasets = examples(history)
    train_end = stamp('2026-01-01T00:00:00Z')
    fit_end, calibration_end = stamp(FIT_END), stamp(CALIBRATION_END)
    train = [r for r in datasets['points'] if r['ts'] < train_end]
    full_train = [r for r in datasets['points'] if r['ts'] < fit_end]
    early_minutes, final_minutes = minute_fit(train), minute_fit(full_train)
    artifact = dict(schema=1, version='nba-props-opportunity-empirical-v2',
        active_model_version='nba-research-2.0.1',
        training_cutoff=CUTOFF, point_fit_end_exclusive=FIT_END,
        calibration_end_exclusive=CALIBRATION_END, eligible_from=calibration_end,
        ridge_alpha=100., window=15, min_games=5, minutes_model=final_minutes,
        feature_names=dict(minutes=list(model.MINUTE_FEATURES), stats=list(model.STAT_FEATURES)),
        source_catalog=source_catalog, markets={})
    report = dict(training_cutoff=CUTOFF,
        design='Point-method selection: Jan 2026 only. Final coefficient fit: before Feb 1. '
               'Probability-method selection: first-half February residual fit, second-half February validation; '
               'empirical law must improve both CRPS and synthetic-threshold Brier. Residual calibration: all February. '
               'March–April labels excluded from fitting and per-market selection. '
               'This is retrospective historical validation: the initial March–April report informed addition of the generic probability gate; '
               'it is not independent proof of future performance. '
               'Current-roster sample has survivorship bias. No historical sportsbook lines or selection hit-rate claim.',
        markets={})
    for market, rows in datasets.items():
        training = [r for r in rows if r['ts'] < train_end]
        development = [r for r in rows if train_end <= r['ts'] < fit_end]
        calibration = [r for r in rows if fit_end <= r['ts'] < calibration_end]
        holdout = [r for r in rows if calibration_end <= r['ts'] < stamp(CUTOFF)]
        early_fit = stat_fit(training, early_minutes)
        methods = ('baseline', 'opportunity', 'ridge')
        dev = {method: mae([predict(r, method, early_fit, early_minutes)[0] - r['target'] for r in development])
               for method in methods}
        chosen = min(methods, key=lambda method: dev[method])
        final_fit = stat_fit([r for r in rows if r['ts'] < fit_end], final_minutes)
        probability_method,probability_validation = select_distribution(
            market,calibration,chosen,final_fit,final_minutes)
        residuals = sorted((r['target'] - predict(r, chosen, final_fit, final_minutes)[0]) /
                           max(statistics.stdev(x['stat'] for x in r['rows']), FLOORS[market]) for r in calibration)
        artifact['markets'][market] = dict(point_method=chosen, point_model=final_fit,
            standardized_residuals=residuals, calibration_n=len(residuals),
            probability_method=probability_method)
        old_errors, new_errors, old_minute_errors, new_minute_errors = [], [], [], []
        for r in holdout:
            mu, minutes = predict(r, chosen, final_fit, final_minutes)
            baseline = model.features(r['rows'], r['ts'])[0]['baseline']
            old_errors.append(baseline - r['target']); new_errors.append(mu - r['target'])
            old_minute_errors.append(statistics.mean(x['minutes'] for x in r['rows']) - r['minutes'])
            new_minute_errors.append(minutes - r['minutes'])
        report['markets'][market] = dict(point_method=chosen, development_mae=dev,
            probability_method=probability_method,probability_validation=probability_validation,
            train_n=len(training), development_n=len(development), calibration_n=len(calibration),
            holdout_n=len(holdout), baseline_mae=mae(old_errors), upgraded_mae=mae(new_errors),
            baseline_minutes_mae=mae(old_minute_errors), upgraded_minutes_mae=mae(new_minute_errors))
    return artifact, report, datasets


def evaluate_distribution(artifact, datasets):
    scores = {}
    for market, rows in datasets.items():
        accum = dict(baseline_crps=[], upgraded_crps=[], baseline_brier=[], upgraded_brier=[])
        for r in rows:
            if r['ts'] < stamp(CALIBRATION_END):
                continue
            f, _ = model.features(r['rows'], r['ts'])
            mu, _ = model.predict(r['rows'], r['ts'], market, artifact)
            sd = max(statistics.stdev(x['stat'] for x in r['rows']), FLOORS[market])
            # Common, prior-only synthetic threshold, not a bookmaker quote.
            line = math.floor(f['baseline']) + .5
            p_old = model.parametric_probabilities(market, f['baseline'], sd, line)[0]
            p_new = model.probabilities(market, mu, sd, line, artifact)[0]
            actual = float(r['target'] > line)
            accum['baseline_brier'].append((p_old-actual)**2)
            accum['upgraded_brier'].append((p_new-actual)**2)
            # Discrete CRPS on a fixed market support. Tail truncation is
            # identical between methods; this is distribution evaluation only.
            maximum = {'points':100, 'rebounds':40, 'assists':30, 'threes':20, 'pra':150}[market]
            before = after = 0.
            for threshold in range(maximum):
                truth = float(r['target'] <= threshold)
                before += (1.-model.parametric_probabilities(market, f['baseline'], sd, threshold+.5)[0]-truth)**2
                after += (1.-model.probabilities(market, mu, sd, threshold+.5, artifact)[0]-truth)**2
            accum['baseline_crps'].append(before); accum['upgraded_crps'].append(after)
        scores[market] = {key: float(np.mean(values)) for key, values in accum.items()}
    return scores


def save_frozen_artifact(path, encoded):
    """An explicit offline run cannot redefine an already built version."""
    if path.exists():
        if path.read_text().strip() != encoded:
            raise ValueError('Existing NBA model artifact is frozen; a new fit requires a new model version/artifact')
        return
    with path.open('x') as stream:
        stream.write(encoded + '\n')


def main():
    from . import db
    histories = []
    for row in db.q("SELECT v FROM kv WHERE k LIKE 'nba_player_history_%'"):
        value = json.loads(row['v'])
        if isinstance(value, list):
            histories.extend(value)
    # Verify every exact response used by the fitted historical rows.
    source_ids = sorted({int(r['source_artifact_id']) for r in histories if stamp(r['played_at']) < stamp(CUTOFF)})
    catalog = []
    for identity in source_ids:
        source = db.q1('SELECT * FROM nba_source_artifacts WHERE id=?', (identity,))
        body = source['body']
        if isinstance(body, str):
            body = body.encode()
        if source['http_status'] != 200 or hashlib.sha256(body).hexdigest() != source['sha256']:
            raise ValueError('Historical NBA source integrity check failed')
        catalog.append(dict(artifact_id=identity, sha256=source['sha256'], url=source['url']))
    artifact, report, datasets = fit(histories, catalog)
    report['distribution'] = evaluate_distribution(artifact, datasets)
    artifact['historical_validation'] = dict(report,
        holdout_start=CALIBRATION_END,holdout_end=CUTOFF,player_count=len({r['player_id'] for r in datasets['points']}),
        validation_scope='retrospective player-stat forecasts, no sportsbook selection record')
    artifact['historical_validation'].pop('artifact_sha256',None)
    artifact['serving_source_sha256'] = {name: hashlib.sha256(ARTIFACT.with_name(name).read_bytes()).hexdigest()
        for name in ('nba_model.py','nba_prop_model.py','pricing.py')}
    encoded = json.dumps(artifact, sort_keys=True, separators=(',', ':'), allow_nan=False)
    report['artifact_sha256'] = hashlib.sha256(encoded.encode()).hexdigest()
    save_frozen_artifact(ARTIFACT, encoded)
    report_path = ROOT / 'reports/nba/model_upgrade_2026-10-27.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n')
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
