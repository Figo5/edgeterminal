"""Always-on service checks use temporary state and never call providers."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from deploy import runtime


def state(tmp_path):
    (tmp_path / 'data').mkdir()
    con = sqlite3.connect(str(tmp_path / 'data/edgeterminal.db'))
    con.execute('CREATE TABLE research_refresh_ticks(started_at REAL,finished_at REAL,success INTEGER)')
    con.commit()
    con.close()
    return tmp_path


def test_worker_health_requires_recent_successful_real_tick(tmp_path):
    root = state(tmp_path)
    assert not runtime.worker_health(root, now=1000)['ok']
    con = sqlite3.connect(str(root / 'data/edgeterminal.db'))
    con.execute('INSERT INTO research_refresh_ticks VALUES(?,?,?)', (998, 999, 1))
    con.commit()
    assert runtime.worker_health(root, now=1000)['ok']
    assert not runtime.worker_health(root, now=3000)['ok']
    con.execute('INSERT INTO research_refresh_ticks VALUES(?,?,?)', (1000, 1000, 0))
    con.commit()
    assert not runtime.worker_health(root, now=1001)['ok']
    con.close()


def test_worker_health_rejects_future_tick(tmp_path):
    root = state(tmp_path)
    con = sqlite3.connect(str(root / 'data/edgeterminal.db'))
    con.execute('INSERT INTO research_refresh_ticks VALUES(1001,1001,1)')
    con.commit()
    con.close()
    assert not runtime.worker_health(root, now=1000)['ok']


def test_cycle_does_not_treat_a_contended_lock_as_success(tmp_path):
    command = [sys.executable, '-c', 'print(\'{"status":"already_running","steps":{}}\')']
    result = runtime.run_cycle(tmp_path, threading.Event(), command=command)
    assert result['status'] == 'already_running'
    assert result['success'] is False


def test_cycle_reports_only_sanitized_failures(tmp_path):
    command = [sys.executable, '-c', 'print("provider-secret-must-not-leak");raise SystemExit(2)']
    result = runtime.run_cycle(tmp_path, threading.Event(), command=command)
    assert not result['success']
    assert result['returncode'] == 2
    assert 'provider-secret' not in json.dumps(result)


def test_cycle_success_requires_research_designation(tmp_path):
    valid = {'success': True, 'designation': 'PASS', 'readiness': 'RESEARCH_ONLY', 'run_id': 'run1'}
    command = [sys.executable, '-c', 'print(' + repr(json.dumps(valid)) + ')']
    result = runtime.run_cycle(tmp_path, threading.Event(), command=command)
    assert result['success'] is True
    valid['designation'] = 'OFFICIAL'
    command = [sys.executable, '-c', 'print(' + repr(json.dumps(valid)) + ')']
    assert not runtime.run_cycle(tmp_path, threading.Event(), command=command)['success']
    command = [sys.executable, '-c', 'print("[]")']
    assert not runtime.run_cycle(tmp_path, threading.Event(), command=command)['success']


def test_stop_terminates_running_child_promptly(tmp_path):
    stopping = threading.Event()
    timer = threading.Timer(.15, stopping.set)
    timer.start()
    start = time.monotonic()
    result = runtime.run_cycle(tmp_path, stopping,
                               command=[sys.executable, '-c', 'import time;time.sleep(30)'])
    timer.join()
    assert result['status'] == 'stopped'
    assert time.monotonic() - start < 3


def test_preflight_rejects_checkout_dotenv_before_git_or_database(tmp_path):
    (tmp_path / '.env').write_text('SPORTSGAMEODDS_API_KEY=secret')
    with pytest.raises(ValueError, match='separate'):
        runtime.preflight(tmp_path, mode='web', environ={})


def test_preflight_rejects_unprotected_hosting(tmp_path):
    with pytest.raises(ValueError, match='HOSTED_MODE'):
        runtime.preflight(tmp_path, mode='web', environ={})
    with pytest.raises(ValueError, match='HTTPS'):
        runtime.preflight(tmp_path, mode='web', environ={
            'HOSTED_MODE': 'true', 'PUBLIC_URL': 'http://example.com', 'ODDS_PROVIDER': 'espn'})


def test_preflight_rejects_provider_key_in_web(tmp_path):
    with pytest.raises(ValueError, match='web'):
        runtime.preflight(tmp_path, mode='web', environ={
            'HOSTED_MODE': 'true', 'PUBLIC_URL': 'https://private.example.com',
            'ODDS_PROVIDER': 'espn', 'SPORTSGAMEODDS_API_KEY': 'never-log'})


def test_cycle_schedule_skips_missed_intervals():
    assert runtime.cycle_delay(1000, 1010, 300) == 290
    assert runtime.cycle_delay(1000, 1610, 300) == 290
    assert runtime.cycle_delay(1000, 1300, 300) == 0


def test_preflight_checks_actual_git_revision_and_dirty_model(tmp_path):
    root = state(tmp_path)
    (root / 'data/research').mkdir()
    (root / 'reports').mkdir()
    (root / 'edgeterminal').mkdir()
    (root / 'edgeterminal/model.py').write_text('MODEL = 1\n')
    for name in ('requirements.txt', 'requirements-model.txt'):
        (root / name).write_text('')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', 'edgeterminal', 'requirements.txt',
                    'requirements-model.txt'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
                    'commit', '-qm', 'fixture'], check=True)
    env = {'HOSTED_MODE': 'true', 'PUBLIC_URL': 'https://private.example.com', 'ODDS_PROVIDER': 'espn'}
    result = runtime.preflight(root, mode='web', environ=env)
    assert result['ok'] and len(result['commit']) == 40
    env['EDGETERMINAL_RUNTIME'] = 'always_on'
    with pytest.raises(ValueError, match='metadata'):
        runtime.preflight(root, mode='web', environ=env)
    env.pop('EDGETERMINAL_RUNTIME')
    env['EDGETERMINAL_IMAGE_REVISION'] = '0' * 40
    with pytest.raises(ValueError, match='revisions differ'):
        runtime.preflight(root, mode='web', environ=env)
    env.pop('EDGETERMINAL_IMAGE_REVISION')
    (root / 'json.py').write_text('SHADOW = True\n')
    with pytest.raises(ValueError, match='Commit'):
        runtime.preflight(root, mode='web', environ=env)
    (root / 'json.py').unlink()
    (root / 'edgeterminal/model.py').write_text('MODEL = 2\n')
    with pytest.raises(ValueError, match='Commit'):
        runtime.preflight(root, mode='web', environ=env)
