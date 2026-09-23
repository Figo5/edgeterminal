"""SportsGameOdds fixture tests. No test spends provider quota."""
import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from edgeterminal import db, nfl_prop_coverage, nfl_props
from edgeterminal.api import app, nfl_prop_board
from edgeterminal.config import settings
from edgeterminal.providers.base import ProviderError

FIXTURE = Path(__file__).parent / "fixtures/sgo_nfl_props.json"
GAME = "nfl-2026-reg-04-buf-hou"
STAMP = datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def prop_db(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'props.db'}")
    db.init()
    db.upsert_event(dict(id=GAME, sport="NFL", league="NFL", home_team="Houston Texans",
                         away_team="Buffalo Bills", start_time="2026-10-01T00:15:00Z",
                         status="STATUS_SCHEDULED", meta={"season": 2026, "season_type": 2, "week": 4}))
    yield
    db._conn.close()
    db._conn = None


def fixture():
    return json.loads(FIXTURE.read_text())


class FixtureProvider:
    def __init__(self, payload=None):
        self.payload = fixture() if payload is None else payload

    def fetch(self, starts_after, starts_before, batch_id=None):
        assert starts_after < starts_before
        return copy.deepcopy(self.payload)


def test_all_eight_markets_identity_and_immutable_board(prop_db):
    report = nfl_props.capture_week(2026, 4, FixtureProvider(), STAMP)
    assert report["events_seen"] == report["events_mapped"] == 1
    assert report["event_mapping_rate"] == 1
    assert report["quotes_stored"] == 12
    assert report["alternate_quotes"] == 2
    assert set(report["bookmakers"]) == {"draftkings", "fanduel"}
    assert set(report["markets"]) == set(nfl_props.STAT_MARKETS.values()) | {"anytime_touchdowns"}
    assert db.mapped_nfl_game("sportsgameodds", "sgo-fixture-buf-hou") == GAME
    assert db.q1("SELECT player_id FROM nfl_prop_player_aliases")["player_id"] == "sgo:JOSH_ALLEN_1_NFL"
    payload = nfl_prop_board(GAME)
    assert payload["readiness"] == "RESEARCH_ONLY"
    assert payload["designation"] == "PASS"
    assert payload["quote_count"] == 12
    assert {row["line"] for row in payload["quotes"]
            if row["market"] == "passing_yards" and row["sportsbook"] == "draftkings"} == {219.5, 224.5, 229.5}
    assert any(route.path == "/api/research/nfl/prop-board/{game_id}" and
               "GET" in route.methods for route in app.routes)
    assert "recommendations" not in payload
    anytime = nfl_prop_board(GAME, "anytime_touchdowns")["quotes"]
    assert {row["side"] for row in anytime} == {"yes"}
    assert all(row["line"] is None for row in anytime)
    assert all(row["team"] == "Buffalo Bills" and row["opponent"] == "Houston Texans"
               for row in payload["quotes"])
    assert all(row["source"] == nfl_props.URL and row["provider"] == "sportsgameodds"
               for row in payload["quotes"])
    assert db.q1("SELECT COUNT(*) n FROM odds_snapshots")["n"] == 0
    assert db.q1("SELECT COUNT(*) n FROM predictions")["n"] == 0
    raw = db.q1("SELECT raw_payload,raw_sha256 FROM nfl_prop_capture_batches")
    import hashlib
    assert hashlib.sha256(raw["raw_payload"].encode()).hexdigest() == raw["raw_sha256"]
    coverage = nfl_props.coverage_report(report["batch_id"])
    assert coverage["games"][0]["quotes"] == 12
    assert coverage["alternate_quotes"] == 2
    assert set(coverage["normalization_samples"]) == set(nfl_props.STAT_MARKETS.values()) | {"anytime_touchdowns"}
    assert all(sample["all_passed"] for sample in coverage["normalization_samples"].values())
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("UPDATE nfl_prop_snapshots SET price=-105 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_prop_capture_batches")


