"""Offline totals learning: sealed prospective predictions and consumed dates."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import json
import math
import statistics

from . import db, nba_totals_data as data, nba_totals_model as model
from . import nba_totals_store as store, learning_publication as publication
from .config import settings

DAY = 86400
WEEK = 7 * DAY


def protocol():
    """The finite family and metric gates are sealed before any outer label."""
    return dict(schema=1, family='nba-totals-prospective-v1',
        proposal_frequency='one_selected_candidate_per_cohort', excluded_through='2026-11-04',
        validation_start='first_whole_ET_date_after_candidate_seal',
        methods=['incumbent', 'intercept', 'season', 'pace'], shrinkages=[5., 10., 20.],
        recent_weights=[0., .25, .5], min_games=60, min_dates=7,
        min_mae_points=.25, min_mae_fraction=.02, min_brier_improvement=.002,
        brier='conditional_over_excluding_pushes', target='full_game_total_including_overtime',
        bootstrap=dict(unit='whole_Eastern_date', replicates=2000, seed=20261105, confidence=.95),
        nonregression=['rmse', 'absolute_bias', 'decided_win_count', 'coverage'],
        benchmark='candidate_mae_at_most_saved_line_mae',
        development='earlier_whole_date_fit_development_calibration',
        prediction_freeze='sealed_artifact_before_forecast_before_physical_tipoff')


def _physical_now(requested):
    return max(store.stamp(requested), db.now())


def _validation_start(candidate):
    day = datetime.fromtimestamp(candidate['created_at'], store.ET).date()
    return (day + timedelta(days=1)).isoformat()


def code_catalog():
    return store.code_catalog()


def ensure_schema():
    store.ensure_schema()


def _record(now, status, detail):
    return store.record_run(now, status, detail)


def needs_inputs():
    """Cheap existence check; never fit, read labels, or replay sources."""
    ensure_schema()
    return store.active_event() is not None or store.pending() is not None


def active():
    """Capture one verified immutable artifact for a complete projection."""
    ensure_schema()
    event = store.active_event()
    if not event or not event['version']:
        return None
    candidate = store.candidate(event['version'], verify_code=True)
    proof = db.q1('SELECT * FROM nba_totals_publication_proofs WHERE version=?', (event['version'],))
    evaluation = store.evaluation_for(event['version'])
    if not proof or not evaluation or not evaluation['evaluation'].get('passed'):
        raise ValueError('NBA totals active publication proof is unavailable')
    payload = json.loads(proof['payload'])
    if (store.sha(payload) != proof['sha256'] or payload['candidate_sha256'] != candidate['sha256']
            or payload['evaluation_id'] != evaluation['evaluation_id']
            or payload['protocol_sha256'] != candidate['protocol_sha256']
            or payload['reservation_id'] != evaluation['reservation_id']
            or json.loads(event['detail']).get('proof_id') != proof['proof_id']):
        raise ValueError('NBA totals active publication seal mismatch')
    return dict(version=candidate['version'], sha256=candidate['sha256'], artifact=candidate['artifact'],
                activated_at=event['created_at'], event_id=event['id'])


def serving_state():
    return active()


def evaluate(rows, *, training_cutoff, sealed_protocol=None):
    """Point/probability gates with paired whole-Eastern-date intervals."""
    policy = protocol() if sealed_protocol is None else sealed_protocol
    if policy != protocol():
        raise ValueError('NBA totals evaluation protocol differs from predeclared gates')
    cutoff = store.stamp(training_cutoff)
    if len({r['game_id'] for r in rows}) != len(rows):
        raise ValueError('NBA totals validation repeats a game')
    for row in rows:
        tip, forecast, result = (store.stamp(row[k]) for k in ('tipoff', 'forecast_at', 'result_at'))
        day = datetime.fromtimestamp(tip, store.ET).date().isoformat()
        if day <= policy['excluded_through']:
            raise ValueError('NBA totals prospective validation includes inspected outcomes')
        if row.get('date_et', day) != day or not cutoff < forecast < tip < result:
            raise ValueError('NBA totals validation chronology is invalid')
        for key in ('actual_total', 'baseline_total', 'candidate_total'):
            store.stamp(row[key])
        if row.get('saved_total_line') is not None:
            store.stamp(row['saved_total_line'])
            for key in ('baseline_probability', 'candidate_probability'):
                if not 0 <= store.stamp(row[key]) <= 1:
                    raise ValueError('NBA totals probability outside zero-one')
        elif row.get('baseline_probability') is not None or row.get('candidate_probability') is not None:
            raise ValueError('NBA totals unpaired line probabilities')
        for key in ('forecast_sha256', 'label_sha256'):
            store._digest(row[key])
    paired = [r for r in rows if r.get('saved_total_line') is not None]
    days = sorted({datetime.fromtimestamp(r['tipoff'], store.ET).date().isoformat() for r in paired})
    binary = [r for r in paired if r['actual_total'] != r['saved_total_line']]
    reasons = []
    if len(paired) < policy['min_games']:
        reasons.append('fewer_than_sixty_prospective_pairs')
    if len(days) < policy['min_dates']:
        reasons.append('fewer_than_seven_whole_dates')
    if not rows:
        return dict(passed=False, reasons=reasons, rows=0, pairs=0, days=0)
    def mae(name):
        return statistics.mean(abs(r[name + '_total'] - r['actual_total']) for r in rows)
    def rmse(name):
        return statistics.mean((r[name + '_total'] - r['actual_total']) ** 2 for r in rows) ** .5
    def bias(name):
        return abs(statistics.mean(r[name + '_total'] - r['actual_total'] for r in rows))
    def brier(name):
        return statistics.mean((r[name + '_probability'] - (r['actual_total'] > r['saved_total_line'])) ** 2
                               for r in binary) if binary else None
    def wins(name):
        return sum((r[name + '_total'] >= r['saved_total_line']) ==
                   (r['actual_total'] > r['saved_total_line']) for r in binary)
    before, after = mae('baseline'), mae('candidate')
    b_before, b_after = brier('baseline'), brier('candidate')
    benchmark = statistics.mean(abs(r['saved_total_line'] - r['actual_total']) for r in paired) if paired else None
    cand_paired_mae = statistics.mean(abs(r['candidate_total'] - r['actual_total']) for r in paired) if paired else None
    if before - after < max(policy['min_mae_points'], policy['min_mae_fraction'] * before):
        reasons.append('total_improvement_too_small')
    if b_before is None or b_before - b_after < policy['min_brier_improvement']:
        reasons.append('over_calibration_not_improved')
    if rmse('candidate') > rmse('baseline'):
        reasons.append('rmse_regressed')
    if bias('candidate') > bias('baseline'):
        reasons.append('absolute_bias_regressed')
    if wins('candidate') < wins('baseline'):
        reasons.append('decided_win_count_regressed')
    if benchmark is None or cand_paired_mae > benchmark:
        reasons.append('saved_line_point_benchmark_not_met')
    intervals = {}
    if len(days) >= policy['min_dates'] and binary:
        import numpy as np
        all_days = sorted({datetime.fromtimestamp(r['tipoff'], store.ET).date().isoformat() for r in rows})
        blocks = [[r for r in rows if datetime.fromtimestamp(r['tipoff'], store.ET).date().isoformat() == day]
                  for day in all_days]
        rng = np.random.default_rng(policy['bootstrap']['seed'])
        deltas = []
        for _ in range(policy['bootstrap']['replicates']):
            sample = [r for i in rng.integers(0, len(blocks), len(blocks)) for r in blocks[i]]
            decided = [r for r in sample if r.get('saved_total_line') is not None and r['actual_total'] != r['saved_total_line']]
            if decided:
                deltas.append((statistics.mean(abs(r['candidate_total'] - r['actual_total']) -
                    abs(r['baseline_total'] - r['actual_total']) for r in sample),
                    statistics.mean((r['candidate_probability'] - (r['actual_total'] > r['saved_total_line'])) ** 2 -
                    (r['baseline_probability'] - (r['actual_total'] > r['saved_total_line'])) ** 2 for r in decided)))
        for index, name in enumerate(('total_mae', 'over_brier')):
            if not deltas:
                reasons.append('insufficient_binary_date_blocks')
                break
            low, high = np.quantile([pair[index] for pair in deltas], [.025, .975])
            intervals[name] = dict(low=float(low), high=float(high))
            if high >= 0:
                reasons.append(name + '_improvement_not_established')
    return dict(passed=not reasons, reasons=reasons, rows=len(rows), pairs=len(paired), days=len(days),
        binary_pairs=len(binary), pushes=len(paired) - len(binary), baseline_mae=before, candidate_mae=after,
        baseline_rmse=rmse('baseline'), candidate_rmse=rmse('candidate'), baseline_absolute_bias=bias('baseline'),
        candidate_absolute_bias=bias('candidate'), baseline_brier=b_before, candidate_brier=b_after,
        baseline_wins=wins('baseline'), candidate_wins=wins('candidate'), saved_line_mae=benchmark,
        candidate_paired_mae=cand_paired_mae, baseline_coverage=len(rows), candidate_coverage=len(rows),
        paired_day_intervals=intervals, all_supported_games_predicted=True, evaluated_rows=rows)


def _capture_record(saved, input_map):
    payload = saved.get('payload') or saved
    projection = payload.get('projection') or saved.get('projection') or {}
    market = payload.get('market') or saved.get('market') or input_map.get('market') or {}
    tipoff = input_map.get('tipoff', input_map.get('tipoff_ts'))
    as_of = input_map.get('as_of', payload.get('as_of', saved.get('created_at')))
    return dict(game_id=saved.get('game_id', input_map.get('game_id')),
        forecast_sha256=saved.get('payload_sha256', saved.get('sha256', saved.get('forecast_sha256'))),
        forecast_at=as_of, tipoff=tipoff,
        date_et=datetime.fromtimestamp(store.stamp(tipoff), store.ET).date().isoformat(),
        baseline_total=projection.get('total', input_map.get('baseline_total')),
        model_base_total=projection.get('total_base_total', projection.get('total', input_map.get('baseline_total'))),
        model_base_sd_total=projection.get('total_base_sd_total', projection.get('sd_total', model.DEFAULT_SD_TOTAL)),
        saved_total_line=market.get('total', market.get('total_line')),
        projection=projection, market=market, input_map=input_map)


def capture_forecast(saved, input_map, now=None):
    """Freeze pure inference from a caller-verified map; no label/source replay."""
    stamp = _physical_now(now)
    ensure_schema()
    pending = store.pending()
    if not pending:
        return None
    record = _capture_record(saved, input_map)
    if not pending['created_at'] <= record['forecast_at'] <= stamp < record['tipoff']:
        return None
    if record['date_et'] <= pending['protocol']['excluded_through']:
        return None
    pending = store.candidate(pending['version'], verify_code=True)
    record.update(_infer_prediction(pending['artifact'], record, input_map))
    record.update(input_sha256=input_map['sha256'], source_ids=saved.get('source_artifact_ids', []))
    record.pop('projection')
    physical = _physical_now(stamp)
    if physical >= record['tipoff']:
        return None
    return store.freeze_forecast(pending['version'], record, now=physical)



def _infer_prediction(artifact, record, mapped):
    """Use the serving mean rounding and saved incumbent's actual uncertainty."""
    raw_base = record.get('model_base_total', record['projection'].get('total_base_total', record['baseline_total']))
    raw_sd = record.get('model_base_sd_total', record['projection'].get('total_base_sd_total',
                        record['projection'].get('sd_total', model.DEFAULT_SD_TOTAL)))
    context = model.features(mapped['home'], mapped['away'], mapped['rows'], mapped['tipoff'],
                             as_of=mapped['as_of'], baseline_total=raw_base)
    result = model.predict(artifact, context, raw_base)
    applied = result.get('applied', True)
    total = round(result['total'], 2) if applied else raw_base
    projection = record['projection']
    baseline_sd = projection.get('sd_total', model.DEFAULT_SD_TOTAL)
    sigma = result['sd_total'] if applied else raw_sd
    line = record['saved_total_line']
    before = after = None
    if line is not None:
        # This is the incumbent's existing distribution, conditioned on no push.
        over, under, _ = data.nba_model.probabilities('total', record['baseline_total'], baseline_sd, line)
        before = over / (over + under) if over + under > 0 else .5
        after = model.total_probabilities(total, sigma, line)['conditional_over']
    return dict(candidate_total=total, baseline_probability=before,
                candidate_probability=after, candidate_branch=result.get('branch'))


