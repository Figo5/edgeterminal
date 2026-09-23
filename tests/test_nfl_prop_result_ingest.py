"""Offline postgame operations fixtures: no NFL or nflverse requests."""
import csv
import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from edgeterminal import db, nfl_prop_lab
from edgeterminal.config import settings
from edgeterminal.nfl_prop_result_ingest import sync, status_view

GAME = "nfl-2026-reg-03-atl-gb"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("edgeterminal.nfl_prop_result_ingest.FORWARD_REPORT_PATH",
                        tmp_path / "forward-report.json")
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'operations.db'}")
    db.init()
    quotes = []
    for i, gsis in enumerate(("gsis-a", "gsis-b", "gsis-c", "gsis-d"), 1):
        quotes.append({"quote_id": i, "canonical_game_id": GAME,
                       "player_id": f"sgo:{i}", "gsis_player_id": gsis,
                       "player_name": gsis, "market": "receptions", "line": 2.5,
                       "side": "over", "price": -110, "sportsbook": "fanduel",
                       "projection": 3.0, "probability_over": .55,
                       "probability_under": .45, "uncertainty_sigma": 2.0,
                       "model_version": "fixture-v1", "observed_at": 10.0,
                       "kickoff_utc": 100.0})
    payload = json.dumps({"metadata": {}, "projections": quotes}, sort_keys=True)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  ("batch", "sportsgameodds", 10, "source", "{}", 1, 1, 4, "{}"))
        c.execute("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)",
                  (sha, "batch", 20, payload))
    db.link_nfl_provider_game("espn", "espn-123", GAME)
    monkeypatch.setattr(nfl_prop_lab, "_schedule", lambda: {
        "2026_03_ATL_GB": {"game_id": "2026_03_ATL_GB", "season": "2026",
                           "game_type": "REG", "week": "3", "away_team": "ATL",
                           "home_team": "GB"}})
    yield sha, tmp_path
    if db._conn is not None:
        db._conn.close(); db._conn = None


def scoreboard(status="STATUS_FINAL", score=21, kickoff="1970-01-01T00:01:40Z"):
    return json.dumps({"season": {"year": 2026, "type": 2}, "week": {"number": 3},
                       "events": [{"id": "123", "date": kickoff,
                                   "status": {"type": {"name": status,
                                                       "completed": status == "STATUS_FINAL"}},
                                   "competitions": [{"competitors": [
                                       {"homeAway": "home", "score": str(score),
                                        "team": {"displayName": "Green Bay Packers"}},
                                       {"homeAway": "away", "score": "17",
                                        "team": {"displayName": "Atlanta Falcons"}}]}]}]}).encode()


def stats(receptions=5):
    fields = ["game_id", "player_id", "season", "week", "season_type", "team",
              "opponent_team", "passing_yards", "rushing_yards", "carries",
              "receptions", "receiving_yards", "targets"]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for i in range(60):
        gsis = "gsis-a" if i == 0 else "gsis-b" if i == 1 else f"other-{i}"
        team, opponent = ("ATL", "GB") if i < 30 else ("GB", "ATL")
        writer.writerow({"game_id": "2026_03_ATL_GB", "player_id": gsis,
                         "season": 2026, "week": 3, "season_type": "REG",
                         "team": team, "opponent_team": opponent,
                         "passing_yards": 0, "rushing_yards": 0, "carries": 0,
                         "receptions": receptions if i == 0 else 0,
                         "receiving_yards": 50 if i == 0 else 0,
                         "targets": 5 if i == 0 else 2 if i == 1 else 0})
    return stream.getvalue().encode()


def manifest(tmp_path):
    book = tmp_path / "official-gamebook.pdf"
    book.write_bytes(b"%PDF-1.4\nsynthetic NFL Game Book fixture\n")
    body = {"canonical_game_id": GAME, "reviewer": "fixture-reviewer", "reviewed_at": 20000,
            "source_url": "https://www.nfl.com/games/fixture/gamebook",
            "source_file": str(book),
            "source_sha256": hashlib.sha256(book.read_bytes()).hexdigest(),
            "players": [
                {"gsis_player_id": "gsis-c", "active_roster": False,
                 "snap_played": False, "certified_no_stats": True},
                {"gsis_player_id": "gsis-d", "active_roster": True,
                 "snap_played": False, "certified_no_stats": True}]}
    return json.dumps(body).encode()


