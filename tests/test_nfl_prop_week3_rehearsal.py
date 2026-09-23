"""Synthetic outcomes against an exact copy of the frozen Week 3 prop structure."""
import hashlib
import json
from pathlib import Path

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal.nfl_prop_grading import grade_freeze, forward_report


@pytest.fixture
def week3(tmp_path, monkeypatch):
    report_path = Path(__file__).resolve().parents[1] / "data/research/nfl_prop_lab_2026_week3.json"
    report = json.loads(report_path.read_text())
    metadata = {"created_at": report["forward_freeze"]["frozen_at"],
                "feature_names": report["features"], "feature_version": report["feature_version"],
                "input_sha256": report["dataset_sha256"], "model_version": "prop-lab-v1",
                "selection_basis": "2024 development MAE", "source_batch_id": report["forward"]["batch_id"],
                "validation_holdout": "2025"}
    payload = json.dumps({"metadata": metadata, "projections": report["forward"]["projections"]},
                         sort_keys=True, separators=(",", ":"))
    original = {"freeze_sha": report["forward_freeze"]["freeze_sha"],
                "source_batch_id": report["forward"]["batch_id"],
                "frozen_at": report["forward_freeze"]["frozen_at"], "payload": payload}
    assert hashlib.sha256(original["payload"].encode()).hexdigest() == original["freeze_sha"]
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'rehearsal.db'}")
    db.init()
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  (original["source_batch_id"], "sportsgameodds", 1, "fixture-source", "{}", 16, 16, 1746, "{}"))
        c.execute("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)",
                  tuple(original[key] for key in ("freeze_sha","source_batch_id","frozen_at","payload")))
    yield original, tmp_path
    if db._conn is not None:
        db._conn.close(); db._conn = None


def _source(path, stamp):
    return {"source": "fixture://synthetic-week3", "file": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "retrieved_at": stamp}


def test_week3_final_inactive_zero_push_postponed_and_correction(week3):
    original, tmp_path = week3
    payload = json.loads(original["payload"])
    quotes = [q for q in payload["projections"] if q["canonical_game_id"] == "nfl-2026-reg-03-atl-gb"]
    assert len(quotes) == 356
    ids = list(dict.fromkeys(q["gsis_player_id"] for q in quotes))
    assert len(ids) >= 4
    now = max(q["kickoff_utc"] for q in quotes) + 6*3600
    source_path = tmp_path / "synthetic-evidence.json"
    source_path.write_text('{"fixture":"fabricated outcomes only"}\n')
    src = _source(source_path, now - 60)
    base = {"game_id": quotes[0]["canonical_game_id"], "actual_kickoff_utc": quotes[0]["kickoff_utc"],
            "result_retrieved_at": now-60, "final_game_source": src, "player_stat_source": src}
    postponed = {**base, "game_status": "postponed", "players": []}
    assert grade_freeze(original["freeze_sha"], [postponed], now=now)["new_revisions"] == 0
    markets = {q["market"] for q in quotes if q["gsis_player_id"] == ids[0]}
    normal_stats = {market: 17.0 for market in markets}
    push_quote = next(q for q in quotes if q["gsis_player_id"] == ids[3])
    push_stats = {push_quote["market"]: push_quote["line"]}
    participation_active = {**src, "active_roster": True, "snap_played": True}
    participation_inactive = {**src, "active_roster": False, "snap_played": False}
    final = {**base, "game_status": "final", "final_score": {"home": 21, "away": 17},
             "players": [
                 {"gsis_player_id": ids[0], "status": "active", "stats": normal_stats},
                 {"gsis_player_id": ids[1], "status": "inactive", "stats": {},
                  "participation_evidence": participation_inactive},
                 {"gsis_player_id": ids[2], "status": "active", "stats": {
                     q["market"]: 0 for q in quotes if q["gsis_player_id"] == ids[2]},
                  "participation_evidence": participation_active},
                 {"gsis_player_id": ids[3], "status": "active", "stats": push_stats,
                  "participation_evidence": participation_active}]}
    first = grade_freeze(original["freeze_sha"], [final], now=now)
    assert first["new_revisions"] > 0
    grades = [json.loads(row["payload"]) for row in db.q("SELECT payload FROM nfl_prop_grade_revisions")]
    assert {"void", "push", "won", "lost"} <= {g["result"] for g in grades}
    assert any(g["actual_stat"] == 0 for g in grades)
    assert all(g["final_score"] == {"home": 21, "away": 17} for g in grades)
    assert all(g["final_game_source"]["sha256"] and g["player_stat_source"]["sha256"] for g in grades)
    assert all(g["absolute_error"] is not None for g in grades if g["actual_stat"] is not None)
    assert grade_freeze(original["freeze_sha"], [final], now=now+1)["new_revisions"] == 0
    corrected = json.loads(json.dumps(final))
    corrected["players"][0]["stats"] = {market: value+5 for market, value in normal_stats.items()}
    revision = grade_freeze(original["freeze_sha"], [corrected], now=now+2)
    assert revision["corrections"] > 0
    changed_id = next(g["quote_id"] for g in grades if g["gsis_player_id"] == ids[0])
    history = db.q("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=? ORDER BY revision", (changed_id,))
    assert len(history) == 2
    assert json.loads(history[0]["payload"])["actual_stat"] != json.loads(history[1]["payload"])["actual_stat"]
    report = forward_report(original["freeze_sha"])["prospective_2026"]
    assert report["quote_sides_graded"] == len(grades)
    assert report["markets"]["receiving_yards"]["calibration"]
    assert report["conditional_settlement_by_sportsbook"]
    assert db.q1("SELECT payload FROM nfl_prop_research_freezes")["payload"] == original["payload"]
    assert hashlib.sha256(db.q1("SELECT payload FROM nfl_prop_research_freezes")["payload"].encode()).hexdigest() == original["freeze_sha"]
