"""Offline learning jobs, independent of the capture/grading worker lifecycle.

Only this command executes heavy learning. A local OS lock owns the complete
job; heartbeat age never authorizes a second fitter. SQLite transactions contain
only short claim/status writes. Publication has its own verified short lock.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import json
import math
import os
import resource
import re
import signal
import socket
import stat
import threading
import time
import uuid


def offline_environment(environ=None):
    """Remove inherited credentials before configuration or numeric imports."""
    result = {k: v for k, v in (os.environ if environ is None else environ).items()
              if not any(part in k.upper() for part in ('KEY', 'SECRET', 'PASSWORD', 'TOKEN', 'CREDENTIAL'))}
    result.update(EDGETERMINAL_OFFLINE_LEARNER='1', PYTHONDONTWRITEBYTECODE='1',
                  OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1',
                  NUMEXPR_NUM_THREADS='1', VECLIB_MAXIMUM_THREADS='1')
    return result


if __name__ == '__main__':
    _early_environment = offline_environment()
    os.environ.clear()
    os.environ.update(_early_environment)

from . import db  # noqa: E402 — CLI environment is sanitized before config imports.
from .config import settings  # noqa: E402

COMPONENTS = {'NBA': ('diagnostics', 'retuning'), 'NFL': ('diagnostics', 'retuning'),
              'NBA_SPREAD': ('retuning',), 'NBA_TOTAL': ('diagnostics', 'retuning'),
              'NFL_SPREAD': ('retuning',), 'NFL_WEEKLY': ('retuning',)}
HEARTBEAT_SECONDS = 30
MAX_JOB_WALL_SECONDS = 3 * 3600
MAX_JOB_CPU_SECONDS = 3600
_CURRENT_JOB = ContextVar('learning_current_job', default=None)


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _stamp(value):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('learning time must be finite')
    return float(value)


def _catalog(component, kind):
    if component not in COMPONENTS or kind not in COMPONENTS[component]:
        raise ValueError('unsupported learning action')


def ensure_schema():
    db.ex('CREATE TABLE IF NOT EXISTS learning_jobs ('
          'id INTEGER PRIMARY KEY,component TEXT NOT NULL,kind TEXT NOT NULL,queued_at REAL NOT NULL,'
          'state TEXT NOT NULL,started_at REAL,finished_at REAL,heartbeat_at REAL,owner TEXT,'
          'elapsed_seconds REAL,cpu_seconds REAL,detail TEXT NOT NULL)')
    db.ex("CREATE UNIQUE INDEX IF NOT EXISTS learning_one_pending ON learning_jobs(component,kind) "
          "WHERE state IN ('queued','running')")
    db.ex('CREATE TABLE IF NOT EXISTS learning_job_events ('
          'id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES learning_jobs(id),'
          'recorded_at REAL NOT NULL,state TEXT NOT NULL,detail TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        db.ex(f'CREATE TRIGGER IF NOT EXISTS learning_events_no_{action.lower()} BEFORE {action} '
              "ON learning_job_events BEGIN SELECT RAISE(ABORT,'learning job events are append-only'); END")


def _event(con, job_id, state, now, detail=None):
    con.execute('INSERT INTO learning_job_events(job_id,recorded_at,state,detail) VALUES(?,?,?,?)',
                (job_id, now, state, _json(detail or {})))


def enqueue(component, kind, now=None):
    """Idempotent dispatch only: never fit, verify a full dataset or fetch."""
    _catalog(component, kind)
    stamp = _stamp(now)
    ensure_schema()
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM learning_jobs WHERE component=? AND kind=? AND state IN ('queued','running')",
                          (component, kind)).fetchone()
        if row:
            return dict(row)
        old = con.execute("SELECT state,detail FROM learning_jobs WHERE component=? AND kind=? "
                          'ORDER BY id DESC LIMIT 1', (component, kind)).fetchone()
        previous = json.loads(old['detail']) if old and old['state'] in ('failed', 'deferred') else {}
        checkpoint = {'candidate_id': previous['candidate_id']} if previous.get('candidate_id') else {}
        cur = con.execute('INSERT INTO learning_jobs(component,kind,queued_at,state,detail) VALUES(?,?,?,\'queued\',?)',
                          (component, kind, stamp, _json(checkpoint)))
        _event(con, cur.lastrowid, 'queued', stamp)
        return dict(con.execute('SELECT * FROM learning_jobs WHERE id=?', (cur.lastrowid,)).fetchone())


def _recover(now):
    # The caller owns the OS lock, so no previous process can still execute.
    with db.tx() as con:
        for row in con.execute("SELECT * FROM learning_jobs WHERE state='running'").fetchall():
            _event(con, row['id'], 'interrupted', now,
                   {'started_at': row['started_at'], 'message': 'Previous learner stopped before completion.'})
            con.execute("UPDATE learning_jobs SET state='queued',owner=NULL,heartbeat_at=NULL WHERE id=?", (row['id'],))
            _event(con, row['id'], 'queued', now, {'reason': 'automatic_interruption_recovery'})


def _claim(now, owner):
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM learning_jobs WHERE state='queued' "
                          "ORDER BY CASE kind WHEN 'diagnostics' THEN 0 ELSE 1 END,queued_at,id LIMIT 1").fetchone()
        if row is None:
            return None
        con.execute("UPDATE learning_jobs SET state='running',started_at=?,finished_at=NULL,heartbeat_at=?,owner=? WHERE id=?",
                    (now, now, owner, row['id']))
        _event(con, row['id'], 'running', now)
        return dict(con.execute('SELECT * FROM learning_jobs WHERE id=?', (row['id'],)).fetchone())


def _heartbeat(job_id=None, owner=None):
    stamp = time.time()
    with db.tx() as con:
        if job_id is not None:
            con.execute("UPDATE learning_jobs SET heartbeat_at=? WHERE id=? AND owner=? AND state='running'",
                        (stamp, job_id, owner))
        con.execute('INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) ON CONFLICT(k) '
                    'DO UPDATE SET v=excluded.v,updated_at=excluded.updated_at',
                    ('learning_worker_heartbeat', _json({'checked_at': stamp}), stamp))


@contextmanager
def offline_runtime():
    """Learning consumes archived evidence; attempted network access fails closed."""
    def denied(*args, **kwargs):
        raise RuntimeError('Learning is offline; provider access is disabled')
    saved_env = dict(os.environ)
    os.environ.clear(); os.environ.update(offline_environment(saved_env))
    saved = [(socket, name, getattr(socket, name)) for name in ('create_connection', 'getaddrinfo')]
    saved += [(socket.socket, name, getattr(socket.socket, name)) for name in ('connect', 'connect_ex', 'sendto')]
    credentials = {name: getattr(settings, name) for name in ('openai_api_key', 'odds_api_key',
                   'sportsgameodds_api_key', 'tavily_api_key', 'brave_api_key', 'smtp_password')}
    try:
        for obj, name, _ in saved:
            setattr(obj, name, denied)
        for name in credentials:
            setattr(settings, name, '')
        yield
    finally:
        for obj, name, original in saved:
            setattr(obj, name, original)
        for name, original in credentials.items():
            setattr(settings, name, original)
        os.environ.clear(); os.environ.update(saved_env)


class LearningDeadlineExceeded(RuntimeError):
    """An isolated job exceeded its own resource budget."""


@contextmanager
def execution_limits(*, wall_seconds=MAX_JOB_WALL_SECONDS, cpu_seconds=None):
    """CLI-only limits: the normal refresh controller remains at 900 seconds."""
    def expired(*_):
        raise LearningDeadlineExceeded('Offline learning deadline exceeded')
    previous = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, expired)
    previous_cpu = None
    if cpu_seconds is not None:
        previous_cpu = signal.getsignal(signal.SIGXCPU)
        signal.signal(signal.SIGXCPU, expired)
        used = math.ceil(time.process_time())
        old_soft, old_hard = resource.getrlimit(resource.RLIMIT_CPU)
        soft = used + int(cpu_seconds)
        if old_soft != resource.RLIM_INFINITY:
            soft = min(soft, old_soft)
        hard = soft + 30 if old_hard == resource.RLIM_INFINITY else min(soft + 30, old_hard)
        resource.setrlimit(resource.RLIMIT_CPU, (min(soft, hard), hard))
    signal.setitimer(signal.ITIMER_REAL, float(wall_seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_cpu is not None:
            signal.signal(signal.SIGXCPU, previous_cpu)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _execute(job):
    _catalog(job['component'], job['kind'])
    checkpoint = json.loads(job['detail']).get('candidate_id')
    if job['component'] == 'NBA_TOTAL':
        if checkpoint:
            from . import nba_totals_learning
            return nba_totals_learning.resume_publication(checkpoint)
        from . import nba_totals_stages
        return (nba_totals_stages.run_diagnostics() if job['kind'] == 'diagnostics' else
                nba_totals_stages.run_fitting())
    if job['component'] == 'NFL_SPREAD':
        from . import nfl_spread_learning
        return nfl_spread_learning.run_fitting()
    if job['component'] == 'NFL_WEEKLY':
        from . import nfl_weekly_preparation
        return nfl_weekly_preparation.run_job(job)
    if job['component'] == 'NBA_SPREAD':
        from . import nba_spread_learning
        return (nba_spread_learning.resume_publication(checkpoint) if checkpoint else
                nba_spread_learning.run_fitting())
    from . import model_learning
    if checkpoint:
        return model_learning.resume_candidate(job['component'], checkpoint)
    return model_learning.run_kind(job['component'], job['kind'])


def checkpoint_candidate(connection, candidate_id):
    """Called inside proposal registration's transaction; never fits or publishes."""
    job = _CURRENT_JOB.get()
    if job is None:
        return
    totals_diagnostic = job['component'] == 'NBA_TOTAL' and job['kind'] == 'diagnostics'
    if (job['kind'] != 'retuning' and not totals_diagnostic) or job['component'] not in COMPONENTS:
        raise ValueError('only the owning fit can checkpoint its candidate')
    version_tables = {'NBA_SPREAD': 'nba_spread_model_candidates', 'NBA_TOTAL': 'nba_totals_candidates',
                      'NFL_SPREAD': 'nfl_spread_candidates'}
    if job['component'] in version_tables:
        table = version_tables[job['component']]
        valid = (isinstance(candidate_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', candidate_id)
                 and connection.execute('SELECT 1 FROM ' + table + ' WHERE version=?', (candidate_id,)).fetchone())
    else:
        valid = (isinstance(candidate_id, str) and len(candidate_id) == 64 and
                 all(c in '0123456789abcdef' for c in candidate_id))
    if not valid:
        raise ValueError('invalid checkpoint candidate')
    row = connection.execute("SELECT detail FROM learning_jobs WHERE id=? AND owner=? AND state='running'",
                             (job['id'], job['owner'])).fetchone()
    if row is None:
        raise ValueError('learning checkpoint ownership changed')
    detail = dict(json.loads(row['detail']), candidate_id=candidate_id)
    connection.execute('UPDATE learning_jobs SET detail=? WHERE id=?', (_json(detail), job['id']))


def _finish(job, owner, state, detail, elapsed, cpu):
    finished = max(job['started_at'], time.time())
    # Store only structured status/timing here; full validated diagnostic output
    # remains in its existing append-only research ledger.
    safe = {'result_status': detail.get('status', 'ok'), 'error_type': detail.get('error_type'),
            'message': ('Learning job failed; the next due attempt retries.' if state == 'failed' else
                        'Publication deferred; source work takes priority.' if state == 'deferred' else
                        'Learning job completed; saved predictions remain unchanged.')}
    with db.tx() as con:
        current = con.execute('SELECT detail FROM learning_jobs WHERE id=? AND owner=?', (job['id'], owner)).fetchone()
        if current:
            checkpoint = json.loads(current['detail']).get('candidate_id')
            if checkpoint:
                safe['candidate_id'] = checkpoint
        cur = con.execute('UPDATE learning_jobs SET state=?,finished_at=?,heartbeat_at=?,elapsed_seconds=?,'
                          'cpu_seconds=?,detail=? WHERE id=? AND owner=? AND state=\'running\'',
                          (state, finished, finished, elapsed, cpu, _json(safe), job['id'], owner))
        if cur.rowcount != 1:
            raise ValueError('learning job ownership changed')
        _event(con, job['id'], state, finished, safe)


def run_once(now=None):
    """One recoverable offline job. Does not acquire the research lock to fit."""
    ensure_schema()
    stamp = _stamp(now)
    path = settings.db_path.with_name(settings.db_path.name + '-learning.lock')
    directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('learning lock must be a regular file')
    with os.fdopen(fd, 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running', 'success': False}
        _recover(stamp)
        _heartbeat()
        owner = uuid.uuid4().hex
        job = _claim(stamp, owner)
        if job is None:
            return {'status': 'idle', 'success': True, 'designation': 'PASS', 'readiness': 'RESEARCH_ONLY'}
        stopped = threading.Event()
        def beat():
            while not stopped.wait(HEARTBEAT_SECONDS):
                try:
                    _heartbeat(job['id'], owner)
                except Exception:
                    # The OS lock remains authoritative during database contention.
                    pass
        pulse = threading.Thread(target=beat, name='learning-heartbeat', daemon=True)
        pulse.start()
        monotonic, cpu_start = time.monotonic(), time.process_time()
        token = _CURRENT_JOB.set(job)
        try:
            with offline_runtime():
                detail = _execute(job)
            if not isinstance(detail, dict):
                raise ValueError('learning returned no structured result')
            _json(detail)
            state = ('failed' if detail.get('status') == 'error' or detail.get('ok') is False else
                     'deferred' if detail.get('status') == 'deferred' else 'completed')
        except Exception as exc:
            detail = {'status': 'error', 'error_type': type(exc).__name__}
            state = 'failed'
        finally:
            _CURRENT_JOB.reset(token)
            stopped.set(); pulse.join(timeout=1)
        _finish(job, owner, state, detail, time.monotonic() - monotonic, time.process_time() - cpu_start)
        return {'status': state, 'success': state != 'failed', 'job_id': job['id'],
                'designation': 'PASS', 'readiness': 'RESEARCH_ONLY'}


def status(component=None, *, now=None):
    """Read job health; queuing a retry cannot erase an earlier failure."""
    stamp = _stamp(now)
    if component is not None and component not in COMPONENTS:
        raise ValueError('unsupported learning component')
    exists = db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_jobs'")
    if component in ('NBA', 'NFL'):
        args = ('NBA', 'NBA_SPREAD', 'NBA_TOTAL') if component == 'NBA' else ('NFL', 'NFL_SPREAD', 'NFL_WEEKLY')
        where = ' WHERE component IN (?,?,?)'
    else:
        where, args = (' WHERE component=?', (component,)) if component else ('', ())
    rows = db.q('SELECT * FROM learning_jobs' + where + ' ORDER BY id DESC LIMIT 12', args) if exists else []
    jobs = []
    for row in rows:
        detail = json.loads(row['detail'])
        jobs.append({**{key: row[key] for key in ('id', 'component', 'kind', 'queued_at', 'started_at',
                     'finished_at', 'heartbeat_at', 'elapsed_seconds', 'cpu_seconds')},
                     'status': row['state'], 'result_status': detail.get('result_status'),
                     'message': detail.get('message', 'Learning job queued.' if row['state'] == 'queued' else 'Learning job running.')})
    latest = {}
    if exists:
        for row in db.q('SELECT j.component,j.kind,e.state FROM learning_job_events e JOIN learning_jobs j ON j.id=e.job_id '
                        "WHERE e.state IN ('completed','failed','interrupted')" +
                        (' AND j.component IN (?,?,?)' if component in ('NBA', 'NFL') else ' AND j.component=?' if component else '') +
                        ' ORDER BY e.id', args):
            latest[row['component'], row['kind']] = row['state']
    heartbeat = db.kv_get('learning_worker_heartbeat') or {}
    at = heartbeat.get('checked_at')
    running = any(row['state'] == 'running' for row in rows)
    state = ('not_checked' if at is None else 'stale' if stamp - at > 25 * 60 else
             'running' if running else 'idle')
    overdue = any(row['state'] == 'queued' and stamp - row['queued_at'] > 25 * 60 for row in rows)
    return {'worker': {'state': state, 'last_heartbeat_at': at}, 'jobs': jobs,
            'needs_attention': state == 'stale' or overdue or any(value in ('failed', 'interrupted') for value in latest.values())}


def refresh_checks(now=None):
    from . import model_learning
    return model_learning.refresh_checks(now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'status'), nargs='?', default='run')
    args = parser.parse_args()
    if args.action == 'run':
        def stopping(*_):
            # Unwind atomic-file/transaction contexts. The durable running claim
            # is intentionally recovered as interrupted by the next OS owner.
            raise SystemExit(143)
        signal.signal(signal.SIGTERM, stopping)
        try:
            os.nice(10)
        except OSError:
            pass
    if args.action == 'run':
        with execution_limits(cpu_seconds=MAX_JOB_CPU_SECONDS):
            result = run_once()
    else:
        result = status()
    print(_json(result))
    return 2 if result.get('success') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
