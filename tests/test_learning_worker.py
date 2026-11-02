"""Heavy learning is independent of the ordinary refresh lock and lifecycle."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from edgeterminal import db, model_learning
from edgeterminal.config import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'jobs.db'))
    db.init()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def worker():
    return importlib.import_module('edgeterminal.learning_worker')


def test_refresh_enqueues_without_fitting_or_full_integrity(monkeypatch):
    adapter = SimpleNamespace(QUALITY_PROTOCOL='fixture', build_candidate=lambda now: pytest.fail('fit in refresh'),
        nightly_diagnostics=lambda now: pytest.fail('diagnostics in refresh'))
    registry = SimpleNamespace(load_active=lambda sport: None, active=lambda sport: None,
        verify_active=lambda *a, **k: pytest.fail('heavy integrity in refresh'))
    monkeypatch.setattr(model_learning, '_adapter', lambda sport: adapter)
    monkeypatch.setattr(model_learning, '_registry', lambda: registry)
    monkeypatch.setattr(model_learning, '_near_kickoff', lambda now: False)
    monkeypatch.setattr(model_learning, '_evidence_key', lambda sport: 'same')
    result = worker().refresh_checks(1793188800.)
    assert result['ok'] and result['sports']['NBA']['retuning']['status'] == 'queued'
    assert db.q1('SELECT COUNT(*) AS n FROM learning_jobs')['n'] == 4
    worker().refresh_checks(1793188801.)
    assert db.q1('SELECT COUNT(*) AS n FROM learning_jobs')['n'] == 4


def test_jobs_deduplicate_while_pending_and_execute_once(monkeypatch):
    w = worker(); first = w.enqueue('NBA', 'retuning', now=100.)
    assert w.enqueue('NBA', 'retuning', now=101.)['id'] == first['id']
    calls = []
    monkeypatch.setattr(w, '_execute', lambda job: calls.append(job['id']) or {'status': 'waiting'})
    assert w.run_once(now=102.)['success']
    assert w.run_once(now=103.)['status'] == 'idle'
    assert calls == [first['id']]
    assert w.status(component='NBA', now=103.)['jobs'][0]['status'] == 'completed'


def test_failure_stays_visible_when_retry_is_only_queued(monkeypatch):
    w = worker(); w.enqueue('NFL', 'diagnostics', now=100.)
    def fail(job):
        raise ValueError('provider-key-must-not-leak')
    monkeypatch.setattr(w, '_execute', fail)
    assert not w.run_once(now=101.)['success']
    assert w.status(component='NFL', now=102.)['needs_attention']
    w.enqueue('NFL', 'diagnostics', now=102.)
    status = w.status(component='NFL', now=103.)
    assert status['needs_attention']
    assert 'provider-key-must-not-leak' not in json.dumps(status)
    monkeypatch.setattr(w, '_execute', lambda job: {'status': 'ok'})
    assert w.run_once(now=104.)['success']
    assert not w.status(component='NFL', now=105.)['needs_attention']


def test_real_task_timing_not_the_refresh_enqueue_timestamp(monkeypatch):
    w = worker(); w.enqueue('NBA', 'diagnostics', now=100.)
    monkeypatch.setattr(w, '_execute', lambda job: {'status': 'ok'})
    before = time.time(); w.run_once(); after = time.time()
    job = w.status(component='NBA')['jobs'][0]
    assert before <= job['started_at'] <= job['finished_at'] <= after
    assert job['queued_at'] == 100.
    assert job['elapsed_seconds'] >= 0 and job['cpu_seconds'] >= 0


def test_stale_runner_is_visible_without_reclaiming_the_os_lock(monkeypatch):
    w=worker();job=w.enqueue('NBA','retuning',now=100.)
    db.ex("UPDATE learning_jobs SET state='running',started_at=101,heartbeat_at=101,owner='active' WHERE id=?",(job['id'],))
    db.kv_set('learning_worker_heartbeat',{'checked_at':101})
    status=w.status(component='NBA',now=2000.)
    assert status['worker']['state']=='stale' and status['needs_attention']
    assert db.q1('SELECT state FROM learning_jobs WHERE id=?',(job['id'],))['state']=='running'


def test_idle_runner_with_stale_heartbeat_needs_attention():
    w=worker();w.ensure_schema()
    db.kv_set('learning_worker_heartbeat',{'checked_at':101})
    assert w.status(now=2000.)['needs_attention']


def test_changing_proof_is_deferred_without_rollback(monkeypatch):
    from edgeterminal.learning_publication import PublicationDeferred
    registry=SimpleNamespace(active=lambda sport:{'candidate_id':'existing'},
        verify_active=lambda *a,**k:(_ for _ in ()).throw(PublicationDeferred('publication_dependencies_changed')),
        rollback=lambda *a,**k:pytest.fail('changing proof is not artifact damage'))
    monkeypatch.setattr(model_learning,'_registry',lambda:registry)
    with pytest.raises(PublicationDeferred):
        model_learning._integrity('NBA',1793188800.)
    monkeypatch.setattr(model_learning,'_evidence_key',lambda sport:'fixture')
    result=model_learning.run_kind('NBA','diagnostics',now=1793188800.)
    assert result['status']=='deferred' and result['retry_after_seconds']==300


@pytest.mark.parametrize('component,kind', [('NBA', 'shell'), ('invalid', 'retuning'), ('NBA_SPREAD', 'diagnostics')])
def test_job_action_catalog_is_closed(component, kind):
    with pytest.raises(ValueError):
        worker().enqueue(component, kind, now=100.)


def test_an_inherited_or_dotenv_secret_cannot_reach_the_learner(tmp_path):
    w = worker()
    original = dict(os.environ)
    os.environ['SPORTSGAMEODDS_API_KEY'] = 'fixture-key'
    os.environ['OPENAI_API_KEY'] = 'fixture-openai'
    try:
        env = w.offline_environment()
        assert env['EDGETERMINAL_OFFLINE_LEARNER'] == '1'
        assert env['OMP_NUM_THREADS'] == env['OPENBLAS_NUM_THREADS'] == '1'
        assert not any('KEY' in name or 'SECRET' in name or 'PASSWORD' in name for name in env)
        # Fresh config import with a real temporary .env, without touching live credentials.
        code = Path(settings.__class__.__module__.replace('.', '/'))
        source = Path(__file__).resolve().parents[1] / 'edgeterminal/config.py'
        target = tmp_path / 'config.py'; target.write_bytes(source.read_bytes())
        (tmp_path / '.env').write_text('SPORTSGAMEODDS_API_KEY=dotenv-secret\n')
        # config derives ROOT as parent.parent, so use package/config.py.
        package = tmp_path / 'package'; package.mkdir(); (package / 'config.py').write_bytes(source.read_bytes())
        command = [sys.executable, '-c', 'import runpy,os;runpy.run_path(' + repr(str(package/'config.py')) + ');assert not os.getenv("SPORTSGAMEODDS_API_KEY")']
        subprocess.run(command, env=env, check=True)
    finally:
        os.environ.clear(); os.environ.update(original)


def test_offline_runtime_blocks_network_even_without_provider_keys():
    w = worker()
    script = 'from edgeterminal.learning_worker import offline_runtime;import socket\nwith offline_runtime():\n try: socket.create_connection(("example.invalid",443))\n except RuntimeError: pass\n else: raise AssertionError("network allowed")\n'
    subprocess.run([sys.executable, '-c', script], env=w.offline_environment(), check=True)


def test_learning_wall_deadline_is_bounded_and_does_not_touch_refresh_timeout():
    script='from edgeterminal.learning_worker import execution_limits,LearningDeadlineExceeded;import time\ntry:\n with execution_limits(wall_seconds=.05):time.sleep(2)\nexcept LearningDeadlineExceeded:pass\nelse:raise AssertionError("deadline missing")\n'
    subprocess.run([sys.executable,'-c',script],env=worker().offline_environment(),check=True,timeout=3)


def test_learning_cpu_deadline_is_separate_from_sleeping_time():
    script='from edgeterminal.learning_worker import execution_limits,LearningDeadlineExceeded\ntry:\n with execution_limits(wall_seconds=5,cpu_seconds=1):\n  while True:pass\nexcept LearningDeadlineExceeded:pass\nelse:raise AssertionError("cpu deadline missing")\n'
    subprocess.run([sys.executable,'-c',script],env=worker().offline_environment(),check=True,timeout=4)


def test_running_learner_does_not_hold_the_research_lock(tmp_path, monkeypatch):
    w = worker(); w.enqueue('NBA', 'retuning', now=100.)
    lock = settings.db_path.with_name(settings.db_path.name + '-research.lock')
    lock.touch()
    acquired = []
    def work(job):
        code = 'import fcntl,sys;f=open(sys.argv[1],"r+");fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print("acquired")'
        result = subprocess.run([sys.executable, '-c', code, str(lock)], capture_output=True, text=True, check=True)
        acquired.append(result.stdout.strip())
        db.ex("INSERT INTO kv VALUES('capture-grade-write','ok',100)")
        return {'status': 'ok'}
    monkeypatch.setattr(w, '_execute', work)
    assert w.run_once()['success']
    assert acquired == ['acquired']
    assert db.q1("SELECT v FROM kv WHERE k='capture-grade-write'")['v'] == 'ok'


def test_real_refresh_and_grading_finish_while_another_process_fit_is_blocked(monkeypatch):
    w=worker();w.enqueue('NBA','retuning')
    entered=threading.Event();release=threading.Event();finished=[]
    def slow_fit(job):
        entered.set()
        assert release.wait(8), 'fixture fit release was lost'
        return {'status':'waiting'}
    monkeypatch.setattr(w,'_execute',slow_fit)
    learner=threading.Thread(target=lambda:finished.append(w.run_once()))
    learner.start()
    script = '''
import socket,json
def offline(*a,**k):raise AssertionError('source fixture attempted network')
socket.create_connection=offline;socket.socket.connect=offline
from edgeterminal import research_refresh as r,db
db.init()
r._events=lambda now:[]
r._due=lambda *a:False
r._result_due=lambda name,*a:name=='top_five_grading'
r._failed_prop_pair_batches=lambda now:[]
r.nfl_prop_capture_ops.plan=lambda *a,**k:{'due':{}}
r.paper_research.needs_settlement=lambda:False
r.paper_research.active_policy=lambda:{'version':'fixture','enabled':False}
r.paper_research.evaluate=lambda *a,**k:{}
def grade(now):
 db.kv_set('graded_while_learning',{'checked':True})
 return {'freezes_checked':0,'results':[]}
r.nfl_top_five.grade_due=grade
result=r.run()
assert result['success'],result
assert result['steps']['top_five_grading']['ok']
print(json.dumps({'success':True,'run_id':result['run_id']}))
'''
    try:
        assert entered.wait(2)
        env=dict(os.environ,DATABASE_URL=settings.database_url,NBA_RESEARCH_ENABLED='false',PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,check=True,timeout=4)
        assert json.loads(result.stdout)['success']
        assert not release.is_set() and learner.is_alive()
        assert db.q1("SELECT v FROM kv WHERE k='graded_while_learning'")
        assert db.q1('SELECT success FROM research_refresh_ticks')['success']==1
        assert db.q1("SELECT success FROM research_refresh_steps WHERE name='top_five_grading'")['success']==1
    finally:
        release.set();learner.join(timeout=3)
    assert finished[0]['success']


def test_second_runner_does_not_reclaim_a_live_job(monkeypatch):
    w = worker(); w.enqueue('NBA', 'retuning')
    results = []
    def work(job):
        results.append(w.run_once())
        return {'status': 'ok'}
    monkeypatch.setattr(w, '_execute', work)
    assert w.run_once()['success']
    assert results[0]['status'] == 'already_running'


def test_sigterm_leaves_a_recoverable_claim_and_releases_os_lock(monkeypatch):
    w=worker();job=w.enqueue('NBA','diagnostics')
    script='from edgeterminal import learning_worker as w;import time,sys;sys.argv=["fixture","run"];w._execute=lambda job:time.sleep(30);raise SystemExit(w.main())'
    env=w.offline_environment(dict(os.environ,DATABASE_URL=settings.database_url))
    process=subprocess.Popen([sys.executable,'-c',script],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        until=time.monotonic()+3
        while time.monotonic()<until:
            if db.q1('SELECT state FROM learning_jobs WHERE id=?',(job['id'],))['state']=='running':break
            time.sleep(.02)
        else:pytest.fail('fixture learner did not claim job')
        process.terminate();process.communicate(timeout=3)
        assert process.returncode==143
        monkeypatch.setattr(w,'_execute',lambda row:{'status':'ok'})
        assert w.run_once()['success']
        assert db.q1("SELECT 1 FROM learning_job_events WHERE job_id=? AND state='interrupted'",(job['id'],))
    finally:
        if process.poll() is None:
            process.kill();process.communicate()


def test_lock_symlink_substitution_is_rejected_at_open(tmp_path, monkeypatch):
    w = worker(); target = tmp_path / 'elsewhere'; target.touch()
    lock = settings.db_path.with_name(settings.db_path.name + '-learning.lock')
    lock.symlink_to(target)
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: False if path == lock else original(path))
    with pytest.raises((OSError, ValueError)):
        w.run_once()


def test_fifo_lock_is_rejected_without_blocking(tmp_path):
    worker()
    lock = settings.db_path.with_name(settings.db_path.name + '-learning.lock')
    os.mkfifo(lock)
    code = ('from edgeterminal.learning_worker import run_once;'
            'from edgeterminal.config import settings;'
            'settings.database_url=' + repr(settings.database_url) + ';'
            '\ntry:run_once()\nexcept (OSError,ValueError):pass\nelse:raise AssertionError("accepted FIFO")')
    subprocess.run([sys.executable, '-c', code], check=True, timeout=2)


def test_crashed_claim_is_recorded_and_retried_after_os_lock_release(monkeypatch):
    w = worker(); j = w.enqueue('NBA', 'diagnostics', now=100.)
    db.ex("UPDATE learning_jobs SET state='running',started_at=101,owner='dead',heartbeat_at=101 WHERE id=?", (j['id'],))
    monkeypatch.setattr(w, '_execute', lambda job: {'status': 'ok'})
    assert w.run_once(now=500.)['success']
    events = db.q('SELECT state FROM learning_job_events WHERE job_id=? ORDER BY id', (j['id'],))
    assert 'interrupted' in [r['state'] for r in events]
    assert w.status()['jobs'][0]['status'] == 'completed'


def test_interrupted_registered_candidate_resumes_without_refitting(monkeypatch):
    w=worker();job=w.enqueue('NBA','retuning',now=100.)
    candidate='c'*64
    db.ex("UPDATE learning_jobs SET state='running',started_at=101,owner='dead',detail=? WHERE id=?",
          (json.dumps({'candidate_id':candidate}),job['id']))
    calls=[]
    monkeypatch.setattr(model_learning,'run_kind',lambda *a,**k:pytest.fail('registered candidate was fitted again'))
    monkeypatch.setattr(model_learning,'resume_candidate',lambda sport,cid:calls.append((sport,cid)) or {'status':'rejected'},raising=False)
    assert w.run_once(now=500.)['success']
    assert calls==[('NBA',candidate)]


def test_candidate_checkpoint_is_owned_and_survives_retry(monkeypatch):
    w=worker();job=w.enqueue('NFL','retuning',now=100.)
    candidate='d'*64
    def execute(row):
        with db.tx() as con:
            w.checkpoint_candidate(con,candidate)
        raise ValueError('interrupt after durable registration')
    monkeypatch.setattr(w,'_execute',execute)
    assert not w.run_once(now=101.)['success']
    retry=w.enqueue('NFL','retuning',now=500.)
    assert json.loads(retry['detail'])['candidate_id']==candidate
    with db.tx() as con:
        # Non-learner callers cannot accidentally attach another candidate.
        w.checkpoint_candidate(con,'e'*64)
    assert json.loads(db.q1('SELECT detail FROM learning_jobs WHERE id=?',(job['id'],))['detail'])['candidate_id']==candidate


@pytest.mark.parametrize('initial_state', ['failed', 'deferred'])
def test_a_later_completed_retry_prevents_old_checkpoint_resurrection(monkeypatch, initial_state):
    w=worker();w.enqueue('NBA','retuning',now=100.)
    def fail_after_registration(row):
        with db.tx() as con:w.checkpoint_candidate(con,'f'*64)
        if initial_state=='deferred':return {'status':'deferred'}
        raise ValueError('retryable fixture')
    monkeypatch.setattr(w,'_execute',fail_after_registration)
    assert w.run_once(now=101.)['status']==initial_state
    w.enqueue('NBA','retuning',now=500.)
    monkeypatch.setattr(w,'_execute',lambda row:{'status':'rejected'})
    assert w.run_once(now=501.)['success']
    next_job=w.enqueue('NBA','retuning',now=1000.)
    assert json.loads(next_job['detail'])=={}


def test_spread_checkpoint_resumes_prepared_publication_without_refitting(monkeypatch):
    from edgeterminal import nba_spread_learning
    w=worker();nba_spread_learning.ensure_schema()
    version='nba-game-adaptive-'+'a'*16
    db.ex('INSERT INTO nba_spread_model_candidates VALUES(?,?,?,?,?,?)',(version,1.,.5,'{}','hash','{}'))
    job=w.enqueue('NBA_SPREAD','retuning',now=100.)
    original=w._execute
    def prepare(row):
        with db.tx() as con:w.checkpoint_candidate(con,version)
        return {'status':'deferred'}
    monkeypatch.setattr(w,'_execute',prepare)
    assert w.run_once(now=101.)['status']=='deferred'
    retry=w.enqueue('NBA_SPREAD','retuning',now=500.)
    assert json.loads(retry['detail'])['candidate_id']==version
    calls=[]
    monkeypatch.setattr(w,'_execute',original)
    monkeypatch.setattr(nba_spread_learning,'run_fitting',lambda *a,**k:pytest.fail('prepared spread was refitted'))
    monkeypatch.setattr(nba_spread_learning,'resume_publication',lambda ident:calls.append(ident) or {'status':'rejected'},raising=False)
    assert w.run_once(now=501.)['success']
    assert calls==[version]


def test_run_kind_executes_only_one_due_kind_with_actual_start(monkeypatch):
    calls=[]
    monkeypatch.setattr(model_learning, '_registry', lambda: SimpleNamespace(
        load_active=lambda sport:None,verify_active=lambda *a,**k:None,active=lambda sport:None))
    monkeypatch.setattr(model_learning, '_adapter', lambda sport: SimpleNamespace(
        build_candidate=lambda now:calls.append(('fit',sport)) or {'status':'waiting'},
        nightly_diagnostics=lambda now:calls.append(('diagnostics',sport)) or {'status':'ok'}))
    monkeypatch.setattr(model_learning, '_evidence_key', lambda sport:'fixture')
    monkeypatch.setattr(model_learning, '_near_kickoff', lambda now:False)
    result=model_learning.run_kind('NBA','diagnostics',now=1793188800.)
    assert result['status']=='ok' and calls==[('diagnostics','NBA')]


def test_dedicated_scheduler_never_uses_the_refresh_command():
    scheduler=importlib.import_module('edgeterminal.learning_scheduler')
    spec=scheduler.specification()
    assert 'edgeterminal.learning_worker' in spec['ProgramArguments']
    assert spec['StartInterval']==300 and spec['RunAtLoad']
    assert spec['EnvironmentVariables']['EDGETERMINAL_OFFLINE_LEARNER']=='1'
    assert not any('API_KEY' in name for name in spec['EnvironmentVariables'])
    assert spec['EnvironmentVariables']['DATABASE_URL']==settings.database_url
    assert spec['EnvironmentVariables']['NFLVERSE_CACHE_DIR']==str(settings.nflverse_cache_dir)
