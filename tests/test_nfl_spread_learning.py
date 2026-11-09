"""Automatic spread staging, untouched whole-week evaluation and recovery."""
from copy import deepcopy
import json

import pytest

from edgeterminal import db
from test_nfl_spread_component import isolated, row, NOW


def outer_rows(candidate_better=True, season=2027):
    rows=[]
    for week in range(1,7):
        for i in range(10):
            item=row(i,week=week,season=season,start=NOW+86400)
            actual=float(i%3*7)
            baseline=actual+5+(i%2)
            candidate=actual+(1 if candidate_better else 8)
            item.update(actual_margin=actual,incumbent_margin=baseline,candidate_margin=candidate,
                incumbent_probability=.7,candidate_probability=.52 if actual+item['home_spread']<0 else .8,
                candidate_sd=13.2,freeze_id=f'{week}-{i}')
            # Fixture probabilities must be evaluated from means/sigmas in the real path.
            rows.append(item)
    return rows


def test_actual_line_gates_are_week_paired_and_do_not_conflate_point_and_win_accuracy():
    from edgeterminal import nfl_spread_learning as learning
    rows=outer_rows()
    result=learning.evaluate(rows)
    assert result['games']==60 and result['weeks']==6
    assert result['bootstrap_unit']=='NFL_week'
    assert not learning.evaluate(rows[:50])['passed']
    bad=outer_rows(False)
    assert not learning.evaluate(bad)['passed']
    assert 'mae_improvement_too_small' in learning.evaluate(bad)['reasons']
    cross=deepcopy(rows);cross[-1]['season']=2028
    with pytest.raises(ValueError,match='season'):
        learning.evaluate(cross)


def test_mae_bootstrap_keeps_all_push_resamples(monkeypatch):
    import numpy as np
    from edgeterminal import nfl_spread_learning as learning
    rows = outer_rows()
    for item in rows:
        actual = 4. if item['week'] == 6 else 3.
        item.update(home_spread=-3., actual_margin=actual,
                    incumbent_margin=actual + (2. if item['week'] == 6 else 10.),
                    candidate_margin=actual + 1.)
    class FixedBlocks:
        calls = 0
        def integers(self, low, high, size):
            self.calls += 1
            return [0 if self.calls <= 1000 else 5] * size
    rng = FixedBlocks()
    monkeypatch.setattr(np.random, 'default_rng', lambda seed: rng)
    result = learning.evaluate(rows)
    assert rng.calls == 2000
    assert result['intervals']['mae'] == [-9., -1.]
    assert 'brier' in result['intervals']


def test_retained_incumbent_opens_no_candidate_or_outer_reservation(monkeypatch):
    from edgeterminal import nfl_spread_learning as learning, nfl_spread_data as data
    monkeypatch.setattr(learning.time,'time',lambda:NOW)
    rows=[row(i,w,start=NOW-8*7*86400) for w in range(1,7) for i in range(20)]
    for r in rows: r['actual_margin']=r['incumbent_margin']
    monkeypatch.setattr(data,'observations',lambda **kw:(rows,0))
    monkeypatch.setattr(data,'labels',lambda rs,**kw:{r['game_id']:{k:r[k] for k in ('actual_margin','label_received_at','label_sha256')} for r in rs})
    monkeypatch.setattr(data,'complete_week',lambda *args:True)
    monkeypatch.setattr(data,'prepare_schedule_proofs',lambda *a,**kw:True)
    monkeypatch.setattr(data,'week_universe',lambda season,week:[dict(id=r['game_id']) for r in rows if r['season']==season and r['week']==week])
    monkeypatch.setattr(learning,'_verify_stage_sources',lambda *args:None)
    # Code/data verification is independent from fixture numerical test.
    monkeypatch.setattr(learning.store,'code_catalog',lambda:{})
    result=learning.run_fitting(now=NOW)
    assert result['status']=='retained'
    assert db.q1('SELECT COUNT(*) n FROM nfl_spread_candidates')['n']==0
    assert db.q1('SELECT COUNT(*) n FROM nfl_spread_reservations')['n']==0


