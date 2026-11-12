#!/usr/bin/env python3
"""Always-on web/worker entry points, without changing research cadence guards.

The same clean checkout and local persistent state must be mounted in both
services. This controller does not fetch, grade, tune, or bypass worker locks.
It runs the normal CLI at most once per five-minute interval and records only
sanitized process status. An actual successful database tick is required for
worker health; an HTTP 200 or a skipped lock acquisition is insufficient.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

INTERVAL = 300
MAX_CYCLE_SECONDS = 900
MAX_LEARNING_SECONDS = 3 * 3600


def _git(root, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                          text=True, check=True, timeout=15, env=env).stdout.rstrip('\n')


def preflight(root, *, mode, environ=None):
    """Fail closed on unsafe hosting or a mismatched/uncommitted release."""
    root = Path(root).resolve()
    env = os.environ if environ is None else environ
    if (root / '.env').exists() or (root / '.env').is_symlink():
        raise ValueError('Keep secrets in separate service env files, not the mounted checkout .env')
    if env.get('HOSTED_MODE', '').lower() not in {'true', '1', 'yes'}:
        raise ValueError('HOSTED_MODE=true is required for the always-on service')
    url = urlsplit(env.get('PUBLIC_URL', ''))
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in {'', '/'}):
        raise ValueError('An HTTPS PUBLIC_URL with one origin is required')
    if env.get('ODDS_PROVIDER') != 'espn' or env.get('ODDS_API_KEY', ''):
        raise ValueError('Use ODDS_PROVIDER=espn and omit the unused Odds API key')
    if mode in {'web', 'learner'} and env.get('SPORTSGAMEODDS_API_KEY', ''):
        raise ValueError(f'The {mode} service must not receive the provider key')
    raw = env.get('DATABASE_URL', 'sqlite:///./data/edgeterminal.db')
    if not raw.startswith('sqlite:///'):
        raise ValueError('This deployment requires one local SQLite state directory')
    configured = Path(raw.split('///', 1)[1])
    if not configured.is_absolute():
        configured = root / configured
    expected = root / 'data/edgeterminal.db'
    if configured.resolve() != expected.resolve() or expected.is_symlink():
        raise ValueError('DATABASE_URL must identify the shared data/edgeterminal.db')
    if not expected.is_file():
        raise ValueError('Restore and verify existing state before starting the service')
    with expected.open('rb') as handle:
        if handle.read(16) != b'SQLite format 3\x00':
            raise ValueError('The restored database is not SQLite')
    for relative in ('data/research', 'reports'):
        path = root / relative
        if not path.is_dir() or path.is_symlink():
            raise ValueError('Persistent research and report directories are required')
    commit = _git(root, 'rev-parse', 'HEAD')
    dirty = _git(root, 'status', '--porcelain', '-z', '--untracked-files=all',
                 '--ignored=matching', '--', 'edgeterminal', 'requirements.txt',
                 'requirements-model.txt', 'deploy', ':(top,glob)*.py')
    changed = [r for r in dirty.split('\x00') if r and '__pycache__' not in Path(r[3:]).parts]
    if changed:
        raise ValueError('Commit the reviewed model and deployment code before release')
    image_commit = env.get('EDGETERMINAL_IMAGE_REVISION')
    requirements = env.get('EDGETERMINAL_IMAGE_REQUIREMENTS')
    if env.get('EDGETERMINAL_RUNTIME') == 'always_on' and (
            not isinstance(image_commit, str) or not re.fullmatch('[0-9a-f]{40}', image_commit)
            or not requirements):
        raise ValueError('Always-on releases require image revision and requirement metadata')
    if image_commit and image_commit != commit:
        raise ValueError('Runtime image and mounted checkout revisions differ')
    if requirements:
        manifest = json.loads(Path(requirements).read_text())
        for name in ('requirements.txt', 'requirements-model.txt', 'deploy/constraints.txt'):
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest.get(name):
                raise ValueError('Rebuild the image after a requirements change')
    return {'ok': True, 'commit': commit, 'mode': mode}


def worker_health(root, *, now=None, max_age=1500):
    stamp = time.time() if now is None else now
    database = Path(root) / 'data/edgeterminal.db'
    try:
        con = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            con.execute('PRAGMA query_only=ON')
            row = con.execute('SELECT started_at,finished_at,success FROM research_refresh_ticks '
                              'ORDER BY started_at DESC LIMIT 1').fetchone()
        finally:
            con.close()
        if not row:
            return {'ok': False, 'status': 'no_worker_tick'}
        age = stamp - row[1]
        valid = math.isfinite(age) and row[0] <= row[1] and 0 <= age <= max_age and row[2] == 1
        return {'ok': valid, 'status': 'current' if valid else 'stale_or_failed_tick',
                'age_seconds': round(age, 1), 'tick_success': bool(row[2])}
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {'ok': False, 'status': 'worker_state_unavailable'}


def learner_health(root, *, now=None, max_age=1500):
    """Learning heartbeat is independent of genuine source-worker ticks."""
    stamp=time.time() if now is None else now
    try:
        database=Path(root)/'data/edgeterminal.db'
        con=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
        try:
            con.execute('PRAGMA query_only=ON')
            row=con.execute("SELECT v FROM kv WHERE k='learning_worker_heartbeat'").fetchone()
        finally:
            con.close()
        at=json.loads(row[0])['checked_at'] if row else None
        healthy=isinstance(at,(int,float)) and not isinstance(at,bool) and math.isfinite(at) and 0<=stamp-at<=max_age
        return {'ok':healthy,'status':'current' if healthy else 'learning_heartbeat_stale'}
    except (OSError,sqlite3.Error,TypeError,ValueError,KeyError):
        return {'ok':False,'status':'learning_state_unavailable'}


def run_cycle(root, stopping, *, command=None, timeout=None, learning=False):
    timeout = (MAX_LEARNING_SECONDS if learning else MAX_CYCLE_SECONDS) if timeout is None else timeout
    command = command or [sys.executable, '-m', 'edgeterminal.learning_cli' if learning else 'edgeterminal.research_refresh', 'run']
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if learning:
        environment = {key: value for key, value in environment.items()
                       if not any(part in key.upper() for part in ('KEY', 'SECRET', 'PASSWORD', 'TOKEN', 'CREDENTIAL'))}
        environment.update(EDGETERMINAL_OFFLINE_LEARNER='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                           MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1', VECLIB_MAXIMUM_THREADS='1')
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=str(root), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               start_new_session=True,
                               env=environment)
    while True:
        if stopping.is_set() or time.monotonic() - started > timeout:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            return {'status': 'stopped' if stopping.is_set() else 'timeout',
                    'success': False, 'returncode': process.returncode}
        try:
            output, _ = process.communicate(timeout=.1)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        if len(output) > 2 * 1024 * 1024:
            raise ValueError('oversized status')
        payload = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if payload.get('status') == 'already_running':
        return {'status': 'already_running', 'success': False, 'returncode': process.returncode}
    success = (process.returncode == 0 and payload.get('success') is True
               and payload.get('designation') == 'PASS' and payload.get('readiness') == 'RESEARCH_ONLY')
    # Never copy arbitrary child stdout, exception text, URLs, or credentials to health/logs.
    return {'status': 'completed' if success else 'failed', 'success': success,
            'returncode': process.returncode}


def cycle_delay(started, current, interval=INTERVAL):
    elapsed = max(0, current - started)
    return max(0, max(1, math.ceil(elapsed / interval)) * interval - elapsed)


def _heartbeat(root, status, *, learning=False):
    directory = Path(root) / 'data/runtime'
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('Runtime status directory must not be a symlink')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='worker-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(dict(status, checked_at=time.time()), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / ('learning.json' if learning else 'worker.json'))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def serve_worker(root, *, once=False, learning=False):
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    directory = Path(root) / 'data/runtime'
    directory.mkdir(parents=True, exist_ok=True)
    parent=os.open(str(directory),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        fd=os.open('learning-controller.lock' if learning else 'worker-controller.lock',
                   os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('Controller lock must be a regular file')
    with os.fdopen(fd,'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stopping.is_set():
            started = time.monotonic()
            # A host-side checkout must not switch the code of the next child.
            try:
                preflight(root, mode='learner' if learning else 'worker')
            except (ValueError, OSError, subprocess.SubprocessError):
                _heartbeat(root, {'status': 'release_changed', 'success': False}, learning=learning)
                return 2
            _heartbeat(root, {'status': 'running'}, learning=learning)
            try:
                result = run_cycle(root, stopping, learning=learning)
            except OSError:
                result = {'status': 'launch_failed', 'success': False}
            _heartbeat(root, result, learning=learning)
            print(json.dumps(result, sort_keys=True), flush=True)
            if once:
                return 0 if result['success'] else 2
            stopping.wait(cycle_delay(started, time.monotonic()))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('web', 'worker', 'learner', 'check-worker', 'check-learner', 'check-web', 'preflight'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.mode == 'check-worker':
        result = worker_health(args.root)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['ok'] else 1
    if args.mode == 'check-learner':
        result = learner_health(args.root)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['ok'] else 1
    if args.mode == 'check-web':
        try:
            host = urlsplit(os.environ['PUBLIC_URL']).netloc
            request = Request('http://127.0.0.1:8799/api/health', headers={'Host': host})
            with urlopen(request, timeout=5) as response:
                return 0 if response.status == 200 else 1
        except (OSError, KeyError, ValueError):
            return 1
    try:
        result = preflight(args.root, mode='web' if args.mode == 'web' else 'learner' if args.mode == 'learner' else 'worker')
        if args.mode == 'preflight':
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.mode == 'worker':
            return serve_worker(args.root, once=args.once)
        if args.mode == 'learner':
            return serve_worker(args.root, once=args.once, learning=True)
        os.chdir(args.root)
        os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'edgeterminal.api:app',
                                '--host', '0.0.0.0', '--port', '8799', '--workers', '1'])
    except (ValueError, OSError, subprocess.SubprocessError):
        # The preflight error type is safe to expose; exception messages may contain a secret path.
        print(json.dumps({'ok': False, 'status': 'preflight_failed'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
