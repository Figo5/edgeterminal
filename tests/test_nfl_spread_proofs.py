"""Publication and recovery use genuine sealed store records, with owned fixtures."""
from copy import deepcopy
from datetime import datetime,timezone
import json

import pytest

from edgeterminal import db
from test_nfl_spread_component import isolated, row, NOW


@pytest.fixture
def environment(isolated,monkeypatch):
    from edgeterminal import nfl_spread_store as store,nfl_spread_learning as learning
    root=isolated/'code'
    (root/'edgeterminal').mkdir(parents=True)
    (root/'edgeterminal/component.py').write_text('reviewed immutable code\n')
    monkeypatch.setattr(store,'ROOT',root)
    monkeypatch.setattr(store,'CODE_NAMES',('edgeterminal/component.py',))
    monkeypatch.setattr(learning.time,'time',lambda:NOW)
    # These tests isolate immutable outer proofs; finite inner derivation is
    # independently reproduced by test_missing_calibration_retains_same_bank.
    monkeypatch.setattr(learning,'_verify_training_stage',lambda *args:True)
    monkeypatch.setattr(learning,'_source_paths',lambda *args:[])
    return store,learning


def proposal(environment,outer_season=2027,outer_first_week=1):
    store,learning=environment
    state=dict(stage='outer',fit_rows=[],development_rows=[],calibration_rows=[],
               code_sha256=store.code_catalog(),protocol_sha256=learning.model.sha(learning.model.protocol()))
    stage=store.append_stage(None,state,now=NOW-1)
    member=dict(method='affine',feature_names=['baseline_margin'],alpha=100.,means=[0.],scales=[1.],coefficients=[1.],intercept=-5.)
    artifact=dict(kind=learning.model.KIND,member=member,sd_margin=13.2,training_cutoff=NOW-100,
        formula_version=learning.model.FORMULA_VERSION,input_policy_id='nfl-game-current-inputs-v1',
        incumbent_version=learning.model.FORMULA_VERSION,protocol=learning.model.protocol(),
        code_sha256=store.code_catalog(),stage_id=stage['id'],stage_sha256=stage['sha256'],
        incumbent_event_id=None,outer_season=outer_season,outer_first_week=outer_first_week)
    candidate=store.register(artifact,now=NOW)
    store.append_stage(stage['id'],dict(state,candidate_version=candidate['version']),now=NOW)
    return candidate


def forecasts(environment,candidate,count=60):
    store,learning=environment
    observations={};finals={};freezes=[]
    for index in range(count):
        week=index//10+1;i=index%10
        item=row(i,week=week,start=NOW+86400)
        actual=(i%3)*7
        item.update(baseline_margin=actual+6.,incumbent_margin=actual+6.,
            incumbent_probability=learning.model.probability(actual+6.,13.2,item['home_spread']),
            incumbent_version=learning.model.FORMULA_VERSION)
        final={k:item.pop(k) for k in ('actual_margin','label_received_at','label_sha256')}
        final['actual_margin']=float(actual)
        observations[item['snapshot_id']]=item;finals[item['game_id']]=final
        db.ex('INSERT INTO nfl_game_snapshots VALUES(?,?,?,?,?,?)',
              (item['snapshot_id'],item['game_id'],'{}','et-model-0.4.0','fixture',item['as_of']))
        db.ex('INSERT INTO nfl_game_predictions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              (item['snapshot_id'],item['game_id'],item['snapshot_id'],'et-model-0.4.0','fixture',20.,20.,0.,40.,.5,.1,item['as_of']))
        db.ex('INSERT INTO nfl_spread_observations VALUES(?,?,?,?,?,?,?)',
              (item['snapshot_id'],item['game_id'],item['season'],item['week'],item['as_of'],store.encoded(item),store.sha(item)))
        mean=learning.model.predict(candidate['artifact']['member'],item)
        frozen=store.freeze(candidate['version'],item,dict(margin=mean,home_cover_probability=learning.model.probability(mean,13.2,item['home_spread'])),now=item['as_of']+1)
        freezes.append(frozen)
    return observations,finals,freezes


def setup_replay(monkeypatch,learning,observations,finals):
    monkeypatch.setattr(learning.data,'observation',lambda sid,**kw:deepcopy(observations[sid]))
    monkeypatch.setattr(learning.data,'labels',lambda rows,**kw:{r['game_id']:deepcopy(finals[r['game_id']]) for r in rows})
    monkeypatch.setattr(learning.data,'complete_week',lambda *a:True)
    monkeypatch.setattr(learning.data,'prepare_schedule_proofs',lambda *a,**kw:True)
    monkeypatch.setattr(learning.data,'week_universe',lambda season,week:[dict(id=r['game_id']) for r in observations.values() if r['season']==season and r['week']==week])


def test_candidate_freeze_is_physically_pregame_exact_and_immutable(environment):
    store,learning=environment;c=proposal(environment)
    observations,_,freezes=forecasts(environment,c,count=1)
    item=next(iter(observations.values()));f=freezes[0]
    assert store.verify_freeze(f)['payload']['snapshot_id']==item['snapshot_id']
    wrong=deepcopy(f);wrong['payload']['candidate_margin']=999.
    with pytest.raises(ValueError,match='freeze'):
        store.verify_freeze(wrong)
    with pytest.raises(ValueError,match='chronology'):
        store.freeze(c['version'],dict(item,snapshot_id='late'),dict(margin=0.,home_cover_probability=.5),now=item['kickoff'])
    with pytest.raises(ValueError,match='prediction'):
        store.freeze(c['version'],dict(item,snapshot_id='wrong'),dict(margin=100.,home_cover_probability=.5),now=item['as_of']+1)
    assert db.q1('SELECT count(*) n FROM nfl_spread_freezes')['n']==1


def test_pending_outer_waits_for_six_complete_weeks_without_reading_labels(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c,count=50)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.data,'labels',lambda *a,**kw:pytest.fail('unreserved outer labels read'))
    result=learning.resume_publication(c['version'],now=NOW+90*86400)
    assert result['status']=='waiting'
    assert result['detail']['coverage']==dict(outer_games=50,outer_weeks=5)
    assert db.q1('SELECT count(*) n FROM nfl_spread_reservations')['n']==0


