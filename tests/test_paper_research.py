"""Offline paper research behavior. No provider requests."""
import hashlib
import json
import sqlite3

import pytest

from edgeterminal import db
from edgeterminal.nfl_context_observations import record
from edgeterminal.config import settings
from edgeterminal import paper_research


@pytest.fixture
def research_db(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'paper.db'}")
    db.init()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_disabled_policy_and_immutable_version(research_db):
    policy = paper_research.example_policy()
    assert policy["enabled"] is False
    version = paper_research.register_policy(policy)
    assert paper_research.register_policy(policy) == version
    changed = dict(policy, eligible_markets=["receptions"])
    with pytest.raises(ValueError, match="version"):
        paper_research.register_policy(changed)
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("UPDATE paper_policies SET payload='{}' WHERE version=?", (version,))
    assert paper_research.evaluate(policy, now=50)["positions_created"] == 0
    assert paper_research.dashboard()["starting_bankroll"] == 1000


def test_prospective_position_idempotent_and_correction(research_db):
    policy = dict(paper_research.example_policy(), version="fixture-v1", enabled=True,
                  eligible_markets=["receptions"], min_model_line_difference=0.5,
                  min_probability_difference=0.01)
    paper_research.register_policy(policy)
    game = "nfl-2026-reg-03-atl-gb"
    quote = dict(quote_id=7, canonical_game_id=game, player_id="sgo:a", player_name="A",
                 market="receptions", side="over", line=2.5, price=-110,
                 sportsbook="fanduel", observed_at=50.0, kickoff_utc=100.0,
                 model_version="prop-v1", projection=4.0, probability_over=.70,
                 probability_under=.30, uncertainty_sigma=2.0)
    payload = json.dumps({"metadata": {"model_hash": "fixture-model", "source_batch_id": "batch"},
                          "projections": [quote]}, sort_keys=True)
    freeze_sha = hashlib.sha256(payload.encode()).hexdigest()
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  ("batch", "sportsgameodds", 50, "raw", "{}", 1, 1, 1, "{}"))
        c.execute("INSERT INTO nfl_prop_snapshots(id,batch_id,canonical_game_id,provider_event_id,"
                  "player_id,provider_player_id,player_name,team,opponent,market,line,side,price,"
                  "sportsbook,retrieved_at,kickoff_utc,provider,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (7, "batch", game, "event-1", "sgo:a", "a", "A", "ATL", "GB", "receptions",
                   2.5, "over", -110, "fanduel", 50, 100, "sportsgameodds", "fixture"))
        c.execute("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)", (freeze_sha, "batch", 60, payload))
    record({"game_id": game, "kind": "offensive_injury", "subject": "A",
            "payload": {"status": "available"}, "source_url": "fixture:roster",
            "fetched_at": 65, "kickoff_utc": 100}, now=70)
    first = paper_research.evaluate(policy, now=70)
    assert first["positions_created"] == 1
    assert paper_research.evaluate(policy, now=71)["positions_created"] == 0
    pos = db.q1("SELECT * FROM paper_positions")
    assert pos["quote_id"] == 7 and pos["policy_version"] == "fixture-v1"
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("UPDATE paper_positions SET stake=999 WHERE id=?", (pos["id"],))
    assert paper_research.dashboard()["open_positions"] == 1
    grade = dict(result="won", sportsbook_settlement_interpretation="won",
                 actual_stat=5.0, sportsbook="fanduel", frozen_line=2.5,
                 frozen_side="over", frozen_price=-110)
    for revision, outcome in ((1, grade), (2, dict(grade, result="lost",
                                                   sportsbook_settlement_interpretation="lost",
                                                   actual_stat=1.0))):
        db.ex("INSERT INTO nfl_prop_grade_evidence VALUES(?,?,?,?,?,?,?,?)",
              (f"evidence-{revision}", freeze_sha, game, "final", "fixture:stats",
               200+revision, 200+revision, "{}"))
        db.ex("INSERT INTO nfl_prop_grade_revisions VALUES(?,?,?,?,?,?)",
              (freeze_sha, 7, revision, f"evidence-{revision}", 200+revision,
               json.dumps(outcome)))
        assert paper_research.settle() == 1
        assert paper_research.settle() == 0
    assert db.q1("SELECT COUNT(*) n FROM paper_settlement_revisions")["n"] == 2
    assert paper_research.dashboard()["current_bankroll"] == pytest.approx(990.0)
    for revision, interpretation in ((3, "push"), (4, "void"), (5, "undetermined")):
        outcome = dict(grade, result=interpretation,
                       sportsbook_settlement_interpretation=interpretation)
        db.ex("INSERT INTO nfl_prop_grade_evidence VALUES(?,?,?,?,?,?,?,?)",
              (f"evidence-{revision}", freeze_sha, game, "final", "fixture:stats",
               200+revision, 200+revision, "{}"))
        db.ex("INSERT INTO nfl_prop_grade_revisions VALUES(?,?,?,?,?,?)",
              (freeze_sha, 7, revision, f"evidence-{revision}", 200+revision,
               json.dumps(outcome)))
        assert paper_research.needs_settlement()
        assert paper_research.settle() == 1
    latest = paper_research.dashboard()
    assert latest["open_positions"] == 1
    assert latest["current_bankroll"] == 1000
    assert latest["available_bankroll"] == 990
    assert [r["result"] for r in db.q("SELECT result FROM paper_settlement_revisions ORDER BY revision")] == [
        "win", "loss", "push", "void", "pending"]


