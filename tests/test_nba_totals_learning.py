"""No prospective labels before durable reservation, and no unchecked promotion."""
import copy
from datetime import datetime
import sqlite3

import pytest

from edgeterminal import db, nba_totals_store as store, nba_totals_learning as learning
from test_nba_totals_store import isolated, NOW, freeze, reserve


def registered(version='candidate-a'):
    return store.register_candidate(dict(version=version, training_cutoff=NOW - 86400,
        code_sha256=store.code_catalog(), sd_total=20., training_sources_sha256=store.sha([]),
        evaluation_protocol_sha256=store.sha(learning.protocol())), learning.protocol(), [], now=NOW)


def metrics_rows(count=63):
    rows = []
    for i in range(count):
        tip = NOW + (i // 9 + 1) * 86400
        rows.append(dict(game_id=f'g{i}', date_et=datetime.fromtimestamp(tip, store.ET).date().isoformat(),
            forecast_at=tip - 3600, tipoff=tip, result_at=tip + 14400, actual_total=220.,
            baseline_total=230., candidate_total=220.3, saved_total_line=224.5,
            baseline_probability=.7, candidate_probability=.1,
            forecast_sha256=store.sha(['f', i]), label_sha256=store.sha(['l', i])))
    return rows


def test_predeclared_gate_requires_point_brier_line_and_whole_day_support():
    result = learning.evaluate(metrics_rows(), training_cutoff=NOW - 86400)
    assert result['passed'] and result['pairs'] == 63 and result['days'] == 7
    assert result['paired_day_intervals']['total_mae']['high'] < 0
    assert result['paired_day_intervals']['over_brier']['high'] < 0
    assert not learning.evaluate(metrics_rows(59), training_cutoff=NOW - 86400)['passed']
    worse = metrics_rows()
    for row in worse:
        row.update(candidate_total=229.9, candidate_probability=.69)
    assert 'total_improvement_too_small' in learning.evaluate(worse, training_cutoff=NOW - 86400)['reasons']
    benchmark = metrics_rows()
    for row in benchmark:
        row['saved_total_line'] = 220.1
    assert 'saved_line_point_benchmark_not_met' in learning.evaluate(benchmark, training_cutoff=NOW - 86400)['reasons']


def test_pushes_are_point_errors_but_excluded_from_brier_and_decided_wins():
    rows = metrics_rows()
    rows[0]['saved_total_line'] = rows[0]['actual_total']
    result = learning.evaluate(rows, training_cutoff=NOW - 86400)
    assert result['pushes'] == 1 and result['binary_pairs'] == 62
    assert result['candidate_wins'] == 62 and result['baseline_wins'] == 0
    assert result['candidate_mae'] == pytest.approx(.3)


@pytest.mark.parametrize('change', [
    {'candidate_probability': 1.1}, {'actual_total': float('nan')},
    {'forecast_at': NOW - 90000}, {'result_at': NOW}, {'saved_total_line': None},
])
def test_evaluation_rejects_invalid_or_unpaired_evidence(change):
    rows = metrics_rows()
    rows[0].update(change)
    with pytest.raises(ValueError):
        learning.evaluate(rows, training_cutoff=NOW - 86400)


def test_duplicate_games_and_audited_dates_never_become_validation():
    rows = metrics_rows()
    rows[-1] = rows[0]
    with pytest.raises(ValueError, match='repeat'):
        learning.evaluate(rows, training_cutoff=NOW - 86400)
    rows = metrics_rows()
    rows[0].update(tipoff=NOW - 2 * 86400, forecast_at=NOW - 2 * 86400 - 3600, result_at=NOW - 86400)
    with pytest.raises(ValueError, match='inspected|prospective'):
        learning.evaluate(rows, training_cutoff=NOW - 4 * 86400)


def test_fitting_seals_one_candidate_without_ever_reading_outer_labels(monkeypatch):
    monkeypatch.setattr(learning.data, 'training_examples', lambda cutoff: ([{'context': {}, 'ts': NOW - 172800, 'actual_total': 220.}], []))
    monkeypatch.setattr(learning.model, 'fit_candidate', lambda examples, cutoff: dict(version='fresh', training_cutoff=cutoff, sd_total=20.))
    monkeypatch.setattr(learning.model, '_version', lambda artifact: 'fresh')
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: pytest.fail('fit read prospective labels'))
    result = learning.run_fitting(now=NOW)
    assert result['status'] == 'waiting'
    assert store.pending()['version'] == result['version']
    assert store.reservation_for(result['version']) is None
    assert learning.needs_inputs()
    monkeypatch.setattr(learning.model, 'fit_candidate', lambda *a, **k: pytest.fail('refit pending candidate'))
    monkeypatch.setattr(learning.data, 'forecast_inputs', lambda *a, **k: [])
    assert learning.run_fitting(now=NOW + 100)['status'] == 'waiting'


