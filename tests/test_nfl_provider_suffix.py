"""Provider suffix compatibility preserves sourced quotes and immutable identity."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from edgeterminal import db, nfl_props, nfl_prop_lab as lab, nfl_prop_model as model
from edgeterminal.config import settings
from edgeterminal.nfl_prop_grading import grade_freeze
from test_nfl_prop_grading import frozen, _evidence

GAME = "nfl-2026-reg-04-buf-hou"
STAMP = datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp()
FIXTURE = Path(__file__).parent / "fixtures/sgo_nfl_props.json"
WALKER = "KENNETH_WALKER_III_1_NFL"


@pytest.fixture
def capture_db(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "suffix.db"))
    db.init()
    db.upsert_event(dict(id=GAME, sport="NFL", league="NFL", home_team="Houston Texans",
                        away_team="Buffalo Bills", start_time="2026-10-01T00:15:00Z",
                        status="STATUS_SCHEDULED", meta={"season": 2026, "season_type": 2, "week": 4}))
    yield
    db._conn.close()
    db._conn = None


def payload(name, *, player_id=WALKER, first="Kenneth", last="Walker"):
    source = json.loads(FIXTURE.read_text())
    event = source["data"][0]
    person = dict(playerID=player_id, teamID="BUFFALO_BILLS_NFL", name=name)
    if first is not None:
        person["firstName"] = first
    if last is not None:
        person["lastName"] = last
    event["players"] = {player_id: person}
    event["odds"] = {}
    for side in ("over", "under"):
        key = "rushing_yards-" + player_id + "-game-ou-" + side
        event["odds"][key] = dict(oddID=key, statID="rushing_yards", statEntityID=player_id,
                                  playerID=player_id, periodID="game", betTypeID="ou", sideID=side,
                                  byBookmaker={"draftkings": {"available": True, "odds": "-110",
                                                             "overUnder": "59.5"}})
    return source


class FixtureProvider:
    def __init__(self, body):
        self.body = body
        self.calls = 0

    def fetch(self, starts_after, starts_before, batch_id=None):
        assert starts_after < starts_before
        self.calls += 1
        return copy.deepcopy(self.body)


def capture(body, stamp=STAMP, batch="original"):
    return nfl_props.capture_week(2026, 4, FixtureProvider(body), stamp, batch_id=batch)


@pytest.mark.parametrize("old,new,player_id,first,last", [
    ("Kenneth Walker", "Kenneth Walker III", WALKER, "Kenneth", "Walker"),
    ("Kenneth Walker III", "Kenneth Walker", WALKER, "Kenneth", "Walker"),
    ("Brian Robinson", "Brian Robinson Jr.", "BRIAN_ROBINSON_1_NFL", "Brian", "Robinson"),
    ("Brian Robinson", "Brian Robinson Jr", "BRIAN_ROBINSON_1_NFL", "Brian", "Robinson"),
    ("Brian Robinson Jr.", "Brian Robinson Jr", "BRIAN_ROBINSON_1_NFL", "Brian", "Robinson"),
    ("Kenneth Walker III", "kenneth walker iii.", WALKER, " Kenneth ", " Walker "),
    ("D'Andre Swift", "D'Andre Swift II", "DANDRE_SWIFT_1_NFL", "D'Andre", "Swift"),
])
def test_future_capture_preserves_identity_and_projects_same_player(
        capture_db, monkeypatch, old, new, player_id, first, last):
    capture(payload(old, player_id=player_id, first=first, last=last))
    original_alias = db.q("SELECT * FROM nfl_prop_player_aliases")
    original_players = db.q("SELECT * FROM nfl_prop_players")
    original_quotes = db.q("SELECT * FROM nfl_prop_snapshots ORDER BY id")
    original_batch = db.q1("SELECT * FROM nfl_prop_capture_batches WHERE batch_id='original'")
    incoming = payload(new, player_id=player_id, first=first, last=last)
    provider = FixtureProvider(incoming)
    result = nfl_props.capture_week(2026, 4, provider, STAMP + 60, batch_id="suffix")
    assert result["quotes_stored"] == 2 and result["unique_players"] == 1
    quotes = db.q("SELECT * FROM nfl_prop_snapshots WHERE batch_id='suffix' ORDER BY id")
    assert {q["player_name"] for q in quotes} == {new}
    assert {q["player_id"] for q in quotes} == {"sgo:" + player_id}
    assert db.q("SELECT * FROM nfl_prop_player_aliases") == original_alias
    assert db.q("SELECT * FROM nfl_prop_players") == original_players
    assert db.q("SELECT * FROM nfl_prop_snapshots WHERE batch_id='original' ORDER BY id") == original_quotes
    assert db.q1("SELECT * FROM nfl_prop_capture_batches WHERE batch_id='original'") == original_batch
    archived = db.q1("SELECT raw_payload,raw_sha256 FROM nfl_prop_capture_batches WHERE batch_id='suffix'")
    assert json.loads(archived["raw_payload"]) == incoming
    assert hashlib.sha256(archived["raw_payload"].encode()).hexdigest() == archived["raw_sha256"]

    # Exercise real quote pairing, identity matching, frozen serving and probabilities.
    # Only the already-fitted fixture artifact and prior player inputs are supplied.
    config = dict(family="baseline", residuals=[-5.] * 100 + [0.] * 100 + [5.] * 100,
                  conditioning="absolute", probability_method="empirical")
    monkeypatch.setattr(model, "load", lambda: ({"configurations": {m: config for m in lab.MARKETS}},
                                               {m: None for m in lab.MARKETS}))
    monkeypatch.setattr(lab, "_read_stats", lambda _: ([dict(season="2026", week="3", player_id="gsis-1",
                                                           player_display_name=old, team="BUF")], {}))
    features = {key: 0. for key in lab.FEATURES}
    features.update(prior_games=10., carries_last3=15., carries_last5=14.,
                    rushing_yards_last3=62., rushing_yards_last5=60.)
    monkeypatch.setattr(lab, "_week3_features", lambda *a: {("gsis-1", "buf"): {
        "feature": features, "player": [{"carries": 15.}], "opp_hist": []}})
    monkeypatch.setattr(model.time, "time", lambda: STAMP + 120)
    projected = model.project_current({}, {}, {}, "suffix", season=2026, week=4)
    assert projected["identity_matched"] == 1
    assert len(projected["projections"]) == 2
    assert {p["gsis_player_id"] for p in projected["projections"]} == {"gsis-1"}
    assert {p["player_id"] for p in projected["projections"]} == {"sgo:" + player_id}
    assert {p["player_name"] for p in projected["projections"]} == {new}
    assert {p["projection"] for p in projected["projections"]} == {62.}
    assert projected["model_metadata"]["training_cutoff"] == [2026, 3]
    assert projected["model_metadata"]["parameter_fingerprint"] == model.PARAMETER_FINGERPRINT
    assert projected["designation"] == "PASS" and projected["readiness"] == "RESEARCH_ONLY"
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_research_freezes")["n"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"] == 0

    # Replay of a sealed capture fails before another provider read or append.
    with pytest.raises(ValueError, match="sealed"):
        nfl_props.capture_week(2026, 4, provider, STAMP + 120, batch_id="suffix")
    assert provider.calls == 1
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 4


@pytest.mark.parametrize("old,new,first,last", [
    ("Kenneth Walker", "Kenny Walker III", "Kenny", "Walker"),
    ("Kenneth Walker", "Kenneth Walkers III", "Kenneth", "Walkers"),
    ("Brian Robinson Jr.", "Brian Robinson Sr.", "Brian", "Robinson"),
    ("Kenneth Walker II", "Kenneth Walker III", "Kenneth", "Walker"),
    ("Kenneth Walker", "Kenneth Walker III", None, "Walker"),
    ("Kenneth Walker", "Kenneth Walker III", "Kenneth", None),
    ("Kenneth Walker", "Kenneth Walker III", "Ken", "Walker"),
    ("Kenneth Walker", "Kenneth Walker III", "Kenneth", "Other"),
    ("Kenneth Walker", "Kenneth Walker III", "Kenneth", "Walker Jr."),
    ("Kenneth Walker", "Kenneth Walker III", "Kenneth Walker", "III"),
    ("Kenneth Walker", "Kenneth Walker III", True, "Walker"),
    ("Kenneth Walker", "Kenneth Walker VI", "Kenneth", "Walker"),
    ("Kenneth Walker", "Kenneth Walker III extra", "Kenneth", "Walker"),
    ("Kenneth Walker", "Kenneth Walker III Jr.", "Kenneth", "Walker"),
    ("Kenneth Walker III Jr", "Kenneth Walker III Jr.", "Kenneth", "Walker III Jr."),
    ("Walker", "Walker Jr.", "Walker", ""),
    ("Kenneth", "Kenneth III", "Kenneth", "III"),
    ("Kenneth Walker", "K. Walker III", "K.", "Walker"),
])
def test_rename_or_insufficient_identity_evidence_fails_without_appending(capture_db, old, new, first, last):
    capture(payload(old))
    tables = ("nfl_prop_player_aliases", "nfl_prop_players", "nfl_prop_snapshots", "nfl_prop_capture_batches")
    before = {table: db.q("SELECT * FROM " + table) for table in tables}
    with pytest.raises(ValueError, match="changed name"):
        capture(payload(new, first=first, last=last), STAMP + 60, "rejected")
    assert {table: db.q("SELECT * FROM " + table) for table in tables} == before


def test_different_provider_id_cannot_reuse_an_existing_suffix_identity(capture_db):
    capture(payload("Kenneth Walker"))
    capture(payload("Kenneth Walker III", player_id="OTHER_KENNETH_1_NFL"), STAMP + 60, "other")
    quote = db.q1("SELECT player_id FROM nfl_prop_snapshots WHERE batch_id='other'")
    assert quote["player_id"] == "sgo:OTHER_KENNETH_1_NFL"
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_player_aliases")["n"] == 2


def test_suffix_does_not_bypass_player_object_or_odd_identity(capture_db):
    capture(payload("Kenneth Walker"))
    changed = payload("Kenneth Walker III")
    changed["data"][0]["players"][WALKER]["playerID"] = "OTHER_1_NFL"
    result = capture(changed, STAMP + 60, "mismatched")
    assert result["quotes_stored"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_player_aliases")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 2


def test_suffix_override_requires_the_explicit_player_id(capture_db):
    capture(payload("Kenneth Walker"))
    changed = payload("Kenneth Walker III")
    changed["data"][0]["players"][WALKER].pop("playerID")
    with pytest.raises(ValueError, match="changed name"):
        capture(changed, STAMP + 60, "missing-id")
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 2


def test_suffix_capture_archives_the_actual_http_bytes(capture_db):
    capture(payload("Kenneth Walker"))
    raw = (" \n" + json.dumps(payload("Kenneth Walker III"), indent=2) + "\n").encode()
    requests = []
    def getter(url, **kwargs):
        requests.append(url)
        return 200, raw, {"X-RateLimit-Remaining": "9"}
    provider = nfl_props.SportsGameOddsProps(api_key="fixture", getter=getter)
    report = nfl_props.capture_week(2026, 4, provider, STAMP + 60, batch_id="raw-suffix")
    assert report["quotes_stored"] == 2
    archived = db.q1("SELECT * FROM nfl_prop_raw_responses WHERE batch_id='raw-suffix'")
    assert archived["body"] == raw
    assert archived["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(requests) == 1
    assert {r["player_name"] for r in db.q("SELECT player_name FROM nfl_prop_snapshots WHERE batch_id='raw-suffix'")} == {"Kenneth Walker III"}
    assert db.q1("SELECT player_name FROM nfl_prop_player_aliases")["player_name"] == "Kenneth Walker"


def test_future_suffix_capture_preserves_populated_historical_grades(frozen):
    # Reuse the grading suite's isolated historical freeze and real grading API.
    # A later prospective capture must not rewrite its evidence or revisions.
    graded = grade_freeze(frozen, [_evidence()], now=20000.)
    assert graded["new_revisions"] == 6
    tables = ("nfl_prop_research_freezes", "nfl_prop_grade_evidence",
              "nfl_prop_grade_source_artifacts", "nfl_prop_grade_revisions")
    before = {table: db.q("SELECT * FROM " + table) for table in tables}
    db.upsert_event(dict(id=GAME, sport="NFL", league="NFL", home_team="Houston Texans",
                        away_team="Buffalo Bills", start_time="2026-10-01T00:15:00Z",
                        status="STATUS_SCHEDULED", meta={"season": 2026, "season_type": 2, "week": 4}))
    capture(payload("Kenneth Walker"))
    capture(payload("Kenneth Walker III"), STAMP + 60, "future-suffix")
    assert {table: db.q("SELECT * FROM " + table) for table in tables} == before
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots WHERE batch_id='future-suffix'")["n"] == 2
