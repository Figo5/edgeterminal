"""Offline prop-lab controls; no provider calls or future-week features."""
import csv
import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from edgeterminal import db, nfl_prop_lab
from edgeterminal.api import nfl_prop_board
from edgeterminal.config import settings


@pytest.fixture
def lab_db(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'lab.db'}")
    db.init()
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  ("batch-test", "sportsgameodds", 100, "hash", "{}", 1, 1, 2,
                   '{"events":[{"canonical_game_id":"nfl-2026-reg-03-atl-gb"}]}'))
        c.execute("INSERT INTO events(id,sport,league,start_time) VALUES(?,?,?,?)",
                  ("nfl-2026-reg-03-atl-gb", "NFL", "NFL", "2026-09-25T00:15:00Z"))
    yield
    db._conn.close()
    db._conn = None


def _quote(side, line=40.5, kind="main", player="sgo:BIJAN_ROBINSON_1_NFL"):
    return ("batch-test", "nfl-2026-reg-03-atl-gb", "provider-game", player,
            "BIJAN_ROBINSON_1_NFL", "Bijan Robinson", "Atlanta Falcons", "Green Bay Packers",
            "receiving_yards", line, side, -110, "fanduel", 100, 200,
            "sportsgameodds", "fixture", None, kind, 0)