def _replay_predictions(candidate, reservation, stamp):
    """Independently bind every frozen value to saved inputs and sealed code."""
    for game in reservation['games']:
        if not game.get('input_map'):
            raise ValueError('NBA totals inference replay requires a verified input map')
    # This adapter verifies exact saved bytes, source receipts and original lines;
    # it deliberately does not read outcomes or grades.
    records = {row['forecast_sha256']: row for row in data.forecast_inputs(
        now=stamp, limit=10000, start_date=min(reservation['date_ids']))}
    for game in reservation['games']:
        record = records.get(game['forecast_sha256'])
        if record is None:
            raise ValueError('NBA totals inference replay lost exact selected forecast')
        for key in ('game_id', 'date_et', 'forecast_at', 'tipoff', 'baseline_total', 'saved_total_line'):
            if game.get(key) != record.get(key):
                raise ValueError('NBA totals frozen prediction differs from saved forecast')
        raw_base = record.get('model_base_total', record['projection'].get('total_base_total', record['baseline_total']))
        raw_sd = record.get('model_base_sd_total', record['projection'].get('total_base_sd_total',
                            record['projection'].get('sd_total', model.DEFAULT_SD_TOTAL)))
        if (game.get('model_base_total', game['baseline_total']) != raw_base
                or game.get('model_base_sd_total', raw_sd) != raw_sd):
            raise ValueError('NBA totals frozen raw model base differs from saved forecast')
        mapped = record.get('input_map')
        if (not mapped or mapped != game['input_map'] or mapped['sha256'] != game['input_sha256']):
            raise ValueError('NBA totals inference replay input-map seal mismatch')
        inferred = _infer_prediction(candidate['artifact'], record, mapped)
        for key, value in inferred.items():
            observed = game.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not isinstance(observed, (int, float)) or not math.isclose(observed, value, rel_tol=0, abs_tol=1e-10):
                    raise ValueError('NBA totals frozen prediction disagrees with rounded sealed inference')
            elif observed != value:
                raise ValueError('NBA totals frozen prediction branch disagrees with sealed inference')

