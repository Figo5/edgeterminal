#!/usr/bin/env python3
"""Reproduce the locked local-only experiment; no production writes or HTTP.

Run from any directory with the repository's Python environment. Every output
path is constrained to this script's report directory. Source bodies are read
through a read-only SQLite connection and verified against their stored hash.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import sklearn
from sklearn.linear_model import LogisticRegression
from edgeterminal import nba_model, nba_sources

ALPHAS = (5.0, 20.0, 80.0)
HALF_LIVES = (None, 30.0, 60.0)
SHRINK_WEIGHTS = (0.5, 0.75, 1.0)
LAG = 4 * 3600
HFA = 2.2
SD = 11.6
SEED = 20261028
SCHEDULE_RECEIPT_CUTOFF = '2026-10-28T00:00:00Z'
HISTORICAL_END = '2026-10-01T00:00:00Z'


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_history():
    """Freeze the earliest successful pre-audit archive for each 2026 URL."""
    dbpath = ROOT / 'data/edgeterminal.db'
    conn = sqlite3.connect(f'{dbpath.as_uri()}?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    records = conn.execute('''
      SELECT o.id,o.url,o.observed_at,o.sha256,a.body
      FROM nba_http_observations o JOIN nba_http_artifacts a USING(sha256)
      WHERE o.http_status=200 AND o.url LIKE '%/schedule?season=2026&seasontype=2'
        AND o.observed_at < ? ORDER BY o.observed_at,o.id
    ''', (stamp(SCHEDULE_RECEIPT_CUTOFF),)).fetchall()
    conn.close()
    earliest = {}
    for rec in records:
        earliest.setdefault(rec[1], rec)
    assert len(earliest) == 30, f'Expected 30 team schedule URLs: {len(earliest)}'
    rows = {}
    catalogue = []
    for ident, url, observed, sha, body in earliest.values():
        assert hashlib.sha256(body).hexdigest() == sha, 'Source hash mismatch'
        catalogue.append(dict(observation_id=ident, url=url, observed_at=iso(observed), sha256=sha))
        for row in nba_sources.team_history(json.loads(body), sha):
            if nba_model.parse_time(row['played_at']) >= stamp(HISTORICAL_END):
                continue
            key = (row['game_id'], row['team'])
            signature = {k: v for k, v in row.items() if k != 'source_sha256'}
            if key in rows:
                old = {k: v for k, v in rows[key].items() if k != 'source_sha256'}
                assert old == signature, f'Conflicting historical team row: {key}'
            rows.setdefault(key, row)
    grouped = {}
    for row in rows.values():
        grouped.setdefault(row['game_id'], []).append(row)
    games = []
    for gid, pair in grouped.items():
        assert len(pair) == 2, f'Incomplete game: {gid}'
        h = next(r for r in pair if r['home'])
        a = next(r for r in pair if not r['home'])
        assert h['team'] == a['opponent'] and a['team'] == h['opponent']
        assert h['points'] == a['opponent_points'] and a['points'] == h['opponent_points']
        assert h['played_at'] == a['played_at']
        games.append(dict(game_id=gid, ts=nba_model.parse_time(h['played_at']),
                          home=h['team'], away=a['team'],
                          margin=h['points'] - a['points']))
    games.sort(key=lambda g: (g['ts'], g['game_id']))
    assert len({g['game_id'] for g in games}) == len(games)
    return list(rows.values()), games, sorted(catalogue, key=lambda x: x['url'])


def design(games):
    teams = sorted(nba_model.NBA_TEAMS)
    idx = {team: i + 1 for i, team in enumerate(teams)}
    x = np.zeros((len(games), len(teams) + 1))
    for i, g in enumerate(games):
        x[i, 0] = 1
        x[i, idx[g['home']]] = 1
        x[i, idx[g['away']]] = -1
    return x


def rating_prediction(x, y, times, target_i, alpha, half_life):
    cutoff = times[target_i] - LAG
    eligible = times <= cutoff
    assert not eligible[target_i]
    if np.any(eligible):
        assert np.max(times[eligible]) <= cutoff
    prior_x, prior_y = x[eligible], y[eligible]
    weights = (np.ones(len(prior_y)) if half_life is None else
               np.exp2(-(times[target_i] - times[eligible]) / (86400 * half_life)))
    penalty = np.eye(x.shape[1]) * alpha
    penalty[0, 0] = 0
    beta = np.linalg.solve(prior_x.T @ (weights[:, None] * prior_x) + penalty,
                           prior_x.T @ (weights * prior_y))
    return float(x[target_i] @ beta)


def metrics(y, pred, probs=None):
    err = np.asarray(pred) - y
    out = dict(n=len(y), margin_mae=float(np.mean(np.abs(err))),
               margin_rmse=float(np.sqrt(np.mean(err ** 2))),
               margin_bias=float(np.mean(err)))
    if probs is not None:
        out['winner_brier'] = float(np.mean((np.asarray(probs) - (y > 0)) ** 2))
    return out


def normal_probs(pred):
    return np.array([0.5 * (1 + math.erf(v / SD / math.sqrt(2))) for v in pred])


def paired_intervals(y, base, cand, base_p, cand_p, days):
    """Fixed-seed calendar-day block bootstrap; negative delta is better."""
    blocks = [np.flatnonzero(days == d) for d in sorted(set(days))]
    rng = np.random.default_rng(SEED)
    differences = []
    for _ in range(2000):
        chosen = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
        b = metrics(y[chosen], base[chosen], base_p[chosen])
        c = metrics(y[chosen], cand[chosen], cand_p[chosen])
        differences.append([c[k] - b[k] for k in ('margin_mae', 'margin_rmse', 'winner_brier')])
    quantiles = np.quantile(differences, [.025, .975], axis=0)
    return {k: dict(low=float(quantiles[0, i]), high=float(quantiles[1, i]))
            for i, k in enumerate(('margin_mae', 'margin_rmse', 'winner_brier'))}


def run():
    rows, games, catalogue = load_history()
    x = design(games)
    y = np.array([g['margin'] for g in games])
    times = np.array([g['ts'] for g in games])
    eligible, base, saved_base_probs = [], [], []
    for i, g in enumerate(games):
        pred = nba_model.project_game(g['home'], g['away'], rows, g['ts'])
        if pred['status'] == 'ok':
            eligible.append(i)
            base.append(pred['margin'])
            saved_base_probs.append(pred['home_win_probability'])
    eligible = np.array(eligible)
    yy, tt = y[eligible], times[eligible]
    base = np.array(base)
    baseline_probs = np.array(saved_base_probs)
    jan = (tt >= stamp('2026-01-01T00:00:00Z')) & (tt < stamp('2026-02-01T00:00:00Z'))
    feb = (tt >= stamp('2026-02-01T00:00:00Z')) & (tt < stamp('2026-03-01T00:00:00Z'))
    test = (tt >= stamp('2026-03-01T00:00:00Z')) & (tt < stamp(HISTORICAL_END))
    assert not np.any((jan & feb) | (feb & test) | (jan & test))
    assert int(jan.sum()) == 233 and int(feb.sum()) == 168 and int(test.sum()) == 338
    assert max(times) < stamp(HISTORICAL_END)
    # Only January predictions are computed while settings are being selected.
    january_grid = []
    strength_candidates = {}
    for alpha in ALPHAS:
        for half in HALF_LIVES:
            name = f'strength_alpha{alpha:g}_half{half if half else "none"}'
            vals = np.array([rating_prediction(x, y, times, i, alpha, half)
                             for i in eligible[jan]])
            strength_candidates[name] = (alpha, half)
            january_grid.append(dict(name=name, family='strength', alpha=alpha,
                                     half_life_days=half, **metrics(yy[jan], vals)))
    for weight in SHRINK_WEIGHTS:
        name = f'baseline_shrink{weight:g}'
        vals = HFA + weight * (base[jan] - HFA)
        january_grid.append(dict(name=name, family='shrinkage', baseline_weight=weight,
                                 **metrics(yy[jan], vals)))
    rank = lambda item: (item['margin_mae'], item['margin_rmse'], item['name'])
    selected_by_family = {family: min([v for v in january_grid if v['family'] == family], key=rank)
                          for family in ('strength', 'shrinkage')}
    selected = min(selected_by_family.values(), key=rank)
    locked_hash = hashlib.sha256(json.dumps(selected_by_family, sort_keys=True).encode()).hexdigest()
    print('January settings locked:', json.dumps(selected_by_family, sort_keys=True), flush=True)
    forecasts = {'baseline': base}
    for family, choice in selected_by_family.items():
        if family == 'strength':
            alpha, half = strength_candidates[choice['name']]
            forecasts[family] = np.array([rating_prediction(x, y, times, i, alpha, half)
                                          for i in eligible])
        else:
            forecasts[family] = (base.copy() if choice['baseline_weight'] == 1.0 else
                                 HFA + choice['baseline_weight'] * (base - HFA))
    # Prespecified probability calibration, using February outcomes only.
    results, calibration, probs, raw_probs = {}, {}, {}, {}
    for name, predictions in forecasts.items():
        model = LogisticRegression(C=1.0, solver='lbfgs', max_iter=1000)
        model.fit(predictions[feb, None], (yy[feb] > 0).astype(int))
        assert model.coef_[0, 0] >= 0, 'Winner link must be monotonic'
        probs[name] = model.predict_proba(predictions[:, None])[:, 1]
        raw_probs[name] = (baseline_probs.copy() if np.array_equal(predictions, base)
                           else normal_probs(predictions))
        calibration[name] = dict(intercept=float(model.intercept_[0]),
                                 margin_coefficient=float(model.coef_[0, 0]), C=1.0,
                                 fit_n=int(feb.sum()), method='logistic_February_only')
        results[name] = {}
        for period, mask in (('january_development', jan), ('february_calibration', feb),
                             ('march_april_assessment', test)):
            # Never publish a January score from a February-fitted link.
            item = metrics(yy[mask], predictions[mask],
                           None if period == 'january_development' else probs[name][mask])
            item['winner_brier_raw_normal'] = float(np.mean((raw_probs[name][mask] - (yy[mask] > 0)) ** 2))
            results[name][period] = item
    assert locked_hash == hashlib.sha256(json.dumps(selected_by_family, sort_keys=True).encode()).hexdigest()
    # Direct poison checks: target and future labels have no influence on ratings.
    last_i = eligible[np.flatnonzero(test)[-1]]
    alpha, half = strength_candidates[selected_by_family['strength']['name']]
    clean = rating_prediction(x, y, times, last_i, alpha, half)
    poisoned = y.copy()
    poisoned[times > times[last_i] - LAG] += 10000
    assert clean == rating_prediction(x, poisoned, times, last_i, alpha, half)
    # Production baseline also ignores target/future score rows.
    first_test_i = eligible[np.flatnonzero(test)[0]]
    g = games[first_test_i]
    corrupted = [dict(r, points=r['points'] + 10000) if nba_model.parse_time(r['played_at']) > g['ts'] - LAG else r for r in rows]
    assert nba_model.project_game(g['home'], g['away'], rows, g['ts']) == nba_model.project_game(g['home'], g['away'], corrupted, g['ts'])
    days = np.array([iso(v)[:10] for v in tt[test]])
    uncertainty = {}
    for name in ('strength', 'shrinkage'):
        uncertainty[name] = dict(
            calibrated_probability_comparison=paired_intervals(yy[test], base[test], forecasts[name][test], probs['baseline'][test], probs[name][test], days),
            raw_normal_probability_comparison=paired_intervals(yy[test], base[test], forecasts[name][test], raw_probs['baseline'][test], raw_probs[name][test], days))
    out = dict(protocol_file='experiment_protocol.md', protocol_sha256=digest(HERE / 'experiment_protocol.md'),
               script_sha256=digest(Path(__file__)), created_with=dict(python=sys.version.split()[0], numpy=np.__version__, sklearn=sklearn.__version__),
               database_access='SQLite mode=ro plus query_only; no network calls',
               source_selection='Earliest successful per-team 2026 regular-season schedule archive before 2026-10-28 UTC',
               source_catalog=catalogue, source_rows=len(rows), unique_games=len(games),
               eligible_games=len(eligible), feature_lag_seconds=LAG,
               counts=dict(initial_2025=int((tt < stamp('2026-01-01T00:00:00Z')).sum()), january=int(jan.sum()), february=int(feb.sum()), march_april=int(test.sum())),
               grid=dict(alphas=ALPHAS, half_lives_days=HALF_LIVES, baseline_shrink_weights=SHRINK_WEIGHTS),
               january_grid=sorted(january_grid, key=rank), selected_by_family=selected_by_family,
               overall_january_selected_candidate=selected, selected_settings_sha256=locked_hash,
               probability_calibration=calibration, metrics=results,
               paired_day_block_bootstrap=dict(draws=2000, seed=SEED, intervals_95pct_candidate_minus_baseline=uncertainty),
               leakage_checks=dict(full_game_groups=True, four_hour_lag=True, target_future_poison_invariance=True, october_labels_used=False, setting_selection_january_only=True),
               limits=[
                 'March-April labels were already examined in the October 27 alternative study; this is retrospective, not an independent fresh holdout.',
                 'Rolling strength estimates incorporate earlier eligible assessment-game results for later targets, just as baseline histories do; hyperparameters and probability links stay locked.',
                 'Historical source snapshots were received in October 2026; the reconstruction assumes final scores were available four hours after their recorded tipoff, not verified historical publication timestamps.',
                 'One historical regular season; no preceding season available to validate offseason transfer, roster changes, or early-current-season weighting.',
                 'No historical sportsbook lines: no ATS win-rate or profit claim. No October 2026 labels used in any fit, calibration, or selection.',
                 'No possession, injury, lineup, or roster point-in-time inputs. Team-effect ratings cannot correct those omissions.',
                 'No production code, database rows, saved forecasts, grades, or pinned model artifacts changed.'
               ], interpretation=dict(
                   margin_assessment='The January-selected strength model improved assessment MAE by only 0.0081 point; its day-block interval includes deterioration. This does not establish useful spread-projection improvement.',
                   probability_assessment='Raw-normal winner Brier improved by 0.00576, but its interval crosses zero; February logistic calibration worsened assessment Brier for both baseline and strength.',
                   recency_and_shrinkage='January selected no recency decay and no baseline shrinkage. Faster recency and margin shrinking are not supported by this development set.',
                   evidence_needed='Keep the candidate experimental. Obtain additional historical seasons and point-in-time sportsbook lines; then assess offseason transfer and genuine spread forecasts prospectively.'),
               decision='Available evidence does not support spread-model promotion; no production deployment performed.')
    per_game = []
    for j in np.flatnonzero(test):
        g = games[eligible[j]]
        per_game.append(dict(game_id=g['game_id'], tipoff=iso(g['ts']), home=g['home'], away=g['away'], actual_margin=float(yy[j]),
                             predicted_margin={name: float(vals[j]) for name, vals in forecasts.items()},
                             home_winner_probability_calibrated={name: float(vals[j]) for name, vals in probs.items()},
                             home_winner_probability_raw_normal={name: float(vals[j]) for name, vals in raw_probs.items()}))
    out['march_april_per_game'] = per_game
    (HERE / 'experiment_results.json').write_text(json.dumps(out, indent=2, sort_keys=True) + '\n')
    write_summary(out)
    print(json.dumps(dict(selected=selected['name'], metrics=results), indent=2), flush=True)


def write_summary(out):
    lines = ['# Offline NBA margin experiment — October 28 audit', '',
             'No production change was made. Settings were selected on January only; '
             'February fit the fixed winner-probability link. October 2026 outcomes were excluded from every fit and selection.', '',
             f"Verified {out['source_rows']:,} sourced team rows / {out['unique_games']:,} complete games. "
             f"Eligible forecasts: {out['counts']}. Four-hour feature lag and full-game grouping were preserved.", '',
             'The fixed grid is in [experiment_protocol.md](experiment_protocol.md); '
             'exact source hashes, all January grid results, fitted probability links, per-game assessment forecasts, '
             'and day-block uncertainty intervals are in [experiment_results.json](experiment_results.json).', '',
             f"January selected **{out['overall_january_selected_candidate']['name']}** overall. "
             f"Family selections: strength `{out['selected_by_family']['strength']['name']}`; "
             f"shrinkage `{out['selected_by_family']['shrinkage']['name']}`. "
             'This selects the identity baseline over both proposed shrinkages, and selects no decay over faster recency weighting.', '',
             '| Model | January margin MAE | Assessment margin MAE | Assessment margin RMSE | Winner Brier, original normal link | Winner Brier, February logistic link |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for name, result in out['metrics'].items():
        dev, test = result['january_development'], result['march_april_assessment']
        lines.append(f"| {name} | {dev['margin_mae']:.4f} | {test['margin_mae']:.4f} | {test['margin_rmse']:.4f} | {test['winner_brier_raw_normal']:.5f} | {test['winner_brier']:.5f} |")
    lines += ['', 'Both probability columns use the same point forecasts. The original column uses SD=11.6 '
              '(production rounded baseline probabilities); the logistic column fits each model on February only. '
              'February logistic scores are in-sample. No January score from the later-fitted link is published. '
              'Only the assessment-period probability comparison is evaluated.', '',
              'Negative differences below favor the candidate. Intervals use 2,000 paired calendar-day bootstrap '
              'resamples with a fixed seed; they describe sampling variability within this single season.', '',
              '| Candidate | Margin MAE difference, 95% interval | RMSE difference, 95% interval | Raw-normal Brier difference, 95% interval |',
              '| --- | ---: | ---: | ---: |']
    intervals = out['paired_day_block_bootstrap']['intervals_95pct_candidate_minus_baseline']
    for name in ('strength', 'shrinkage'):
        q = intervals[name]['raw_normal_probability_comparison']
        cell = lambda k: f"[{q[k]['low']:+.4f}, {q[k]['high']:+.4f}]"
        lines.append(f"| {name} | {cell('margin_mae')} | {cell('margin_rmse')} | {cell('winner_brier')} |")
    lines += ['', out['interpretation']['margin_assessment'] + ' ' + out['interpretation']['probability_assessment'], '',
              out['interpretation']['evidence_needed'], '',
              'This evaluates opponent-adjusted net-strength estimation rather than the recent-score ridge '
              'rejected October 27. It tests a limited available-data hypothesis; it does not estimate individual '
              'player availability or resolve stale forecast timing. A smaller winner Brier is not evidence for '
              'better spread selection without actual historical market lines.', '', 'Limitations:', '']
    lines += ['- ' + item for item in out['limits']]
    lines += ['', 'Reproduce with:', '', '```sh',
              'PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python reports/nba/spread_audit_2026_10_28/experiment_strength.py',
              '```', '']
    (HERE / 'experiment_findings.md').write_text('\n'.join(lines))


if __name__ == '__main__':
    run()
