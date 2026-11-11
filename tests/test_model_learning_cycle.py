"""Retuning uses the built-in worker with bounded, recorded attempts."""
from types import SimpleNamespace

import pytest

from edgeterminal import db, model_learning
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "learning.db"}')
    db.init()
    yield
    db._conn.close()
    db._conn = None


def setup(monkeypatch, result=None):
    calls=[]
    def train(now):
        calls.append(('train',now))
        return result or {'status':'waiting','reason':'insufficient validation coverage'}
    adapter=SimpleNamespace(build_candidate=train,
        nightly_diagnostics=lambda now:calls.append(('diagnostics',now)) or {'status':'ok','graded':0})
    registry=SimpleNamespace(active=lambda sport:None,load_active=lambda sport:None,
        verify_active=lambda sport,**kwargs:None,
        history=lambda sport,limit=20:[],
        register_candidate=lambda *args,**kwargs:{'candidate_id':'reviewed'},
        activate_if_passes=lambda *args,**kwargs:{'status':'rejected','candidate_id':'reviewed','reasons':['market regression']})
    monkeypatch.setattr(model_learning,'_adapter',lambda sport:adapter)
    monkeypatch.setattr(model_learning,'_registry',lambda:registry)
    monkeypatch.setattr(model_learning,'_evidence_key',lambda sport:'finished-games-1')
    monkeypatch.setattr(model_learning,'_near_kickoff',lambda now:False)
    return calls,registry


def test_learning_waits_for_new_data_and_never_fits_during_status_reads(isolated,monkeypatch):
    calls,_=setup(monkeypatch)
    now=1793188800.  # 2026-10-28 08:00 Eastern
    first=model_learning.run(now)
    assert first['ok']
    assert len([r for r in calls if r[0]=='train'])==2
    model_learning.run(now+300)
    model_learning.status(now=now+301)
    assert len([r for r in calls if r[0]=='train'])==2
    monkeypatch.setattr(model_learning,'_evidence_key',lambda sport:'finished-games-2')
    model_learning.run(now+3600)
    assert len([r for r in calls if r[0]=='train'])==4


def test_rejected_update_keeps_active_model_and_uses_weekly_cadence(isolated,monkeypatch):
    ready={'status':'ready','version':'test','training_cutoff':1.,
           'artifact':{},'evaluation':{},'sources':[]}
    calls,_=setup(monkeypatch,ready)
    now=1793188800.
    result=model_learning.run(now)
    assert result['ok']
    assert result['sports']['NBA']['retuning']['status']=='rejected'
    model_learning.run(now+86400)
    assert len([r for r in calls if r[0]=='train'])==2
    assert model_learning.status('NBA',now=now+86400)['retuning']['next_due_at']==now+7*86400


def test_training_failure_retries_after_five_minutes_without_changing_active(isolated,monkeypatch):
    calls,registry=setup(monkeypatch)
    def fail(now):
        calls.append(('failed',now))
        raise ValueError('source checksum failed')
    monkeypatch.setattr(model_learning,'_adapter',lambda sport:SimpleNamespace(
        build_candidate=fail,nightly_diagnostics=lambda now:{'status':'ok'}))
    now=1793188800.
    assert model_learning.run(now)['ok'] is False
    model_learning.run(now+299)
    assert len(calls)==2
    model_learning.run(now+300)
    assert len(calls)==4
    assert registry.active('NBA') is None


def test_near_kickoff_defers_training_but_keeps_diagnostics_running(isolated,monkeypatch):
    calls,_=setup(monkeypatch)
    monkeypatch.setattr(model_learning,'_near_kickoff',lambda now:True)
    result=model_learning.run(1793188800.)
    assert result['ok']
    assert not any(r[0]=='train' for r in calls)
    assert any(r[0]=='diagnostics' for r in calls)


def test_diagnostics_catch_up_after_four_am_and_are_daily(isolated,monkeypatch):
    calls,_=setup(monkeypatch)
    model_learning.run(1793167200.)  # 02:00 Eastern
    assert not any(r[0]=='diagnostics' for r in calls)
    model_learning.run(1793188800.)
    model_learning.run(1793189100.)
    assert len([r for r in calls if r[0]=='diagnostics'])==2


def test_empty_new_install_waits_without_provider_calls(isolated,monkeypatch):
    from edgeterminal import nba_sources, nfl_prop_capture_ops
    monkeypatch.setattr(nba_sources, 'fetch', lambda *args, **kwargs: pytest.fail('learning fetched a source'))
    monkeypatch.setattr(nfl_prop_capture_ops, 'capture', lambda *args, **kwargs: pytest.fail('learning called provider'))
    result=model_learning.run(1793188800.)
    assert result['ok'], result
    assert all(checks['retuning']['status']=='waiting' for checks in result['sports'].values())
    model_learning.status(now=1793189100.)


def test_damaged_active_artifact_rolls_back_only_after_integrity_check(isolated,monkeypatch):
    _,registry=setup(monkeypatch)
    active={'candidate_id':'damaged','version':'fixture'}
    rolled=[]
    registry.active=lambda sport:active if sport=='NBA' and not rolled else None
    def verify(sport,**kwargs):
        if sport=='NBA' and not rolled:
            raise ValueError('artifact checksum failed')
    registry.verify_active=verify
    registry.rollback=lambda sport,reason,**kwargs:rolled.append((sport,reason)) or {'status':'rolled_back'}
    result=model_learning.run(1793188800.)
    assert result['ok'],result
    assert len(rolled)==1
    assert 'checksum failed' in rolled[0][1]
    assert result['sports']['NBA']['diagnostics']['integrity_rollback']['status']=='rolled_back'