def _freeze_future(candidate, inputs, stamp):
    for record in inputs:
        if record['tipoff'] <= stamp or record['forecast_at'] < candidate['created_at']:
            continue
        mapped = record.get('input_map')
        if mapped:
            saved = dict(game_id=record['game_id'], payload_sha256=record['forecast_sha256'],
                         payload=dict(projection=record['projection'], market=record['market']),
                         source_artifact_ids=record.get('source_ids', []))
            capture_forecast(saved, mapped, now=stamp)


def _cohort(candidate, inputs, stamp):
    """Select only completed clock dates with every exact selected hash frozen."""
    by_hash = {g['forecast_sha256']: g for g in store.freezes(candidate['version'])}
    dates = defaultdict(list)
    for row in inputs:
        if (row['date_et'] > candidate['protocol']['excluded_through']
                and row['date_et'] >= _validation_start(candidate)):
            dates[row['date_et']].append(row)
    today = datetime.fromtimestamp(stamp, store.ET).date().isoformat()
    consumed = {r['date_et'] for r in db.q('SELECT date_et FROM nba_totals_reserved_dates')}
    games, universe = [], {}
    for day, rows in sorted(dates.items()):
        if day >= today or day in consumed:
            continue
        if any(r['tipoff'] >= stamp or r['forecast_sha256'] not in by_hash for r in rows):
            continue
        frozen = [by_hash[r['forecast_sha256']] for r in rows]
        if any(g['game_id'] != r['game_id'] for g, r in zip(frozen, rows)):
            raise ValueError('NBA totals frozen forecast game identity conflict')
        games.extend(frozen)
        universe[day] = sorted(r['game_id'] for r in rows)
        pairs = sum(g.get('saved_total_line') is not None for g in games)
        paired_dates = {g['date_et'] for g in games if g.get('saved_total_line') is not None}
        if len(paired_dates) >= candidate['protocol']['min_dates'] and pairs >= candidate['protocol']['min_games']:
            return games, universe
    return None


