"""Durable staged totals progress does not refit or backfill predictions."""
import copy
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from edgeterminal import db, nba_totals_stages as stages
from edgeterminal import nba_totals_stage_store as store, nba_totals_fit_bank as bank
from edgeterminal.config import settings
from test_nba_totals_model import examples, history

NOW=datetime(2027,1,1,tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    if db._conn is not None:
        db._conn.close(); db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path / "stages.db"}')
    root=tmp_path/'repo'; (root/'edgeterminal').mkdir(parents=True)
    (root/'edgeterminal/component.py').write_text('sealed stage component\n')
    monkeypatch.setattr(store,'ROOT',root)
    monkeypatch.setattr(store,'CODE_NAMES',('edgeterminal/component.py',))
    monkeypatch.setattr(store,'RESEARCH_ROOT',tmp_path/'research')
    monkeypatch.setattr(stages,'_physical_now',lambda requested:float(requested))
    monkeypatch.setattr(store.time,'time',lambda:NOW)
    monkeypatch.setattr(stages,'FLOORS',dict(fit=6,development=3,calibration=3))
    store.ensure_schema(); stages.ensure_schema()
    yield
    db._conn.close(); db._conn=None


def test_event_ledger_is_append_only_and_compare_and_swap():
    first=stages.append_event('chain','fit',{'bank':'sealed'},now=NOW,expected=None)
    assert stages.current('chain')['sha256']==first['sha256']
    with pytest.raises(ValueError,match='changed'):
        stages.append_event('chain','development',{},now=NOW+1,expected=None)
    with pytest.raises(sqlite3.DatabaseError,match='append-only'):
        db.ex('UPDATE nba_totals_training_stages SET payload=?',('{}',))
    second=stages.append_event('chain','development',{},now=NOW+1,expected=first['sha256'])
    assert second['previous_sha256']==first['sha256']


def test_insufficient_history_waits_without_candidate_or_activation(monkeypatch):
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([],[]))
    result=stages.run_fitting(now=NOW)
    assert result['status']=='waiting'
    assert result['stage']=='fit'
    assert result['coverage']['fit']==0
    assert store.pending() is None


def test_fit_stage_is_persistent_and_does_not_reconstruct_daily(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:[])
    result=stages.run_fitting(now=NOW)
    assert result['stage']=='development'
    original=stages.current()
    assert original['payload']['bank']['fit_rows']>=6
    monkeypatch.setattr(stages.data,'training_examples',lambda *args:pytest.fail('waiting stage reparsed fit archive'))
    again=stages.run_fitting(now=NOW+86400)
    assert again['stage']=='development'
    assert stages.current()['chain_id']==original['chain_id']
    assert stages.current()['payload']['bank']['sha256']==original['payload']['bank']['sha256']


def test_legacy_protocol_and_bytes_are_not_reinterpreted():
    from edgeterminal import nba_totals_learning as legacy
    assert legacy.protocol()['excluded_through']=='2026-11-04'
    assert stages.outer.protocol()['excluded_through']=='2026-11-08'
    assert stages.outer.protocol()['family']!=legacy.protocol()['family']
    assert stages.needs_inputs() is False


def test_capture_does_not_backfill_a_finished_game(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:[])
    stages.run_fitting(now=NOW)
    assert stages.capture_forecast({'game_id':'finished'}, {'tipoff':NOW-1}, now=NOW) is None


def test_saved_snapshot_inner_adapter_rejects_late_features(monkeypatch):
    original=examples()[6]
    supplied=dict(game_id=original['game_id'],forecast_sha256='a'*64,
                  baseline_total=original['baseline_total'],forecast_at=original['as_of'],
                  tipoff=original['ts'],input_map=None)
    assert stages.inner_example(supplied,{'actual_total':220,'result_at':NOW}) is None


def test_stage_code_change_is_visible_and_never_refits_silently(monkeypatch):
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in examples() if r['label_received_at']<=cutoff],[]))
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:[])
    stages.run_fitting(now=NOW)
    (store.ROOT/'edgeterminal/component.py').write_text('changed')
    result=stages.run_fitting(now=NOW+1)
    assert result['status']=='error'
    assert stages.current()['kind']=='invalidated'
    assert store.active_event() is None


