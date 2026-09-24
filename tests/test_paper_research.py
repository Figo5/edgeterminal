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
        c.execute("INSERT INTO nfl_provider_games VALUES(?,?,?,?)",
                  ("sportsgameodds", "event-1", game, 50))
        c.execute("INSERT INTO nfl_prop_players VALUES(?,?,?)", ("sgo:a", "A", 50))
        c.execute("INSERT INTO nfl_prop_player_aliases VALUES(?,?,?,?,?)",
                  ("sportsgameodds", "a", "sgo:a", "A", 50))
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
    assert pos["policy_hash"] == paper_research.policy_hash(policy)
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


def _fixture_prop_freeze(specs):
    """Create only immutable fixture inputs; no network calls or actual Week 3 data."""
    projections = []
    games = {spec["game"] for spec in specs}
    players = {spec["player"] for spec in specs}
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  ("fixture-batch", "sportsgameodds", 50, "fixture-raw", "{}",
                   len(games), len(games), len(specs), "{}"))
        for game in games:
            c.execute("INSERT INTO nfl_provider_games VALUES(?,?,?,?)",
                      ("sportsgameodds", "event-" + game, game, 50))
        for player in players:
            c.execute("INSERT INTO nfl_prop_players VALUES(?,?,?)",
                      ("sgo:" + player, player, 50))
            c.execute("INSERT INTO nfl_prop_player_aliases VALUES(?,?,?,?,?)",
                      ("sportsgameodds", player, "sgo:" + player, player, 50))
        for spec in specs:
            quote_id = spec["id"]
            game = spec["game"]
            player = spec["player"]
            observed = spec.get("observed", 50)
            kickoff = spec.get("kickoff", 100)
            book = spec.get("book", "fanduel")
            market = spec.get("market", "receptions")
            line = spec.get("line", 2.5)
            price = spec.get("price", -110)
            away, home = game.split("-")[-2:]
            c.execute("INSERT INTO nfl_prop_snapshots(id,batch_id,canonical_game_id,provider_event_id,"
                      "player_id,provider_player_id,player_name,team,opponent,market,line,side,price,"
                      "sportsbook,retrieved_at,kickoff_utc,provider,source,line_type) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (quote_id, "fixture-batch", game, "event-" + game, "sgo:" + player,
                       player, player, away.upper(), home.upper(), market, line, "over", price,
                       book, observed, kickoff, "sportsgameodds", "fixture", spec.get("line_type", "main")))
            projections.append(dict(quote_id=quote_id, canonical_game_id=game,
                                    player_id="sgo:" + player, player_name=player,
                                    market=market, side="over", line=line, price=price,
                                    sportsbook=book, observed_at=observed, kickoff_utc=kickoff,
                                    model_version="fixture-model", projection=line + 2,
                                    probability_over=.90, probability_under=.10,
                                    uncertainty_sigma=2.0))
        payload = json.dumps({"metadata": {"model_hash": "model-fixture-sha",
                                           "source_batch_id": "fixture-batch"},
                              "projections": projections}, sort_keys=True)
        freeze_sha = hashlib.sha256(payload.encode()).hexdigest()
        c.execute("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)",
                  (freeze_sha, "fixture-batch", 60, payload))
    for game, player, status in {(spec["game"], spec["player"], spec.get("status", "available"))
                                 for spec in specs}:
        record({"game_id": game, "kind": "offensive_injury", "subject": player,
                "payload": {"status": status}, "source_url": "fixture:roster",
                "fetched_at": 65, "kickoff_utc": 100}, now=70)
    return freeze_sha