def test_built_in_worker_runs_learning_after_nba_sources_and_grades(isolated,monkeypatch):
    from edgeterminal import research_refresh,nba_ops,learning_worker
    monkeypatch.setattr(settings,'nba_research_enabled',True)
    monkeypatch.setattr(research_refresh,'_events',lambda now:[])
    monkeypatch.setattr(research_refresh,'_due',lambda name,*args:name in {'nba_research','model_learning'})
    monkeypatch.setattr(research_refresh,'_result_due',lambda *args:False)
    monkeypatch.setattr(research_refresh,'_failed_prop_pair_batches',lambda now:[])
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops,'plan',lambda *args,**kwargs:{'due':{}})
    monkeypatch.setattr(research_refresh.paper_research,'needs_settlement',lambda:False)
    monkeypatch.setattr(research_refresh.paper_research,'evaluate',lambda *args,**kwargs:{})
    order=[]
    monkeypatch.setattr(nba_ops,'run',lambda now:order.append('NBA grading') or {'ok':True})
    monkeypatch.setattr(model_learning,'run',lambda now:pytest.fail('heavy learning inside source refresh'))
    monkeypatch.setattr(learning_worker,'refresh_checks',lambda now:order.append('learning') or {'ok':True})
    result=research_refresh._run_locked(1793188800.,record_tick=False)
    assert result['success'],result
    assert order==['NBA grading','learning']
    assert result['steps']['model_learning']['ok']


def test_integrity_recovery_runs_even_between_scheduled_training_attempts(isolated,monkeypatch):
    _,registry=setup(monkeypatch)
    now=1793188800.
    assert model_learning.run(now)['ok']
    damaged={'value':True}
    registry.active=lambda sport:{'candidate_id':'damaged'} if sport=='NBA' and damaged['value'] else None
    def load(sport):
        if sport=='NBA' and damaged['value']:
            raise ValueError('artifact changed')
    registry.load_active=load
    registry.verify_active=lambda sport,**kwargs:load(sport)
    def rollback(sport,reason,**kwargs):
        damaged['value']=False
        return {'status':'rolled_back','sport':sport}
    registry.rollback=rollback
    result=model_learning.run(now+300)
    assert result['ok'],result
    assert result['sports']['NBA']['integrity']['status']=='rolled_back'
    assert not damaged['value']


def test_repaired_integrity_warning_clears_after_a_real_successful_recheck(isolated,monkeypatch):
    setup(monkeypatch)
    now=1793188800.
    model_learning.ensure_schema()
    model_learning._record('NBA','integrity',now,'old',{'status':'error','message':'damaged'})
    assert model_learning.status('NBA',now=now)['integrity']['status']=='error'
    result=model_learning.run(now+300)
    assert result['ok']
    assert model_learning.status('NBA',now=now+300)['integrity']['status']=='ok'


def test_new_quality_protocol_runs_once_then_obeys_existing_cadence(isolated,monkeypatch):
    calls,_=setup(monkeypatch)
    now=1793188800.
    model_learning.run(now)
    adapter=model_learning._adapter('NBA')
    adapter.QUALITY_PROTOCOL='props-quality-v2'
    adapter.build_quality_candidate=lambda at:calls.append(('quality',at)) or {'status':'waiting','reason':'new outer games needed'}
    model_learning.run(now+300)
    model_learning.status(now=now+301)
    assert len([r for r in calls if r[0]=='quality'])==2
    model_learning.run(now+600)
    assert len([r for r in calls if r[0]=='quality'])==2
    assert model_learning._last('NBA','retuning')['detail']['learning_protocol']=='props-quality-v2'


def test_consumed_outer_cohort_waits_without_registering_again(isolated,monkeypatch):
    ready={'status':'ready','version':'test','training_cutoff':1.,
           'artifact':{'evaluation_family':'quality-v2'},'evaluation':{'rows':[{'game_id':'used'}]},'sources':[]}
    _,registry=setup(monkeypatch,ready)
    registry.holdout_available=lambda *args:False
    registry.register_candidate=lambda *args,**kwargs:pytest.fail('reused outer cohort was registered')
    result=model_learning._train('NBA',1793188800.)
    assert result['status']=='waiting' and 'outer' in result['reason']


def test_nonfinite_diagnostics_are_recorded_as_retryable_error(isolated,monkeypatch):
    setup(monkeypatch)
    adapter=model_learning._adapter('NBA')
    adapter.nightly_diagnostics=lambda now:{'status':'ok','brier':float('nan')}
    result=model_learning.run(1793188800.)
    assert not result['ok']
    assert model_learning._last('NBA','diagnostics')['status']=='error'


def test_waiting_result_keeps_bounded_lane_progress_without_raw_evidence(isolated,monkeypatch):
    waiting={'status':'waiting','reason':'no_market_calibration_improvement_on_inner_games',
        'market_selection':{'changed_markets':[],'quote_ids':['secret-quote'],'markets':{'rebounds':{
            'rows':29,'games':12,'reasons':['brier_regression'],'selected':False,'baseline':{'brier':.25}}}},
        'role_proposal':{'status':'waiting','reason':'full_same_selector_policy_contexts_required',
            'available_contexts':89,'required_contexts':100,'incomplete_pretip_contexts':2,'contexts':['/raw/path']},
        'sources':['/raw/source'],'artifact':{'secret':1}}
    setup(monkeypatch,waiting)
    result=model_learning._train('NBA',1793188800.)
    assert result=={'status':'waiting','reason':'no_market_calibration_improvement_on_inner_games',
        'markets':{'rebounds':{'rows':29,'games':12,'reasons':['brier_regression'],'selected':False}},
        'role_proposal':{'reason':'full_same_selector_policy_contexts_required',
            'available_contexts':89,'required_contexts':100,'incomplete_pretip_contexts':2}}