def test_real_proof_can_activate_once_and_resume_without_reconsuming(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    called=[]
    original=learning.data.labels
    def labels(rows,**kwargs):
        assert db.q1('SELECT count(*) n FROM nfl_spread_reservations')['n']==1
        called.append('reserved-before-labels')
        return original(rows,**kwargs)
    monkeypatch.setattr(learning.data,'labels',labels)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    result=learning.resume_publication(c['version'],now=NOW+90*86400)
    assert result['status']=='activated',result
    assert called==['reserved-before-labels']
    assert learning.active()['version']==c['version']
    assert learning.resume_publication(c['version'],now=NOW+90*86400+1)['status']=='activated'
    assert db.q1('SELECT count(*) n FROM nfl_spread_reservations')['n']==1
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_weeks')['n']==6
    assert db.q1('SELECT count(*) n FROM nfl_spread_events WHERE kind="activated"')['n']==1


def test_publication_busy_preserves_same_consumed_candidate_and_resumes(environment,monkeypatch):
    from contextlib import contextmanager
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    @contextmanager
    def busy():
        raise learning.publication.PublicationDeferred('refresh_busy')
        yield
    original=learning.publication.publish_lock
    monkeypatch.setattr(learning.publication,'publish_lock',busy)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='deferred'
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_games')['n']==60
    monkeypatch.setattr(learning.publication,'publish_lock',original)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400+1)['status']=='activated'


@pytest.mark.parametrize('field', ['row_version', 'payload_version', 'candidate_sha256'])
def test_active_reader_binds_reservation_to_exact_candidate(environment, monkeypatch, field):
    store, learning = environment
    candidate = proposal(environment)
    observations, finals, _ = forecasts(environment, candidate)
    setup_replay(monkeypatch, learning, observations, finals)
    monkeypatch.setattr(learning.publication, 'near_kickoff', lambda: False)
    assert learning.resume_publication(candidate['version'], now=NOW+90*86400)['status'] == 'activated'
    original = store.reservation
    def crossed(ident):
        reserved = deepcopy(original(ident))
        if field == 'row_version':
            reserved['version'] = 'nfl-spread-' + '0' * 32
        elif field == 'payload_version':
            reserved['payload']['version'] = 'nfl-spread-' + '0' * 32
        else:
            reserved['payload']['candidate_sha256'] = '0' * 64
        return reserved
    monkeypatch.setattr(store, 'reservation', crossed)
    with pytest.raises(ValueError, match='publication seal'):
        learning.active()


