"""Current health must not hide live failures or resurrect expired windows."""
import json

import httpx
import pytest

from edgeterminal import db, nfl_prop_result_ingest, product_insights, visit_refresh
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "health.db"}')
    monkeypatch.setattr(settings, 'odds_provider', 'espn')
    monkeypatch.setattr(settings, 'demo_mode', False)
    db.init()
    yield
    db._conn.close()
    db._conn = None


def step(name, stamp, success=False, error=''):
    db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) '
          'VALUES(?,?,?,?,?)', (str(stamp), name, stamp, int(success), json.dumps({'error': error})))


def test_expired_kickoff_gap_is_history_but_grading_read_error_stays_current(isolated):
    step('near_kickoff_market', 100.)
    step('top_five_grading', 200., error='ValueError: freeze: ReadError')
    steps = db.q('SELECT name,attempted_at,success,detail FROM research_refresh_steps')
    assert len(product_insights.unresolved_failures(steps, 999.)) == 2
    assert [r['name'] for r in product_insights.unresolved_failures(steps, 1000.)] == ['top_five_grading']
    checks = {r['name']: r for r in product_insights.update_checks(steps, 1000.)}
    assert checks['near_kickoff_market']['state'] == 'past_window'
    assert checks['top_five_grading']['state'] == 'retry_pending'
    assert checks['top_five_grading']['next_retry_at'] == 200 + 6 * 3600


def test_page_warning_does_not_duplicate_step_error_and_recovery_clears_it(isolated):
    step('top_five_grading', 900., error='ReadError')
    db.kv_set(visit_refresh.KEY, {'ok': False, 'attention': 'updates', 'started_at': 900.})
    h = product_insights.health(1000.)
    assert any('Player-prop grading:' in x for x in h['warnings'])
    assert not any('page-triggered' in x for x in h['warnings'])
    step('top_five_grading', 950., True)
    h = product_insights.health(1000.)
    assert not any('grading:' in x or 'page-triggered' in x for x in h['warnings'])
    assert visit_refresh.check(1000.)['ok']


def test_independent_page_failure_is_kept_until_successful_worker(isolated):
    db.kv_set(visit_refresh.KEY, {'ok': False, 'checked_at': 900.})
    assert any('page-triggered' in x for x in product_insights.health(1000.)['warnings'])
    db.ex('INSERT INTO research_refresh_ticks VALUES(?,?,?,?)', ('ok', 950., 951., 1))
    assert not any('page-triggered' in x for x in product_insights.health(1000.)['warnings'])


def test_result_source_retries_transport_once_without_using_cached_bytes(monkeypatch):
    calls = []
    class Source:
        def get(self, url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise httpx.ReadError('connection lost')
            return httpx.Response(200, content=b'fresh', request=httpx.Request('GET', url))
    monkeypatch.setattr(nfl_prop_result_ingest, 'client', lambda: Source())
    assert nfl_prop_result_ingest._fetch('https://example.test/result') == b'fresh'
    assert len(calls) == 2


def test_result_source_stops_after_two_transport_failures(monkeypatch):
    calls = []
    class Source:
        def get(self, url, **kwargs):
            calls.append(url)
            raise httpx.ReadError('still unavailable')
    monkeypatch.setattr(nfl_prop_result_ingest, 'client', lambda: Source())
    with pytest.raises(httpx.ReadError):
        nfl_prop_result_ingest._fetch('https://example.test/result')
    assert len(calls) == 2


def test_result_source_never_retries_http_rejection(monkeypatch):
    calls = []
    class Source:
        def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(429, request=httpx.Request('GET', url))
    monkeypatch.setattr(nfl_prop_result_ingest, 'client', lambda: Source())
    with pytest.raises(httpx.HTTPStatusError):
        nfl_prop_result_ingest._fetch('https://example.test/result')
    assert len(calls) == 1