def inner_records(source):
    result=[]
    for row in source:
        mapped=dict(schema='nba-totals-input-1',game_id=row['game_id'],home='BOS',away='NYK',
                    as_of=row['as_of'],tipoff=row['ts'],rows=history(row['ts']))
        mapped['sha256']=store.sha(mapped)
        result.append(dict(game_id=row['game_id'],date_et=datetime.fromtimestamp(row['ts'],store.ET).date().isoformat(),
            forecast_sha256=store.sha(['forecast',row['game_id']]),tipoff=row['ts'],forecast_at=row['as_of'],
            baseline_total=row['baseline_total'],input_map=mapped,projection={'total':row['baseline_total']},
            market={},source_ids=[]))
    return result


def install_inner(monkeypatch,records,source):
    monkeypatch.setattr(stages.data,'verify_input_map',lambda mapped,game,asof:mapped)
    monkeypatch.setattr(stages.data,'validation_state',lambda *a,**k:{'fixture':'state'})
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:records)
    by_game={row['game_id']:row for row in source}
    def labels(reservation,now=None):
        stages.validate_inner_reservation(reservation)
        return dict(rows=[dict(game_id=gid,actual_total=by_game[gid]['actual_total'],
                         result_at=by_game[gid]['label_received_at'],label_sha256=store.sha(['label',gid]))
                         for gid in reservation['game_ids']],complete_dates=reservation['date_ids'],
                    universe_game_ids_by_date=reservation['universe_game_ids_by_date'])
    monkeypatch.setattr(stages.data,'inner_labels',labels)


