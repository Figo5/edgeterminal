"""Usage-blended shadow model; original prospective model remains immutable."""
from __future__ import annotations
import argparse
import hashlib
import json
import time
import subprocess
from pathlib import Path
import joblib
import numpy as np
from . import db, nfl_prop_lab as lab
from .config import ROOT
from collections import defaultdict, Counter

VERSION = 'prop-usage-empirical-v1'
DIRECTORY = ROOT / 'data/research/prop_challenger'
WEIGHTS = (0., .25, .5, .75, 1.)
CUTOFF = (2026, 3)


def usage(rows, market):
    """Forecast volume times prior efficiency with identical training/serving math."""
    predictions = []
    for row in rows:
        f = row['features']
        if market == 'rushing_attempts':
            point = f['carries_last3']
        elif market == 'passing_yards':
            point = f['attempts_last3'] * f['passing_yards_last5'] / max(f['attempts_last5'], 1)
        elif market == 'rushing_yards':
            point = f['carries_last3'] * f['rushing_yards_last5'] / max(f['carries_last5'], 1)
        elif market == 'receptions':
            point = f['targets_last3'] * min(1., f['receptions_last5'] / max(f['targets_last5'], 1))
        else:
            point = f['targets_last3'] * f['receiving_yards_last5'] / max(f['targets_last5'], 1)
        predictions.append(max(0., point))
    return np.array(predictions)


def fit(rows, family):
    if family == 'rolling':
        return None
    x, y = lab._matrix(rows)
    return lab._estimator(family).fit(x, y)


def predict(model, rows, market):
    if model is None:
        return np.array([r['features'][lab.LABEL[market] + '_last3'] for r in rows])
    return np.maximum(0., model.predict(lab._matrix(rows)[0]))


def choose_weight(actual, baseline, role):
    return min(WEIGHTS, key=lambda w: (float(np.mean(np.abs(actual - ((1-w)*baseline+w*role)))), w))


def probability(line, point, residuals):
    residuals = np.asarray(residuals)
    if len(residuals) < 100 or not np.isfinite(residuals).all():
        raise ValueError('insufficient valid calibration residuals')
    # Finite-sample smoothing avoids certainty; exact threshold equality is a push.
    return float((np.sum(point + residuals > line) + .5) / (len(residuals) + 1))


def metrics(test, baseline, candidate, residuals, sigma, market):
    y = np.array([r['target'] for r in test])
    centers = np.array([r['features'][lab.LABEL[market] + '_last3'] for r in test])
    old, new = [], []
    for offset in (-.5, 0., .5):
        lines = np.maximum(0., centers + offset*sigma)
        keep = y != lines
        labels = y > lines
        old.extend((lab._prob_over(l, p, sigma)-z)**2 for l,p,z,k in zip(lines,baseline,labels,keep) if k)
        new.extend((probability(l,p,residuals)-z)**2 for l,p,z,k in zip(lines,candidate,labels,keep) if k)
    return {'n': len(test), 'baseline_mae': float(np.mean(np.abs(y-baseline))),
            'challenger_mae': float(np.mean(np.abs(y-candidate))),
            'baseline_synthetic_brier': float(np.mean(old)), 'challenger_synthetic_brier': float(np.mean(new)),
            'baseline_bias': float(np.mean(baseline-y)), 'challenger_bias': float(np.mean(candidate-y))}