def test_no_compatible_history_stays_explicitly_waiting_without_refitting(monkeypatch):
    from edgeterminal import nfl_spread_learning as learning,nfl_spread_data as data
    monkeypatch.setattr(learning.time,'time',lambda:NOW)
    monkeypatch.setattr(data,'observations',lambda **kw:([],104))
    monkeypatch.setattr(data,'labels',lambda *a,**k:{})
    result=learning.run_fitting(now=NOW)
    assert result['status']=='waiting'
    assert result['detail']['stage']=='fit'
    assert result['detail']['unverifiable_legacy_snapshots']==104
    assert learning.status(now=NOW)['detail']['required']=={'fit':40,'development':20,'calibration':20,'outer_games':60,'outer_weeks':6,'validation':60}
    assert learning.run_fitting(now=NOW+1)['detail']['stage']=='fit'


def test_reservation_consumes_complete_week_before_label_access(monkeypatch):
    from edgeterminal import nfl_spread_store as store
    store.ensure_schema()
    freezes=[dict(game_id=f'g{i}',season=2027,week=1,freeze_id=f'f{i}',sha256='a'*64) for i in range(2)]
    with pytest.raises(ValueError,match='whole|universe'):
        store.reserve('v1',freezes[:1],{(2027,1):['g0','g1']},now=NOW)
    # This isolated boundary test exercises reservation membership; persisted seals
    # are separately required by freeze/register integration tests.
    monkeypatch.setattr(store,'verify_freeze',lambda value:value)
    monkeypatch.setattr(store,'candidate',lambda *args,**kw:{'sha256':'b'*64})
    reserved=store.reserve('v1',freezes,{(2027,1):['g0','g1']},now=NOW)
    assert store.reservation(reserved['reservation_id'])['payload']['game_ids']==['g0','g1']
    assert db.q1('SELECT count(*) n FROM nfl_spread_consumed_weeks')['n']==1
    with pytest.raises(Exception):
        store.reserve('v2',freezes,{(2027,1):['g0','g1']},now=NOW+1)


def test_insufficient_season_remaining_is_explicit_and_never_uses_playoffs():
    from edgeterminal import nfl_spread_learning as learning
    assert learning.outer_window(2026,14,[])['reason']=='insufficient_season_remaining'
    assert learning.outer_window(2026,10,[])['season']==2026