def test_result_sync_wait_pending_participation_revision_and_idempotency(setup):
    sha, tmp_path = setup
    assert status_view(sha, now=99)["counts"] == {"not_started": 1}
    before = db.q1("SELECT payload FROM nfl_prop_research_freezes")["payload"]
    in_play = sync(sha, scoreboard_bytes=scoreboard("STATUS_IN_PROGRESS"),
                   scoreboard_source="fixture://scoreboard",
                   now=101)
    assert in_play["status"]["counts"] == {"in_progress": 1}
    assert in_play["network_requests"] == 0
    waiting = sync(sha, scoreboard_bytes=scoreboard(),
                   scoreboard_source="fixture://scoreboard", now=200)
    assert waiting["status"]["counts"] == {"final_wait": 1}
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"] == 0
    first = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                 scoreboard_source="fixture://scoreboard", stats_source="fixture://stats", now=20000)
    assert first["grade_counts"][0]["new_revisions"] == 2
    report = json.loads((tmp_path / "forward-report.json").read_text())
    assert report["prospective_2026"]["quote_sides_graded"] == 2
    assert "retrospective_validation" in report
    assert first["status"]["counts"] == {"participation_pending": 1}
    assert first["status"]["players_pending_participation"] == 2
    assert sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                scoreboard_source="fixture://scoreboard", stats_source="fixture://stats",
                now=20001)["grade_counts"][0]["new_revisions"] == 0
    filled = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                  participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                  stats_source="fixture://stats", now=20002)
    assert filled["grade_counts"][0]["new_revisions"] == 2
    assert filled["status"]["counts"] == {"graded": 1}
    grades = [json.loads(r["payload"]) for r in db.q("SELECT payload FROM nfl_prop_grade_revisions")]
    assert {r["player_id"]: r["player_status"] for r in grades} == {
        "sgo:1": "active", "sgo:2": "active", "sgo:3": "inactive", "sgo:4": "active"}
    assert next(r for r in grades if r["player_id"] == "sgo:4")["actual_stat"] == 0
    assert next(r for r in grades if r["player_id"] == "sgo:4")["participation_evidence"]["manifest_sha256"]
    assert next(r for r in grades if r["player_id"] == "sgo:4")["participation_evidence"]["snap_played"] is False
    assert next(r for r in grades if r["player_id"] == "sgo:2")["participation_evidence"]["snap_played"] is True
    assert {r["player_id"]: r["participation_class"] for r in grades} == {
        "sgo:1": "participated_stat_recorded", "sgo:2": "participated_target_stat_zero",
        "sgo:3": "confirmed_inactive", "sgo:4": "active_zero_no_snap"}
    ingestions = db.q1("SELECT COUNT(*) n FROM nfl_prop_result_ingestions")["n"]
    assert sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                stats_source="fixture://stats", now=20003)["grade_counts"][0]["new_revisions"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_result_ingestions")["n"] == ingestions
    corrected = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(6),
                     participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                     stats_source="fixture://stats", now=20004)
    assert corrected["grade_counts"][0]["corrections"] == 1
    assert corrected["status"]["source_corrections_detected"] == 1
    assert sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(6),
                participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                stats_source="fixture://stats", now=20005)["grade_counts"][0]["new_revisions"] == 0
    score_corrected = sync(sha, scoreboard_bytes=scoreboard(score=24),
                           stats_bytes=stats(6), participation_bytes=manifest(tmp_path),
                           scoreboard_source="fixture://scoreboard", stats_source="fixture://stats",
                           now=20006)
    assert score_corrected["grade_counts"][0]["corrections"] == 4
    assert score_corrected["status"]["source_corrections_detected"] == 2
    assert score_corrected["status"]["games"][0]["corrected_quote_revisions"] == 5
    history = [json.loads(r["payload"]) for r in db.q(
        "SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=1 ORDER BY revision")]
    assert [r["actual_stat"] for r in history] == [5, 6, 6]
    assert history[1]["previous_revision"] == 1
    assert history[1]["previous_evidence_sha"] == history[0]["evidence_sha"]
    assert db.q1("SELECT payload FROM nfl_prop_research_freezes")["payload"] == before
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_source_artifacts")["n"] >= 4
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_prop_result_ingestions")


def test_identity_incomplete_stats_and_bad_gamebook_fail_closed(setup):
    sha, tmp_path = setup
    wrong = json.loads(scoreboard())
    wrong["events"][0]["competitions"][0]["competitors"][0]["team"]["displayName"] = "Chicago Bears"
    with pytest.raises(ValueError, match="identity"):
        sync(sha, scoreboard_bytes=json.dumps(wrong).encode(),
             scoreboard_source="fixture://scoreboard", now=20000)
    with pytest.raises(ValueError, match="source URL"):
        sync(sha, scoreboard_bytes=scoreboard(), now=20000)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_source_artifacts")["n"] == 0
    short = stats().splitlines()[:20]
    result = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=b"\n".join(short),
                  scoreboard_source="fixture://scoreboard", stats_source="fixture://stats", now=20000)
    assert result["grade_counts"] == []
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"] == 0
    bad = json.loads(manifest(tmp_path))
    bad["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
             participation_bytes=json.dumps(bad).encode(),
             scoreboard_source="fixture://scoreboard", stats_source="fixture://stats", now=20001)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"] == 0


def test_moved_earlier_kickoff_uses_current_schedule_to_start_capture(setup):
    sha, _ = setup
    with db.tx() as c:
        c.execute("INSERT INTO events(id,sport,league,start_time) VALUES(?,?,?,?)",
                  (GAME, "NFL", "NFL", "1970-01-01T00:00:50Z"))
    result = sync(sha, scoreboard_bytes=scoreboard("STATUS_IN_PROGRESS",
                                                 kickoff="1970-01-01T00:00:50Z"),
                  scoreboard_source="fixture://scoreboard", now=75)
    assert result["status"]["counts"] == {"in_progress": 1}
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_result_ingestions")["n"] == 1
