"""NFL identity, market, and frozen-prediction invariants."""
from __future__ import annotations

import json
import sqlite3

import pytest

from edgeterminal import db, engine, nfl_identity as identity, nfl_market as market
from edgeterminal.config import settings
from edgeterminal.providers import registry
from edgeterminal.providers.espn import EspnEvents
from edgeterminal.analytics.football import project_game
from edgeterminal.pricing import ev_per_unit


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'nfl.db'}")
    monkeypatch.setattr(settings, "demo_mode", False)
    registry.set_family("NFL", False)
    engine._ctx_cache.clear()
    db.init()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    engine._ctx_cache.clear()


def _espn(start="2027-09-24T00:15:00Z"):
    return dict(id="espn-401", sport="NFL", name="Atlanta Falcons at Green Bay Packers",
                home_team="Green Bay Packers", away_team="Atlanta Falcons",
                start_time=start, venue="Lambeau Field", status="STATUS_SCHEDULED",
                meta=dict(season=2027, season_type=2, week=3,
                          retrieved_at=1_800_000_000, city="Green Bay", state="WI"))


def _api_event(start="2027-09-24T00:15:00Z"):
    return dict(id="api-abc", sport="NFL", home_team="GB", away_team="ATL",
                start_time=start)


def test_canonical_identity_aliases_home_away_and_reschedule():
    first = _espn()
    assert identity.canonical_id(first) == "nfl-2027-reg-03-atl-gb"
    moved = _espn("2027-09-25T00:15:00Z")
    assert identity.canonical_id(moved) == identity.canonical_id(first)
    assert identity.team_code("JAC") == identity.team_code("JAX")
    assert identity.team_code("WAS") == identity.team_code("WSH")
    assert identity.team_code("LA") == identity.team_code("LAR")
    flipped = dict(first, home_team=first["away_team"], away_team=first["home_team"])
    assert identity.canonical_id(flipped) != identity.canonical_id(first)
    assert identity.canonical_id(dict(first, home_team="unknown team")) is None


def test_provider_match_refuses_wrong_game_and_ambiguous_repeat():
    schedule = [dict(_espn(), id="nfl-2027-reg-03-atl-gb")]
    assert identity.match_schedule_event(_api_event(), schedule) == schedule[0]["id"]
    assert identity.match_schedule_event(dict(_api_event(), home_team="Chicago Bears"), schedule) is None
    repeated = schedule + [dict(schedule[0], id="another", start_time="2027-09-24T01:15:00Z")]
    assert identity.match_schedule_event(_api_event(), repeated) is None


def test_refresh_reconciles_provider_ids_and_keeps_one_weekly_game(isolated_db, monkeypatch):
    from edgeterminal import nfl_identity as NI
    event = _espn()
    odds = [dict(event_id="api-abc", sport="NFL", book="fanduel", market="spread",
                 selection="Green Bay Packers", line=-3.5, price=-110),
            dict(event_id="api-abc", sport="NFL", book="fanduel", market="spread",
                 selection="Atlanta Falcons", line=3.5, price=-110)]
    monkeypatch.setattr(EspnEvents, "list_events", lambda self, sport: [event])
    monkeypatch.setattr(registry, "fetch_odds", lambda sport: (odds, "theoddsapi", False))
    monkeypatch.setattr(registry, "list_events", lambda sport, prefer=None:
                        ([_api_event()], "theoddsapi-events", False))
    out = engine.refresh(["NFL"])
    game_id = NI.canonical_id(event)
    assert out["sports"]["NFL"]["odds_rows"] == 2
    assert db.mapped_nfl_game("espn", "espn-401") == game_id
    assert db.mapped_nfl_game("theoddsapi", "api-abc") == game_id
    assert {r["event_id"] for r in db.latest_odds(event_id=game_id)} == {game_id}
    assert [e["id"] for e in engine.slate("NFL")] == [game_id]
    # A kickoff change keeps the internal ID and its provider links.
    monkeypatch.setattr(EspnEvents, "list_events", lambda self, sport:
                        [_espn("2027-09-25T00:15:00Z")])
    engine.refresh_schedule("NFL")
    assert db.q1("SELECT start_time FROM events WHERE id=?", (game_id,))["start_time"] == \
           "2027-09-25T00:15:00Z"
    assert db.mapped_nfl_game("theoddsapi", "api-abc") == game_id