def _state(games, stamp):
    return dict(heads=data.validation_state(games, now=_physical_now(stamp)), active=store.active_event())


def _code_paths(candidate):
    return [store._safe_path(store.ROOT, name) for name in candidate['artifact']['code_sha256']] + [
        store._safe_path(store._research(), path) for path in candidate['code_archive'].values()]


def _verify_sources(candidate, reservation, labels, stamp):
    """Expensive byte/semantic replay outside SQLite and publication locks."""
    catalog = {}
    for descriptor in candidate['sources']:
        ident = descriptor.get('id', descriptor.get('source_artifact_id'))
        if ident is None:
            raise ValueError('NBA totals training source lacks immutable identity')
        catalog[ident] = descriptor.get('sha256')
    for game in reservation['games']:
        mapped = game.get('input_map')
        if mapped:
            identity = dict(game_id=game['game_id'], home_team=mapped['home'], away_team=mapped['away'],
                            tipoff_ts=game['tipoff'])
            data.verify_input_map(mapped, identity, game['forecast_at'])
            for descriptor in mapped.get('source_artifacts', []):
                old = catalog.get(descriptor['id'])
                if old and old != descriptor['sha256']:
                    raise ValueError('NBA totals conflicting source seal')
                catalog[descriptor['id']] = descriptor['sha256']
    for label in labels:
        if label.get('label_source_artifact_id') is not None:
            catalog[label['label_source_artifact_id']] = label['label_source_sha256']
    for ident, digest in catalog.items():
        source, _, _ = data._source(ident, stamp)
        if source['sha256'] != digest:
            raise ValueError('NBA totals proposal source seal changed')


