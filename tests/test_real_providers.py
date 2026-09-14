import time

import pytest

from edgeterminal.config import settings
from edgeterminal.providers import registry
from edgeterminal.providers.espn import EspnEvents, EspnOdds
from edgeterminal.providers.normalize import normalize_book, normalize_team


SCOREBOARD = {
    "season": {"year": 2026, "type": 2},
    "week": {"number": 1},
    "events": [{
        "id": "401872931",
        "name": "Denver Broncos at Kansas City Chiefs",
        "date": "2026-09-15T00:15Z",
        "status": {"type": {"name": "STATUS_SCHEDULED", "description": "Scheduled", "completed": False}},
        "competitions": [{
            "venue": {"fullName": "GEHA Field", "indoor": False, "address": {"city": "Kansas City", "state": "MO"}},
            "competitors": [
                {"homeAway": "home", "score": "0", "team": {"id": "12", "displayName": "Kansas City Chiefs", "abbreviation": "KC"}},
                {"homeAway": "away", "score": "0", "team": {"id": "7", "displayName": "Denver Broncos", "abbreviation": "DEN"}},
            ],
            "odds": [{
                "provider": {"id": "100", "name": "DraftKings", "displayName": "DraftKings"},
                "moneyline": {
                    "home": {"close": {"odds": "-130"}, "open": {"odds": "-155"}},
                    "away": {"close": {"odds": "+110"}, "open": {"odds": "+130"}},
                },
                "pointSpread": {
                    "home": {"close": {"line": "-2.5", "odds": "-108"}},
                    "away": {"close": {"line": "+2.5", "odds": "-112"}},
                },
                "total": {
                    "over": {"close": {"line": "o43.5", "odds": "-108"}},
                    "under": {"close": {"line": "u43.5", "odds": "-112"}},
                },
            }],
        }],
    }],
}


def test_canonical_team_and_sportsbook_normalization():
    assert normalize_team("KC Chiefs") == "Kansas City Chiefs"
    assert normalize_team("LA Rams") == "Los Angeles Rams"
    assert normalize_book("draft kings") == "draftkings"
    assert normalize_book("Bet 365") == "bet365"
    assert normalize_book("FanDuel Sportsbook") == "fanduel"


def test_espn_event_normalization_includes_real_schedule_metadata(monkeypatch):
    monkeypatch.setattr("edgeterminal.providers.espn._scoreboard", lambda sport, force=False: SCOREBOARD)
    event = EspnEvents().list_events("NFL")[0]
    assert event["id"] == "espn-401872931"
    assert event["home_team"] == "Kansas City Chiefs"
    assert event["away_team"] == "Denver Broncos"
    assert event["meta"]["season"] == 2026
    assert event["meta"]["week"] == 1
    assert event["meta"]["source_url"].startswith("https://")
    assert event["meta"]["retrieved_at"] <= time.time()


def test_espn_odds_normalizes_primary_markets(monkeypatch):
    monkeypatch.setattr("edgeterminal.providers.espn._scoreboard", lambda sport, force=False: SCOREBOARD)
    rows = EspnOdds().fetch_odds("NFL")
    assert len(rows) == 6
    assert {r["market"] for r in rows} == {"moneyline", "spread", "total"}
    assert {r["book"] for r in rows} == {"draftkings"}
    assert all(r["event_id"] == "espn-401872931" for r in rows)
    assert next(r for r in rows if r["market"] == "spread" and r["selection"] == "Kansas City Chiefs")["line"] == -2.5
    assert next(r for r in rows if r["market"] == "total" and r["selection"] == "Over")["line"] == 43.5
    assert all(r["source_url"].startswith("https://") for r in rows)


def test_live_family_never_falls_back_to_fixture_context(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    registry.set_family("NFL", False)

    class BrokenStats:
        is_demo = False
        def team_stats(self, sport):
            raise RuntimeError("offline")

    monkeypatch.setattr(registry, "EspnStats", BrokenStats)
    with pytest.raises(Exception):
        registry.team_stats("NFL")


def test_odds_provider_chain_prefers_real_espn_before_demo(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "odds_api_key", "")
    registry.set_family("NFL", False)
    names = [name for name, _factory in registry._odds_candidates("NFL")]
    assert names == ["espn-odds", "demo-odds"]