def test_schedule_with_only_invalid_rows_keeps_existing_week(isolated_db):
    old_week = {"game_ids": ["nfl-2027-reg-03-atl-gb"]}
    db.kv_set("nfl_current_schedule", old_week)
    assert engine._ingest_nfl_schedule([dict(_espn(), home_team="unknown")],
                                       "espn-events", False) == []
    assert db.kv_get("nfl_current_schedule") == old_week


def test_spread_orientation_and_point_specific_shopping():
    event = _espn()
    base = dict(event_id="g", book="fanduel", market="spread", price=-110)
    home = dict(base, selection="Green Bay Packers", line=-3.5)
    away = dict(base, selection="Atlanta Falcons", line=3.5)
    assert market.home_spread(home, event) == -3.5
    assert market.home_spread(away, event) == -3.5
    assert len(market.valid_game_quotes([home, away], event)) == 2
    wrong = dict(away, line=-3.5)
    assert market.valid_game_quotes([home, wrong], event) == []
    # A +4.5 alternate line at a different price cannot win the +3.5 row.
    quotes = [dict(away, book="fanduel", price=-110),
              dict(away, book="bet365", price=-105),
              dict(away, book="alternate", line=4.5, price=130)]
    row = next(r for r in market.board_rows(quotes, event)
               if r["selection"] == "Atlanta Falcons")
    assert (row["line"], row["book"], row["price"]) == (3.5, "bet365", -105)


def test_snapshot_and_prediction_history_are_immutable(isolated_db, monkeypatch):
    event = dict(_espn(), id="nfl-2027-reg-03-atl-gb")
    db.upsert_event(event)
    projection = dict(proj_home=24.0, proj_away=20.0, margin=4.0, total=44.0,
                      home_win_prob=0.62, uncertainty=0.4,
                      feature_version="test-features-1")
    ctx = dict(event=event, projection=projection, stats={}, injuries=[], news=[],
               weather=None, sources=[], quality_parts={"odds_fresh": False},
               warnings=[], data_quality=0.2, data_quality_tier="poor",
               retrieved_at=1_800_000_000)
    monkeypatch.setattr(engine, "build_context", lambda ev: ctx)
    first = engine.analyze_event(event["id"])
    assert first["recommendations"] == []
    assert first["projection"]["margin"] == 4.0
    first_snapshot = first["snapshot_id"]
    original = db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=?", (first_snapshot,))["payload"]
    ctx["projection"] = dict(projection, margin=5.0, proj_home=24.5, proj_away=19.5)
    second = engine.analyze_event(event["id"])
    assert second["snapshot_id"] != first_snapshot
    assert db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=?", (first_snapshot,))["payload"] == original
    assert db.q1("SELECT COUNT(*) AS n FROM nfl_game_predictions")["n"] == 2
    with pytest.raises(ValueError):
        db.save_nfl_snapshot(first_snapshot, event["id"], {"tampered": True},
                             "other", "other")
    db.save_prediction(dict(id="issued-once", event_id=event["id"], sport="NFL",
                            market="spread", selection="Green Bay Packers"))
    with pytest.raises(sqlite3.IntegrityError):
        db.save_prediction(dict(id="issued-once", event_id=event["id"], sport="NFL",
                                market="spread", selection="Atlanta Falcons"))