def test_repeat_capture_appends_history_and_board_uses_latest(prop_db):
    original = fixture()
    nfl_props.capture_week(2026, 4, FixtureProvider(original), STAMP)
    newer = fixture()
    newer["data"][0]["odds"]["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]["draftkings"]["odds"] = "-105"
    nfl_props.capture_week(2026, 4, FixtureProvider(newer), STAMP + 60)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 24
    board = nfl_props.board(GAME, "passing_yards")
    assert board["quote_count"] == 4
    assert next(row for row in board["quotes"] if row["sportsbook"] == "draftkings"
                and row["line_type"] == "main")["price"] == -105
    assert db.q1("SELECT price FROM nfl_prop_snapshots WHERE id=1")["price"] == -110


def test_newer_empty_capture_does_not_resurface_stale_quotes(prop_db):
    nfl_props.capture_week(2026, 4, FixtureProvider(), STAMP)
    payload = fixture()
    for odd in payload["data"][0]["odds"].values():
        for offer in odd["byBookmaker"].values():
            offer["available"] = False
            for alternate in offer.get("altLines", []):
                alternate["available"] = False
    result = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP + 60)
    assert result["quotes_stored"] == 0
    assert nfl_props.board(GAME)["quote_count"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 12


def test_unmapped_event_is_counted_but_never_attached(prop_db):
    payload = fixture()
    payload["data"][0]["teams"]["away"]["names"]["long"] = "New York Jets"
    report = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert report["events_seen"] == 1 and report["events_mapped"] == 0
    assert report["quotes_stored"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 0


def test_provider_cancellation_and_local_postponement_cannot_store_quotes(prop_db):
    payload = fixture()
    payload["data"][0]["status"]["cancelled"] = True
    report = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert report["events_seen"] == 1 and report["quotes_stored"] == 0
    db.upsert_event(dict(id=GAME, sport="NFL", league="NFL", home_team="Houston Texans",
                         away_team="Buffalo Bills", start_time="2026-10-01T00:15:00Z",
                         status="POSTPONED", meta={"season": 2026, "season_type": 2, "week": 4}))
    with pytest.raises(ValueError, match="no canonical, live NFL schedule"):
        nfl_props.capture_week(2026, 4, FixtureProvider(), STAMP + 60)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 0


def test_player_name_conflict_and_bad_market_fail_without_new_rows(prop_db):
    nfl_props.capture_week(2026, 4, FixtureProvider(), STAMP)
    payload = fixture()
    payload["data"][0]["players"]["JOSH_ALLEN_1_NFL"]["name"] = "Different Person"
    with pytest.raises(ValueError, match="changed name"):
        nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP + 60)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 12
    payload = fixture()
    payload["data"][0]["odds"]["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["sideID"] = "under"
    report = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP + 60)
    assert report["normalization_warnings"]["sgo-fixture-buf-hou"]["odd_id_mismatch"] == 1
    assert report["quotes_stored"] == 8
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_capture_batches")["n"] == 2


def test_missing_line_player_team_and_unavailable_quote_are_skipped(prop_db):
    payload = fixture()
    odds = payload["data"][0]["odds"]
    odds["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]["draftkings"].pop("overUnder")
    odds["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]["fanduel"]["available"] = False
    report = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert report["quotes_stored"] == 10
    assert nfl_props.board(GAME, "passing_yards")["quote_count"] == 2
    assert report["normalization_warnings"]["sgo-fixture-buf-hou"] == {
        "missing_or_invalid_line": 1, "unavailable_offer": 1}


def test_no_key_and_malformed_page_do_not_persist(prop_db):
    called = []
    def getter(*args, **kwargs):
        called.append(kwargs)
        return {"success": False, "data": []}
    with pytest.raises(ProviderError, match="SPORTSGAMEODDS_API_KEY"):
        nfl_props.capture_week(2026, 4, nfl_props.SportsGameOddsProps(api_key="", getter=getter), STAMP)
    assert called == []
    with pytest.raises(ValueError, match="malformed"):
        nfl_props.capture_week(2026, 4, nfl_props.SportsGameOddsProps(api_key="test", getter=getter), STAMP)
    assert called[0]["headers"] == {"x-api-key": "test"}
    assert "apiKey" not in called[0]["params"]
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_capture_batches")["n"] == 0


def test_fetch_paginates_with_header_auth_and_bounded_cursor(prop_db):
    pages = [dict(success=True, data=[fixture()["data"][0]], nextCursor="cursor-2"),
             dict(success=True, data=[], nextCursor=None)]
    calls = []
    def getter(url, **kwargs):
        calls.append((url, kwargs))
        return pages.pop(0)
    provider = nfl_props.SportsGameOddsProps(api_key="fixture-key", getter=getter)
    result = provider.fetch(STAMP, STAMP + 86400)
    assert len(result["data"]) == 1
    assert len(calls) == 2
    assert calls[0][0] == "https://api.sportsgameodds.com/v2/events"
    assert calls[0][1]["params"]["leagueID"] == "NFL"
    assert calls[0][1]["params"]["includeAltLines"] == "true"
    assert calls[1][1]["params"]["cursor"] == "cursor-2"
    assert calls[0][1]["headers"] == {"x-api-key": "fixture-key"}
    def loop_cursor(url, **kwargs):
        return dict(success=True, data=[], nextCursor="same")
    with pytest.raises(ValueError, match="pagination cursor"):
        nfl_props.SportsGameOddsProps(api_key="fixture-key", getter=loop_cursor).fetch(STAMP, STAMP + 86400)


def test_book_name_normalization_and_conflicting_alias_fail_closed(prop_db):
    payload = fixture()
    offers = payload["data"][0]["odds"]["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]
    offers["Draft Kings"] = offers.pop("draftkings")
    report = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert "draftkings" in report["bookmakers"]
    payload = fixture()
    offers = payload["data"][0]["odds"]["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]
    offers["Draft Kings"] = dict(offers["draftkings"])
    with pytest.raises(ValueError, match="duplicate normalized sportsbook"):
        nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP + 60)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 12


def test_raw_http_body_is_archived_byte_exact_before_parsing(prop_db):
    raw = (b' {\n  "success" : true, "data" : [], "nextCursor" : null\n}\n')
    def getter(url, **kwargs):
        return 200, raw, {"X-RateLimit-Remaining": "9", "Set-Cookie": "private"}
    batch_id = "raw-test-batch"
    result = nfl_props.SportsGameOddsProps(api_key="fixture", getter=getter).fetch(
        STAMP, STAMP + 86400, batch_id=batch_id)
    assert result["_request_count"] == 1
    row = db.q1("SELECT body,body_sha256,response_headers FROM nfl_prop_raw_responses WHERE batch_id=?",
                (batch_id,))
    import hashlib
    assert row["body"] == raw
    assert row["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert json.loads(row["response_headers"])["x-ratelimit-remaining"] == "9"
    assert "set-cookie" not in row["response_headers"]
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("UPDATE nfl_prop_raw_responses SET body=? WHERE batch_id=?", (b"different", batch_id))


def test_bad_json_is_preserved_and_no_quotes_are_saved(prop_db):
    def getter(url, **kwargs):
        return 200, b"bad json", {}
    with pytest.raises(ValueError, match="malformed SportsGameOdds JSON"):
        nfl_props.capture_week(2026, 4,
                               nfl_props.SportsGameOddsProps(api_key="fixture", getter=getter), STAMP)
    assert db.q1("SELECT body FROM nfl_prop_raw_responses")["body"] == b"bad json"
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 0


def test_rate_limit_response_is_archived_without_retry(prop_db):
    calls = []
    def getter(url, **kwargs):
        calls.append(url)
        return 429, b'{"error":"rate limited"}', {"retry-after": "60"}
    with pytest.raises(ProviderError, match="HTTP 429"):
        nfl_props.capture_week(2026, 4,
                               nfl_props.SportsGameOddsProps(api_key="fixture", getter=getter), STAMP)
    assert calls == [nfl_props.URL]
    row = db.q1("SELECT http_status,body,response_headers FROM nfl_prop_raw_responses")
    assert row["http_status"] == 429
    assert row["body"] == b'{"error":"rate limited"}'
    assert json.loads(row["response_headers"])["retry-after"] == "60"


def test_invalid_offers_are_counted_by_reason(prop_db):
    payload = fixture()
    odd = payload["data"][0]["odds"]["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]
    odd["byBookmaker"]["draftkings"]["odds"] = "invalid"
    odd["byBookmaker"]["fanduel"]["overUnder"] = None
    odd["byBookmaker"]["draftkings"]["altLines"].append({"odds": "-110", "available": True})
    result = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    warnings = result["normalization_warnings"]["sgo-fixture-buf-hou"]
    assert warnings["invalid_price"] == 1
    assert warnings["missing_or_invalid_line"] == 2
    assert result["alternate_quotes"] == 2


def test_archived_response_replays_without_network(prop_db):
    payload = fixture()
    payload["notice"] = "Fixture plan limitation"
    body = json.dumps(payload, indent=2).encode()
    calls = []
    def getter(url, **kwargs):
        calls.append(url)
        return 200, body, {"x-ratelimit-remaining": "7"}
    batch_id = "replay-test-batch"
    kickoff = datetime(2026, 10, 1, 0, 15, tzinfo=timezone.utc).timestamp()
    nfl_props.SportsGameOddsProps(api_key="fixture", getter=getter).fetch(
        kickoff - 4 * 3600, kickoff + 4 * 3600, batch_id=batch_id)
    result = nfl_props.capture_week(2026, 4, provider=nfl_props.ArchivedSportsGameOddsProps(),
                                    retrieved_at=STAMP, batch_id=batch_id)
    assert calls == [nfl_props.URL]
    assert result["live_requests"] == 0 and result["archived_requests"] == 1
    assert result["provider_notices"] == ["Fixture plan limitation"]
    assert result["quotes_stored"] == 12
    assert db.q1("SELECT body FROM nfl_prop_raw_responses WHERE batch_id=?", (batch_id,))["body"] == body
    with pytest.raises(ValueError, match="already been sealed"):
        nfl_props.capture_week(2026, 4, provider=nfl_props.ArchivedSportsGameOddsProps(),
                               retrieved_at=STAMP, batch_id=batch_id)


def test_migrates_existing_prop_snapshot_table_without_source(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    path = tmp_path / "legacy-props.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE nfl_prop_snapshots (id INTEGER PRIMARY KEY, canonical_game_id TEXT, "
                    "market TEXT, player_id TEXT, sportsbook TEXT, retrieved_at REAL)")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{path}")
    db.init()
    columns = {row["name"] for row in db.q("PRAGMA table_info(nfl_prop_snapshots)")}
    assert {"source", "line_type", "offer_index"} <= columns
    db._conn.close()
    db._conn = None


def test_report_file_is_write_once(prop_db, tmp_path):
    captured = nfl_props.capture_week(2026, 4, FixtureProvider(), STAMP)
    output = tmp_path / "coverage.json"
    report = nfl_prop_coverage.write_report(captured["batch_id"], output)
    assert report["readiness"] == "RESEARCH_ONLY"
    assert json.loads(output.read_text())["total_quotes"] == 12
    with pytest.raises(FileExistsError):
        nfl_prop_coverage.write_report(captured["batch_id"], output)


def test_postkickoff_and_duplicate_event_fail_closed(prop_db):
    with pytest.raises(ValueError, match="already started"):
        nfl_props.capture_week(2026, 4, FixtureProvider(),
                                datetime(2026, 10, 1, 1, tzinfo=timezone.utc).timestamp())
    payload = fixture()
    payload["data"].append(copy.deepcopy(payload["data"][0]))
    with pytest.raises(ValueError, match="duplicate provider event ID"):
        nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 0
