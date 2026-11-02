"""Usage-blended shadow model; original prospective model remains immutable."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
import re
import stat
import time
import subprocess
from contextlib import ExitStack, contextmanager
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
LEGACY_BASE = '/Users/giofiore/edgeterminal/data/research/prop_challenger'


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


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _read(name, directory_fd):
    """Read a regular, non-symlink file directly inside an already-trusted directory descriptor."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('challenger evidence is not a regular file')
        return handle.read()


def _create(name, data, directory_fd):
    """Exclusively create a read-only file inside the trusted directory descriptor."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=directory_fd)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fchmod(handle.fileno(), 0o444)


def _unique(pairs):
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError('duplicate JSON key')
    return dict(pairs)


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_unique)


@contextmanager
def _active():
    """Yield verified (artifact fd, artifact path, manifest sha256, manifest), or None when untrained.

    Trust boundary: every directory from '/' down to the artifact is opened once with O_NOFOLLOW
    relative to its parent and the descriptors stay open for the caller's whole lifetime. All later
    IO is relative to those descriptors, so renaming or replacing a path after these checks can never
    redirect reads, enumeration or fresh writes to a substitute directory.
    """
    base = Path(DIRECTORY)
    if not base.is_absolute() or os.path.normpath(str(base)) != str(base):
        raise ValueError('challenger directory is not canonical')
    with ExitStack() as stack:
        def descend(name, parent=None):
            fd = os.open(name, DIR_FLAGS, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd
        try:
            base_fd = descend('/')
            for part in base.parts[1:]:
                base_fd = descend(part, base_fd)
        except FileNotFoundError:
            base_fd = None  # Missing ordinary base: untrained. Symlinks and non-directories propagate.
        active = None if base_fd is None else _verify(base, base_fd, descend)
        yield active


def _verify(base, base_fd, descend):
    try:
        pointer = _read('active.json', base_fd)
    except FileNotFoundError:
        return None  # A dangling symlink fails with ELOOP, not here.
    ref = _loads(pointer)
    if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or not isinstance(ref['path'], str)
            or not isinstance(ref['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', ref['sha256'])):
        raise ValueError('invalid challenger pointer')
    text = ref['path']
    if text.startswith(LEGACY_BASE + '/') and re.fullmatch(r'[0-9]{19}', text[len(LEGACY_BASE) + 1:]):
        name = text[len(LEGACY_BASE) + 1:]
    else:
        name = text.rpartition('/')[2]
        if text != f'{base}/{name}' or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('challenger artifact outside trusted local directory')
    artifact_fd = descend(name, base_fd)  # Held by the caller's ExitStack until it finishes.
    body = _read('manifest.json', artifact_fd)
    if hashlib.sha256(body).hexdigest() != ref['sha256']:
        raise ValueError('challenger manifest changed')
    manifest = _loads(body)
    if (not isinstance(manifest, dict) or manifest.get('training_cutoff') != list(CUTOFF)
            or manifest.get('mode') != 'shadow'):
        raise ValueError('challenger training contract changed')
    return artifact_fd, base / name, ref['sha256'], manifest


def _snapshot(name, artifact_fd, manifest_sha):
    """Parse a snapshot after verifying its seal and model identity."""
    raw = _read(name, artifact_fd)
    if hashlib.sha256(raw).hexdigest() != _read(name[:-5] + '.sha256', artifact_fd).decode():
        raise ValueError('challenger snapshot changed')
    snapshot = _loads(raw)
    if snapshot['model_manifest_sha256'] != manifest_sha:
        raise ValueError('challenger model identity mismatch')
    snapshot['_sha256'] = hashlib.sha256(raw).hexdigest()
    return snapshot


def shadow(batch_id, season, week):
    """Snapshot quotes already captured by the quota-controlled worker."""
    with _active() as active:
        return _shadow(active, batch_id, season, week)


def _shadow(active, batch_id, season, week):
    if active is None:
        return {'status': 'not_trained'}
    artifact_fd, directory, manifest_sha, manifest = active
    if not isinstance(batch_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', batch_id):
        raise ValueError('unsafe challenger batch id')
    filename = 'shadow-' + batch_id + '.json'
    path = directory / filename
    try:
        os.stat(filename, dir_fd=artifact_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:  # Any entry, even a dangling symlink, is existing evidence that must validate.
        _snapshot(filename, artifact_fd, manifest_sha)
        return {'status': 'already_saved', 'path': str(path)}
    model_bytes = _read('models.joblib', artifact_fd)
    if hashlib.sha256(model_bytes).hexdigest() != manifest['models_sha256']:
        raise ValueError('challenger model changed')
    models = joblib.load(io.BytesIO(model_bytes))
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
              'created_at': time.time(), 'model_manifest_sha256': manifest_sha, 'feature_hashes': feature_hashes,
              'batch_id': batch_id, 'season': season, 'week': week, 'projections': output}
    report['top_five'] = rank(output)
    if report['created_at'] >= min(q['kickoff_utc'] for q in pairs if any(r['quote_id']==q['id'] for r in output)):
        raise ValueError('challenger snapshot crossed kickoff')
    body = json.dumps(report, sort_keys=True, allow_nan=False).encode()
    _create(filename, body, artifact_fd)
    _create(filename[:-5] + '.sha256', hashlib.sha256(body).hexdigest().encode(), artifact_fd)
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
    with _active() as active:
        if active is None:
            return []
        artifact_fd, _, manifest_sha, _ = active
        # Enumerate the held descriptor, never a path, so a replaced parent cannot hide sealed files.
        names = [n for n in os.listdir(artifact_fd) if n.startswith('shadow-') and n.endswith('.json')]
        snapshots = [_snapshot(name, artifact_fd, manifest_sha) for name in names]
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
    with _active() as active:
        if active is None:
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
