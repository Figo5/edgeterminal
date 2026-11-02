"""Source ticks enqueue spread work and never call a fitting function."""
import sys
from types import SimpleNamespace

import pytest
from test_nba_spread_learning import database
from edgeterminal import nba_spread_learning as learning, nba_spread_data as data
from edgeterminal import db


def queue(monkeypatch):
    import edgeterminal
    calls=[]
    worker=SimpleNamespace(enqueue=lambda component,kind,now=None:
        calls.append((component,kind,now)) or {'status':'queued','job_id':1})
    monkeypatch.setitem(sys.modules,'edgeterminal.learning_worker',worker)
    monkeypatch.setattr(edgeterminal,'learning_worker',worker,raising=False)
    return calls


def test_source_refresh_enqueues_without_building_a_candidate(database, monkeypatch):
    calls=queue(monkeypatch); fetches=[]
    monkeypatch.setattr(data,'refresh',lambda *a,**k:fetches.append(k) or {'status':'ok','requests':0})
    monkeypatch.setattr(learning,'build_candidate',lambda *a:pytest.fail('source tick fitted a model'))
    result=learning.refresh_inputs(100000.)
    assert result['status']=='queued' and result['ok']
    assert calls==[('NBA_SPREAD','retuning',100000.)]
    assert fetches==[{'max_requests':4}]


def test_fitting_runner_never_fetches_sources(database, monkeypatch):
    monkeypatch.setattr(data,'refresh',lambda *a,**k:pytest.fail('learner requested a provider'))
    monkeypatch.setattr(learning,'build_candidate',lambda *a:{'status':'waiting','reason':'more_future_games'})
    assert learning.run_fitting(100000.)['status']=='waiting'


def test_source_error_does_not_queue_fit_or_disappear(database, monkeypatch):
    calls=queue(monkeypatch)
    monkeypatch.setattr(data,'refresh',lambda *a,**k:{'status':'error','errors':['source unavailable']})
    monkeypatch.setattr(learning,'build_candidate',lambda *a:pytest.fail('source error fitted'))
    result=learning.refresh_inputs(100000.)
    assert not result['ok'] and result['status']=='error' and calls==[]


def test_source_tick_reports_an_already_running_job_honestly(database, monkeypatch):
    import edgeterminal
    worker=SimpleNamespace(enqueue=lambda *args,**kwargs:{'state':'running','id':1})
    monkeypatch.setitem(sys.modules,'edgeterminal.learning_worker',worker)
    monkeypatch.setattr(edgeterminal,'learning_worker',worker,raising=False)
    monkeypatch.setattr(data,'refresh',lambda *args,**kwargs:{'requests':0})
    assert learning.refresh_inputs(100000.)['status']=='running'


def preverified_proposal():
    """Isolate publication from fitting with immutable, source-bound fixture heads."""
    import hashlib,json
    db.ex('CREATE TABLE nba_game_grade_revisions(id INTEGER PRIMARY KEY,game_id TEXT,'
          'source_artifact_id INTEGER,graded_at REAL,payload TEXT,sha256 TEXT)')
    receipts=[]
    for i in range(60):
        eid=str(401900000+i);gid='nba-2026-'+eid
        body=json.dumps({'fixture':eid}).encode();sha=hashlib.sha256(body).hexdigest()
        url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+eid
        db.ex('INSERT INTO nba_source_artifacts VALUES(?,?,?,?,?,?,?)',(i+1,'final-boxscore',url,1000.+i,200,sha,body))
        db.ex('INSERT INTO nba_games VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (gid,eid,2026,'2026-27','regular','NBA','BOS','NYK','1970-01-01T00:10:00Z',600.,
             '1970-01-01','final',100,90,i+1,1000.+i))
        payload=json.dumps({'forecast_sha256':'c'*64})
        db.ex('INSERT INTO nba_game_grade_revisions VALUES(?,?,?,?,?,?)',(i+1,gid,i+1,1000.+i,payload,'b'*64))
        receipts.append({'game_id':gid,'game_date_et':f'2024-01-{i%28+1:02d}',
                         'forecast_sha256':'c'*64,'label_sha256':sha})
    artifact={'schema':2,'kind':'nba-spread-market-v2','version':'publication-fixture',
        'feature_names':list(learning.model.FEATURE_NAMES),'training_cutoff':100.,'core_training_cutoff':90.,
        'intercept':0.,'coefficients':[0.]*6,'means':[0.]*6,'scales':[1.]*6,'sd_margin':12.,
        'market_anchor':{'kind':'positive_convex_market_anchor','market_basis':'exact_saved_ESPN_home_spread',
                         'weight':.5,'core_sd_margin':12.,'calibration_sd':12.},
        'code_sha256':learning.code_catalog(),'validation_receipts':receipts,
        'validation_game_ids':[r['game_id'] for r in receipts]}
    return {'status':'ready','artifact':artifact,'evaluation':{'passed':True,'rows':60,'days':28,'reasons':[]}}