def train():
    datasets, _, hashes = lab.build_dataset(CUTOFF)
    source = ROOT / 'data/research/nfl_prop_lab_2026_week3.json'
    raw = source.read_bytes()
    validation = json.loads(raw)['validation']
    models, config, evaluation = {}, {}, {}
    for market, rows in datasets.items():
        family = validation[market]['selected_on_2024']
        development = [r for r in rows if r['season'] == 2024]
        holdout = [r for r in rows if r['season'] == 2025]
        before_dev = [r for r in rows if r['season'] < 2024]
        before_holdout = [r for r in rows if r['season'] < 2025]
        if min(map(len, (development, holdout, before_dev, before_holdout))) < 100:
            raise ValueError('insufficient historical split coverage')
        baseline_dev = predict(fit(before_dev, family), development, market)
        role_dev = usage(development, market)
        y = np.array([r['target'] for r in development])
        weight = choose_weight(y, baseline_dev, role_dev)
        residuals = y - ((1-weight)*baseline_dev + weight*role_dev)
        baseline_test = predict(fit(before_holdout, family), holdout, market)
        candidate_test = (1-weight)*baseline_test + weight*usage(holdout, market)
        scores = metrics(holdout, baseline_test, candidate_test, residuals,
                         validation[market]['uncertainty_sigma_from_2024'], market)
        scores['offline_gate_passed'] = (scores['challenger_mae'] <= scores['baseline_mae'] and
                                         scores['challenger_synthetic_brier'] <= scores['baseline_synthetic_brier'])
        evaluation[market] = scores
        config[market] = {'family': family, 'usage_weight': weight, 'residuals': residuals.tolist()}
        models[market] = fit([r for r in rows if (r['season'], r['week']) < CUTOFF], family)
        print(market, json.dumps({'weight': weight, **scores}), flush=True)
    report = {'version': VERSION, 'readiness': 'RESEARCH_ONLY', 'designation': 'PASS',
              'mode': 'shadow', 'training_cutoff': list(CUTOFF), 'source_hashes': hashes,
              'baseline_sha256': hashlib.sha256(raw).hexdigest(), 'markets': config,
              'evaluation_2025': evaluation,
              'code_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'feature_code_sha256': hashlib.sha256(Path(lab.__file__).read_bytes()).hexdigest(),
              'diagnostic_note': 'Synthetic thresholds are not sportsbook lines. These metrics do not establish betting profitability.'}
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = DIRECTORY / str(time.time_ns())
    destination.mkdir()
    joblib.dump(models, destination / 'models.joblib')
    report['models_sha256'] = hashlib.sha256((destination/'models.joblib').read_bytes()).hexdigest()
    body = json.dumps(report, sort_keys=True, allow_nan=False).encode()
    (destination/'manifest.json').write_bytes(body)
    for saved in destination.iterdir():
        saved.chmod(0o444)
    pointer = DIRECTORY/'active.tmp'
    pointer.write_text(json.dumps({'path': str(destination), 'sha256': hashlib.sha256(body).hexdigest()}))
    pointer.replace(DIRECTORY/'active.json')
    return report


