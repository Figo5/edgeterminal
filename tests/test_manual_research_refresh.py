"""The product Refresh endpoint delegates to the guarded research worker."""
import hashlib

import pytest
from fastapi.testclient import TestClient

from edgeterminal import db, research_refresh
from edgeterminal.api import app
from edgeterminal.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'refresh.db'}")
    monkeypatch.setattr(research_refresh, "LOCK", tmp_path / "refresh.lock")
    monkeypatch.setattr(research_refresh, "ROOT", tmp_path)
    db.init()
    yield TestClient(app), tmp_path
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_endpoint_returns_concise_success_and_busy(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(research_refresh, "run", lambda **_kwargs: {
        "success": True, "steps": {"schedule": {"ok": True, "result": {"secret": "hidden"}},
                                     "props": {"ok": True, "result": {"api_key": "hidden"}}}})
    body = http.post("/api/research/refresh").json()
    assert body["ok"] and body["did_work"]
    assert body["steps"] == {"schedule": True, "props": True}
    assert "hidden" not in str(body)
    assert body["updated_at"].endswith("+00:00")
    monkeypatch.setattr(research_refresh, "run", lambda **_kwargs: {"status": "already_running", "steps": {}})
    busy = http.post("/api/research/refresh").json()
    assert busy["ok"] and busy["busy"] and not busy["did_work"]


def test_noop_respects_cooldown_and_preserves_freeze_and_paper(client, monkeypatch):
    http, root = client
    artifact = root / "reports/nfl_model_v2/forward_2026_week03.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"immutable research freeze fixture\n")
    before = hashlib.sha256(artifact.read_bytes()).hexdigest()
    calls = []
    monkeypatch.setattr(research_refresh, "_events", lambda _now: [])
    monkeypatch.setattr(research_refresh.engine, "refresh_schedule", lambda sport: calls.append(sport) or
                        {"ok": True, "events": 0})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, "capture",
                        lambda *a, **k: pytest.fail("paid provider called"))
    first = http.post("/api/research/refresh")
    second = http.post("/api/research/refresh")
    assert first.status_code == second.status_code == 200
    assert first.json()["did_work"] and not second.json()["did_work"]
    assert calls == ["NFL"]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == before
    assert db.q1("SELECT COUNT(*) n FROM paper_positions")["n"] == 0
    assert db.q1("SELECT COUNT(*) n FROM research_refresh_ticks")["n"] == 0


def test_provider_failure_is_clean_and_does_not_leak_details(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(research_refresh, "run", lambda **_kwargs: {
        "success": False, "steps": {"props": {"ok": False, "error": "secret provider token"}}})
    response = http.post("/api/research/refresh")
    assert response.status_code == 503
    assert response.json()["detail"] == "Research refresh failed; see Research for details"
    assert "secret" not in response.text
    monkeypatch.setattr(research_refresh, "run", lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("secret transport error")))
    response = http.post("/api/research/refresh")
    assert response.status_code == 503 and "secret" not in response.text
