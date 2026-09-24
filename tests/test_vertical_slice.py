import json
from pathlib import Path

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
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'edge.db'}")
    monkeypatch.setattr(settings, "demo_mode", True)
    with TestClient(app) as test_client:
        yield test_client
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_nfl_vertical_slice_refresh_persists_and_exposes_dashboard(client):
    before = db.q1("SELECT COUNT(*) AS n FROM odds_snapshots")["n"]
    response = client.post("/api/refresh", json={"sports": ["NFL"]})
    assert response.status_code == 200
    payload = response.json()
    nfl = payload["sports"]["NFL"]
    assert nfl["is_demo"] is True
    assert nfl["odds_rows"] > 0

    after = db.q1("SELECT COUNT(*) AS n FROM odds_snapshots")["n"]
    assert after > before

    dashboard = client.get("/api/dashboard", params={"sport": "NFL"})
    assert dashboard.status_code == 200
    data = dashboard.json()
    assert data["upcoming"]
    assert all(event["sport"] == "NFL" for event in data["upcoming"])
    assert all(bet["confidence"] >= 80 for bet in data["best_bets"])
    assert all(bet["state"] == "OFFICIAL PICK" for bet in data["best_bets"])

    event_id = data["upcoming"][0]["id"]
    detail = client.get(f"/api/event/{event_id}")
    assert detail.status_code == 200
    event = detail.json()
    for key in ("event", "projection", "recommendations", "injuries", "news", "weather", "stats", "sources", "warnings"):
        assert key in event


def test_second_refresh_creates_history_and_line_movement_inputs(client):
    client.post("/api/refresh", json={"sports": ["NFL"]}).raise_for_status()
    client.post("/api/refresh", json={"sports": ["NFL"]}).raise_for_status()
    event = client.get("/api/slate", params={"sport": "NFL"}).json()["events"][0]
    market = event["markets"][0]
    history = client.get(
        "/api/line-history",
        params={
            "event_id": event["id"],
            "market": market["market"],
            "selection": market["selection"],
            "book": market["book"],
        },
    )
    assert history.status_code == 200
    rows = history.json()["history"]
    assert len(rows) >= 2
    assert rows[0]["captured_at"] <= rows[-1]["captured_at"]


def test_live_provider_failure_does_not_seed_demo_data(client, monkeypatch):
    from edgeterminal.providers.base import ProviderError
    from edgeterminal.providers.espn import EspnEvents, EspnOdds

    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "odds_api_key", "")
    monkeypatch.setattr(EspnOdds, "fetch_odds", lambda self, sport: (_ for _ in ()).throw(
        ProviderError("espn-odds", "simulated outage")))
    monkeypatch.setattr(EspnEvents, "list_events", lambda self, sport: [])
    before = db.q1("SELECT COUNT(*) AS n FROM odds_snapshots")["n"]
    client.post("/api/refresh", json={"sports": ["NFL"]}).raise_for_status()
    dashboard = client.get("/api/dashboard", params={"sport": "NFL"}).json()
    assert dashboard["demo_mode"] is False
    assert db.q1("SELECT COUNT(*) AS n FROM odds_snapshots")["n"] == before


def test_chat_is_grounded_without_openai_key(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    event = client.get("/api/slate", params={"sport": "NFL"}).json()["events"][0]
    response = client.post(
        "/api/chat",
        json={"message": "What's the best bet in this game?", "event_id": event["id"], "history": []},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] in {"deterministic-router", "research-views"}
    assert data["tool_trace"]
    assert data["tool_trace"][0]["tool"] in {"run_game_analysis", "research_views"}
    assert "model" in data["answer"].lower() or "no market" in data["answer"].lower() or \
           "no stored" in data["answer"].lower()


def test_parlay_chat_uses_parlay_tool_without_openai_key(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    response = client.post(
        "/api/chat",
        json={"message": "Build me the best parlay from today's games.", "history": []},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["tool_trace"][0]["tool"] == "build_best_parlay"
    assert "parlay" in data["answer"].lower()


def test_frontend_script_exists_and_wires_required_flows(client):
    response = client.get("/static/app.js")
    assert response.status_code == 200
    script = response.text
    for endpoint in ("/api/product/nfl", "/api/product/performance",
                     "/api/research/paper", "/api/chat", "/api/model-report"):
        assert endpoint in script
    for control in ("backBtn", "chatForm", "propSort"):
        assert control in script
    assert '"/api/refresh"' not in script
    assert "sportTabs" not in script


def test_read_only_product_routes_expose_research_artifacts(client):
    board = client.get("/api/product/nfl")
    assert board.status_code == 200
    assert board.json()["readiness"] == "RESEARCH_ONLY"
    index = client.get("/api/product/research-index")
    assert index.status_code == 200
    assert index.json()["week3_game_freeze"]["matches_record"] is True
    document = client.get("/api/product/research-document/settlement-rules")
    assert document.status_code == 200
    assert "sportsbook" in document.text.lower()
    assert client.get("/api/product/research-document/../../etc/passwd").status_code in (404, 422)