def _publish(candidate, reservation, evaluated, stamp):
    outcome = evaluated['evaluation']
    _replay_predictions(candidate, reservation, stamp)
    frozen = {game['game_id']: game for game in reservation['games']}
    rows = outcome.get('evaluated_rows') or []
    if len(rows) != len(frozen) or {row['game_id'] for row in rows} != set(frozen):
        raise ValueError('NBA totals evaluation lost reserved forecast coverage')
    for row in rows:
        if any(row.get(key) != value for key, value in frozen[row['game_id']].items()):
            raise ValueError('NBA totals evaluation differs from frozen prediction')
    replay = evaluate(rows, training_cutoff=candidate['training_cutoff'], sealed_protocol=candidate['protocol'])
    if replay != outcome:
        raise ValueError('NBA totals evaluation metric replay mismatch')
    expected = evaluated['state']
    verified = store.candidate(candidate['version'], verify_code=True)
    paths = _code_paths(verified)
    files = publication.file_seals(paths)
    _verify_sources(verified, reservation, outcome.get('evaluated_rows') or [], stamp)
    publication.same(expected, _state(reservation['game_ids'], stamp))
    with publication.publish_lock():
        with db.tx() as con:
            con.execute('BEGIN IMMEDIATE')
            publication.same(expected, _state(reservation['game_ids'], stamp))
            publication.same(files, publication.file_seals(paths))
            current = store.candidate(candidate['version'])
            publication.same(candidate['sha256'], current['sha256'])
            kind = 'activated' if outcome['passed'] else 'rejected'
            proof_payload = dict(schema=1, candidate_sha256=candidate['sha256'],
                protocol_sha256=candidate['protocol_sha256'], reservation_id=reservation['reservation_id'],
                evaluation_id=evaluated['evaluation_id'], evaluation_sha256=evaluated['evaluation_sha256'],
                state=expected, code_sha256=current['artifact']['code_sha256'], sources_sha256=current['sources_sha256'])
            proof_id = store.sha(proof_payload)
            published_at = _physical_now(stamp)
            con.execute('INSERT INTO nba_totals_publication_proofs VALUES(?,?,?,?,?,?,?)',
                (proof_id, candidate['version'], reservation['reservation_id'], evaluated['evaluation_id'],
                 published_at, store.encoded(proof_payload), proof_id))
            con.execute('INSERT INTO nba_totals_model_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
                        (candidate['version'], published_at, kind, store.encoded(dict(proof_id=proof_id, evaluation_id=evaluated['evaluation_id']))))
            store._checkpoint(con, candidate['version'])
    return _record(stamp, kind, dict(version=candidate['version'], evaluation=outcome))


