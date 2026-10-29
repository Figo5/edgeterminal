import pytest

from edgeterminal import db, research_scheduler
from edgeterminal.config import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close();db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path / "host.db"}')
    monkeypatch.setattr(settings,'runtime_mode','always_on')
    monkeypatch.setattr(research_scheduler,'_launchctl',lambda *a:pytest.fail('cloud status called launchctl'))
    db.init()
    yield
    db._conn.close();db._conn=None


def test_hosted_worker_uses_completed_tick_instead_of_launchd():
    db.ex('INSERT INTO research_refresh_ticks VALUES(?,?,?,?)',('success',900.,950.,1))
    status=research_scheduler.status(now=1000.)
    assert status['kind']=='always_on'
    assert status['loaded'] and status['last_worker_at']==950.


def test_failed_latest_tick_is_not_hidden_by_older_success():
    db.ex('INSERT INTO research_refresh_ticks VALUES(?,?,?,?)',('success',900.,950.,1))
    db.ex('INSERT INTO research_refresh_ticks VALUES(?,?,?,?)',('failed',960.,980.,0))
    assert research_scheduler.status(now=1000.)['loaded'] is False


def test_missing_stale_and_future_ticks_are_not_healthy():
    assert research_scheduler.status(now=1000.)['loaded'] is False
    db.ex('INSERT INTO research_refresh_ticks VALUES(?,?,?,?)',('old',100.,200.,1))
    assert research_scheduler.status(now=2000.)['loaded'] is False
    db.ex('INSERT INTO research_refresh_ticks VALUES(?,?,?,?)',('future',2000.,2100.,1))
    assert research_scheduler.status(now=2000.)['loaded'] is False