def test_post_reservation_source_failure_is_terminal_without_recycling(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.data,'observation',lambda *a,**kw:(_ for _ in ()).throw(ValueError('tampered source')))
    result=learning.resume_publication(c['version'],now=NOW+90*86400)
    assert result['status']=='error'
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_games')['n']==60
    assert learning.resume_publication(c['version'],now=NOW+90*86400+1)['status']=='error'
    assert learning.active() is None


def test_tampered_code_and_stage_are_detected_before_any_outer_reservation(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    (store.ROOT/'edgeterminal/component.py').write_text('changed code\n')
    with pytest.raises(ValueError,match='code'):
        store.candidate(c['version'],current_code=True)
    assert store.candidate(c['version'])['sha256']==c['sha256']  # old archived bytes remain verifiable
    archive=next(iter(c['code_archive'].values()));path=store.safe_path(store._root(),archive)
    path.write_bytes(b'tampered archive')
    with pytest.raises(ValueError,match='hash'):
        store.candidate(c['version'])


def test_result_correction_after_deferred_evaluation_does_not_reuse_holdout(environment,monkeypatch):
    from contextlib import contextmanager
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    @contextmanager
    def busy():
        raise learning.publication.PublicationDeferred('refresh_busy')
        yield
    monkeypatch.setattr(learning.publication,'publish_lock',busy)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='deferred'
    first=next(iter(finals));finals[first]['label_sha256']='d'*64
    assert learning.resume_publication(c['version'],now=NOW+90*86400+1)['status']=='error'
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_games')['n']==60
    assert learning.active() is None


def test_accepted_component_does_not_mutate_the_raw_game_projection(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='activated'
    item=next(iter(observations.values()))
    monkeypatch.setattr(learning,'_feature_row',lambda *args:dict(item))
    raw=dict(margin=7.,total=45.,home_win_prob=.6,proj_home=26.,proj_away=19.)
    before=deepcopy(raw)
    component=learning.project_component(raw,{},[],{},NOW+100)
    assert raw==before
    assert component['used'] and component['version']==c['version']
    assert component['quote_sha256']==item.get('quote_sha256')


def test_run_repairs_active_integrity_without_rolling_back_for_a_loss(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='activated'
    assert not learning._integrity(NOW+90*86400+1)
    (store.ROOT/'edgeterminal/component.py').write_text('unexpected edit\n')
    assert learning._integrity(NOW+90*86400+2)
    assert learning.active() is None


def test_genuine_capture_enrolls_and_freezes_exact_snapshot_once(environment,monkeypatch):
    from test_nfl_spread_evidence import saved
    store,learning=environment;c=proposal(environment,outer_season=2026,outer_first_week=10)
    monkeypatch.setattr(learning.time,'time',lambda:NOW+60)
    sid,payload=saved(monkeypatch,when=NOW+60)
    first=learning.capture(sid,now=NOW+60)
    assert first['status']=='captured' and first['frozen']
    assert learning.capture(sid,now=NOW+61)['frozen']
    assert db.q1('SELECT count(*) n FROM nfl_spread_observations')['n']==1
    assert db.q1('SELECT count(*) n FROM nfl_spread_freezes')['n']==1


def test_inference_uses_accepted_component_without_touching_other_markets(environment,monkeypatch):
    from test_nfl_spread_evidence import saved
    store,learning=environment
    sid,payload=saved(monkeypatch,when=NOW+60)
    source=dict(payload['game_input_map'],source_id='e'*64)
    c=proposal(environment,outer_season=2026,outer_first_week=10)
    monkeypatch.setattr(learning,'active',lambda:dict(version=c['version'],sha256=c['sha256'],artifact=c['artifact']))
    raw=deepcopy(payload['projection']);before=deepcopy(raw)
    component=learning.project_component(raw,payload['game'],payload['market'],source,NOW+60)
    assert component['used'] and raw==before
    assert component['input_policy_id']==source['policy_id']
    assert learning.project_component(raw,payload['game'],payload['market'],dict(status='legacy_fallback'),NOW+60) is None
    with pytest.raises(ValueError,match='identity'):
        learning.project_component(raw,payload['game'],payload['market'],{},NOW+60)
    assert learning.project_component(raw,payload['game'],[],source,NOW+60) is None


def test_active_after_official_result_head_change_uses_fallback_and_checks_offline(environment,monkeypatch):
    import hashlib
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='activated'
    frozen_before=db.q('SELECT freeze_id,sha256 FROM nfl_spread_freezes ORDER BY freeze_id')
    original_evaluation=db.q1('SELECT payload FROM nfl_spread_evaluations')['payload']
    raw=b'new official receipt metadata fixture';digest=hashlib.sha256(raw).hexdigest()
    db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',(digest,
        learning.data.SCOREBOARD+'?dates=2027&seasontype=2&week=1',NOW+90*86400+1,raw))
    # These outer-proof tests substitute a verified official reader in setup_replay;
    # production validates raw bytes/status/identity in test_nfl_spread_evidence.
    for final in finals.values():
        final['actual_margin']+=30;final['label_sha256']='e'*64
    assert not learning.active()['evidence_current']
    assert learning.project_component({}, {}, [], {}, NOW+90*86400+2) is None
    assert learning.refresh_checks(now=NOW+90*86400+2)['status']=='queued'
    assert learning._integrity(NOW+90*86400+2)
    assert learning.active() is None
    assert db.q1('SELECT passed FROM nfl_spread_integrity')['passed']==0
    assert db.q('SELECT freeze_id,sha256 FROM nfl_spread_freezes ORDER BY freeze_id')==frozen_before
    assert db.q1('SELECT payload FROM nfl_spread_evaluations')['payload']==original_evaluation
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_games')['n']==60


def test_identical_result_poll_revalidates_fixed_artifact_without_new_activation(environment,monkeypatch):
    import hashlib
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='activated'
    raw=b'identical game outcome new source body';digest=hashlib.sha256(raw).hexdigest()
    db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',(digest,
        learning.data.SCOREBOARD+'?dates=2027&seasontype=2&week=1',NOW+90*86400+1,raw))
    assert not learning.active()['evidence_current']
    assert not learning._integrity(NOW+90*86400+2)
    assert learning.active()['evidence_current']
    assert db.q1('SELECT count(*) n FROM nfl_spread_events WHERE kind="activated"')['n']==1
    assert db.q1('SELECT count(*) n FROM nfl_spread_reservations')['n']==1