def resume_publication(version, now=None):
    """Resume this candidate/cohort without refitting or releasing consumption."""
    stamp = store.stamp(now)
    ensure_schema()
    try:
        old = db.q1("SELECT kind,detail FROM nba_totals_model_events WHERE version=? AND kind IN ('activated','rejected','abandoned') ORDER BY id DESC LIMIT 1", (version,))
        if old and old['kind'] == 'abandoned':
            store.abandon(version, json.loads(old['detail']).get('reason'), now=stamp)
            return _record(stamp, 'error', dict(version=version, candidate_status='abandoned',
                reason=json.loads(old['detail']).get('reason', 'sealed_candidate_abandoned')))
        candidate = store.candidate(version, verify_code=True)
        if old:
            return _record(stamp, old['kind'], dict(version=version, evaluation=(store.evaluation_for(version) or {}).get('evaluation')))
        reservation = store.reservation_for(version)
        if reservation is None:
            inputs = data.forecast_inputs(now=stamp, limit=10000, start_date=_validation_start(candidate))
            _freeze_future(candidate, inputs, stamp)
            cohort = _cohort(candidate, inputs, stamp)
            if cohort is None:
                return _record(stamp, 'waiting', dict(version=version, reason='fresh_frozen_whole_dates_required',
                    frozen_predictions=len(store.freezes(version)), required_pairs=60, required_dates=7))
            reservation = store.reserve(version, *cohort, now=stamp)
        evaluated = store.evaluation_for(version)
        if evaluated is None:
            _replay_predictions(candidate, reservation, stamp)
            expected = _state(reservation['game_ids'], stamp)
            try:
                labels = data.read_labels(reservation, now=stamp)
            except data.ResultPending as exc:
                return _record(stamp, 'waiting', dict(version=version, reservation_id=reservation['reservation_id'],
                    reason='reserved_dates_await_matching_grade', detail=str(exc)[:250]))
            if labels.get('universe_game_ids_by_date') != reservation['universe_game_ids_by_date']:
                raise ValueError('NBA totals reserved whole-date universe changed')
            if sorted(labels.get('complete_dates') or []) != reservation['date_ids']:
                return _record(stamp, 'waiting', dict(version=version, reservation_id=reservation['reservation_id'], reason='reserved_dates_await_complete_finals'))
            by_game = {r['game_id']: r for r in labels['rows']}
            if len(by_game) != len(labels['rows']) or set(by_game) != set(reservation['game_ids']):
                raise ValueError('NBA totals label coverage differs from reserved cohort')
            rows = [dict(g, **by_game[g['game_id']]) for g in reservation['games']]
            result = evaluate(rows, training_cutoff=candidate['training_cutoff'], sealed_protocol=candidate['protocol'])
            publication.same(expected, _state(reservation['game_ids'], stamp))
            evaluated = store.record_evaluation(reservation, result, expected, now=stamp)
        return _publish(candidate, reservation, evaluated, stamp)
    except publication.PublicationDeferred as exc:
        if str(exc) == 'publication_dependencies_changed':
            abandoned = store.abandon(version, str(exc), now=stamp)
            return _record(stamp, 'error', dict(version=version, candidate_status='abandoned' if abandoned else None, reason=str(exc)))
        return _record(stamp, 'deferred', dict(version=version, reason=str(exc)))
    except TimeoutError:
        raise  # A resource interruption resumes the identical sealed candidate.
    except Exception as exc:
        from .learning_worker import LearningDeadlineExceeded
        if isinstance(exc, LearningDeadlineExceeded):
            raise
        abandoned = store.abandon(version, str(exc), now=stamp)
        return _record(stamp, 'error', dict(version=version, candidate_status='abandoned' if abandoned else None,
            reason=str(exc)[:250], error_type=type(exc).__name__))


def _training_examples(raw, cutoff):
    result = []
    for row in raw:
        if 'context' in row and 'ts' in row:
            result.append(row)
            continue
        tip = row.get('ts', row.get('tipoff'))
        context = model.features(row['home'], row['away'], row['rows'], tip, as_of=row['as_of'],
            baseline_total=row['baseline_total'], reconstruction_cutoff=cutoff if row.get('reconstructed') else None)
        if context['status'] == 'ok':
            result.append(dict(row, ts=tip, context=context))
    return result