def shadow(batch_id, season, week):
    """Snapshot quotes already captured by the quota-controlled worker."""
    pointer = DIRECTORY / 'active.json'
    if not pointer.exists():
        return {'status': 'not_trained'}
    ref = json.loads(pointer.read_text())
    directory = Path(ref['path']).resolve()
    if directory.parent != DIRECTORY.resolve():
        raise ValueError('challenger artifact outside trusted local directory')
    body = (directory/'manifest.json').read_bytes()
    if hashlib.sha256(body).hexdigest() != ref['sha256']:
        raise ValueError('challenger manifest changed')
    manifest = json.loads(body)
    if manifest.get('training_cutoff') != list(CUTOFF) or manifest.get('mode') != 'shadow':
        raise ValueError('challenger training contract changed')
    path = directory / ('shadow-' + batch_id + '.json')
    if path.exists():
        return {'status': 'already_saved', 'path': str(path)}
    model_path = directory/'models.joblib'
    if hashlib.sha256(model_path.read_bytes()).hexdigest() != manifest['models_sha256']:
        raise ValueError('challenger model changed')
    models = joblib.load(model_path)
    stats = ROOT/'data/nflverse/prop_live'
    _, history, feature_hashes = lab.build_dataset((season, week), stats)
    inputs = lab._week3_features(history, season, week, stats)
    recent, _ = lab._read_stats(stats)
    identities = {}
    for r in recent:
        if season-1 <= int(r['season']) and (int(r['season']), int(r['week'])) < (season, week):
            identities.setdefault((lab._norm_name(r['player_display_name']), lab._team(r['team'])), set()).add(r['player_id'])
    pairs, _ = lab._pair_quotes(batch_id)
    now = time.time()
    output = []
    for quote in pairs:
        if (not quote['retrieved_at'] <= now < quote['kickoff_utc'] or
                now-quote['retrieved_at'] > 3600 or
                (quote.get('provider_updated_at') is not None and quote['provider_updated_at'] > now)):
            continue
        from .nfl_identity import team_code
        team = team_code(quote['team'])
        ids = identities.get((lab._norm_name(quote['player_name']), team), set())
        if len(ids) != 1:
            continue
        prepared = inputs.get((next(iter(ids)), team))
        market = quote['market']
        if not prepared or not lab._eligible(market, prepared['player']):
            continue
        f = dict(prepared['feature'])
        f['opponent_allowed_last3'] = lab._avg(prepared['opp_hist'], lab.LABEL[market])
        row = {'features': f, 'target': 0.}
        config = manifest['markets'][market]
        baseline = float(predict(models[market],[row],market)[0])
        point = float((1-config['usage_weight'])*baseline +
                      config['usage_weight']*usage([row],market)[0])
        over = probability(quote['line'], point, config['residuals'])
        from .nfl_prop_availability import view as availability
        output.append({'quote_id': quote['id'], 'game_id': quote['canonical_game_id'], 'player_id': quote['player_id'],
                       'player': quote['player_name'], 'market': market, 'line': quote['line'],
                       'side': quote['side'], 'price': quote['price'], 'sportsbook': quote['sportsbook'],
                       'observed_at': quote['retrieved_at'], 'kickoff_utc': quote['kickoff_utc'],
                       'projection': point, 'baseline_projection': baseline, 'probability_over': over,
                       'probability_under': probability(-quote['line'], -point, -np.array(config['residuals'])),
                       'availability': availability(quote['player_name'],quote['canonical_game_id'],quote['kickoff_utc'],market,now),
                       'offline_gate_passed': manifest['evaluation_2025'][market]['offline_gate_passed']})
    if not output:
        return {'status': 'no_supported_pregame_quotes'}
    report = {'version': VERSION, 'readiness': 'RESEARCH_ONLY', 'designation': 'PASS', 'mode': 'shadow',
              'created_at': time.time(), 'model_manifest_sha256': ref['sha256'], 'feature_hashes': feature_hashes,
              'batch_id': batch_id, 'season': season, 'week': week, 'projections': output}
    report['top_five'] = rank(output)
    if report['created_at'] >= min(q['kickoff_utc'] for q in pairs if any(r['quote_id']==q['id'] for r in output)):
        raise ValueError('challenger snapshot crossed kickoff')
    with path.open('x') as handle:
        json.dump(report, handle, sort_keys=True, allow_nan=False)
    seal = path.with_suffix('.sha256')
    seal.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    seal.chmod(0o444)
    path.chmod(0o444)
    return {'status': 'saved', 'path': str(path), 'projections': len(output)}


def rank(rows):
    """At most two markets per player, no duplicate sides or player/stat pairs."""
    grouped = defaultdict(list)
    for row in rows:
        if isinstance(row.get('availability'), dict) and row['availability'].get('blocked'):
            continue
        grouped[row['game_id']].append(row)
    selected = {}
    for game, candidates in grouped.items():
        used, players, picked = set(), Counter(), []
        candidates = sorted(candidates, key=lambda r: (-r['probability_'+r['side']], r['quote_id']))
        for row in candidates:
            key = row['player_id'], row['market']
            if key in used or players[row['player_id']] >= 2:
                continue
            picked.append({**row, 'confidence': row['probability_'+row['side']]})
            used.add(key)
            players[row['player_id']] += 1
            if len(picked) == 5:
                break
        selected[game] = picked
    return selected


def selected_rows():
    """Read the first sealed pregame challenger selections for each game."""
    if not (DIRECTORY/'active.json').exists():
        return []
    ref = json.loads((DIRECTORY/'active.json').read_text())
    directory = Path(ref['path']).resolve()
    if directory.parent != DIRECTORY.resolve():
        raise ValueError('invalid challenger report directory')
    snapshots = []
    for path in directory.glob('shadow-*.json'):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.with_suffix('.sha256').read_text():
            raise ValueError('challenger snapshot changed')
        snapshot = json.loads(raw)
        if snapshot['model_manifest_sha256'] != ref['sha256']:
            raise ValueError('challenger model identity mismatch')
        snapshot['_sha256'] = hashlib.sha256(raw).hexdigest()
        snapshots.append(snapshot)
    snapshots.sort(key=lambda s: s['created_at'])
    games, rows = set(), []
    for snapshot in snapshots:
        for game, selected in snapshot['top_five'].items():
            if game in games:
                continue
            games.add(game)
            for row in selected:
                if row['game_id'] != game:
                    raise ValueError('challenger selection game mismatch')
                rows.append({**row, 'selection_sha256': snapshot['_sha256'],
                             'selected_at': snapshot['created_at']})
    return rows


