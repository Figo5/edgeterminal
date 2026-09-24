"""Refresh cadence and quota guards without external requests."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from edgeterminal import db, research_refresh, research_scheduler
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'refresh.db'}")
    monkeypatch.setattr(research_refresh, "LOCK", tmp_path / "refresh.lock")
    db.init()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_idle_refresh_is_idempotent_and_does_not_call_paid_provider(isolated, monkeypatch):
    calls = {"schedule": 0}
    def schedule(_sport):
        calls["schedule"] += 1
        return {"ok": True, "events": 0}
    monkeypatch.setattr(research_refresh.engine, "refresh_schedule", schedule)
    monkeypatch.setattr(research_refresh, "_events", lambda _now: [])
    monkeypatch.setattr(research_refresh, "_availability", lambda *_args: {"games": 0, "observations": 0})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, "capture",
                        lambda *_args, **_kwargs: pytest.fail("paid provider called"))
    first = research_refresh.run(now=1000)
    second = research_refresh.run(now=1001)
    assert first["success"] and second["success"]
    assert calls["schedule"] == 1
    assert second["status"] == "no_work_due"
    assert db.q1("SELECT COUNT(*) n FROM research_refresh_runs")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM paper_accounts")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM paper_positions")["n"] == 0
    assert research_refresh.status(now=1001)["last_successful_research_refresh"] is not None


def test_prop_capture_waits_for_frozen_projection(isolated, monkeypatch):
    event = {"id": "nfl-2026-reg-04-atl-gb", "kickoff_utc": 100000.0,
             "start_time": "1970-01-02T03:46:40Z", "meta": {}}
    monkeypatch.setattr(research_refresh, "_events", lambda _now: [event])
    monkeypatch.setattr(research_refresh, "_availability", lambda *_args: {"games": 1, "observations": 0})
    monkeypatch.setattr(research_refresh.engine, "refresh_schedule", lambda _sport: {"ok": True})
    monkeypatch.setattr(settings, "sportsgameodds_api_key", "fixture-key")
    calls = []
    def fail_capture(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("provider called without frozen projection")
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, "capture", fail_capture)
    monkeypatch.setattr(research_refresh.nfl_forward_weekly, "run", lambda **_kwargs: {"freeze": "fixture"})
    monkeypatch.setattr(research_refresh.nfl_market_capture, "capture_week",
                        lambda *_args, **_kwargs: {"target_games": 1, "rows_stored": 2})
    first = research_refresh.run(now=20000)
    second = research_refresh.run(now=20001)
    assert first["success"] and second["success"]
    assert calls == []
    assert research_refresh.interval(99900, [event]) == 300


def test_concurrent_read_endpoints_share_sqlite_safely(isolated):
    with ThreadPoolExecutor(max_workers=4) as workers:
        outputs = list(workers.map(lambda _: (research_refresh.status(now=1000),
                                               db.q1("SELECT COUNT(*) AS n FROM events")),
                                   range(80)))
    assert all(status["designation"] == "PASS" and row["n"] == 0
               for status, row in outputs)


def test_launchagent_runs_background_worker_without_terminal():
    spec = research_scheduler.specification()
    assert spec["ProgramArguments"][1:] == ["-m", "edgeterminal.research_refresh", "run"]
    assert spec["RunAtLoad"] is True and spec["StartInterval"] == 300
    assert spec["WorkingDirectory"].endswith("/edgeterminal")
    assert spec["StandardErrorPath"].endswith("research-refresh.err.log")
