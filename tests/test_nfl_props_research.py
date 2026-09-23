"""SportsGameOdds fixture tests. No test spends provider quota."""
import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from edgeterminal import db, nfl_props
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

    def fetch(self, starts_after, starts_before):
        assert starts_after < starts_before
        return copy.deepcopy(self.payload)


def test_all_eight_markets_identity_and_immutable_board(prop_db):
    report = nfl_props.capture_week(2026, 4, FixtureProvider(), STAMP)
    assert report["events_seen"] == report["events_mapped"] == 1
    assert report["event_mapping_rate"] == 1
    assert report["quotes_stored"] == 10
    assert set(report["bookmakers"]) == {"draftkings", "fanduel"}
    assert set(report["markets"]) == set(nfl_props.STAT_MARKETS.values()) | {"anytime_touchdowns"}
    assert db.mapped_nfl_game("sportsgameodds", "sgo-fixture-buf-hou") == GAME
    assert db.q1("SELECT player_id FROM nfl_prop_player_aliases")["player_id"] == "sgo:JOSH_ALLEN_1_NFL"
    payload = nfl_prop_board(GAME)
    assert payload["readiness"] == "RESEARCH_ONLY"
    assert payload["designation"] == "PASS"
    assert payload["quote_count"] == 10
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
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 20
    board = nfl_props.board(GAME, "passing_yards")
    assert board["quote_count"] == 2
    assert next(row for row in board["quotes"] if row["sportsbook"] == "draftkings")["price"] == -105
    assert db.q1("SELECT price FROM nfl_prop_snapshots WHERE id=1")["price"] == -110


def test_unmapped_event_is_counted_but_never_attached(prop_db):
    payload = fixture()
    payload["data"][0]["teams"]["away"]["names"]["long"] = "New York Jets"
    report = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert report["events_seen"] == 1 and report["events_mapped"] == 0
    assert report["quotes_stored"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 0


def test_player_name_conflict_and_bad_market_fail_without_new_rows(prop_db):
    nfl_props.capture_week(2026, 4, FixtureProvider(), STAMP)
    payload = fixture()
    payload["data"][0]["players"]["JOSH_ALLEN_1_NFL"]["name"] = "Different Person"
    with pytest.raises(ValueError, match="changed name"):
        nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP + 60)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 10
    payload = fixture()
    payload["data"][0]["odds"]["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["sideID"] = "under"
    with pytest.raises(ValueError, match="oddID disagrees"):
        nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP + 60)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_capture_batches")["n"] == 1


def test_missing_line_player_team_and_unavailable_quote_are_skipped(prop_db):
    payload = fixture()
    odds = payload["data"][0]["odds"]
    odds["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]["draftkings"].pop("overUnder")
    odds["passing_yards-JOSH_ALLEN_1_NFL-game-ou-over"]["byBookmaker"]["fanduel"]["available"] = False
    report = nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert report["quotes_stored"] == 8
    assert nfl_props.board(GAME, "passing_yards")["quote_count"] == 0


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
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 10


def test_postkickoff_and_duplicate_event_fail_closed(prop_db):
    with pytest.raises(ValueError, match="already started"):
        nfl_props.capture_week(2026, 4, FixtureProvider(),
                                datetime(2026, 10, 1, 1, tzinfo=timezone.utc).timestamp())
    payload = fixture()
    payload["data"].append(copy.deepcopy(payload["data"][0]))
    with pytest.raises(ValueError, match="duplicate provider event ID"):
        nfl_props.capture_week(2026, 4, FixtureProvider(payload), STAMP)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_snapshots")["n"] == 0
