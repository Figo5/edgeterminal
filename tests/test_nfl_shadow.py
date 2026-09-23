"""Forward NFL records must be frozen before kickoff and graded from final scores."""
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from edgeterminal import db, shadow
from edgeterminal.config import settings
from edgeterminal.providers import espn


@pytest.fixture
def game(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'shadow.db'}")
    monkeypatch.setattr(settings, "model_version", "test-model")
    db.init()
    now = time.time()
    kickoff = now + 2 * 86400
    start = datetime.fromtimestamp(kickoff, timezone.utc).isoformat()
    game_id = "nfl-2026-reg-03-atl-gb"
    event = dict(id=game_id, sport="NFL", league="NFL",
                 home_team="Green Bay Packers", away_team="Atlanta Falcons",
                 start_time=start, status="scheduled",
                 meta=dict(season=2026, season_type=2, week=3, is_demo=False))
    db.upsert_event(event)
    db.link_nfl_provider_game("espn", "espn-123", game_id)
    projection = dict(proj_home=27.0, proj_away=20.0, margin=7.0,
                      total=47.0, home_win_prob=0.7, uncertainty=12.0,
                      feature_version="test-features")
    snapshot = dict(game=dict(event, kickoff_utc=start), projection=projection, market=[
        dict(market="spread", selection="Green Bay Packers", line=-3.0,
             price=-110, book="draftkings", captured_at=now-60, is_demo=0),
        dict(market="total", selection="Over", line=44.5,
             price=-110, book="draftkings", captured_at=now-60, is_demo=0),
    ])
    db.save_nfl_snapshot("snapshot-1", game_id, snapshot, "test-model", "test-features")
    db.save_nfl_game_prediction("prediction-1", game_id, "snapshot-1",
                                projection, "test-model")
    yield dict(id=game_id, now=now, kickoff=kickoff, start=start)
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _final(game, home_score=27, away_score=20):
    return dict(id="espn-123", sport="NFL", home_team="Green Bay Packers",
                away_team="Atlanta Falcons", start_time=game["start"],
                status="STATUS_FINAL", meta=dict(season=2026, season_type=2, week=3,
                                                 home_score=str(home_score),
                                                 away_score=str(away_score)))


def test_freeze_is_pregame_immutable_and_idempotent(game):
    assert shadow.freeze_pending(now=game["now"]+1) == 1
    assert shadow.freeze_pending(now=game["now"]+2) == 0
    frozen = db.q1("SELECT * FROM nfl_shadow_entries WHERE game_id=?", (game["id"],))
    assert frozen["prediction_id"] == "prediction-1"
    assert frozen["designation"] == "PASS"
    assert frozen["home_spread_line"] == -3.0
    assert frozen["total_line"] == 44.5
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_shadow_entries WHERE game_id=?", (game["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("INSERT OR REPLACE INTO nfl_shadow_entries "
              "SELECT * FROM nfl_shadow_entries WHERE game_id=?", (game["id"],))


