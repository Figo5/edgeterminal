"""Quote freshness and usage accounting.

The defect these pin: prop rows carried no timestamp at all, so `_todays_age`
returned 0.0 for every board. A price of any age read as zero seconds old, the
stale-quote gate could never fire, and confidence always took full freshness
credit. Reading a cache must report the original age, not reset it.
"""
import datetime as dt
import time

import pytest

from edgeterminal import props
from edgeterminal.config import settings
from edgeterminal.providers import theoddsapi as toa
from edgeterminal.integrity import parse_time
from edgeterminal.research import cache as rcache

# Tue 15 Sep 2026 12:00 UTC -- one frozen clock for every test in this file,
# deliberately BEFORE the fixture event's kickoff (Thu 2026-09-18 00:15 UTC) so
# the stored game is still upcoming under the frozen clock and the provider
# payload is not rejected as already finished.
NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def _payload(last_update="2026-09-15T11:40:00Z"):
    return {
        "id": "ev1", "home_team": "Buffalo Bills", "away_team": "Detroit Lions",
        "bookmakers": [{
            "key": "draftkings", "last_update": "2026-09-15T11:30:00Z",
            "markets": [{
                "key": "player_anytime_td", "last_update": last_update,
                "outcomes": [
                    {"description": "Jahmyr Gibbs", "name": "Yes", "price": -275},
                    {"description": "James Cook", "name": "Yes", "price": -150},
                ],
            }],
        }],
    }


class _Api(toa.TheOddsApi):
    """Just the flattener and the cache: no network, no key requirement."""

    def __init__(self):
        self.key = "test"
        self.quota = {}
        self.last_fetch = {}


# ---------------------------------------------------------------- timestamps
def test_prop_rows_carry_capture_and_provider_timestamps():
    rows = _Api()._flatten_props(_payload(), "ev1", retrieved_at=NOW)
    assert rows, "the payload must flatten to rows"
    market_t = parse_time("2026-09-15T11:40:00Z")
    book_t = parse_time("2026-09-15T11:30:00Z")
    for r in rows:
        assert r["captured_at"] == NOW
        # The market's own last_update wins over the bookmaker's.
        assert r["provider_updated_at"] == pytest.approx(market_t)
        assert r["provider_updated_at"] != book_t


def test_age_is_measured_from_the_provider_timestamp_not_the_fetch():
    rows = _Api()._flatten_props(_payload(), "ev1", retrieved_at=NOW)
    provider_t = rows[0]["provider_updated_at"]
    age = props.quote_age_s(rows, now=provider_t + 600)
    assert age == pytest.approx(600, abs=1), "age comes from when the book last moved"


def test_untimestamped_rows_report_unknown_age_not_zero():
    """The original bug: no stamps used to mean 'zero seconds old'."""
    assert props._todays_age([{"price": -110}], NOW) is None
    assert props._todays_age([], NOW) is None


def test_unknown_age_is_not_recommendable():
    state = props.prop_state(confidence=95.0, edge_pct=10.0, books=6, age_s=None,
                             agreement=1.0, usage_available=True, is_demo=False)
    assert state == "INSUFFICIENT DATA", "unverified freshness is not fresh"


def test_old_prices_still_trip_the_existing_stale_gate():
    state = props.prop_state(confidence=95.0, edge_pct=10.0, books=6,
                             age_s=settings.stale_odds_s + 1,
                             agreement=1.0, usage_available=True, is_demo=False)
    assert state == "STALE"


def test_fresh_prices_are_unaffected():
    state = props.prop_state(confidence=95.0, edge_pct=10.0, books=6, age_s=30.0,
                             agreement=1.0, usage_available=True, is_demo=False)
    assert state == "OFFICIAL PICK"


# -------------------------------------------------------------------- caching
def test_a_cache_hit_does_not_reset_quote_age(monkeypatch):
    """Serving from cache must report the original age, not a fresh one."""
    store = rcache.TTLCache(now=lambda: NOW)  # same frozen clock: the cache keeps
    # serving the board so the test can prove the true age comes from the row
    # stamps versus the fetch clock, not from a TTL restamp.
    api = _Api()
    captured = {}

    def _fake_get(url, params, provider=None):
        return _payload(), {}

    monkeypatch.setattr(toa, "get_json_meta", _fake_get)
    monkeypatch.setattr(toa.time, "time", lambda: NOW)
    event = {"id": "ev1", "home_team": "Buffalo Bills", "away_team": "Detroit Lions",
             "start_time": "2026-09-18T00:15:00Z"}
    first = api.fetch_event_props("ev1", markets=["player_anytime_td"],
                                  cache=store, event=event)
    assert first["cache"] == "miss"
    captured["retrieved_at"] = first["retrieved_at"]
    assert captured["retrieved_at"] == NOW

    # Twenty-one minutes later, served from cache.
    later = NOW + 21 * 60
    monkeypatch.setattr(toa.time, "time", lambda: later)
    second = api.fetch_event_props("ev1", markets=["player_anytime_td"],
                                   cache=store, event=event)
    assert second["cache"] == "hit"
    assert second["retrieved_at"] == captured["retrieved_at"], \
        "a cache read must not restamp the payload"
    age = props._todays_age(second["rows"], later)
    assert age is not None and age >= 21 * 60, \
        f"a 21-minute-old cached board must report its real age, got {age}"
    assert age > settings.stale_odds_s or settings.stale_odds_s >= 21 * 60


def test_cached_rows_keep_their_own_timestamps():
    rows = _Api()._flatten_props(_payload(), "ev1", retrieved_at=NOW)
    # Round-tripping through the cache must not touch the stamps.
    store = rcache.TTLCache()
    store.set("k", {"rows": rows}, ttl=600.0, topic="odds")
    back = store.get("k")["rows"]
    assert [r["captured_at"] for r in back] == [NOW] * len(rows)


# ------------------------------------------------- usage accounting (task 3)
def test_a_citation_url_is_not_evidence_of_a_paid_search():
    """A utm_source on a link says where the link was formatted, not that a
    billable search ran. Only real tool-call/telemetry records count."""
    from edgeterminal.agents import llm

    trace = [dict(tool="analyze_td_scorers", arguments={"event_id": "e1"}, ok=True,
                  result_preview='{"citations": [{"url": '
                                 '"https://example.com/a?utm_source=openai"}]}')]
    assert llm.searches_used(trace) == 0, \
        "a citation URL must never be counted as a search invocation"


def test_only_recorded_search_tool_calls_are_counted():
    from edgeterminal.agents import llm

    trace = [dict(tool="search_web", arguments={"query": "x"}, ok=True, result_preview="{}"),
             dict(tool="search_player_context", arguments={"player": "y"}, ok=True,
                  result_preview="{}"),
             dict(tool="get_schedule", arguments={}, ok=True, result_preview="{}")]
    assert llm.searches_used(trace) == 2


def test_usage_is_unknown_rather_than_guessed_when_no_trace_exists():
    from edgeterminal.agents import llm
    assert llm.searches_used(None) is None
    assert llm.searches_used([]) == 0