def test_nfl_market_keeps_model_probability_independent(isolated_db):
    event = dict(_espn(), id="nfl-2027-reg-03-atl-gb")
    db.upsert_event(event)
    stats = {
        "Green Bay Packers": {"points_for": 28, "points_against": 18, "games": 8},
        "Atlanta Falcons": {"points_for": 19, "points_against": 24, "games": 8},
    }
    projection = project_game("NFL", event["home_team"], event["away_team"], stats)
    ctx = dict(sport="NFL", event=event, projection=projection, stats=stats,
               injuries=[], news=[], sources=[], data_quality=0.7)
    home = event["home_team"]
    quotes = [dict(book="fanduel", price=-180, line=None, captured_at=1_800_000_000),
              dict(book="bet365", price=-175, line=None, captured_at=1_800_000_000)]
    rows = [dict(q, event_id=event["id"], market="moneyline", selection=home)
            for q in quotes]
    rows += [dict(event_id=event["id"], market="moneyline", selection=event["away_team"],
                  book=q["book"], price=150, line=None) for q in quotes]
    rec = engine.evaluate_market(event, ctx, "moneyline", home, quotes, rows)
    assert rec["book"] == "bet365"
    assert rec["model_prob"] == rec["raw_model_prob"]
    assert rec["market_fair_prob"] != rec["model_prob"]
    assert rec["ev_per_unit"] == pytest.approx(
        ev_per_unit(rec["model_prob"], rec["price"]), abs=0.0002)
    assert rec["market_blend"] is None
    assert rec["state"] == "PASS"


def test_weekly_board_projects_unpriced_games_and_reuses_frozen_prediction(
        isolated_db, monkeypatch):
    event = dict(_espn(), id="nfl-2027-reg-03-atl-gb")
    db.upsert_event(event)
    db.kv_set("nfl_current_schedule", {"game_ids": [event["id"]]})
    stats = {
        "Green Bay Packers": {"points_for": 28, "points_against": 18,
                              "games": 8, "retrieved_at": 1_800_000_000},
        "Atlanta Falcons": {"points_for": 19, "points_against": 24,
                            "games": 8, "retrieved_at": 1_800_000_000},
    }
    monkeypatch.setattr(registry, "team_stats", lambda sport: (stats, "test-stats", False))
    first = engine.weekly_board()
    assert len(first) == 1
    assert first[0]["markets"] == []
    assert first[0]["projection"]["proj_home"] > first[0]["projection"]["proj_away"]
    assert db.q1("SELECT COUNT(*) AS n FROM nfl_game_predictions")["n"] == 1
    second = engine.weekly_board()
    assert second[0]["snapshot_id"] == first[0]["snapshot_id"]
    assert db.q1("SELECT COUNT(*) AS n FROM nfl_game_predictions")["n"] == 1


def test_started_game_only_reads_prekickoff_projection(isolated_db, monkeypatch):
    event = dict(_espn("2024-09-24T00:15:00Z"), id="nfl-2024-reg-03-atl-gb")
    db.upsert_event(event)
    monkeypatch.setattr(engine, "build_context", lambda _: pytest.fail("postgame context fetched"))
    no_forecast = engine.analyze_event(event["id"])
    assert no_forecast["projection"] is None
    assert no_forecast["recommendations"] == []

    projection = dict(proj_home=24.0, proj_away=20.0, margin=4.0, total=44.0,
                      home_win_prob=0.6)
    snapshot_id = "pregame-snapshot"
    db.save_nfl_snapshot(snapshot_id, event["id"],
                         {"projection": projection, "model_version": "pregame"},
                         "pregame", "test-features")
    db.save_nfl_game_prediction("pregame-prediction", event["id"], snapshot_id,
                                projection, "pregame")
    db.ex("UPDATE nfl_game_predictions SET created_at=? WHERE id=?",
          (1_727_000_000, "pregame-prediction"))
    frozen = engine.analyze_event(event["id"])
    assert frozen["projection"] == projection
    assert frozen["snapshot_id"] == snapshot_id
    assert frozen["recommendations"] == []
    assert db.q1("SELECT COUNT(*) AS n FROM nfl_game_predictions")["n"] == 1