def test_final_result_grades_once_and_keeps_corrections(game):
    shadow.freeze_pending(now=game["now"]+1)
    for market, selection, line in (("spread", "Green Bay Packers", -4.0),
                                    ("total", "Over", 45.5)):
        db.ex("INSERT INTO odds_snapshots(event_id,sport,book,market,selection,line,"
              "price,captured_at,is_demo) VALUES(?,?,?,?,?,?,?,?,0)",
              (game["id"], "NFL", "draftkings", market, selection, line,
               -110, game["kickoff"]-3600))

    class Provider:
        def __init__(self, scores): self.scores = scores
        def list_events_for_week(self, season, season_type, week):
            assert (season, season_type, week) == (2026, 2, 3)
            return [_final(game, *self.scores)]

    result = shadow.sync_results(provider=Provider((27, 20)),
                                 now=game["kickoff"]+5*3600)
    assert result["graded"] == 1
    grade = db.q1("SELECT * FROM nfl_shadow_grades WHERE game_id=?", (game["id"],))
    assert grade["revision"] == 1
    assert grade["su_result"] == "win"
    assert grade["ats_result"] == "win"
    assert grade["total_result"] == "win"
    assert grade["score_mae"] == 0
    assert grade["spread_clv_points"] == 1.0
    assert grade["total_clv_points"] == 1.0
    assert shadow.sync_results(provider=Provider((27, 20)),
                               now=game["kickoff"]+6*3600)["graded"] == 0
    corrected = shadow.sync_results(provider=Provider((28, 20)),
                                    now=game["kickoff"]+7*3600)
    assert corrected["corrected"] == 1
    assert db.q1("SELECT COUNT(*) n FROM nfl_shadow_grades WHERE game_id=?",
                 (game["id"],))["n"] == 2
    assert db.q1("SELECT COUNT(*) n FROM nfl_shadow_audit WHERE game_id=?",
                 (game["id"],))["n"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_shadow_grades WHERE game_id=?", (game["id"],))
    record = shadow.performance()
    assert record["forward_shadow_status"] == "TRACKING"
    assert record["graded_nfl_game_predictions"] == 1
    assert record["all_projections"]["score_mae"] == 0.5
    assert record["model_versions"]["test-model"]["n"] == 1
    assert record["official_picks"]["n"] == 0


def test_refuses_unverified_or_mismatched_results(game):
    shadow.freeze_pending(now=game["now"]+1)
    bad = _final(game)
    bad["id"] = "espn-different-game"

    class Provider:
        def list_events_for_week(self, season, season_type, week):
            return [bad]

    assert shadow.sync_results(provider=Provider(), now=game["kickoff"]+5*3600)["graded"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_shadow_grades")["n"] == 0
    bad["id"] = "espn-123"
    bad["home_team"] = "Dallas Cowboys"
    assert shadow.sync_results(provider=Provider(), now=game["kickoff"]+5*3600)["graded"] == 0


def test_cannot_freeze_after_kickoff(game):
    assert shadow.freeze_pending(now=game["kickoff"]+1) == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_shadow_entries")["n"] == 0


def test_freeze_requires_current_model_version(game, monkeypatch):
    monkeypatch.setattr(settings, "model_version", "next-model")
    assert shadow.freeze_pending(now=game["now"]+1) == 0


def test_provider_failure_retries_without_losing_frozen_record(game):
    shadow.freeze_pending(now=game["now"]+1)
    class Failed:
        def list_events_for_week(self, *args): raise RuntimeError("provider unavailable")
    class Recovered:
        def list_events_for_week(self, *args): return [_final(game)]
    first = shadow.sync_results(provider=Failed(), now=game["kickoff"]+5*3600)
    assert first["pending"] == 1 and first["errors"]
    assert shadow.sync_results(provider=Recovered(), now=game["kickoff"]+6*3600)["graded"] == 1


def test_later_quote_cannot_enter_the_frozen_reference(game):
    event = db.q1("SELECT * FROM events WHERE id=?", (game["id"],))
    future = {"market": [dict(event_id=game["id"], market="spread",
                              selection="Green Bay Packers", line=-3.0,
                              price=-110, book="draftkings",
                              captured_at=game["now"]+60, is_demo=0)]}
    assert shadow._quote(future, event, "spread", event["home_team"], game["now"]) is None


@pytest.mark.parametrize("home,away,model_margin,model_total,total_line,expected", [
    (20, 24, -7.0, 40.0, 44.5, "win"),
    (27, 20, -7.0, 40.0, 44.5, "loss"),
    (24, 21, -7.0, 40.0, 45.0, "push"),
])
def test_away_side_and_under_grade_directions(game, home, away, model_margin,
                                               model_total, total_line, expected):
    shadow.freeze_pending(now=game["now"]+1)
    entry = db.q1("SELECT * FROM nfl_shadow_entries WHERE game_id=?", (game["id"],))
    entry.update(projected_margin=model_margin, projected_total=model_total,
                 total_line=total_line)
    grade = shadow._grade(entry, _final(game, home, away), game["kickoff"]+5*3600)
    assert grade["ats_result"] == expected
    assert grade["total_result"] == expected


def test_historical_espn_week_keeps_final_status_and_scores(monkeypatch):
    seen = {}
    def fake_get(url, params, provider):
        seen.update(url=url, params=params, provider=provider)
        return {"season": {"year": 2025, "type": 2}, "week": {"number": 1},
                "events": [{"id": "123", "date": "2025-09-05T00:20:00Z",
                    "status": {"type": {"name": "STATUS_FINAL"}},
                    "competitions": [{"competitors": [
                        {"homeAway": "home", "team": {"displayName": "Green Bay Packers"},
                         "score": "27"},
                        {"homeAway": "away", "team": {"displayName": "Atlanta Falcons"},
                         "score": "20"}]}]}]}
    monkeypatch.setattr(espn, "get_json", fake_get)
    event = espn.EspnEvents().list_events_for_week(2025, 2, 1)[0]
    assert seen["params"] == {"dates": 2025, "seasontype": 2, "week": 1, "limit": 100}
    assert event["status"] == "STATUS_FINAL"
    assert (event["meta"]["home_score"], event["meta"]["away_score"]) == ("27", "20")