def result_evidence(selected):
    from . import nfl_prop_grading, nfl_prop_challenger_grading
    evidence = {}
    for frozen in db.q('SELECT freeze_sha FROM nfl_prop_research_freezes'):
        for grade in nfl_prop_grading._latest(frozen['freeze_sha']):
            key = grade['canonical_game_id'], grade['player_id'], grade['market']
            if grade['actual_stat'] is not None or grade.get('research_statistical_result') == 'void':
                if key in evidence and evidence[key]['actual_stat'] != grade['actual_stat']:
                    raise ValueError('conflicting challenger result evidence')
                evidence[key] = grade
    for key, grade in nfl_prop_challenger_grading.verified_results(selected).items():
        if key in evidence and evidence[key]['actual_stat'] != grade['actual_stat']:
            raise ValueError('challenger result conflicts with main result evidence')
        evidence[key] = grade
    return evidence


def report():
    """Evaluate sealed selections using verified main and challenger evidence."""
    if not (DIRECTORY/'active.json').exists():
        return {'status': 'not_trained'}
    selected = selected_rows()
    evidence = result_evidence(selected)
    rows = []
    for row in selected:
        grade = evidence.get((row['game_id'], row['player_id'], row['market']))
        result = 'pending'
        if grade:
            actual = grade['actual_stat']
            if actual is None:
                result = 'void'
            else:
                signed = (actual-row['line'])*(1 if row['side']=='over' else -1)
                result = 'won' if signed > 0 else 'lost' if signed < 0 else 'push'
        rows.append({**row, 'result': result, 'actual_stat': grade['actual_stat'] if grade else None})
    result = {'version': VERSION, 'mode': 'shadow', 'readiness': 'RESEARCH_ONLY', 'designation': 'PASS',
              'checked_at': time.time(), 'record': dict(Counter(r['result'] for r in rows)), 'rows': rows,
              'note': 'Separate challenger record; no changes to the live top-five policy.'}
    decided = [r for r in rows if r['actual_stat'] is not None]
    result['same_selections_comparison'] = {'n': len(decided),
        'baseline_mae': float(np.mean([abs(r['baseline_projection']-r['actual_stat']) for r in decided])) if decided else None,
        'challenger_mae': float(np.mean([abs(r['projection']-r['actual_stat']) for r in decided])) if decided else None}
    destination = DIRECTORY/'performance.json'
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(result,sort_keys=True,allow_nan=False))
    temporary.replace(destination)
    return {'status': 'updated', 'record': result['record'], 'selections': len(rows)}


def view():
    """Public comparison totals only; never expose upcoming shadow picks."""
    path = DIRECTORY/'performance.json'
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    rows = result.get('rows', [])
    decided = [r for r in rows if r['result'] in {'won','lost'}]
    brier = float(np.mean([(r['confidence']-(r['result']=='won'))**2 for r in decided])) if decided else None
    return {'version': result['version'], 'record': result['record'], 'selections': len(rows),
            'comparison': result.get('same_selections_comparison', {}), 'brier': brier,
            'checked_at': result['checked_at']}


def grade_due(now=None):
    """Resolve finished challenger selections on the ordinary grading cadence."""
    from . import nfl_prop_challenger_grading
    result = nfl_prop_challenger_grading.grade_due(now=now)
    failures = [r for r in result.get('checks', []) if r['status'] == 'needs_review']
    if failures:
        raise ValueError('challenger result sources need review: ' +
                         '; '.join(r['game_id'] + ': ' + r['reason'] for r in failures))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('train', 'shadow', 'report'))
    parser.add_argument('--batch-id')
    parser.add_argument('--season', type=int, default=2026)
    parser.add_argument('--week', type=int, default=6)
    args = parser.parse_args()
    result = train() if args.action == 'train' else report() if args.action == 'report' else shadow(args.batch_id, args.season, args.week)
    print(json.dumps({k:v for k,v in result.items() if k!='markets'},sort_keys=True))


if __name__ == '__main__':
    main()
