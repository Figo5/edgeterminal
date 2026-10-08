"""Visitor requests reuse current work and cannot bypass source/cadence guards."""
import json

import pytest
from fastapi.testclient import TestClient

from edgeterminal import db, research_refresh, service_ops, visit_refresh
from edgeterminal.api import app
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "visit.db"}')
    monkeypatch.setattr(settings, 'odds_provider', 'espn')
    monkeypatch.setattr(settings, 'demo_mode', False)
    monkeypatch.setattr(service_ops, 'maintain_local', lambda: None)
    db.init()
    yield
    db._conn.close()
    db._conn = None


def test_requests_share_cooldown_and_never_force_quotes(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(research_refresh, 'run', lambda **kwargs: calls.append(kwargs) or
                        {'success': True, 'steps': {'schedule': {'secret': 'private'}}})
    assert visit_refresh.check(1000)['did_work']
    assert visit_refresh.check(1001)['status'] == 'cached'
    assert visit_refresh.check(1299)['status'] == 'cached'
    assert visit_refresh.check(1300)['did_work']
    assert calls == [{'record_tick': True}, {'record_tick': True}]
    assert 'private' not in json.dumps(db.kv_get(visit_refresh.KEY))


def test_recent_scheduled_worker_is_reused(isolated, monkeypatch):
    db.ex('INSERT INTO research_refresh_ticks(run_id,started_at,finished_at,success) VALUES(?,?,?,?)',
          ('tick', 900., 901., 1))
    monkeypatch.setattr(research_refresh, 'run', lambda **kw: pytest.fail('worker called twice'))
    assert visit_refresh.check(1000) == {'ok': True, 'status': 'cached', 'did_work': False, 'checked_at': 900.}


def test_weekly_freeze_failure_is_specific_and_survives_idle_checks(isolated, monkeypatch):
    db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) '
          'VALUES(?,?,?,?,?)', ('failed', 'weekly_freeze_2026_05', 900., 0,
                               json.dumps({'error': 'private local details'})))
    # A successful idle tick does not resolve an outstanding snapshot failure.
    db.ex('INSERT INTO research_refresh_ticks(run_id,started_at,finished_at,success) VALUES(?,?,?,?)',
          ('idle', 950., 951., 1))
    monkeypatch.setattr(research_refresh, 'run', lambda **kw: pytest.fail('worker called twice'))
    result = visit_refresh.check(1000)
    assert not result['ok'] and result['attention'] == 'weekly_forecast'
    assert result['status'] == 'needs_attention' and not result['did_work']
    assert 'private' not in json.dumps(result)


def test_source_failure_is_not_described_as_only_a_snapshot_issue(isolated, monkeypatch):
    monkeypatch.setattr(research_refresh, 'run', lambda **kw: {'success': False, 'steps': {
        'weekly_freeze_2026_05': {'ok': False}, 'current_game_odds': {'ok': False}}})
    result = visit_refresh.check(1000)
    assert not result['ok'] and result['attention'] == 'updates'


def test_snapshot_warning_clears_after_successful_retry(isolated, monkeypatch):
    db.kv_set(visit_refresh.KEY, {'ok': False, 'status': 'needs_attention',
                                'attention': 'weekly_forecast', 'started_at': 900.})
    db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) '
          'VALUES(?,?,?,?,?)', ('retry', 'weekly_freeze_2026_05', 950., 1, '{}'))
    monkeypatch.setattr(research_refresh, 'run', lambda **kw: pytest.fail('cooldown bypassed'))
    result = visit_refresh.check(1000)
    assert result['ok'] and result['status'] == 'cached'
    assert 'attention' not in result


def test_concurrent_request_observes_claim(isolated, monkeypatch):
    def worker(**kwargs):
        assert visit_refresh.check(1001)['status'] == 'busy'
        return {'success': True, 'steps': {}}
    monkeypatch.setattr(research_refresh, 'run', worker)
    assert visit_refresh.check(1000)['status'] == 'checked'


@pytest.mark.parametrize('provider,demo', [('auto', False), ('theoddsapi', False), ('espn', True)])
def test_source_modes_fail_before_worker(isolated, monkeypatch, provider, demo):
    monkeypatch.setattr(settings, 'odds_provider', provider)
    monkeypatch.setattr(settings, 'demo_mode', demo)
    monkeypatch.setattr(research_refresh, 'run', lambda **kw: pytest.fail('provider reached'))
    with pytest.raises(ValueError, match='ESPN'):
        visit_refresh.check(1000)


def test_failure_is_sanitized_and_throttled(isolated, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError('private provider credential')
    monkeypatch.setattr(research_refresh, 'run', broken)
    failed = visit_refresh.check(1000)
    assert not failed['ok'] and failed['status'] == 'needs_attention'
    assert 'private' not in json.dumps(failed)
    assert not visit_refresh.check(1001)['ok']


def test_hosted_visit_allowed_with_same_origin_but_legacy_force_stays_private(isolated, monkeypatch):
    monkeypatch.setattr(settings, 'hosted_mode', True)
    monkeypatch.setattr(settings, 'public_url', 'https://edge.example.com')
    calls = []
    monkeypatch.setattr(visit_refresh, 'check', lambda: calls.append(1) or
                        {'ok': True, 'status': 'cached', 'did_work': False})
    client = TestClient(app, base_url=settings.public_url)
    assert client.post('/api/product/nfl/check').status_code == 403
    assert client.post('/api/product/nfl/check', headers={'Origin': 'https://other.example.com'}).status_code == 403
    assert not calls
    response = client.post('/api/product/nfl/check', headers={'Origin': settings.public_url})
    assert response.status_code == 200 and response.json()['ok']
    assert calls == [1]
    assert client.post('/api/product/nfl/refresh', headers={'Origin': settings.public_url}).status_code == 403