def test_label_reader_only_runs_after_reservation_and_full_date_waits(monkeypatch):
    cand = registered()
    game = freeze(cand)
    record = {key: value for key, value in game.items() if key not in ('freeze_id',)}
    monkeypatch.setattr(learning.data, 'forecast_inputs', lambda *a, **k: [record])
    monkeypatch.setattr(learning.data, 'validation_state', lambda *a, **k: {'heads': 'same'})
    calls = []
    monkeypatch.setattr(learning, '_replay_predictions', lambda *a, **k: None)
    def labels(reservation, now=None):
        calls.append(store.verify_reservation(reservation)['reservation_id'])
        assert not db.conn().in_transaction
        return dict(rows=[], complete_dates=[], universe_game_ids_by_date={game['date_et']: [game['game_id']]})
    monkeypatch.setattr(learning.data, 'read_labels', labels)
    result = learning.resume_publication(cand['version'], now=NOW + 7200)
    # An inadequate cohort stays untouched; it must accumulate before consumption.
    assert result['status'] == 'waiting' and not calls
    cohort = reserve(cand, [game])
    result = learning.resume_publication(cand['version'], now=NOW + 7200)
    assert result['status'] == 'waiting' and calls == [cohort['reservation_id']]
    assert store.evaluation_for(cand['version']) is None and learning.active() is None


def test_missing_or_revised_selected_forecast_blocks_cohort_without_labels(monkeypatch):
    cand = registered()
    game = freeze(cand)
    changed = dict(game, forecast_sha256=store.sha('new selected'))
    monkeypatch.setattr(learning.data, 'forecast_inputs', lambda *a, **k: [changed])
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: pytest.fail('unfrozen labels read'))
    assert learning.resume_publication(cand['version'], now=NOW + 7200)['status'] == 'waiting'
    assert store.reservation_for(cand['version']) is None


def full_reservation():
    cand = registered()
    games = []
    labels = []
    for row in metrics_rows():
        supplied = dict(row)
        supplied.pop('actual_total')
        supplied.pop('result_at')
        supplied.pop('label_sha256')
        supplied['input_sha256'] = store.sha(['input', row['game_id']])
        games.append(store.freeze_forecast(cand['version'], supplied, now=row['forecast_at']))
        labels.append({k: row[k] for k in ('game_id', 'actual_total', 'result_at', 'label_sha256')})
    reservation = reserve(cand, games, now=NOW + 9 * 86400)
    return cand, reservation, labels


def ready_data(monkeypatch, reservation, labels):
    monkeypatch.setattr(learning, '_replay_predictions', lambda *a, **k: None)
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: dict(rows=labels,
        complete_dates=reservation['date_ids'], universe_game_ids_by_date=reservation['universe_game_ids_by_date']))
    monkeypatch.setattr(learning.data, 'validation_state', lambda *a, **k: {'heads': 'same'})
    monkeypatch.setattr(learning, '_verify_sources', lambda *a, **k: None)
    monkeypatch.setattr(learning.publication, 'near_kickoff', lambda: False)


def test_passed_cohort_atomic_proof_activation_and_idempotent_resume(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    result = learning.resume_publication(cand['version'], now=NOW + 10 * 86400)
    assert result['status'] == 'activated'
    assert learning.active()['version'] == cand['version']
    assert db.q1('SELECT count(*) n FROM nba_totals_publication_proofs')['n'] == 1
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: pytest.fail('reused labels'))
    assert learning.resume_publication(cand['version'], now=NOW + 11 * 86400)['status'] == 'activated'
    assert db.q1("SELECT count(*) n FROM nba_totals_model_events WHERE kind='activated'")['n'] == 1