def test_missing_calibration_retains_the_same_sealed_bank_then_advances(monkeypatch,isolated):
    from edgeterminal import nfl_spread_learning as learning,nfl_spread_data as data,nfl_spread_store as store
    root=isolated/'code';(root/'edgeterminal').mkdir(parents=True);(root/'edgeterminal/x.py').write_text('same\n')
    monkeypatch.setattr(store,'ROOT',root);monkeypatch.setattr(store,'CODE_NAMES',('edgeterminal/x.py',))
    monkeypatch.setattr(learning.time,'time',lambda:NOW)
    rows=[row(i,w,start=NOW-8*7*86400) for w in range(1,4) for i in range(20)]
    for r in rows:r['actual_margin']=20.
    monkeypatch.setattr(data,'observations',lambda **kw:(deepcopy(rows),0))
    monkeypatch.setattr(data,'labels',lambda rs,**kw:{r['game_id']:{'actual_margin':20.,'label_received_at':r['label_received_at'],'label_sha256':r['label_sha256']} for r in rs})
    monkeypatch.setattr(data,'complete_week',lambda *args:True)
    monkeypatch.setattr(data,'prepare_schedule_proofs',lambda *a,**kw:True)
    monkeypatch.setattr(data,'week_universe',lambda season,week:[dict(id=r['game_id']) for r in rows if r['season']==season and r['week']==week])
    monkeypatch.setattr(data,'week_universe',lambda season,week:[dict(id=r['game_id']) for r in rows if r['season']==season and r['week']==week])
    monkeypatch.setattr(learning,'_verify_stage_sources',lambda *args:None)
    first=learning.run_fitting(now=NOW)
    assert first['detail']['stage']=='calibration'
    old=store.latest_stage()['state'];bank=deepcopy(old['bank']);cutoff=old['fit_cutoff']
    assert learning.run_fitting(now=NOW+1)['detail']['stage']=='calibration'
    assert store.latest_stage()['state']['bank']==bank
    rows.extend(row(i,4,start=NOW-8*7*86400) for i in range(20))
    result=learning.run_fitting(now=NOW+2)
    assert result['status']=='waiting' and result['detail']['stage']=='outer'
    assert store.latest_stage()['state']['bank']==bank
    assert store.latest_stage()['state']['fit_cutoff']==cutoff
    assert db.q1('SELECT count(*) n FROM nfl_spread_candidates')['n']==1
    candidate=store.candidate(result['detail']['candidate_version'])
    state=json.loads(db.q1('SELECT state FROM nfl_spread_stages WHERE id=?',(candidate['artifact']['stage_id'],))['state'])
    assert learning._verify_training_stage(state,candidate['artifact'])
    wrong=deepcopy(candidate['artifact']);wrong['member']['intercept']+=1
    with pytest.raises(ValueError,match='choice'):
        learning._verify_training_stage(state,wrong)


def test_changed_code_invalidates_incomplete_stage_instead_of_silently_refitting(monkeypatch):
    from edgeterminal import nfl_spread_learning as learning,nfl_spread_data as data,nfl_spread_store as store
    monkeypatch.setattr(learning.time,'time',lambda:NOW)
    monkeypatch.setattr(data,'observations',lambda **kw:([],0));monkeypatch.setattr(data,'labels',lambda *a,**k:{})
    current={'x':'a'*64};monkeypatch.setattr(store,'code_catalog',lambda:dict(current))
    assert learning.run_fitting(now=NOW)['status']=='waiting'
    current['x']='b'*64
    result=learning.run_fitting(now=NOW+1)
    assert result['status']=='error' and result['detail']['stage']=='invalidated'
    assert learning.run_fitting(now=NOW+2)['status']=='waiting'


def test_dispatch_queues_only_the_spread_action_and_cached_checks_never_fit(monkeypatch):
    from edgeterminal import nfl_spread_learning as learning,learning_worker as worker
    monkeypatch.setattr(learning.time,'time',lambda:NOW)
    monkeypatch.setattr(learning,'run_fitting',lambda *a,**k:pytest.fail('dispatch fitted inline'))
    first=learning.refresh_checks(now=NOW)
    assert first['status']=='queued'
    assert learning.refresh_checks(now=NOW+1)['job_id']==first['job_id']
    assert db.q1('SELECT component FROM learning_jobs')['component']=='NFL_SPREAD'
    learning._record(NOW,'waiting',dict(stage='fit',next_at=NOW+86400))
    assert learning.refresh_checks(now=NOW+2)['status']=='cached'


def test_absent_status_and_active_are_select_only_on_query_only_database():
    from edgeterminal import nfl_spread_learning as learning
    before={r['name'] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
    sql=[];db.conn().set_trace_callback(sql.append);db.conn().execute('PRAGMA query_only=ON')
    try:
        assert learning.active() is None
        status=learning.status(now=NOW)
        assert status['status']=='not_checked' and status['checked_at'] is None
        assert status['detail']['stage']=='fit' and status['active'] is None
        assert {r['name'] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'") }==before
        assert not any(s.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE','ALTER')) for s in sql)
    finally:
        db.conn().execute('PRAGMA query_only=OFF');db.conn().set_trace_callback(None)