def test_preverified_spread_publication_is_short_and_consumes_holdouts(database, monkeypatch):
    import fcntl,sqlite3
    from edgeterminal.config import settings
    proposal=preverified_proposal()
    def build(now):
        assert not db.conn().in_transaction
        with settings.db_path.with_name(settings.db_path.name+'-research.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with sqlite3.connect(str(settings.db_path),timeout=.1) as other:
            other.execute('INSERT INTO kv(k,v,updated_at) VALUES(?,?,?)',('source-still-writes','{}',1001.))
        return proposal
    monkeypatch.setattr(learning,'build_candidate',build)
    monkeypatch.setattr(data,'refresh',lambda *a,**k:pytest.fail('fitting requested a provider'))
    result=learning.run_fitting(100000.)
    assert result['status']=='activated'
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_validation_consumption')['n']==60
    assert learning.active()['version']==proposal['artifact']['version']


def test_spread_revision_during_fit_is_rejected_and_holdouts_remain_spent(database, monkeypatch):
    proposal=preverified_proposal()
    def build(now):
        row=db.q1('SELECT * FROM nba_game_grade_revisions WHERE id=1')
        db.ex('INSERT INTO nba_game_grade_revisions VALUES(?,?,?,?,?,?)',
              (100,row['game_id'],row['source_artifact_id'],2000.,row['payload'],'f'*64))
        return proposal
    monkeypatch.setattr(learning,'build_candidate',build)
    result=learning.run_fitting(100000.)
    assert result['status']=='rejected' and learning.active() is None
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_validation_consumption')['n']==60
    assert any('publication:' in r for r in result['evaluation']['reasons'])


def test_busy_capture_defers_spread_publication_without_writer_lock(database, monkeypatch):
    import fcntl
    from edgeterminal.config import settings
    proposal=preverified_proposal()
    monkeypatch.setattr(learning,'build_candidate',lambda now:proposal)
    with settings.db_path.with_name(settings.db_path.name+'-research.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=learning.run_fitting(100000.)
    assert result['status']=='deferred' and not db.conn().in_transaction
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_model_candidates')['n']==1
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_validation_consumption')['n']==60


def test_spread_approval_requires_source_bound_receipts_even_for_legacy_kind(database, monkeypatch):
    proposal=preverified_proposal();artifact=proposal['artifact']
    artifact.update(schema=1,kind='nba-spread-residual-v1')
    artifact.pop('validation_receipts')
    monkeypatch.setattr(learning,'build_candidate',lambda now:proposal)
    assert learning.run_fitting(100000.)['status']=='rejected'
    assert learning.active() is None


def test_spread_candidate_resumes_without_refitting_after_tipoff_priority(database, monkeypatch):
    from edgeterminal import model_learning
    proposal=preverified_proposal();near=[False]
    monkeypatch.setattr(model_learning,'_near_kickoff',lambda now:near[0])
    def build(now):
        near[0]=True
        return proposal
    monkeypatch.setattr(learning,'build_candidate',build)
    assert learning.run_fitting(100000.)['status']=='deferred'
    assert learning.active() is None
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_model_candidates')['n']==1
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_validation_consumption')['n']==60
    monkeypatch.setattr(learning,'build_candidate',lambda *a:pytest.fail('resuming refitted'))
    near[0]=False
    assert learning.resume_publication(proposal['artifact']['version'],now=100001.)['status']=='activated'
