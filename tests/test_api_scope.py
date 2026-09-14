"""The active HTTP surface must serve NFL only.

Dormant sports keep their provider/model code, but no route may fetch, analyse
or expose them while the product scope is NFL.
"""
import pytest
from fastapi.testclient import TestClient

from edgeterminal import db
from edgeterminal.api import app
from edgeterminal.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'scope.db'}")
    monkeypatch.setattr(settings, "demo_mode", True)
    with TestClient(app) as test_client:
        yield test_client
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_config_exposes_active_nfl_scope():
    assert settings.active_sports == ["NFL"]


def test_refresh_ignores_dormant_sports(client):
    payload = client.post("/api/refresh", json={"sports": ["NFL", "NBA", "CFB", "PGA"]}).json()
    assert set(payload["sports"]) == {"NFL"}
    assert sorted(payload["ignored_sports"]) == ["CFB", "NBA", "PGA"]


def test_refresh_without_a_sport_list_stays_nfl_only(client):
    payload = client.post("/api/refresh", json={}).json()
    assert set(payload["sports"]) == {"NFL"}


def test_dormant_sport_routes_are_gone(client):
    assert client.get("/api/golf").status_code == 410
    assert client.get("/api/slate", params={"sport": "NBA"}).status_code == 410
    assert client.get("/api/best-bets", params={"sport": "PGA"}).status_code == 410
    assert client.get("/api/watchlist", params={"sport": "CFB"}).status_code == 410


def test_dashboard_is_nfl_scoped_and_has_no_golf_payload(client):
    client.post("/api/refresh", json={"sports": ["NFL"]})
    data = client.get("/api/dashboard").json()
    assert "golf" not in data
    assert all(event["sport"] == "NFL" for event in data["upcoming"])
    assert all(row["sport"] == "NFL" for row in data["watchlist"])


def test_event_detail_rejects_dormant_event_ids(client):
    now = 1.0
    db.ex("INSERT OR REPLACE INTO events(id,sport,league,name,home_team,away_team,start_time,"
          "venue,status,meta,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         ("legacy-nba-1", "NBA", "NBA", "Boston Celtics @ New York Knicks", "New York Knicks",
          "Boston Celtics", "2026-01-01T00:00Z", None, "scheduled", "{}", now))
    assert client.get("/api/event/legacy-nba-1").status_code == 410