def test_complete_calibration_is_sealed_before_registration_and_recovery(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    records=inner_records(source[6:])
    install_inner(monkeypatch,records,source)
    assert stages.run_fitting(now=NOW)['stage']=='calibration'
    register=store.register_candidate
    monkeypatch.setattr(store,'register_candidate',lambda *args,**kwargs:(_ for _ in ()).throw(TimeoutError('interrupted')))
    with pytest.raises(TimeoutError):
        stages.run_fitting(now=NOW+1)
    ready=stages.current()
    assert ready['kind']=='calibration'
    assert ready['payload']['stage']=='ready'
    assert len(ready['payload']['calibration_examples'])==3
    monkeypatch.setattr(store,'register_candidate',register)
    monkeypatch.setattr(stages.data,'inner_labels',lambda *a,**k:pytest.fail('completed phase reopened'))
    result=stages.run_fitting(now=NOW+2)
    assert result['stage']=='validation'
    candidate=store.pending()
    assert candidate['sources']['stage_chain_sha256']==ready['sha256']
    assert candidate['artifact']['params']['method']=='intercept'


def test_stage_floors_and_progress_are_truthful_in_status(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    install_inner(monkeypatch,inner_records(source[6:8]),source)
    stages.run_fitting(now=NOW)
    status=stages.status(now=NOW+1)
    assert status['detail']['stage']=='development'
    assert status['coverage']['development']==2
    assert status['detail']['required']['development']==3
    assert status['active'] is None


def test_completed_chain_replays_before_outer_publication_and_invalidation_blocks(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    install_inner(monkeypatch,inner_records(source[6:]),source)
    stages.run_fitting(now=NOW)
    assert stages.run_fitting(now=NOW+1)['stage']=='validation'
    candidate=store.pending()
    stages.outer._verify_sources(candidate,{'games':[]},[],NOW+2)
    event=stages.current()
    stages.append_event(event['chain_id'],'invalidated',dict(event['payload'],stage='invalidated'),now=NOW+3,expected=event['sha256'])
    with pytest.raises(ValueError,match='invalidated'):
        stages.outer._verify_sources(candidate,{'games':[]},[],NOW+4)


def test_diagnostics_never_fits_and_both_jobs_are_queued(monkeypatch):
    monkeypatch.setattr(stages.data,'training_examples',lambda *args:pytest.fail('diagnostics trained'))
    assert stages.run_diagnostics(now=NOW)['status']=='retained'
    calls=[]
    from edgeterminal import learning_worker
    monkeypatch.setattr(learning_worker,'enqueue',lambda component,kind,now: calls.append((component,kind)) or {'id':len(calls)})
    result=stages.refresh_inputs(now=NOW+86400)
    assert result['status']=='queued'
    assert calls==[('NBA_TOTAL','diagnostics'),('NBA_TOTAL','retuning')]


def test_genuine_pending_result_does_not_invalidate_a_sealed_bank(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    install_inner(monkeypatch,inner_records(source[6:]),source)
    monkeypatch.setattr(stages.data,'inner_labels',lambda *a,**k:(_ for _ in ()).throw(stages.data.ResultPending('matching grade pending')))
    result=stages.run_fitting(now=NOW)
    assert result['reason']=='inner_dates_await_matching_grade'
    assert stages.current()['kind']=='fit'


def test_changed_code_during_fit_cannot_be_resealed(monkeypatch):
    source=examples(); calls=[]
    def raw(cutoff):
        calls.append(cutoff)
        if len(calls)==2:
            (store.ROOT/'edgeterminal/component.py').write_text('changed during fit')
        return [r for r in source if r['label_received_at']<=cutoff],[]
    monkeypatch.setattr(stages.data,'training_examples',raw)
    result=stages.run_fitting(now=NOW)
    assert result['status']=='error'
    assert 'code changed during fitting' in result['reason']
    assert stages.current() is None and store.pending() is None


def test_prospective_inner_capture_is_pretip_idempotent_and_source_bound(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:[])
    stages.run_fitting(now=NOW)
    tip=NOW+2*86400; asof=tip-1800
    mapped=dict(schema='nba-totals-input-1',game_id='future',home='BOS',away='NYK',as_of=asof,tipoff=tip,rows=history(tip))
    mapped['sha256']=store.sha(mapped)
    saved=dict(game_id='future',payload_sha256=store.sha('future'),payload=dict(projection={'total':230.,'sd_total':16.5},market={}))
    captured=stages.capture_forecast(saved,mapped,now=asof)
    assert len(captured['predictions'])==20
    assert stages.capture_forecast(saved,mapped,now=asof+1)==captured
    record=stages.outer._capture_record(saved,mapped)
    assert stages._inner_freeze(stages.current(),record)
    with pytest.raises(ValueError,match='seal'):
        stages._inner_freeze(stages.current(),dict(record,input_map={}))
    assert stages.capture_forecast(saved,mapped,now=tip) is None
    bad=copy.deepcopy(mapped);bad['rows'][0]['points']+=1
    with pytest.raises(ValueError,match='input-map seal'):
        stages.capture_forecast(saved,bad,now=asof)


def test_crash_after_registration_resumes_same_candidate_and_finishes_checkpoint(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    install_inner(monkeypatch,inner_records(source[6:]),source)
    stages.run_fitting(now=NOW)
    append=stages.append_event
    def interrupted(chain,kind,*args,**kwargs):
        if kind=='candidate':raise TimeoutError('killed after candidate insertion')
        return append(chain,kind,*args,**kwargs)
    monkeypatch.setattr(stages,'append_event',interrupted)
    with pytest.raises(TimeoutError):stages.run_fitting(now=NOW+1)
    candidate=store.pending();assert candidate is not None
    monkeypatch.setattr(stages,'append_event',append)
    result=stages.run_fitting(now=NOW+2)
    assert result['status']=='waiting'
    assert stages.current()['kind']=='candidate'
    assert store.pending()['sha256']==candidate['sha256']


def test_diagnostic_integrity_failure_is_visible_without_training(monkeypatch):
    monkeypatch.setattr(stages,'serving_state',lambda:(_ for _ in ()).throw(ValueError('broken active seal')))
    assert stages.run_diagnostics(now=NOW)['status']=='error'
    assert stages.status(now=NOW+1)['status']=='error'


def test_adaptive_incumbent_inner_capture_and_outer_inference_share_raw_basis(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:[])
    stages.run_fitting(now=NOW)
    tip=NOW+2*86400;asof=tip-1800
    mapped=dict(schema='nba-totals-input-1',game_id='adaptive',home='BOS',away='NYK',
                as_of=asof,tipoff=tip,rows=history(tip))
    mapped['sha256']=store.sha(mapped)
    saved=dict(game_id='adaptive',payload_sha256=store.sha('adaptive'),payload=dict(
        projection=dict(total=236.,total_base_total=230.,total_base_sd_total=16.5,sd_total=16.5),market=dict(total=234.5)))
    captured=stages.capture_forecast(saved,mapped,now=asof)
    assert captured['baseline_total']==236.
    assert captured['model_base_total']==230.
    assert captured['predictions'][1]==236.
    record=stages.outer._capture_record(saved,mapped)
    example=stages.inner_example(dict(record,input_map=mapped),dict(actual_total=236.,result_at=tip+3600))
    assert example['baseline_total']==236. and example['context']['baseline_total']==230.
    assert stages._inner_freeze(stages.current(),record)
    from test_nba_totals_model import fit
    artifact=fit()
    assert artifact['intercept']==6.
    outer=stages.outer._infer_prediction(artifact,record,mapped)
    assert outer['candidate_total']==captured['predictions'][1]


def test_raw_identity_winning_against_an_adaptive_incumbent_is_not_false_no_change(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    records=inner_records(source[6:])
    for record in records:
        record.update(baseline_total=236.,model_base_total=230.)
        record['projection'].update(total=236.,total_base_total=230.)
    labels=copy.deepcopy(source)
    for row in labels[6:]:row['actual_total']=230.
    install_inner(monkeypatch,records,labels)
    result=stages.run_fitting(now=NOW)
    assert result['stage']=='calibration'
    assert stages.current()['payload']['selected']['member']['params']['method']=='incumbent'
    assert stages.current()['payload']['selected']['identical_served_incumbent'] is False


def test_early_unsupported_inner_date_does_not_hide_later_eligible_dates(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    records=inner_records(source[6:10])
    records[0]['input_map']['rows']=[]
    records[0]['input_map']['sha256']=store.sha({k:v for k,v in records[0]['input_map'].items() if k!='sha256'})
    install_inner(monkeypatch,records,source)
    result=stages.run_fitting(now=NOW)
    assert result['stage']=='calibration'
    selected=stages.current()['payload']['selected']
    assert selected['development']['game_ids']==[r['game_id'] for r in records[1:]]


def test_incomplete_early_date_does_not_permanently_cap_inner_phase(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    records=inner_records(source[6:11]);install_inner(monkeypatch,records,source)
    reader=stages.data.inner_labels
    def missing(reservation,now=None):
        labels=reader(reservation,now=now)
        labels['complete_dates']=[day for day in labels['complete_dates'] if day!=records[0]['date_et']]
        labels['rows']=[row for row in labels['rows'] if row['game_id']!=records[0]['game_id']]
        return labels
    monkeypatch.setattr(stages.data,'inner_labels',missing)
    result=stages.run_fitting(now=NOW)
    assert result['stage']=='calibration'
    assert stages.current()['payload']['selected']['development']['game_ids']==[r['game_id'] for r in records[1:4]]


@pytest.mark.parametrize('fault',[sqlite3.OperationalError('database locked'),OSError('temporarily unavailable')])
def test_transient_inner_failure_retains_sealed_bank_for_retry(monkeypatch,fault):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:[])
    stages.run_fitting(now=NOW);event=stages.current()
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:(_ for _ in ()).throw(fault))
    result=stages.run_fitting(now=NOW+1)
    assert result['status']=='error'
    assert result['stage']=='development'
    assert stages.current()['sha256']==event['sha256']


def test_lost_stage_cas_is_reported_without_stale_invalidation(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    install_inner(monkeypatch,inner_records(source[6:]),source)
    append=stages.append_event;calls=[]
    def conflict(chain,kind,*args,**kwargs):
        calls.append(kind)
        if kind=='development':raise ValueError('NBA totals stage ownership changed')
        return append(chain,kind,*args,**kwargs)
    monkeypatch.setattr(stages,'append_event',conflict)
    result=stages.run_fitting(now=NOW)
    assert result['status']=='deferred'
    assert calls==['fit','development']
    assert stages.current()['kind']=='fit'


def test_calibration_event_does_not_move_the_pregame_capture_enrollment(monkeypatch):
    source=examples()
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    install_inner(monkeypatch,inner_records(source[6:]),source)
    assert stages.run_fitting(now=NOW)['stage']=='calibration'
    first=stages.current();payload=dict(first['payload']);payload['enrolled_at']=NOW-3600
    moved=stages.append_event(first['chain_id'],'development',payload,now=NOW+600,expected=first['sha256'])
    tip=NOW+7200;asof=NOW+300
    mapped=dict(schema='nba-totals-input-1',game_id='gap',home='BOS',away='NYK',as_of=asof,tipoff=tip,rows=history(tip))
    mapped['sha256']=store.sha(mapped)
    saved=dict(game_id='gap',payload_sha256=store.sha('gap'),payload=dict(projection=dict(total=230.),market={}))
    captured=stages.capture_forecast(saved,mapped,now=NOW+900)
    assert captured is not None and stages._inner_freeze(moved,stages.outer._capture_record(saved,mapped))


def test_future_requested_fitting_clock_cannot_advance_physical_cutoff(monkeypatch):
    seen=[]
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:seen.append(cutoff) or ([],[]))
    stages.run_fitting(now=NOW+20*86400)
    assert seen==[NOW-1]


def test_fresh_read_only_status_and_current_do_not_create_ledgers(tmp_path,monkeypatch):
    db._conn.close();db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path/"read-only.db"}')
    con=db.conn();before={row['name'] for row in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
    con.execute('PRAGMA query_only=ON');sql=[];con.set_trace_callback(sql.append)
    try:
        assert stages.current() is None
        assert stages.needs_inputs() is False
        assert stages.serving_state() is None
        assert stages.status(now=NOW)['status']=='not_checked'
        assert stages.outer.needs_inputs() is False
        assert stages.outer.serving_state() is None
    finally:con.set_trace_callback(None)
    after={row['name'] for row in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert after==before
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in sql)


def test_existing_query_only_stage_readers_do_not_run_schema_ddl(monkeypatch):
    stages.legacy_store.ensure_schema()
    monkeypatch.setattr(stages,'_ready',None)
    monkeypatch.setattr(store,'_ready',None)
    monkeypatch.setattr(stages.legacy_store,'_ready',None)
    con=db.conn();con.execute('PRAGMA query_only=ON');sql=[];con.set_trace_callback(sql.append)
    try:
        assert stages.current() is None and stages.needs_inputs() is False
        assert stages.status(now=NOW)['status']=='not_checked'
        assert stages.outer.serving_state() is None
    finally:con.set_trace_callback(None)
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in sql)


def test_true_incumbent_retention_consumes_no_outer_and_next_week_advances_fit(monkeypatch):
    source=examples()
    for row in source:row['actual_total']=row['baseline_total']
    monkeypatch.setattr(stages.data,'training_examples',lambda cutoff:([r for r in source if r['label_received_at']<=cutoff],[]))
    install_inner(monkeypatch,inner_records(source[6:]),source)
    result=stages.run_fitting(now=NOW)
    assert result['status']=='retained' and result['stage']=='retained'
    first=stages.current()
    assert first['payload']['selected']['identical_served_incumbent']
    assert store.pending() is None
    assert db.q1('SELECT COUNT(*) n FROM nba_totals_stage_reservations')['n']==0
    assert stages.run_fitting(now=NOW+86400)['next_due_at']==NOW+stages.WEEK
    monkeypatch.setattr(db,'now',lambda:NOW+stages.WEEK+1)
    result=stages.run_fitting(now=NOW+stages.WEEK+1)
    second=stages.current()
    assert result['stage']=='development'
    assert second['chain_id']!=first['chain_id']
    assert second['payload']['bank']['last_day']>max(first['payload']['selected']['development']['dates'])


def test_initial_enrollment_uses_physical_bank_seal_after_slow_fit(monkeypatch):
    source=examples();clock=[NOW];calls=[]
    monkeypatch.setattr(db,'now',lambda:clock[0])
    monkeypatch.setattr(stages,'_physical_now',lambda requested:max(float(requested),clock[0]))
    def raw(cutoff):
        calls.append(cutoff)
        if len(calls)==2:clock[0]+=600
        return [row for row in source if row['label_received_at']<=cutoff],[]
    monkeypatch.setattr(stages.data,'training_examples',raw)
    monkeypatch.setattr(stages.data,'forecast_inputs',lambda **kwargs:[])
    stages.run_fitting(now=NOW)
    event=stages.current()
    assert event['payload']['enrolled_at']==NOW+600
    assert event['created_at']>=event['payload']['enrolled_at']


def test_legacy_active_read_only_facade_keeps_all_v1_publication_seals(monkeypatch):
    event=dict(version='old',created_at=NOW,id=1,detail=json.dumps({'proof_id':'proof'}))
    artifact=dict(version='old')
    candidate=dict(version='old',sha256='c'*64,artifact=artifact,protocol_sha256='p'*64)
    evaluation=dict(evaluation={'passed':True},evaluation_id='evaluation',reservation_id='reservation')
    payload=dict(candidate_sha256=candidate['sha256'],protocol_sha256=candidate['protocol_sha256'],
                 evaluation_id='evaluation',reservation_id='reservation')
    proof=dict(payload=store.encoded(payload),sha256=store.sha(payload),proof_id='proof')
    monkeypatch.setattr(stages.legacy_store,'active_event',lambda:event)
    calls=[]
    monkeypatch.setattr(stages.legacy_store,'candidate',lambda version,verify_code: calls.append(verify_code) or candidate)
    monkeypatch.setattr(stages.legacy_store,'evaluation_for',lambda version:evaluation)
    monkeypatch.setattr(db,'q1',lambda *a,**k:proof)
    monkeypatch.setattr(stages.legacy,'ensure_schema',lambda:pytest.fail('legacy read created schema'))
    assert stages._legacy_serving_state()['artifact']==artifact
    assert calls==[True]
    proof['sha256']='0'*64
    with pytest.raises(ValueError,match='seal mismatch'):stages._legacy_serving_state()