def test_withdrawn_final_cannot_reapprove_active_candidate_or_release_holdout(environment,monkeypatch):
    import hashlib
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='activated'
    original=db.q1('SELECT payload FROM nfl_spread_evaluations')['payload']
    raw=b'official final retraction metadata fixture';digest=hashlib.sha256(raw).hexdigest()
    db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',(digest,
        learning.data.SCOREBOARD+'?dates=2027&seasontype=2&week=1',NOW+90*86400+1,raw))
    missing=next(iter(finals));finals.pop(missing)
    # The source reader's authenticated withdrawal is permanently tested in
    # test_final_nonfinal_final_receipts_reset_label_availability. Its missing
    # label must not be treated as the old margin or as an invented zero here.
    def no_withdrawn_labels(rows,**kwargs):
        return {r['game_id']:deepcopy(finals[r['game_id']]) for r in rows if r['game_id'] in finals}
    monkeypatch.setattr(learning.data,'labels',no_withdrawn_labels)
    assert not learning.active()['evidence_current']
    evaluate=learning.evaluate
    monkeypatch.setattr(learning,'evaluate',lambda *args:pytest.fail('withdrawn label was evaluated'))
    assert learning._integrity(NOW+90*86400+2)=='pending'
    assert not learning.active()['evidence_current']
    assert learning.project_component({}, {}, [], {}, NOW+90*86400+2) is None
    assert learning._integrity(NOW+90*86400+3)=='pending'
    assert db.q1('SELECT count(*) n FROM nfl_spread_integrity')['n']==1
    assert db.q1('SELECT payload FROM nfl_spread_evaluations')['payload']==original
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_games')['n']==60

    # A later authenticated final restores the same fixed artifact after an
    # offline correction check; no new fit, activation or reservation is made.
    finals[missing]=dict(actual_margin=0.,label_received_at=NOW+90*86400+5,label_sha256='f'*64)
    raw=b'new authenticated final after withdrawal';digest=hashlib.sha256(raw).hexdigest()
    db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',(digest,
        learning.data.SCOREBOARD+'?dates=2027&seasontype=2&week=1',NOW+90*86400+5,raw))
    monkeypatch.setattr(learning,'evaluate',evaluate)
    assert not learning._integrity(NOW+90*86400+6)
    assert learning.active()['evidence_current']
    assert db.q1('SELECT count(*) n FROM nfl_spread_events WHERE kind="activated"')['n']==1
    assert db.q1('SELECT count(*) n FROM nfl_spread_reservations')['n']==1