def test_changed_result_head_during_heavy_verification_defers_without_writer_lock(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    state = {'heads': 'same'}
    monkeypatch.setattr(learning.data, 'validation_state', lambda *a, **k: dict(state))
    def verify(*args, **kwargs):
        assert not db.conn().in_transaction
        with sqlite3.connect(str(learning.settings.db_path), timeout=.1) as other:
            other.execute('INSERT INTO kv(k,v,updated_at) VALUES(?,?,?)', ('parallel', '{}', NOW))
        state['heads'] = 'corrected'
    monkeypatch.setattr(learning, '_verify_sources', verify)
    result = learning.resume_publication(cand['version'], now=NOW + 10 * 86400)
    assert result['status'] == 'error' and result['candidate_status'] == 'abandoned'
    assert store.pending() is None
    assert learning.active() is None
    assert len(store.verify_reservation(reservation)['game_ids']) == 63
    assert store.evaluation_for(cand['version']) is not None


def test_rejected_candidate_retains_incumbent_and_consumption(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    for label in labels:
        label['actual_total'] = 230.
    result = learning.resume_publication(cand['version'], now=NOW + 10 * 86400)
    assert result['status'] == 'rejected' and learning.active() is None
    assert len(store.verify_reservation(reservation)['date_ids']) == 7


def test_capture_seals_prediction_with_exact_physical_pretip_clock(monkeypatch):
    cand = registered()
    monkeypatch.setattr(learning.model, 'features', lambda *a, **k: {})
    monkeypatch.setattr(learning.model, 'predict', lambda *a, **k: dict(total=225., sd_total=20., applied=True, branch='season'))
    monkeypatch.setattr(learning.model, 'total_probabilities', lambda *a, **k: dict(conditional_over=.6))
    input_map = dict(game_id='g1', as_of=NOW + 60, tipoff=NOW + 3600, baseline_total=230., context={}, home='BOS', away='NYK', rows=[],
                     sha256=store.sha('map'), market=dict(total_line=220.5))
    saved = dict(game_id='g1', sha256=store.sha('saved'), created_at=NOW + 60,
                 projection=dict(total=230., sd_total=20., total_over_probability=.7), market=dict(total_line=220.5))
    assert learning.capture_forecast(saved, input_map, now=NOW + 60)['candidate_total'] == 225.
    assert learning.capture_forecast(saved, input_map, now=NOW + 4000) is None
    assert len(store.freezes(cand['version'])) == 1


def test_refresh_is_queue_only_and_status_truthfully_reports_errors(monkeypatch):
    calls = []
    from edgeterminal import learning_worker
    monkeypatch.setattr(learning_worker, 'enqueue', lambda *a, **k: calls.append(a) or {'id': len(calls)})
    monkeypatch.setattr(learning, 'run_fitting', lambda *a, **k: pytest.fail('capture fitted'))
    learning.refresh_inputs(now=NOW)
    assert ('NBA_TOTAL', 'diagnostics') in calls and ('NBA_TOTAL', 'retuning') in calls
    store.record_run(NOW, 'error', {'reason': 'visible integrity failure'})
    result = learning.status(now=NOW + 60)
    assert result['status'] == 'error' and result['detail']['reason'] == 'visible integrity failure'
    assert result['next_due_at'] > NOW


def test_empty_evidence_and_missing_lines_preserve_point_coverage():
    assert not learning.evaluate([], training_cutoff=NOW)['passed']
    rows = metrics_rows()
    rows[0].update(saved_total_line=None, baseline_probability=None, candidate_probability=None)
    result = learning.evaluate(rows, training_cutoff=NOW - 86400)
    assert result['passed'] and result['rows'] == 63 and result['pairs'] == 62
    assert result['baseline_coverage'] == result['candidate_coverage'] == 63
    with pytest.raises(ValueError, match='protocol'):
        learning.evaluate(rows, training_cutoff=NOW - 86400, sealed_protocol=dict(learning.protocol(), min_games=1))


def test_training_adapter_preserves_real_receipts_and_reconstruction(monkeypatch):
    from test_nba_totals_model import examples
    source = examples()[0]
    supplied = dict(source, home='BOS', away='NYK', tipoff=source['ts'], rows=[], reconstructed=True)
    supplied.pop('context')
    supplied.pop('ts')
    calls = []
    def features(home, away, rows, tipoff, **kwargs):
        calls.append(kwargs)
        return source['context']
    monkeypatch.setattr(learning.model, 'features', features)
    built = learning._training_examples([supplied], NOW)
    assert built[0]['label_received_at'] == source['label_received_at']
    assert built[0]['ts'] == supplied['tipoff']
    assert calls[0]['reconstruction_cutoff'] == NOW
    monkeypatch.setattr(learning.model, 'features', lambda *a, **k: dict(status='insufficient'))
    assert learning._training_examples([supplied], NOW) == []


def test_run_fitting_uses_real_synthetic_model_and_envelope_seals(monkeypatch):
    from test_nba_totals_model import examples
    rows = examples()
    original = learning.model.fit_candidate
    monkeypatch.setattr(learning.data, 'training_examples', lambda cutoff: (rows, []))
    monkeypatch.setattr(learning.model, 'fit_candidate', lambda examples, cutoff:
        original(examples, cutoff, min_fit=6, min_development=3, min_calibration=3))
    result = learning.run_fitting(now=NOW)
    assert result['status'] == 'waiting'
    candidate = store.candidate(result['version'])
    learning.model.validate_artifact(candidate['artifact'])
    assert candidate['artifact']['training_sources_sha256'] == store.sha([])
    assert candidate['artifact']['evaluation_protocol_sha256'] == store.sha(learning.protocol())
    assert set(candidate['artifact']['phase_dates']) == {'fit', 'development', 'calibration'}


def test_offline_capture_future_uses_verified_map_and_never_results(monkeypatch):
    candidate = registered()
    mapped = dict(game_id='g1', as_of=NOW + 60, tipoff=NOW + 3600,
                  baseline_total=230., context={}, home='BOS', away='NYK', rows=[], sha256=store.sha('map'))
    record = dict(game_id='g1', forecast_sha256=store.sha('f'), forecast_at=NOW + 60,
        tipoff=NOW + 3600, projection=dict(total=230.), market=dict(total=220.5), input_map=mapped)
    monkeypatch.setattr(learning.model, 'features', lambda *a, **k: {})
    monkeypatch.setattr(learning.model, 'predict', lambda *a, **k: dict(total=225., sd_total=20., branch='season'))
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: pytest.fail('future label read'))
    learning._freeze_future(candidate, [record], NOW + 60)
    assert len(store.freezes(candidate['version'])) == 1
    assert learning._cohort(candidate, [dict(record, date_et=store.freezes(candidate['version'])[0]['date_et'])], NOW + 60) is None


def test_adequate_cohort_is_reserved_before_first_label_read(monkeypatch):
    candidate, reservation, labels = full_reservation()
    records = [dict(game) for game in reservation['games']]
    # Exercise selection independently of the already-consumed reservation.
    original_q = db.q
    monkeypatch.setattr(learning.db, 'q', lambda *a, **k: [] if 'reserved_dates' in a[0] else original_q(*a, **k))
    cohort = learning._cohort(candidate, records, NOW + 9 * 86400)
    assert cohort is not None and len(cohort[0]) == 63 and len(cohort[1]) == 7


def test_source_replay_rejects_conflicting_or_changed_training_bytes(monkeypatch):
    candidate = registered()
    candidate = dict(candidate, sources=[dict(id=1, sha256='a' * 64)])
    monkeypatch.setattr(learning.data, '_source', lambda *a: (dict(sha256='b' * 64), {}, None))
    with pytest.raises(ValueError, match='source seal changed'):
        learning._verify_sources(candidate, dict(games=[]), [], NOW)
    candidate['sources'] = [dict(sha256='a' * 64)]
    with pytest.raises(ValueError, match='identity'):
        learning._verify_sources(candidate, dict(games=[]), [], NOW)
    candidate['sources'] = []
    mapped = dict(home='BOS', away='NYK', source_artifacts=[dict(id=1, sha256='a' * 64)])
    monkeypatch.setattr(learning.data, 'verify_input_map', lambda *a, **k: None)
    monkeypatch.setattr(learning.data, '_source', lambda *a: (dict(sha256='a' * 64), {}, None))
    learning._verify_sources(candidate, dict(games=[dict(game_id='g1', tipoff=NOW+3600,
        forecast_at=NOW, input_map=mapped)]), [dict(label_source_artifact_id=1, label_source_sha256='a' * 64)], NOW)


def test_diagnostics_and_status_expose_integrity_failures(monkeypatch):
    assert learning.run_diagnostics(now=NOW)['status'] == 'retained'
    assert not learning.needs_inputs()
    registered()
    monkeypatch.setattr(learning.data, 'forecast_inputs', lambda *a, **k: [])
    assert learning.run_diagnostics(now=NOW + 60)['status'] == 'waiting'
    monkeypatch.setattr(learning, 'active', lambda: (_ for _ in ()).throw(ValueError('bad active seal')))
    assert learning.run_diagnostics(now=NOW + 120)['status'] == 'error'
    assert learning.status(now=NOW + 180)['status'] == 'error'
    assert learning.refresh_inputs(now=NOW + 180)['status'] == 'not_due'


def test_code_change_and_capture_priority_preserve_reserved_cohort(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    monkeypatch.setattr(learning.publication, 'near_kickoff', lambda: True)
    result = learning.resume_publication(cand['version'], now=NOW + 10 * 86400)
    assert result['status'] == 'deferred' and result['reason'] == 'near_tipoff_capture_priority'
    assert learning.active() is None and store.evaluation_for(cand['version']) is not None
    (store.ROOT / 'edgeterminal/component.py').write_text('changed code')
    result = learning.resume_publication(cand['version'], now=NOW + 10 * 86400 + 1)
    assert result['status'] == 'error' and 'code' in result['reason']
    assert store.verify_reservation(reservation)['reservation_id'] == reservation['reservation_id']


def test_seven_dates_must_each_contribute_saved_line_pairs():
    rows = metrics_rows(630)
    for row in rows[60:]:
        row.update(saved_total_line=None, baseline_probability=None, candidate_probability=None)
    # First sixty pairs span only seven dates in this generator: keep one date instead.
    for row in rows[:60]:
        row['tipoff'] = NOW + DAY_FOR_TEST
        row['forecast_at'] = row['tipoff'] - 3600
        row['result_at'] = row['tipoff'] + 14400
        row['date_et'] = datetime.fromtimestamp(row['tipoff'], store.ET).date().isoformat()
    assert not learning.evaluate(rows, training_cutoff=NOW - 86400)['passed']


DAY_FOR_TEST = 86400


def test_capture_rechecks_physical_clock_after_prediction(monkeypatch):
    registered()
    monkeypatch.setattr(learning.model, 'features', lambda *a, **k: {})
    monkeypatch.setattr(learning.model, 'predict', lambda *a, **k: dict(total=225., sd_total=20., branch='season'))
    clock = iter([NOW + 60, NOW + 4000])
    monkeypatch.setattr(learning, '_physical_now', lambda requested: max(requested, next(clock)), raising=False)
    mapped = dict(game_id='g1', as_of=NOW + 60, tipoff=NOW + 3600,
                  baseline_total=230., context={}, home='BOS', away='NYK', rows=[], sha256=store.sha('map'))
    saved = dict(game_id='g1', payload_sha256=store.sha('physical-clock'),
        payload=dict(projection=dict(total=230.), market=dict(total=220.5)))
    assert learning.capture_forecast(saved, mapped, now=NOW + 60) is None
    assert store.freezes('candidate-a') == []


def test_sealed_evaluation_cannot_swap_frozen_candidate_predictions(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    rows = [dict(game, **label) for game, label in zip(reservation['games'], sorted(labels, key=lambda r: r['game_id']))]
    for row in rows:
        row['candidate_total'] = row['actual_total']
    forged = learning.evaluate(rows, training_cutoff=cand['training_cutoff'])
    expected = learning._state(reservation['game_ids'], NOW + 10 * 86400)
    store.record_evaluation(reservation, forged, expected, now=NOW + 10 * 86400)
    result = learning.resume_publication(cand['version'], now=NOW + 10 * 86400)
    assert result['status'] == 'error'
    assert learning.active() is None


def test_publication_replays_actual_sealed_model_inference_not_arbitrary_freeze(monkeypatch):
    from test_nba_totals_model import fit, input_map, projection
    artifact = fit()
    artifact.update(code_sha256=store.code_catalog(), training_sources_sha256=store.sha([]),
                    evaluation_protocol_sha256=store.sha(learning.protocol()))
    artifact = learning.model.seal_version(artifact)
    sealed = store.register_candidate(artifact, learning.protocol(), [], now=NOW)
    mapped = input_map()
    mapped['tipoff'] = NOW + 86400
    mapped['as_of'] = NOW + 86400 - 1800
    mapped['sha256'] = store.sha({k: v for k, v in mapped.items() if k != 'sha256'})
    record = dict(game_id='target', forecast_sha256=store.sha('real-saved'), forecast_at=mapped['as_of'],
        tipoff=mapped['tipoff'], date_et=datetime.fromtimestamp(mapped['tipoff'], store.ET).date().isoformat(),
        baseline_total=230., saved_total_line=224.5, projection=projection(), input_map=mapped,
        baseline_probability=learning.model.total_probabilities(230., 16.5, 224.5)['conditional_over'])
    monkeypatch.setattr(learning.data, 'forecast_inputs', lambda *a, **k: [record])
    false = dict(record, candidate_total=10., candidate_probability=.01, input_sha256=mapped['sha256'])
    frozen = store.freeze_forecast(sealed['version'], false, now=mapped['as_of'])
    reservation = store.reserve(sealed['version'], [frozen], {record['date_et']: ['target']},
                                now=NOW + 2 * 86400)
    with pytest.raises(ValueError, match='inference|prediction|rounded'):
        learning._replay_predictions(sealed, reservation, NOW + 2 * 86400)


def test_valid_real_inference_can_pass_full_sealed_pipeline(monkeypatch):
    from test_nba_totals_model import fit, input_map, projection
    artifact = fit()
    artifact.update(code_sha256=store.code_catalog(), training_sources_sha256=store.sha([]),
                    evaluation_protocol_sha256=store.sha(learning.protocol()))
    artifact = learning.model.seal_version(artifact)
    cand = store.register_candidate(artifact, learning.protocol(), [], now=NOW)
    records, labels, dates = [], [], {}
    for i in range(63):
        tip = NOW + (i // 9 + 1) * 86400
        mapped = input_map()
        mapped.update(game_id=f'real-{i}', tipoff=tip, as_of=tip-1800)
        mapped['sha256'] = store.sha({k:v for k,v in mapped.items() if k != 'sha256'})
        saved = dict(game_id=f'real-{i}', payload_sha256=store.sha(['real', i]),
            payload=dict(projection=projection(), market=dict(total=234.5)))
        frozen = learning.capture_forecast(saved, mapped, now=tip-1800)
        assert frozen['candidate_total'] == 236.
        records.append(dict(frozen, projection=projection()))
        labels.append(dict(game_id=f'real-{i}', actual_total=236., result_at=tip+14400,
                           label_sha256=store.sha(['real-label', i])))
        dates.setdefault(frozen['date_et'], []).append(frozen['game_id'])
    dates = {day: sorted(ids) for day,ids in dates.items()}
    monkeypatch.setattr(learning.data, 'forecast_inputs', lambda *a, **k: records)
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: dict(rows=labels,
        complete_dates=sorted(dates), universe_game_ids_by_date=dates))
    monkeypatch.setattr(learning.data, 'validation_state', lambda *a, **k: {'heads': 'verified'})
    monkeypatch.setattr(learning, '_verify_sources', lambda *a, **k: None)
    monkeypatch.setattr(learning.publication, 'near_kickoff', lambda: False)
    result = learning.resume_publication(cand['version'], now=NOW+9*86400)
    assert result['status'] == 'activated' and result['evaluation']['passed']
    assert learning.active()['artifact']['intercept'] == 6.
    assert len(store.reservation_for(cand['version'])['game_ids']) == 63


def test_chronology_shortage_is_waiting_not_integrity_error(monkeypatch):
    monkeypatch.setattr(learning.data, 'training_examples', lambda *a: ([], []))
    result = learning.run_fitting(now=NOW)
    assert result['status'] == 'waiting'
    assert result['reason'] == 'chronological_fit_development_calibration_required'


def test_publication_timestamp_is_after_slow_verification(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    entered = NOW + 10*86400
    clock = [entered]
    monkeypatch.setattr(learning, '_physical_now', lambda requested: max(requested, clock[0]))
    def verify(*a, **k):
        clock[0] += 600
    monkeypatch.setattr(learning, '_verify_sources', verify)
    assert learning.resume_publication(cand['version'], now=entered)['status'] == 'activated'
    event = store.active_event()
    proof = db.q1('SELECT * FROM nba_totals_publication_proofs WHERE version=?', (cand['version'],))
    assert event['created_at'] == proof['created_at'] == entered + 600


def test_code_change_during_fitting_cannot_be_resealed_as_new_code(monkeypatch):
    monkeypatch.setattr(learning.data, 'training_examples', lambda cutoff:
        ([dict(context={}, ts=NOW-86400, actual_total=220.)], []))
    def fit(examples, cutoff):
        (store.ROOT / 'edgeterminal/component.py').write_text('changed during fitting')
        return dict(version='wrong-fit-code', training_cutoff=cutoff, sd_total=20.)
    monkeypatch.setattr(learning.model, 'fit_candidate', fit)
    monkeypatch.setattr(learning.model, '_version', lambda artifact: artifact['version'])
    result = learning.run_fitting(now=NOW)
    assert result['status'] == 'error' and 'code' in result['reason']
    assert db.q1('SELECT count(*) n FROM nba_totals_candidates')['n'] == 0


def test_pending_candidate_replay_uses_raw_model_base_after_an_active_total(monkeypatch):
    from test_nba_totals_model import fit, input_map, projection
    artifact = fit()
    artifact.update(code_sha256=store.code_catalog(), training_sources_sha256=store.sha([]),
                    evaluation_protocol_sha256=store.sha(learning.protocol()))
    artifact = learning.model.seal_version(artifact)
    cand = store.register_candidate(artifact, learning.protocol(), [], now=NOW)
    mapped = input_map()
    mapped.update(game_id='after-active', tipoff=NOW+86400, as_of=NOW+86400-1800)
    mapped['sha256'] = store.sha({k:v for k,v in mapped.items() if k != 'sha256'})
    served = projection()
    served.update(total=236., sd_total=3., total_base_total=230., total_base_sd_total=16.5)
    saved = dict(game_id='after-active', payload_sha256=store.sha('after-active'),
                 payload=dict(projection=served, market=dict(total=234.5)))
    frozen = learning.capture_forecast(saved, mapped, now=mapped['as_of'])
    deployed = learning.model.compose(projection(), mapped, artifact=cand['artifact'])
    assert frozen['baseline_total'] == 236.
    assert frozen['model_base_total'] == 230.
    assert frozen['candidate_total'] == deployed['total'] == 236.
    record = dict(frozen, projection=served)
    monkeypatch.setattr(learning.data, 'forecast_inputs', lambda *a, **k: [record])
    reservation = store.reserve(cand['version'], [frozen], {frozen['date_et']: [frozen['game_id']]},
                                now=NOW+2*86400)
    learning._replay_predictions(cand, reservation, NOW+2*86400)


def test_permanently_invalid_cohort_is_terminal_and_stays_consumed(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    monkeypatch.setattr(learning, '_replay_predictions', lambda *a, **k:
        (_ for _ in ()).throw(ValueError('sealed inference mismatch')))
    result = learning.resume_publication(cand['version'], now=NOW+10*86400)
    assert result['status'] == 'error' and result['candidate_status'] == 'abandoned'
    assert store.pending() is None
    assert store.verify_reservation(reservation)['game_ids'] == reservation['game_ids']
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: pytest.fail('abandoned labels reused'))
    assert learning.resume_publication(cand['version'], now=NOW+11*86400)['candidate_status'] == 'abandoned'


def test_forecast_discovery_starts_after_candidate_seal_date(monkeypatch):
    cand = registered()
    calls = []
    def inputs(now=None, limit=1000, *, start_date=None):
        calls.append(start_date)
        return []
    monkeypatch.setattr(learning.data, 'forecast_inputs', inputs)
    assert learning.resume_publication(cand['version'], now=NOW+86400)['status'] == 'waiting'
    assert calls == ['2026-11-07']
    assert cand['protocol']['validation_start'] == 'first_whole_ET_date_after_candidate_seal'


def test_reservation_replay_filters_before_touching_older_input_rows(monkeypatch):
    cand, reservation, labels = full_reservation()
    reservation = dict(reservation, games=[dict(reservation['games'][0], input_map={'sha256': 'verified'})])
    def inputs(now=None, limit=1000, *, start_date=None):
        assert start_date == min(reservation['date_ids'])
        raise RuntimeError('scope checked before old-row replay')
    monkeypatch.setattr(learning.data, 'forecast_inputs', inputs)
    with pytest.raises(RuntimeError, match='scope checked'):
        learning._replay_predictions(cand, reservation, NOW+10*86400)


def test_actual_worker_deadline_preserves_candidate_and_consumed_cohort(monkeypatch):
    from edgeterminal import learning_worker
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    def interrupted(*a, **k):
        raise learning_worker.LearningDeadlineExceeded('synthetic offline deadline')
    monkeypatch.setattr(learning.data, 'read_labels', interrupted)
    with pytest.raises(learning_worker.LearningDeadlineExceeded):
        learning.resume_publication(cand['version'], now=NOW+10*86400)
    assert store.pending()['version'] == cand['version']
    assert store.verify_reservation(reservation)['game_ids'] == reservation['game_ids']
    assert db.q1("SELECT 1 FROM nba_totals_model_events WHERE kind='abandoned'") is None


def test_diagnostics_preserves_actual_worker_deadline(monkeypatch):
    from edgeterminal import learning_worker
    registered()
    def interrupted(*a, **k):
        raise learning_worker.LearningDeadlineExceeded('synthetic offline deadline')
    monkeypatch.setattr(learning, 'resume_publication', interrupted)
    with pytest.raises(learning_worker.LearningDeadlineExceeded):
        learning.run_diagnostics(now=NOW+86400)


def test_verified_final_awaiting_matching_grade_waits_and_resumes_same_candidate(monkeypatch):
    import json
    from edgeterminal import learning_worker
    from test_nba_totals_store import _own_job
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    job = _own_job(cand['version'], other_detail={'preserve': 'same-job-detail'})
    attempts = []
    def read(reserved, now=None):
        assert store.verify_reservation(reserved)['reservation_id'] == reservation['reservation_id']
        attempts.append(now)
        if len(attempts) == 1:
            raise learning.data.ResultPending('verified final correction awaits its matching grade')
        return dict(rows=labels, complete_dates=reservation['date_ids'],
                    universe_game_ids_by_date=reservation['universe_game_ids_by_date'])
    monkeypatch.setattr(learning.data, 'read_labels', read)
    monkeypatch.setattr(learning.model, 'fit_candidate', lambda *a, **k: pytest.fail('reserved candidate refitted'))
    monkeypatch.setattr(learning.data, 'training_examples', lambda *a, **k: pytest.fail('reserved candidate retrained'))
    token = learning_worker._CURRENT_JOB.set(job)
    try:
        waiting = learning.resume_publication(cand['version'], now=NOW+10*86400)
        assert waiting['status'] == 'waiting'
        assert waiting['reason'] == 'reserved_dates_await_matching_grade'
        assert waiting['reservation_id'] == reservation['reservation_id']
        assert store.pending()['version'] == cand['version']
        assert store.evaluation_for(cand['version']) is None
        assert store.verify_reservation(reservation)['date_ids'] == reservation['date_ids']
        detail = json.loads(db.q1('SELECT detail FROM learning_jobs WHERE id=?', (job['id'],))['detail'])
        assert detail == {'candidate_id': cand['version'], 'preserve': 'same-job-detail'}
        assert db.q1("SELECT 1 FROM nba_totals_model_events WHERE kind='abandoned'") is None
        result = learning.resume_publication(cand['version'], now=NOW+10*86400+300)
    finally:
        learning_worker._CURRENT_JOB.reset(token)
    assert result['status'] == 'activated' and result['version'] == cand['version']
    assert learning.active()['version'] == cand['version']
    assert len(attempts) == 2
    assert db.q1('SELECT COUNT(*) n FROM nba_totals_candidates')['n'] == 1
    assert store.reservation_for(cand['version'])['reservation_id'] == reservation['reservation_id']


def test_result_pending_after_immutable_evaluation_still_abandons_without_reevaluation(monkeypatch):
    cand, reservation, labels = full_reservation()
    ready_data(monkeypatch, reservation, labels)
    by_game = {row['game_id']: row for row in labels}
    rows = [dict(game, **by_game[game['game_id']]) for game in reservation['games']]
    outcome = learning.evaluate(rows, training_cutoff=cand['training_cutoff'], sealed_protocol=cand['protocol'])
    evaluated = store.record_evaluation(reservation, outcome,
        learning._state(reservation['game_ids'], NOW+10*86400), now=NOW+10*86400)
    def pending(*a, **k):
        raise learning.data.ResultPending('post-evaluation dependency no longer matches')
    monkeypatch.setattr(learning, '_verify_sources', pending)
    monkeypatch.setattr(learning.data, 'read_labels', lambda *a, **k: pytest.fail('immutable evaluation reread labels'))
    result = learning.resume_publication(cand['version'], now=NOW+10*86400+300)
    assert result['status'] == 'error' and result['candidate_status'] == 'abandoned'
    assert store.pending() is None
    assert store.evaluation_for(cand['version'])['evaluation_id'] == evaluated['evaluation_id']
    assert store.verify_reservation(reservation)['game_ids'] == reservation['game_ids']
