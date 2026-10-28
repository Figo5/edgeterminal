"""Versioned NBA adaptation, offline fitting, and actual-main-line evaluation.

Serving only applies one frozen artifact to current, strictly prior inputs.
The worker may propose a new artifact separately; the common registry decides
whether its independent whole-game holdout earns activation. Existing saved
predictions, model parameters, locks and grades are never rewritten here.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

from . import nba_model as baseline
from . import nba_prop_model
from .nba_learning_data import training_history, validation_records, verify_source
from .pricing import american_to_decimal, american_to_prob

MARKETS = baseline.MARKETS
COUNT_MARKETS = baseline.COUNT_MARKETS
DEVELOPMENT_GAMES = 120
CALIBRATION_GAMES = 120
VALIDATION_GAMES = 40
RECENT_WEIGHTS = (0., .25, .5, .75)
MIN_NEW_GAMES = 10
ROOT = Path(__file__).resolve().parents[1]
_UNSET = object()
_SNAPSHOT = ContextVar('nba_learning_serving_snapshot', default=_UNSET)
DESIGN = ('Expanding chronological coefficient fit; next 120 whole games for '
          'production-weight development; next 120 whole games for residual calibration; '
          'latest 40 fully graded locked games for actual-main-line validation. '
          'All fitting-source receipts precede the earliest validation quote observation. '
          'Development selects a predeclared season-aware shrinkage weight by MAE only. '
          'Count probabilities integrate Gamma-Poisson rate uncertainty. '
          'No outcome-based parameter changes occur while serving a forecast.')


def _active():
    saved = _SNAPSHOT.get()
    if saved is not _UNSET:
        return saved
    from . import model_learning_store
    return model_learning_store.load_active('NBA')


@contextmanager
def snapshot():
    """Keep selection, point outputs and saved headers on one active version."""
    token = _SNAPSHOT.set(_active())
    try:
        yield
    finally:
        _SNAPSHOT.reset(token)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('NBA adaptive parameters must be finite numbers')
    return float(value)


def _validate_artifact(artifact):
    if (artifact.get('schema') != 1 or artifact.get('kind') != 'nba-adaptive-v1'
            or set(artifact.get('markets', {})) != set(MARKETS)):
        raise ValueError('Invalid NBA adaptive artifact contract')
    _number(artifact['training_cutoff'])
    minute_model = artifact['minutes_model']
    _number(minute_model['intercept'])
    if len(minute_model['coefficients']) != len(nba_prop_model.MINUTE_FEATURES):
        raise ValueError('Invalid NBA adaptive minutes coefficient count')
    for value in minute_model['coefficients']:
        _number(value)
    for market, config in artifact['markets'].items():
        weight = _number(config['recent_weight'])
        residuals = config['standardized_residuals']
        method = config['probability_method']
        if (weight not in RECENT_WEIGHTS or len(residuals) < 30 or residuals != sorted(residuals)
                or config['calibration_n'] != len(residuals)
                or method != ('gamma_poisson' if market in COUNT_MARKETS else 'empirical')):
            raise ValueError('Invalid NBA adaptive residual calibration')
        for value in residuals:
            _number(value)
    return artifact


def _info(active):
    if active is None:
        return dict(version=baseline.MODEL_VERSION, cutoff='2026-10-01T00:00:00Z',
                    fingerprint=baseline.PARAMETER_FINGERPRINT)
    artifact = _validate_artifact(active['artifact'])
    return dict(version=active['version'], cutoff=_iso(active['training_cutoff']),
                fingerprint=active.get('fingerprint') or active.get('artifact_sha256')
                or hashlib.sha256(_json(artifact).encode()).hexdigest())


def model_info():
    return _info(_active())


def parameters():
    """Parameters registered with a version; never pretend old parameters apply."""
    current = _active()
    return _validate_artifact(current['artifact']) if current else baseline.PARAMETERS


def historical_evaluation():
    current = _active()
    if current is None:
        return baseline.historical_evaluation()
    artifact = _validate_artifact(current['artifact'])
    report = json.loads(_json(artifact.get('actual_main_line_validation', {})))
    report.update(model_version=current['version'], parameter_fingerprint=_info(current)['fingerprint'],
                  design=DESIGN, source_catalog=artifact.get('training_source_catalog', []))
    return report


def _season(when):
    day = datetime.fromtimestamp(when, timezone.utc)
    return day.year if day.month >= 7 else day.year - 1


def _production(rows, tipoff, recent_weight):
    """Finite recent/season adjustment shrunk toward the 15-game rate.

    The newest season's last five games receive extra exposure weight. With
    only a few new-season games the adjustment is shrunk by n/(n+5), keeping
    the old-season sample useful without treating it as the current role.
    Total pseudo-exposure stays at the original sample size.
    """
    current = [i for i, row in enumerate(rows) if _season(row['ts']) == _season(tipoff)]
    recent = (current or list(range(len(rows))))[-5:]
    strength = recent_weight * len(recent) / (len(recent) + 5.)
    recent_set = set(recent)
    weights = [(1. - strength) + (strength * len(rows) / len(recent) if i in recent_set else 0.)
               for i in range(len(rows))]
    events = sum(row['stat'] * weight for row, weight in zip(rows, weights))
    exposure = sum(row['minutes'] * weight for row, weight in zip(rows, weights))
    return events, exposure, strength


def gamma_poisson_probabilities(shape, exposure, future_minutes, line):
    """Posterior predictive integer mass; rate uncertainty includes finite zeros.

    shape=S+1/2 and exposure=E represent a Jeffreys rate posterior. This law
    has nonnegative support and explicit integer push mass; no confidence cap
    substitutes for uncertainty. The recurrence uses log probabilities to
    remain stable for high-count lines.
    """
    shape, exposure, future_minutes, line = map(_number, (shape, exposure, future_minutes, line))
    if shape <= 0 or exposure <= 0 or future_minutes <= 0:
        raise ValueError('NBA predictive count exposure must be positive')
    if line < 0:
        return 1., 0., 0.
    log_p = math.log(exposure / (exposure + future_minutes))
    log_q = math.log(future_minutes / (exposure + future_minutes))
    top = math.floor(line)
    if top > 10000:
        raise ValueError('NBA count line exceeds supported range')
    masses = [math.exp(math.lgamma(shape + k) - math.lgamma(shape) - math.lgamma(k + 1)
                       + shape * log_p + k * log_q) for k in range(top + 1)]
    integer = abs(line - round(line)) < 1e-9
    push = masses[-1] if integer else 0.
    under = min(1., math.fsum(masses[:-1] if integer else masses))
    over = max(0., 1. - under - push)
    return over, under, push


def _predict(rows, tipoff, market, artifact):
    config = artifact['markets'][market]
    _, minutes = nba_prop_model.features(rows, tipoff, artifact['minutes_model'])
    events, exposure, strength = _production(rows, tipoff, config['recent_weight'])
    shape = events + .5 if market in COUNT_MARKETS else events
    mean = max(0., shape * minutes / exposure)
    sd = max(statistics.stdev(r['stat'] for r in rows), baseline.PROP_SD_FLOOR[market])
    return mean, minutes, sd, dict(shape=shape, exposure=exposure, minutes=minutes,
                                 recent_strength=strength)


def _probabilities(market, mean, sd, line, config, distribution):
    if market in COUNT_MARKETS:
        return gamma_poisson_probabilities(distribution['shape'], distribution['exposure'],
                                           distribution['minutes'], line)
    return nba_prop_model.probabilities(market, mean, sd, line, {'markets': {market: config}})


def _project(player_id, market, player_history, tipoff, line, artifact, info):
    if market not in MARKETS:
        raise ValueError(f'unsupported market {market!r}')
    tip = baseline.parse_time(tipoff)
    if tip is None or tip <= artifact['training_cutoff']:
        # A newly fitted artifact cannot rewrite an earlier projection.
        return baseline.project_prop(player_id, market, player_history, tipoff, line)
    out = dict(model_version=info['version'], parameter_fingerprint=info['fingerprint'],
               readiness='RESEARCH_ONLY', designation='PASS', player_id=str(player_id), market=market,
               tipoff=tip)
    rows, conflicts = baseline._player_rows(player_history, str(player_id), market, tip)
    out['input_conflicts'] = conflicts
    if len(rows) < baseline.MIN_PROP_GAMES:
        return dict(out, status='unavailable', reason='insufficient_games', n=len(rows))
    mean, minutes, sd, distribution = _predict(rows, tip, market, artifact)
    minute_sd = statistics.stdev(r['minutes'] for r in rows)
    config = artifact['markets'][market]
    out.update(status='ok', projection=round(mean, 2), raw_projection=mean,
               raw_sd=sd, sd=round(sd, 2), n=len(rows), projected_minutes=round(minutes, 1),
               minutes_sd=round(minute_sd, 2),
               minutes_flag='volatile' if minute_sd / minutes > baseline.MINUTES_CV_FLAG else 'stable',
               probability_over=None, probability_under=None, probability_push=None,
               point_method='season_shrunk_opportunity',
               probability_method='exposure_gamma_poisson' if market in COUNT_MARKETS else 'adaptive_residual_cdf',
               calibration_n=config['calibration_n'], recent_strength=distribution['recent_strength'],
               limits=['historical current-roster sample has survivorship bias',
                       'no opponent, pace or injury adjustment',
                       'season transition weights shrink sparse recent games toward prior history',
                       'integer lines carry push mass excluded from both sides'])
    if line is not None:
        number = baseline.valid_number(line)
        if number is None:
            raise ValueError('invalid prop line')
        over, under, push = _probabilities(market, mean, sd, number, config, distribution)
        out.update(line=number, probability_over=round(over, 4), probability_under=round(under, 4),
                   probability_push=round(push, 4))
    return out


def project_prop(player_id, market, player_history, tipoff, line=None):
    current = _active()
    if current is None:
        return baseline.project_prop(player_id, market, player_history, tipoff, line)
    artifact = _validate_artifact(current['artifact'])
    return _project(player_id, market, player_history, tipoff, line, artifact, _info(current))


def select_props(quotes, player_history, tipoff, now, injuries=None, count=5):
    current = _active()
    if current is None:
        return baseline.select_props(quotes, player_history, tipoff, now, injuries, count)
    artifact = _validate_artifact(current['artifact'])
    tip, stamp = baseline.parse_time(tipoff), baseline.parse_time(now)
    if tip is None or stamp is None or stamp >= tip:
        raise ValueError('select_props requires valid now before tipoff')
    if stamp < artifact['training_cutoff'] or tip <= artifact['training_cutoff']:
        return baseline.select_props(quotes, player_history, tipoff, now, injuries, count)
    info, groups, excluded, projections = _info(current), {}, [], {}
    for quote in quotes or []:
        # Reuse all baseline eligibility/identity/main-line/availability guards
        # on each real offer, retaining opposite sides and other books until
        # the adaptive probabilities have actually been compared.
        checked = baseline.select_props([quote], player_history, tipoff, now, injuries, count=1)
        excluded.extend(checked['excluded'])
        if not checked['candidates']:
            continue
        candidate = checked['candidates'][0]
        key = (candidate['game_id'], candidate['player_id'], candidate['market'])
        cache_key = (candidate['player_id'], candidate['market'])
        if cache_key not in projections:
            rows, _ = baseline._player_rows(player_history, cache_key[0], cache_key[1], tip)
            projections[cache_key] = _predict(rows, tip, cache_key[1], artifact)
        mean, minutes, sd, distribution = projections[cache_key]
        config = artifact['markets'][candidate['market']]
        over, under, push = _probabilities(candidate['market'], mean, sd, candidate['line'], config, distribution)
        probability = over if candidate['side'] == 'over' else under
        candidate.update(projection=round(mean, 2), sd=round(sd, 2), projected_minutes=round(minutes, 1),
                         probability=round(probability, 4), probability_push=round(push, 4),
                         edge=round(probability - american_to_prob(candidate['price']), 4),
                         point_method='season_shrunk_opportunity',
                         probability_method='exposure_gamma_poisson' if candidate['market'] in COUNT_MARKETS else 'adaptive_residual_cdf',
                         calibration_n=config['calibration_n'], model_version=info['version'],
                         parameter_fingerprint=info['fingerprint'])
        candidate['flags'] = [flag for flag in candidate['flags'] if flag != 'minutes_volatile']
        rows, _ = baseline._player_rows(player_history, candidate['player_id'], candidate['market'], tip)
        if statistics.stdev(row['minutes'] for row in rows) / minutes > baseline.MINUTES_CV_FLAG:
            candidate['flags'].append('minutes_volatile')
        order = (-probability, -american_to_decimal(candidate['price']),
                 str(candidate['sportsbook']), candidate['side'])
        if key not in groups or order < groups[key][0]:
            groups[key] = (order, candidate)
    candidates = sorted((value[1] for value in groups.values()),
                        key=lambda c: (-c['probability'], c['player_id'], c['market']))
    selected, rest, per_player = [], [], defaultdict(int)
    for candidate in candidates:
        if len(selected) < count and per_player[candidate['player_id']] < baseline.MAX_MARKETS_PER_PLAYER:
            selected.append(candidate)
            per_player[candidate['player_id']] += 1
        else:
            rest.append(candidate)
    selected.extend(rest[:max(0, count - len(selected))])
    status = 'ok' if len(selected) == count else 'wait'
    return dict(status=status, selections=selected if status == 'ok' else [], candidates=candidates,
                eligible_candidates=len(candidates), required=count, excluded=excluded, tipoff=tip,
                generated_at=stamp, model_version=info['version'], parameter_fingerprint=info['fingerprint'],
                readiness='RESEARCH_ONLY', designation='PASS')


def _game_id(game_id, when):
    value = str(game_id)
    return value if value.startswith('nba-') else f'nba-{_season(when)}-{value}'


def examples(history, cutoff, excluded_games=()):
    """Dynamic training labels with the same identity and four-hour lag rules."""
    excluded, by_player = set(excluded_games), defaultdict(list)
    for raw in history:
        try:
            when = baseline.parse_time(raw.get('played_at', raw.get('date')))
            values = [baseline.valid_number(raw.get(k), nonnegative=True)
                      for k in ('minutes', 'points', 'rebounds', 'assists', 'threes')]
            gid = _game_id(raw['game_id'], when)
            if (when is None or when + baseline.FEATURE_LAG_S >= cutoff or not raw.get('player_id')
                    or raw.get('verified') is False or any(v is None for v in values)
                    or not 0 < values[0] <= 65 or gid in excluded or str(raw['game_id']) in excluded):
                continue
            by_player[str(raw['player_id'])].append(dict(raw, ts=when, game_id=gid))
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    output = {market: [] for market in MARKETS}
    for pid, raw_rows in sorted(by_player.items()):
        groups = defaultdict(list)
        for row in raw_rows:
            groups[row['game_id']].append(row)
        rows = [group[0] for group in groups.values() if len({
            (r['ts'],) + tuple(float(r[k]) for k in ('minutes', 'points', 'rebounds', 'assists', 'threes'))
            for r in group}) == 1]
        rows.sort(key=lambda r: (r['ts'], r['game_id']))
        for target in rows:
            prior = [r for r in rows if r['ts'] <= target['ts'] - baseline.FEATURE_LAG_S][-15:]
            if len(prior) < baseline.MIN_PROP_GAMES:
                continue
            for market in MARKETS:
                stat = lambda r: sum(float(r[k]) for k in ('points', 'rebounds', 'assists')) if market == 'pra' else float(r[market])
                output[market].append(dict(rows=[dict(ts=r['ts'], minutes=float(r['minutes']), stat=stat(r)) for r in prior],
                                           target=stat(target), minutes=float(target['minutes']), ts=target['ts'],
                                           player_id=pid, game_id=target['game_id']))
    return output


def split_windows(datasets, development_games=DEVELOPMENT_GAMES, calibration_games=CALIBRATION_GAMES):
    """Fixed-size tail windows by whole game; remaining prefix expands."""
    games = {}
    for rows in datasets.values():
        for row in rows:
            games[row['game_id']] = min(games.get(row['game_id'], row['ts']), row['ts'])
    ordered = [gid for gid, _ in sorted(games.items(), key=lambda pair: (pair[1], pair[0]))]
    cut = max(0, len(ordered) - calibration_games)
    fit_cut = max(0, cut - development_games)
    groups = [set(ordered[:fit_cut]), set(ordered[fit_cut:cut]), set(ordered[cut:])]
    return tuple({market: [r for r in rows if r['game_id'] in group]
                  for market, rows in datasets.items()} for group in groups)


def fit_candidate(datasets, cutoff):
    """Fit one predeclared method without accessing outer-validation labels."""
    from .nba_model_fit import minute_fit
    fit, development, calibration = split_windows(datasets)
    if len(fit['points']) < 500 or len(development['points']) < 100 or len(calibration['points']) < 100:
        raise ValueError('insufficient NBA chronological fitting/calibration data')
    minutes_model = minute_fit(fit['points'])
    artifact = dict(schema=1, kind='nba-adaptive-v1', training_cutoff=cutoff,
                    minutes_model=minutes_model, markets={}, method=DESIGN,
                    window=15, min_games=5, feature_lag_s=baseline.FEATURE_LAG_S,
                    gamma_prior_shape=.5, recent_games=5, shrinkage_games=5,
                    feature_names=dict(minutes=list(nba_prop_model.MINUTE_FEATURES)),
                    development_weight_grid=list(RECENT_WEIGHTS))
    for market in MARKETS:
        scores = []
        for weight in RECENT_WEIGHTS:
            artifact['markets'][market] = dict(recent_weight=weight)
            errors = [abs(row['target'] - _predict(row['rows'], row['ts'], market, artifact)[0])
                      for row in development[market]]
            scores.append((statistics.mean(errors), weight))
        _, chosen = min(scores)
        artifact['markets'][market] = dict(recent_weight=chosen)
        residuals = sorted((row['target'] - _predict(row['rows'], row['ts'], market, artifact)[0]) /
                           _predict(row['rows'], row['ts'], market, artifact)[2] for row in calibration[market])
        artifact['markets'][market].update(probability_method='gamma_poisson' if market in COUNT_MARKETS else 'empirical',
                                           standardized_residuals=residuals, calibration_n=len(residuals),
                                           development_mae_by_weight=[dict(weight=w, mae=e) for e, w in scores])
    fitted = fit['points'] + development['points']
    artifact['training'] = dict(fit_game_ids=sorted({r['game_id'] for r in fitted}),
                                coefficient_game_ids=sorted({r['game_id'] for r in fit['points']}),
                                development_game_ids=sorted({r['game_id'] for r in development['points']}),
                                calibration_game_ids=sorted({r['game_id'] for r in calibration['points']}),
                                fit_label_max_at=max(r['ts'] + baseline.FEATURE_LAG_S for r in fitted),
                                development_label_max_at=max(r['ts'] + baseline.FEATURE_LAG_S for r in development['points']),
                                calibration_label_max_at=max(r['ts'] + baseline.FEATURE_LAG_S for r in calibration['points']),
                                fit_n=len(fit['points']), development_n=len(development['points']),
                                calibration_n=len(calibration['points']))
    return artifact


def _summary(rows):
    markets = {}
    for market in MARKETS:
        subset = [r for r in rows if r['market'] == market]
        values = dict(holdout_n=len(subset))
        if subset:
            won = lambda r: float((r['actual'] > r['line']) == (r['side'] == 'over'))
            for label, key in (('baseline', 'baseline'), ('upgraded', 'candidate')):
                values[label + '_mae'] = statistics.mean(abs(r[key + '_prediction'] - r['actual']) for r in subset)
                values[label + '_brier'] = statistics.mean((r[key + '_probability'] - won(r)) ** 2 for r in subset)
                values[label + '_log_loss'] = statistics.mean(-math.log(max(1e-12, min(1 - 1e-12,
                    r[key + '_probability'] if won(r) else 1 - r[key + '_probability']))) for r in subset)
            values['whole_games'] = len({r['game_id'] for r in subset})
        markets[market] = values
    return dict(markets=markets, player_count=len({r['player_id'] for r in rows}),
                game_count=len({r['game_id'] for r in rows}), holdout_n=len(rows))


def build_candidate(now=None):
    """One scheduled, network-free proposal; no mutation or activation here."""
    stamp = time.time() if now is None else _number(now)
    records, context, validation_sources = validation_records(stamp, VALIDATION_GAMES)
    if not records:
        return dict(status='waiting', sport='NBA', reason='no_fully_graded_locked_games')
    current = _active()
    validation_start = min(r['observed_at'] for r in records)
    cutoff = validation_start - 1.
    if current and current['training_cutoff'] >= validation_start:
        return dict(status='waiting', sport='NBA', reason='active_baseline_overlaps_validation_window')
    if current:
        previous = set(current['artifact']['training'].get('validation_game_ids', []))
        if len(set(context['game_ids']) - previous) < MIN_NEW_GAMES:
            return dict(status='waiting', sport='NBA', reason='fewer_than_ten_new_whole_games')
    history, fitting_sources = training_history(cutoff)
    datasets = examples(history, cutoff, excluded_games=set(context['game_ids']))
    try:
        artifact = fit_candidate(datasets, cutoff)
    except ValueError as error:
        if 'insufficient NBA' not in str(error):
            raise
        return dict(status='waiting', sport='NBA', reason=str(error))
    artifact['training']['validation_game_ids'] = context['game_ids']
    artifact['training_source_catalog'] = fitting_sources
    # Baseline imports the uncertainty floors and the reviewed JSON checksum;
    # changes to either dependency must invalidate this artifact's identity.
    artifact['code_sha256'] = {'edgeterminal/' + name: hashlib.sha256((ROOT / 'edgeterminal' / name).read_bytes()).hexdigest()
                              for name in ('nba_learning.py', 'nba_learning_data.py', 'nba_model.py', 'nba_prop_model.py',
                                           'pricing.py', 'nba_sources.py', 'nba_data.py', 'nba_quotes.py', 'nba_identity.py',
                                           'nba_model_fit.py', 'analytics/nba.py', 'nba_model_manifest.py')}
    info = dict(version='nba-adaptive-unregistered', fingerprint='unregistered')
    paired = []
    for record in records:
        pid, market, history, tip, line = (record[k] for k in ('player_id', 'market', 'history', 'kickoff_at', 'line'))
        if current:
            before = _project(pid, market, history, tip, line, current['artifact'], _info(current))
        else:
            before = baseline.project_prop(pid, market, history, tip, line)
        after = _project(pid, market, history, tip, line, artifact, info)
        if before['status'] != 'ok' or after['status'] != 'ok':
            raise ValueError(f'NBA learning frozen inputs cannot reproduce quote {record["quote_id"]}')
        side = 'probability_' + record['side']
        paired.append({**{k: record[k] for k in ('game_id', 'player_id', 'market', 'quote_id', 'line', 'side',
                                                'observed_at', 'kickoff_at', 'result_observed_at', 'actual')},
                       'baseline_prediction': before['raw_projection'], 'candidate_prediction': after['raw_projection'],
                       'baseline_probability': before[side], 'candidate_probability': after[side]})
    evaluation = dict(kind='actual_main_lines', validation_start=validation_start,
                      validation_end=max(r['result_observed_at'] for r in paired), calibration_cutoff=cutoff,
                      baseline_candidate_id=current['candidate_id'] if current else None,
                      rows=paired, design=DESIGN, exclusions=context['exclusions'])
    report = _summary(paired)
    report.update(holdout_start=_iso(min(r['kickoff_at'] for r in paired)),
                  holdout_end=_iso(max(r['kickoff_at'] for r in paired)), design=DESIGN)
    artifact['actual_main_line_validation'] = report
    sources = sorted(fitting_sources + validation_sources, key=lambda s: (s['role'], s['id'], s['sha256']))
    # A result correction or comparator change creates a distinct proposal;
    # retries with identical evidence retain exactly the same immutable version.
    version_seed = hashlib.sha256(_json(dict(artifact=artifact, evaluation=evaluation, sources=sources)).encode()).hexdigest()[:16]
    version = 'nba-adaptive-' + datetime.fromtimestamp(cutoff, timezone.utc).strftime('%Y%m%d') + '-' + version_seed
    artifact['version'] = version
    _validate_artifact(artifact)
    return dict(status='ready', sport='NBA', version=version, training_cutoff=cutoff,
                artifact=artifact, evaluation=evaluation, sources=sources,
                summary=report)


def nightly_diagnostics(now=None):
    """Observed reliability, role and season-transition diagnostics; no fitting."""
    stamp = time.time() if now is None else _number(now)
    records, context, sources = validation_records(stamp, VALIDATION_GAMES)
    groups = defaultdict(list)
    for record in records:
        saved = record['saved']
        probability = saved.get('probability', saved.get('confidence'))
        if baseline.valid_number(probability) is None:
            continue
        won = float((record['actual'] > record['line']) == (record['side'] == 'over'))
        prior, _ = baseline._player_rows(record['history'], record['player_id'], record['market'], record['kickoff_at'])
        recent_n = sum(_season(row['ts']) == _season(record['kickoff_at']) for row in prior)
        row = dict(probability=probability, won=won, absolute_error=abs(saved['projection'] - record['actual']))
        keys = ['all', 'market:' + record['market'], 'version:' + saved['model_version'],
                'season_history:' + ('sparse' if recent_n < 5 else 'established'),
                'minutes:' + ('volatile' if 'minutes_volatile' in saved.get('flags', []) else 'stable')]
        for key in keys:
            groups[key].append(row)
    cohorts = {}
    for key, rows in groups.items():
        bins = defaultdict(list)
        for row in rows:
            bins[min(9, int(row['probability'] * 10))].append(row)
        cohorts[key] = dict(n=len(rows), wins=int(sum(r['won'] for r in rows)),
                            mean_confidence=statistics.mean(r['probability'] for r in rows),
                            win_rate=statistics.mean(r['won'] for r in rows),
                            brier=statistics.mean((r['probability'] - r['won']) ** 2 for r in rows),
                            mae=statistics.mean(r['absolute_error'] for r in rows),
                            reliability=[dict(low=index / 10, high=(index + 1) / 10, n=len(values),
                                              mean_confidence=statistics.mean(r['probability'] for r in values),
                                              win_rate=statistics.mean(r['won'] for r in values))
                                         for index, values in sorted(bins.items())])
    return dict(status='ok' if records else 'waiting', sport='NBA', generated_at=stamp,
                model=model_info(), whole_games=len(context['game_ids']), cohorts=cohorts,
                exclusions=context['exclusions'], verified_sources=len(sources),
                note='Correlated selections are descriptive diagnostics; only the scheduled whole-game gate may change the model.')