def test_pair_matching_keeps_alternates_and_freezes_only_complete_main(lab_db):
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_snapshots(" + ",".join(
            ("batch_id", "canonical_game_id", "provider_event_id", "player_id",
             "provider_player_id", "player_name", "team", "opponent", "market", "line",
             "side", "price", "sportsbook", "retrieved_at", "kickoff_utc", "provider",
             "source", "provider_updated_at", "line_type", "offer_index")) + ") VALUES(" + ",".join("?" * 20) + ")", _quote("over"))
        c.execute("INSERT INTO nfl_prop_snapshots(" + ",".join(
            ("batch_id", "canonical_game_id", "provider_event_id", "player_id",
             "provider_player_id", "player_name", "team", "opponent", "market", "line",
             "side", "price", "sportsbook", "retrieved_at", "kickoff_utc", "provider",
             "source", "provider_updated_at", "line_type", "offer_index")) + ") VALUES(" + ",".join("?" * 20) + ")", _quote("under"))
        c.execute("INSERT INTO nfl_prop_snapshots(" + ",".join(
            ("batch_id", "canonical_game_id", "provider_event_id", "player_id",
             "provider_player_id", "player_name", "team", "opponent", "market", "line",
             "side", "price", "sportsbook", "retrieved_at", "kickoff_utc", "provider",
             "source", "provider_updated_at", "line_type", "offer_index")) + ") VALUES(" + ",".join("?" * 20) + ")", _quote("over", 45.5, "alternate"))
    pairs, coverage = nfl_prop_lab._pair_quotes("batch-test")
    assert len(pairs) == 2
    assert coverage["two_sided_main_groups"] == 1
    assert coverage["alternate_observed"] == 1
    assert db.q1("SELECT count(*) n FROM nfl_prop_snapshots")["n"] == 3
    records = [dict(quote_id=p["id"], batch_id="batch-test",
                    canonical_game_id=p["canonical_game_id"], player_id=p["player_id"],
                    player_name=p["player_name"], market=p["market"], model_version="lab:v1",
                    projection=42.0, uncertainty_sigma=10.0, sportsbook=p["sportsbook"],
                    line=p["line"], side=p["side"], price=p["price"], observed_at=100,
                    kickoff_utc=200, raw_difference=1.5, probability_over=0.55,
                    probability_under=0.45) for p in pairs]
    report = {"batch_id": "batch-test", "projections": records}
    assert nfl_prop_lab.seal_forward(report, {"fixture": "sha"}, 150)["quote_count"] == 2
    with pytest.raises(ValueError, match="already been frozen"):
        nfl_prop_lab.seal_forward(report, {"fixture": "sha"}, 151)
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("UPDATE nfl_prop_research_freezes SET payload='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_prop_research_freezes")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be extended"):
        db.ex("INSERT INTO nfl_prop_snapshots(batch_id,canonical_game_id,provider_event_id,player_id,"
              "provider_player_id,player_name,team,opponent,market,line,side,price,sportsbook,"
              "retrieved_at,kickoff_utc,provider,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              _quote("over")[:17])
    board = nfl_prop_lab.research_board("nfl-2026-reg-03-atl-gb")
    assert board["designation"] == "PASS" and board["projected_quote_count"] == 2
    assert all(q["research_estimate"] is None for q in board["quotes"] if q["line_type"] == "alternate")
    assert nfl_prop_board("nfl-2026-reg-03-atl-gb")["projected_quote_count"] == 2


def test_duplicate_main_side_is_not_arbitrarily_paired(lab_db):
    with db.tx() as c:
        for side in ("over", "over", "under"):
            c.execute("INSERT INTO nfl_prop_snapshots(batch_id,canonical_game_id,provider_event_id,player_id,"
                      "provider_player_id,player_name,team,opponent,market,line,side,price,sportsbook,"
                      "retrieved_at,kickoff_utc,provider,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      _quote(side)[:17])
    pairs, coverage = nfl_prop_lab._pair_quotes("batch-test")
    assert pairs == []
    assert coverage["duplicate_main_sides"] == 1
    assert db.q1("SELECT count(*) n FROM nfl_prop_snapshots")["n"] == 3


def test_freeze_refuses_late_quote_or_projection(lab_db):
    base = {"batch_id": "batch-test", "projections": [dict(observed_at=199, kickoff_utc=200)]}
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_snapshots(batch_id,canonical_game_id,provider_event_id,player_id,"
                  "provider_player_id,player_name,team,opponent,market,line,side,price,sportsbook,"
                  "retrieved_at,kickoff_utc,provider,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  _quote("over")[:17])
    with pytest.raises(ValueError, match="pre-kickoff"):
        nfl_prop_lab.seal_forward(base, {"fixture": "sha"}, 200)
    base["projections"][0]["observed_at"] = 200
    with pytest.raises(ValueError, match="pre-kickoff"):
        nfl_prop_lab.seal_forward(base, {"fixture": "sha"}, 150)


def test_historical_features_only_use_prior_weeks(tmp_path, monkeypatch):
    stat_dir = tmp_path / "stats"
    stat_dir.mkdir()
    for year in range(2022, 2027):
        path = stat_dir / f"stats_player_week_{year}.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["season_type", "player_id", "game_id", "season", "week",
                                                  "player_display_name", "position", "team", "opponent_team", *nfl_prop_lab.USAGE])
            w.writeheader()
            if year == 2022:
                for week, yards in [(1, 10), (2, 30), (3, 50)]:
                    w.writerow(dict(season_type="REG", player_id="gsis-1", game_id=f"2022_0{week}_ATL_GB",
                                    season=year, week=week, player_display_name="Test Player", position="WR",
                                    team="GB", opponent_team="ATL", targets=3, receptions=2,
                                    receiving_yards=yards, carries=0, rushing_yards=0,
                                    attempts=0, passing_yards=0))
    schedule = tmp_path / "games.csv.gz"
    with gzip.open(schedule, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["game_id", "season", "game_type", "week", "gameday",
                                            "away_team", "home_team", "away_rest", "home_rest"])
        w.writeheader()
        for week in range(1, 4):
            w.writerow(dict(game_id=f"2022_0{week}_ATL_GB", season=2022, game_type="REG", week=week,
                            gameday=f"2022-09-{week+10:02d}", away_team="ATL", home_team="GB",
                            away_rest=7, home_rest=7))
    monkeypatch.setattr(nfl_prop_lab, "STATS", stat_dir)
    monkeypatch.setattr(nfl_prop_lab, "SCHEDULE", schedule)
    datasets, _, _ = nfl_prop_lab.build_dataset()
    receiving = datasets["receiving_yards"]
    assert [r["target"] for r in receiving] == [30, 50]
    assert [r["features"]["receiving_yards_last3"] for r in receiving] == [10, 20]
    assert [r["features"]["opponent_allowed_last3"] for r in receiving] == [10, 20]


def test_walk_forward_fits_only_earlier_weeks(monkeypatch):
    seen = []

    class SpyModel:
        def fit(self, x, y):
            seen.append(max(y))
            return self

        def predict(self, x):
            return [0.0] * len(x)

    monkeypatch.setattr(nfl_prop_lab, "_estimator", lambda family: SpyModel())
    features = {name: 0.0 for name in nfl_prop_lab.FEATURES}
    rows = ([dict(season=2022, week=1, target=1.0, features=features) for _ in range(100)] +
            [dict(season=2024, week=1, target=1000.0, features=features) for _ in range(2)] +
            [dict(season=2025, week=1, target=2.0, features=features)])
    result = nfl_prop_lab.evaluate({"receiving_yards": rows})
    assert seen == [1.0, 1.0, 1000.0, 1000.0]
    assert result["receiving_yards"]["metrics"]["development_2024_rolling"]["n"] == 2