def test_game_moneyline_uses_frozen_quote_and_append_only_grade(research_db, tmp_path):
    policy = dict(paper_research.example_policy(), version="game-fixture-v1", enabled=True,
                  eligible_markets=["moneyline"], min_model_line_difference=0,
                  min_probability_difference=0.01)
    game_id = "2026_03_ATL_GB"
    canonical = "nfl-2026-reg-03-atl-gb"
    freeze = {"status": "RESEARCH_ONLY", "designation": "PASS",
              "games": [{"game_id": game_id, "home": "Green Bay Packers",
                         "away": "Atlanta Falcons", "model": {"margin": 7.0,
                         "home_win_prob": 0.7}}]}
    raw = json.dumps(freeze).encode()
    sha = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "game-freeze.json"
    path.write_bytes(raw)
    with db.tx() as c:
        cur = c.execute("INSERT INTO odds_snapshots(event_id,sport,book,market,selection,price,captured_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (canonical, "NFL", "draftkings", "moneyline", "Green Bay Packers", 110, 50))
        quote_id = cur.lastrowid
        c.execute("INSERT INTO nfl_research_market_freezes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sha, game_id, "draftkings", "moneyline", "Green Bay Packers", None,
                   110, "fixture", 50, 100, "current", "snapshot", quote_id))
        c.execute("INSERT INTO nfl_research_freezes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sha, 2026, 3, "linear", str(path), 60, "linear-v1", "model-sha",
                   "features-v1", "features-sha", 100, 60))
    for team in ("ATL", "GB"):
        record({"game_id": canonical, "kind": "qb_start", "subject": team,
                "payload": {"player_id": f"qb-{team}"}, "source_url": "fixture:starter",
                "fetched_at": 65, "kickoff_utc": 100}, now=70)
    assert paper_research.evaluate(policy, now=70)["positions_created"] == 1
    assert paper_research.evaluate(policy, now=71)["positions_created"] == 0
    position = db.q1("SELECT * FROM paper_positions")
    assert position["source_kind"] == "frozen_game_research"
    db.ex("INSERT INTO nfl_research_grades VALUES(?,?,?,?,?,?,?,?,?)",
          (sha, game_id, 1, 21, 17, "fixture", 200, 200, "{}"))
    assert paper_research.settle(now=201) == 1
    assert paper_research.settle(now=202) == 0
    db.ex("INSERT INTO nfl_research_grades VALUES(?,?,?,?,?,?,?,?,?)",
          (sha, game_id, 2, 17, 21, "fixture-correction", 300, 300, "{}"))
    assert paper_research.settle(now=301) == 1
    assert paper_research.dashboard()["current_bankroll"] == pytest.approx(990.0)
    assert db.q1("SELECT COUNT(*) n FROM paper_settlement_revisions")["n"] == 2