def test_experimental_policy_freshest_quote_and_immutable_hash(research_db):
    policy = paper_research.active_policy()
    assert paper_research.validate_policy(policy) is policy
    assert policy["version"] == "EXPERIMENTAL_PAPER_V1"
    assert policy["eligible_markets"] == ["passing_yards", "rushing_yards", "rushing_attempts",
                                           "receptions", "receiving_yards"]
    assert policy["stake_policy"] == {"type": "flat_dollars", "amount": 10}
    game = "nfl-2026-reg-03-atl-gb"
    _fixture_prop_freeze([{"id": 1, "game": game, "player": "A", "observed": 50,
                           "book": "fanduel"},
                          {"id": 2, "game": game, "player": "A", "observed": 55,
                           "book": "draftkings"},
                          {"id": 3, "game": game, "player": "B", "book": "unlisted"},
                          {"id": 4, "game": game, "player": "C", "line_type": "alternate"},
                          {"id": 5, "game": game, "player": "D", "observed": -1000},
                          {"id": 6, "game": game, "player": "E", "status": "questionable"}])
    result = paper_research.evaluate(policy, now=70)
    assert result["positions_created"] == 1
    assert result["skipped"]["competing_quote_player_market_already_selected"] == 1
    assert result["skipped"]["sportsbook_unsupported"] == 1
    assert result["skipped"]["alternate_line_ineligible"] == 1
    assert result["skipped"]["stale_quote"] == 1
    assert result["skipped"]["injury_status_unresolved"] == 1
    position = db.q1("SELECT * FROM paper_positions")
    assert position["quote_id"] == 2 and position["sportsbook"] == "draftkings"
    assert position["policy_hash"] == paper_research.policy_hash(policy)
    assert position["model_hash"] == "model-fixture-sha"
    assert position["stake"] == 10
    assert paper_research.evaluate(policy, now=71)["positions_created"] == 0
    assert db.q1("SELECT COUNT(*) n FROM paper_positions")["n"] == 1
    assert db.q1("SELECT reason FROM paper_opportunity_skips WHERE quote_id=1")["reason"] == (
        "competing_quote_player_market_already_selected")
    with pytest.raises(ValueError, match="version"):
        paper_research.register_policy(dict(policy, min_probability_difference=.19))


def test_experimental_policy_game_slate_and_exposure_caps(research_db):
    policy = paper_research.active_policy()
    games = ["nfl-2026-reg-03-atl-gb", "nfl-2026-reg-03-ne-jax",
             "nfl-2026-reg-03-bal-dal"]
    specs = [{"id": i+1, "game": games[i//4], "player": f"Player{i}"} for i in range(12)]
    _fixture_prop_freeze(specs)
    result = paper_research.evaluate(policy, now=70)
    assert result["positions_created"] == 9  # three per game, for three games
    assert result["skipped"]["game_position_limit"] == 3
    assert db.q1("SELECT SUM(stake) n FROM paper_positions")["n"] == 90
    assert paper_research.dashboard(now=70)["available_bankroll"] == 910


def test_experimental_policy_slate_cap_and_aggregate_exposure(research_db):
    policy = paper_research.active_policy()
    games = ["atl-gb", "ne-jax", "bal-dal", "lac-buf", "cin-pit", "min-tb",
             "lar-den", "phi-chi", "sea-sf", "hou-ind", "mia-nyj", "car-cle"]
    specs = [{"id": i+1, "game": f"nfl-2026-reg-03-{games[i]}",
              "player": f"Player{i}"} for i in range(12)]
    _fixture_prop_freeze(specs)
    result = paper_research.evaluate(policy, now=70)
    assert result["positions_created"] == 10
    assert result["skipped"]["slate_position_limit"] == 2
    assert db.q1("SELECT SUM(stake) n FROM paper_positions")["n"] == 100
    assert paper_research._prop_position_limit(policy, {"canonical_game_id": "new-game",
                                                  "player_id": "new-player", "market": "receptions",
                                                  "kickoff_utc": 86400 + 100}, 10) == "open_position_limit"
    # With wider count caps, the 10% aggregate exposure remains an independent gate.
    widened = dict(policy, version="fixture-exposure", maximum_open_positions=20,
                   maximum_positions_per_slate=20)
    assert paper_research._prop_position_limit(widened, {"canonical_game_id": "new-game",
                                                   "player_id": "new-player", "market": "receptions",
                                                   "kickoff_utc": 86400 + 100}, 10) == "bankroll_constraint"