def run_fitting(now=None):
    stamp = store.stamp(now)
    ensure_schema()
    try:
        pending = store.pending()
        if pending:
            return resume_publication(pending['version'], now=stamp)
        latest = db.q1('SELECT created_at FROM nba_totals_candidates ORDER BY created_at DESC LIMIT 1')
        if latest and stamp < latest['created_at'] + WEEK:
            return _record(stamp, 'waiting', dict(reason='predeclared_weekly_proposal_cadence', next_due_at=latest['created_at'] + WEEK))
        cutoff = stamp - 1.
        fitting_code = code_catalog()
        fitting_files = publication.file_seals([store._safe_path(store.ROOT, name) for name in fitting_code])
        raw, sources = data.training_examples(cutoff)
        examples = _training_examples(raw, cutoff)
        try:
            artifact = model.fit_candidate(examples, cutoff)
        except ValueError as exc:
            if str(exc) not in ('NBA totals fitting needs multiple seasons',
                    'Insufficient NBA totals fit/development/calibration history',
                    'NBA totals phase date/label receipt chronology conflict'):
                raise
            return _record(stamp, 'waiting', dict(reason='chronological_fit_development_calibration_required',
                detail=str(exc), available_examples=len(examples)))
        if (fitting_code != code_catalog() or fitting_files != publication.file_seals(
                [store._safe_path(store.ROOT, name) for name in fitting_code])):
            raise ValueError('NBA totals code changed during fitting')
        artifact.update(code_sha256=fitting_code, training_sources_sha256=store.sha(sources),
                        evaluation_protocol_sha256=store.sha(protocol()))
        artifact['version'] = model._version(artifact)
        candidate = store.register_candidate(artifact, protocol(), sources, now=stamp)
        return _record(stamp, 'waiting', dict(version=candidate['version'], reason='fresh_frozen_whole_dates_required', required_pairs=60, required_dates=7))
    except Exception as exc:
        from .learning_worker import LearningDeadlineExceeded
        if isinstance(exc, (LearningDeadlineExceeded, TimeoutError)):
            raise
        return _record(stamp, 'error', dict(reason=str(exc)[:250], error_type=type(exc).__name__))


def run_diagnostics(now=None):
    stamp = store.stamp(now)
    ensure_schema()
    try:
        current = active()
        pending = store.pending()
        if pending:
            return resume_publication(pending['version'], now=stamp)
        return _record(stamp, 'retained', dict(version=current['version'] if current else None,
                                               reason='incumbent_retained_no_pending_candidate'))
    except Exception as exc:
        from .learning_worker import LearningDeadlineExceeded
        if isinstance(exc, (LearningDeadlineExceeded, TimeoutError)):
            raise
        return _record(stamp, 'error', dict(reason=str(exc)[:250], error_type=type(exc).__name__))


def status(now=None):
    stamp = store.stamp(now)
    ensure_schema()
    last = db.q1('SELECT * FROM nba_totals_runs ORDER BY id DESC LIMIT 1')
    interval = 300 if last and last['status'] in ('error', 'deferred') else DAY if last and last['status'] == 'waiting' else WEEK
    detail = json.loads(last['detail']) if last else {}
    try:
        current = active()
    except Exception as exc:
        current = None
        detail = dict(detail, integrity_error=str(exc))
    pending = store.pending()
    result = dict(enabled=True, component='NBA_TOTAL', status='error' if detail.get('integrity_error') else last['status'] if last else 'not_checked',
        active={k: current[k] for k in ('version', 'sha256', 'activated_at')} if current else None,
        pending_version=pending['version'] if pending else None,
        checked_at=last['started_at'] if last else None, last_check_at=last['started_at'] if last else None,
        next_due_at=last['started_at'] + interval if last else stamp, detail=detail,
        message=detail.get('integrity_error', detail.get('message', 'Totals learner has not checked yet.')),
        coverage=dict(frozen_predictions=len(store.freezes(pending['version'])) if pending else 0,
                      required_pairs=60, required_dates=7))
    from . import learning_worker
    result['runner'] = learning_worker.status('NBA_TOTAL', now=stamp)
    return result


def refresh_inputs(now=None):
    """Queue only; the independent worker owns all numerical work and replay."""
    stamp = store.stamp(now)
    from . import learning_worker
    ensure_schema()
    last = db.q1('SELECT * FROM nba_totals_runs ORDER BY id DESC LIMIT 1')
    interval = 300 if last and last['status'] in ('error', 'deferred') else DAY if last and last['status'] == 'waiting' else WEEK
    if last and stamp < last['started_at'] + interval:
        return dict(status='not_due', checked_at=last['started_at'], next_due_at=last['started_at'] + interval)
    diagnostics = learning_worker.enqueue('NBA_TOTAL', 'diagnostics', now=stamp)
    retuning = learning_worker.enqueue('NBA_TOTAL', 'retuning', now=stamp)
    return dict(status='queued', diagnostics_job_id=diagnostics['id'], retuning_job_id=retuning['id'])
