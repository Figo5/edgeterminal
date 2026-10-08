"""Fixture-only scheduler, quota, availability, and immutable PAPER capture tests."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from edgeterminal import db, nfl_prop_capture_ops, nfl_props, paper_operations, paper_research, research_refresh
from edgeterminal.config import settings
from edgeterminal.nfl_context_observations import record

GAME = "nfl-2026-reg-04-buf-hou"
KICKOFF = datetime(2026, 10, 1, 0, 15, tzinfo=timezone.utc).timestamp()
FIXTURE = Path(__file__).parent / "fixtures/sgo_nfl_props.json"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'capture.db'}")
    monkeypatch.setattr(settings, "sportsgameodds_api_key", "fixture-key")
    monkeypatch.setattr(research_refresh, "LOCK", tmp_path / "research.lock")
    db.init()
    db.upsert_event(dict(id=GAME, sport="NFL", league="NFL", home_team="Houston Texans",
                         away_team="Buffalo Bills", start_time="2026-10-01T00:15:00Z",
                         status="STATUS_SCHEDULED", meta={"season": 2026, "season_type": 2,
                                                          "week": 4}))
    yield monkeypatch
    db._conn.close()
    db._conn = None


def _event():
    return {"id": GAME, "kickoff_utc": KICKOFF, "start_time": "2026-10-01T00:15:00Z",
            "status": "STATUS_SCHEDULED", "home_team": "Houston Texans",
            "away_team": "Buffalo Bills", "meta": {}}


def _provider(stamp: float, *, better_line: bool = False, failure: int | None = None):
    def getter(_url, *, params, headers, provider, retries):
        assert params["includeAltLines"] in {"true", "false"}
        assert headers["x-api-key"] == "fixture-key"
        if failure:
            return failure, b'{"success":false}', {"Retry-After": "60"}
        payload = copy.deepcopy(json.loads(FIXTURE.read_text()))
        updated = datetime.fromtimestamp(stamp - 60, timezone.utc).isoformat().replace("+00:00", "Z")
        for odd in payload["data"][0]["odds"].values():
            for offer in odd["byBookmaker"].values():
                offer["lastUpdatedAt"] = updated
                if params["includeAltLines"] == "false":
                    offer.pop("altLines", None)
        if better_line:
            payload["data"][0]["odds"]["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]["draftkings"]["overUnder"] = "220.5"
        return 200, json.dumps(payload).encode(), {"X-RateLimit-Remaining-Objects": "2000"}
    return nfl_props.SportsGameOddsProps(api_key="fixture-key", getter=getter)


def _freeze_original(stamp: float):
    captured = nfl_props.capture_week(2026, 4, provider=_provider(stamp), retrieved_at=stamp,
                                      batch_id="original-batch")
    source = db.q1("SELECT * FROM nfl_prop_snapshots WHERE batch_id=? AND market='passing_yards' "
                   "AND sportsbook='draftkings' AND line_type='main'", (captured["batch_id"],))
    projection, sigma = 300.0, 25.0
    over = .5 * math.erfc((source["line"] - projection)/(sigma * math.sqrt(2)))
    quote = {"quote_id": source["id"], "canonical_game_id": GAME,
             "player_id": source["player_id"], "player_name": source["player_name"],
             "gsis_player_id": "00-0000001",
             "market": source["market"], "side": source["side"], "line": source["line"],
             "price": source["price"], "sportsbook": source["sportsbook"],
             "observed_at": stamp, "kickoff_utc": KICKOFF,
             "model_version": "fixture-point-v1", "projection": projection,
             "uncertainty_sigma": sigma, "probability_over": round(over, 6),
             "probability_under": round(1-over, 6)}
    body = json.dumps({"metadata": {"model_hash": "fixture-model-sha",
                                    "source_batch_id": captured["batch_id"],
                                    "created_at": stamp + 1},
                       "projections": [quote]}, sort_keys=True, separators=(",", ":"))
    freeze_sha = hashlib.sha256(body.encode()).hexdigest()
    db.ex("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)",
          (freeze_sha, captured["batch_id"], stamp + 1, body))
    return freeze_sha


def test_cadence_and_quota_cooldown_are_fail_closed(lab):
    assert nfl_prop_capture_ops.cadence(25*3600) is None
    assert nfl_prop_capture_ops.cadence(12*3600) == 6*3600
    assert nfl_prop_capture_ops.cadence(3*3600) == 3600
    assert nfl_prop_capture_ops.cadence(45*60) == 15*60
    assert nfl_prop_capture_ops.cadence(10*60) == 5*60
    assert nfl_prop_capture_ops.cadence(-1) is None
    frozen_at = KICKOFF - 3600
    _freeze_original(frozen_at)
    planned = nfl_prop_capture_ops.plan([_event()], KICKOFF - 600)
    assert planned["due"] == {(2026, 4): [GAME]}
    with pytest.raises(RuntimeError, match="HTTP 429"):
        nfl_prop_capture_ops.capture(2026, 4, [GAME], KICKOFF - 600,
                                     provider=_provider(KICKOFF - 600, failure=429))
    attempt = db.q1("SELECT * FROM nfl_prop_capture_attempts")
    assert attempt["requests_made"] == attempt["responses_archived"] == 1
    assert not attempt["success"] and "429" in attempt["error"]
    assert db.q1("SELECT http_status FROM nfl_prop_api_request_results WHERE batch_id=?",
                 (attempt["batch_id"],))["http_status"] == 429
    blocked = nfl_prop_capture_ops.plan([_event()], KICKOFF - 300)
    assert blocked["due"] == {} and blocked["blocked_reason"] == "provider_quota_cooldown"
    assert nfl_prop_capture_ops.plan([_event()], KICKOFF - 300, key_configured=False)["due"] == {}


def test_manual_prop_check_uses_shorter_cooldown_with_same_quota_guard(lab):
    _freeze_original(KICKOFF - 25 * 3600)
    first = KICKOFF - 12 * 3600
    nfl_prop_capture_ops.capture(2026, 4, [GAME], first, provider=_provider(first))
    assert nfl_prop_capture_ops.plan([_event()], first + 20 * 60, manual=True)["due"] == {}
    assert nfl_prop_capture_ops.plan([_event()], first + 35 * 60)["due"] == {}
    assert nfl_prop_capture_ops.plan([_event()], first + 35 * 60, manual=True)["due"] == {
        (2026, 4): [GAME]}


def test_offline_capture_records_request_and_backs_off(lab):
    _freeze_original(KICKOFF - 3600)
    def offline(*_args, **_kwargs):
        raise TimeoutError("fixture offline")
    provider = nfl_props.SportsGameOddsProps(api_key="fixture-key", getter=offline)
    with pytest.raises(RuntimeError, match="TimeoutError"):
        nfl_prop_capture_ops.capture(2026, 4, [GAME], KICKOFF - 600, provider=provider)
    row = db.q1("SELECT * FROM nfl_prop_capture_attempts")
    assert row["requests_made"] == 1 and row["responses_archived"] == 0
    assert db.q1("SELECT http_status,error FROM nfl_prop_api_request_results WHERE batch_id=?",
                 (row["batch_id"],)) == {"http_status": None, "error": "TimeoutError"}
    assert nfl_prop_capture_ops.plan([_event()], KICKOFF - 300)["due"] == {}


def test_missed_capture_window_is_reported_without_creating_a_quote(lab):
    _freeze_original(KICKOFF - 25*3600)
    before = db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"]
    health = nfl_prop_capture_ops.health(KICKOFF - 20*3600, [_event()])
    assert health["requests_today"] == 0
    assert health["upcoming_capture_windows"][0]["overdue_seconds"] == 4*3600
    assert "capture_window_overdue; no missing quote is fabricated" in health["warnings"]
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == before
    after = nfl_prop_capture_ops.health(KICKOFF + 60, [_event()])
    assert after["missed_final_window_games"] == [GAME]
    assert "final_pregame_capture_window_missed" in after["warnings"]


def test_scheduler_captures_pairs_evaluates_and_preserves_later_line(lab):
    initial = KICKOFF - 3600
    freeze_sha = _freeze_original(initial)
    frozen_payload = db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?",
                           (freeze_sha,))["payload"]
    current = [KICKOFF - 600]
    real_capture = nfl_prop_capture_ops.capture
    def fixture_capture(season, week, game_ids, now):
        return real_capture(season, week, game_ids, now,
                            provider=_provider(current[0], better_line=current[0] > KICKOFF - 600))
    lab.setattr(research_refresh.nfl_prop_capture_ops, "capture", fixture_capture)
    lab.setattr(research_refresh, "_events", lambda _now: [_event()])
    lab.setattr(research_refresh.engine, "refresh_schedule", lambda _sport: {"ok": True})
    lab.setattr(research_refresh, "_weekly", lambda *_args: {"freeze": "already_frozen"})
    lab.setattr(research_refresh.nfl_market_capture, "capture_week", lambda *_args, **_kwargs: {"target_games": 1})
    lab.setattr(research_refresh, "_market", lambda _now: {"target_games": 1})
    lab.setattr(research_refresh, "_context", lambda *_args: {"games": 1, "observations": 0})
    def available(_events, now):
        record({"game_id": GAME, "kind": "qb_availability", "subject": "Josh Allen",
                "payload": {"status": "active"}, "source_url": "fixture:ESPN",
                "fetched_at": now-120, "kickoff_utc": KICKOFF}, now=now)
        return {"games": 1, "observations": 1, "source": "fixture:ESPN"}
    lab.setattr(research_refresh, "_availability", available)
    first = research_refresh.run(now=current[0])
    assert first["success"], first["steps"]
    assert first["steps"]["prop_near_game_2026_04"]["result"]["requests_made"] == 1
    assert first["steps"]["prop_pair_2026_04"]["result"]["paired"] == 2
    assert first["steps"]["paper_policy"]["result"]["positions_created"] == 1
    position = db.q1("SELECT * FROM paper_positions")
    original = dict(position)
    assert position["freeze_sha"] == freeze_sha and position["quote_age_seconds"] == 60
    assert position["availability_age_seconds"] == 120
    assert position["policy_hash"] == paper_research.policy_hash(paper_research.active_policy())
    assert position["model_hash"] == "fixture-model-sha"
    assert position["probability_difference"] >= .25
    audit = paper_operations.audit_positions(now=current[0])
    assert audit["verified"] == 1 and audit["failed"] == 0
    assert audit["positions"][0]["grade_quote_id"] != position["quote_id"]
    assert audit["positions"][0]["settlement_status"] == "pending"
    capture_attempt = db.q1("SELECT * FROM nfl_prop_capture_attempts ORDER BY attempted_at DESC LIMIT 1")
    assert capture_attempt["requests_made"] == capture_attempt["responses_archived"] == 1
    assert json.loads(capture_attempt["quota_json"])[0]["x-ratelimit-remaining-objects"] == "2000"
    assert db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?",
                 (freeze_sha,))["payload"] == frozen_payload
    assert paper_research.pair_batch(first["steps"]["prop_near_game_2026_04"]["result"]["batch_id"],
                                     now=current[0])["paired"] == 0
    current[0] += 300
    second = research_refresh.run(now=current[0])
    assert second["success"], second["steps"]
    assert db.q1("SELECT COUNT(*) n FROM paper_positions")["n"] == 1
    assert dict(db.q1("SELECT * FROM paper_positions")) == original
    signatures = {json.loads(row["payload"])["metadata"]["model_signature"] for row in
                  db.q("SELECT payload FROM paper_prop_quote_pairings")}
    assert signatures == {"fixture-model-sha"}
    history = paper_research.dashboard(now=current[0])["recent"][0]["later_same_market_observations"]
    assert history["count"] >= 1 and history["latest"]["line"] == 220.5
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_capture_attempts")["n"] == 2
    paired = db.q1("SELECT frozen_quote_id FROM paper_prop_quote_pairings WHERE quote_id=?",
                   (position["quote_id"],))
    assert paired and paired["frozen_quote_id"] != position["quote_id"]
    for revision, actual in ((1, position["line"] + 5), (2, position["line"] - 5)):
        evidence = f"fixture-grade-{revision}"
        db.ex("INSERT INTO nfl_prop_grade_evidence VALUES(?,?,?,?,?,?,?,?)",
              (evidence, freeze_sha, GAME, "final", "fixture:official-stats",
               KICKOFF + 5*3600 + revision, KICKOFF + 5*3600 + revision, "{}"))
        grade = {"canonical_game_id": GAME, "player_id": position["player_id"],
                 "market": position["market"], "player_status": "active",
                 "actual_stat": actual, "research_statistical_result": "won",
                 "participation_evidence": {"active_roster": True, "snap_played": True},
                 "sportsbook": "fanduel", "frozen_line": position["line"] + 10,
                 "frozen_side": "under", "frozen_price": -120}
        db.ex("INSERT INTO nfl_prop_grade_revisions VALUES(?,?,?,?,?,?)",
              (freeze_sha, paired["frozen_quote_id"], revision, evidence,
               KICKOFF + 5*3600 + revision, json.dumps(grade)))
        assert paper_research.needs_settlement()
        assert paper_research.settle(now=KICKOFF + 5*3600 + revision) == 1
        assert paper_research.settle(now=KICKOFF + 5*3600 + revision) == 0
    settlements = db.q("SELECT * FROM paper_settlement_revisions ORDER BY revision")
    assert [row["result"] for row in settlements] == ["win", "loss"]
    assert all(row["source_frozen_quote_id"] == paired["frozen_quote_id"] for row in settlements)
    assert all(row["rules_sha256"] and row["rule_snapshot_json"] for row in settlements)
    assert db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?",
                 (freeze_sha,))["payload"] == frozen_payload