def test_existing_active_and_status_are_also_select_only_on_query_only_database(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.publication,'near_kickoff',lambda:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='activated'
    sql=[];db.conn().set_trace_callback(sql.append);db.conn().execute('PRAGMA query_only=ON')
    try:
        assert learning.active()['version']==c['version']
        assert learning.status(now=NOW+90*86400+1)['active']['version']==c['version']
        assert not any(s.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE','ALTER')) for s in sql)
    finally:
        db.conn().execute('PRAGMA query_only=OFF');db.conn().set_trace_callback(None)


def test_missing_full_schedule_never_reads_outer_labels_or_releases_reserved_holdout(environment,monkeypatch):
    from contextlib import contextmanager
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    monkeypatch.setattr(learning.data,'labels',lambda *a,**kw:pytest.fail('unproved whole-week labels read'))
    monkeypatch.setattr(learning.data,'complete_week',lambda *a:False)
    monkeypatch.setattr(learning.data,'prepare_schedule_proofs',lambda *a,**kw:False)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='waiting'
    assert db.q1('SELECT count(*) n FROM nfl_spread_reservations')['n']==0

    setup_replay(monkeypatch,learning,observations,finals)
    @contextmanager
    def busy():
        raise learning.publication.PublicationDeferred('refresh_busy')
        yield
    monkeypatch.setattr(learning.publication,'publish_lock',busy)
    assert learning.resume_publication(c['version'],now=NOW+90*86400+1)['status']=='deferred'
    consumed=db.q('SELECT * FROM nfl_spread_consumed_games ORDER BY game_id')
    evaluated=db.q1('SELECT payload FROM nfl_spread_evaluations')['payload']
    monkeypatch.setattr(learning.data,'prepare_schedule_proofs',lambda *a,**kw:False)
    monkeypatch.setattr(learning.data,'labels',lambda *a,**kw:pytest.fail('missing full schedule labels read'))
    result=learning.resume_publication(c['version'],now=NOW+90*86400+2)
    assert result['status']=='waiting' and result['detail']['reason']=='verified_whole_week_schedule_required'
    assert learning.active() is None
    assert db.q('SELECT * FROM nfl_spread_consumed_games ORDER BY game_id')==consumed
    assert db.q1('SELECT payload FROM nfl_spread_evaluations')['payload']==evaluated


def test_reserved_full_week_membership_change_is_terminal_and_never_reads_labels(environment,monkeypatch):
    from contextlib import contextmanager
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    @contextmanager
    def busy():
        raise learning.publication.PublicationDeferred('refresh_busy')
        yield
    monkeypatch.setattr(learning.publication,'publish_lock',busy)
    assert learning.resume_publication(c['version'],now=NOW+90*86400)['status']=='deferred'
    original=learning.data.week_universe
    monkeypatch.setattr(learning.data,'week_universe',lambda season,week:original(season,week)+[dict(id='new-authenticated-game')] if week==1 else original(season,week))
    monkeypatch.setattr(learning.data,'labels',lambda *a,**kw:pytest.fail('changed sealed universe labels read'))
    result=learning.resume_publication(c['version'],now=NOW+90*86400+1)
    assert result['status']=='error' and 'whole-week universe changed' in result['detail']['reason']
    assert learning.active() is None
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_games')['n']==60


def test_new_whole_week_event_during_replay_defers_publication_and_preserves_consumption(environment,monkeypatch):
    store,learning=environment;c=proposal(environment)
    observations,finals,_=forecasts(environment,c)
    setup_replay(monkeypatch,learning,observations,finals)
    original=learning.data.labels
    def labels(rows,**kw):
        assert db.q1('SELECT count(*) n FROM nfl_spread_reservations')['n']==1
        db.upsert_event(dict(id='nfl-2027-reg-01-atl-gb',sport='NFL',home_team='Green Bay Packers',
            away_team='Atlanta Falcons',start_time='2027-09-12T17:00:00Z',status='STATUS_FINAL',
            meta=dict(season=2027,week=1,season_type=2)))
        return original(rows,**kw)
    monkeypatch.setattr(learning.data,'labels',labels)
    result=learning.resume_publication(c['version'],now=NOW+90*86400)
    assert result['status']=='deferred' and result['detail']['reason']=='publication_dependencies_changed'
    assert learning.active() is None
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_games')['n']==60
    assert db.q1('SELECT count(*) n FROM nfl_spread_proofs')['n']==0
