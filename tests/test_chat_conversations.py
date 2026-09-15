"""End-to-end chat conversations, not isolated function checks.

Every test drives a whole turn: message + UI selection + history -> resolved event
-> tool arguments -> answer. Both paths are covered and reported separately: the
deterministic router (no OpenAI key) and the OpenAI orchestration path with a mocked
client. No test makes a paid call.
"""
import datetime as dt
import json

import pytest

from edgeterminal import db
from edgeterminal.agents import context as C
from edgeterminal.agents import llm, tools
from edgeterminal.config import settings

# Tue 15 Sep 2026, 08:00 America/New_York.
NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()
THURSDAY_NIGHT = "2026-09-18T00:15:00Z"      # Thu 20:15 ET -- Friday's UTC date
SUNDAY = "2026-09-20T17:00:00Z"


@pytest.fixture()
def slate(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'chat.db'}")
    monkeypatch.setattr(settings, "demo_mode", False)
    db.init()
    rows = [
        ("thu1", "Detroit Lions @ Buffalo Bills", "Buffalo Bills", "Detroit Lions",
         THURSDAY_NIGHT, "scheduled"),
        ("sun1", "Carolina Panthers @ Atlanta Falcons", "Atlanta Falcons",
         "Carolina Panthers", SUNDAY, "scheduled"),
        ("sun2", "Minnesota Vikings @ Chicago Bears", "Chicago Bears",
         "Minnesota Vikings", SUNDAY, "scheduled"),
        ("old1", "Denver Broncos @ Kansas City Chiefs", "Kansas City Chiefs",
         "Denver Broncos", "2026-09-11T00:15:00Z", "STATUS_FINAL"),
        ("demo-x", "Dallas Cowboys @ Philadelphia Eagles", "Philadelphia Eagles",
         "Dallas Cowboys", THURSDAY_NIGHT, "scheduled"),
    ]
    for eid, name, home, away, start, status in rows:
        db.ex("INSERT OR REPLACE INTO events(id,sport,league,name,home_team,away_team,"
              "start_time,venue,status,meta,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (eid, "NFL", "NFL", name, home, away, start, "", status, "{}", NOW))
    # Stamp the schedule as freshly retrieved. Without this every turn would see
    # a never-retrieved schedule, trigger the on-demand refresh, and reach a real
    # provider -- tests must not touch the network. Tests that want a stale
    # schedule set this back themselves via `stale_schedule`.
    from edgeterminal import engine
    db.kv_set(engine._schedule_key("NFL"),
              dict(at=NOW, source="test-fixture", events=len(rows), ok=True))
    # No test may reach a provider. The default stub records the call and
    # reports a successful refresh that changed nothing, which is what a
    # correct-but-incomplete schedule actually looks like. Tests that care about
    # refresh behaviour replace this.
    refreshes = []

    def _stub(sport="NFL"):
        refreshes.append(sport)
        db.kv_set(engine._schedule_key(sport),
                  dict(at=NOW, source="test-fixture", events=len(rows), ok=True))
        return dict(ok=True, sport=sport, source="test-fixture", events=len(rows),
                    changed=[], is_demo=False, error=None)

    monkeypatch.setattr(engine, "refresh_schedule", _stub)
    yield dict(refreshes=refreshes, rows=rows)
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def ctx_for(message, selected=None, history=None):
    return C.build(message, history or [], selected, NOW)


# ------------------------------------------------------------- event resolution
def test_sunday_selected_but_user_asks_for_thursday_night(slate):
    """The original defect: a Sunday selection answered a Thursday question."""
    ctx = ctx_for("Who's going to score a touchdown on the Thursday night game?",
                  selected="sun1")
    assert ctx["resolution"]["state"] == "OK"
    assert ctx["resolution"]["event_id"] == "thu1"
    assert ctx["resolution"]["source"] == "explicit-request"


def test_thursday_evening_eastern_is_stored_on_fridays_utc_date(slate):
    """The kickoff's UTC date is Friday; the request is still Thursday."""
    assert THURSDAY_NIGHT.startswith("2026-09-18")
    kickoff = C.local_kickoff(THURSDAY_NIGHT)
    assert kickoff.strftime("%a") == "Thu" and kickoff.hour == 20
    assert ctx_for("thursday night game")["resolution"]["event_id"] == "thu1"


def test_tonight_with_no_game_is_reported_not_substituted(slate):
    ctx = ctx_for("Any NFL games tonight?")
    assert ctx["resolution"]["state"] == "UNAVAILABLE"
    answer = llm._answer_without_event(ctx)
    assert "no nfl game is scheduled tonight" in answer.lower()
    assert "Detroit Lions @ Buffalo Bills" in answer      # next slate offered


def test_this_game_uses_the_selected_event(slate):
    ctx = ctx_for("Who scores a touchdown in this game?", selected="sun1")
    assert ctx["resolution"]["event_id"] == "sun1"


def test_followup_changing_market_keeps_the_resolved_event(slate):
    ctx = ctx_for("What about receiving yards?", selected="sun1")
    assert ctx["resolution"]["event_id"] == "sun1"
    assert ctx["market"] == "player_reception_yards"


def test_followup_explicitly_switching_games_overrides_selection(slate):
    ctx = ctx_for("Now show me the Vikings game", selected="sun1")
    assert ctx["resolution"]["event_id"] == "sun2"


def test_latest_message_outranks_stale_conversation_context(slate):
    history = [{"role": "assistant", "content": "Analysis for event id sun1 follows."}]
    ctx = ctx_for("What about the Thursday night game?", selected=None, history=history)
    assert ctx["resolution"]["event_id"] == "thu1"


def test_explicit_team_and_day_conflict_is_reported_not_guessed(slate):
    ctx = ctx_for("How do the Vikings look on Thursday night?")
    assert ctx["resolution"]["state"] == "UNAVAILABLE"
    assert "thursday" in ctx["resolution"]["reason"].lower()


def test_ambiguous_slot_asks_one_question(slate):
    ctx = ctx_for("Who's scoring a touchdown on Sunday?")
    assert ctx["resolution"]["state"] == "AMBIGUOUS"
    answer = llm._answer_without_event(ctx)
    assert "which one" in answer.lower()
    assert "Atlanta Falcons" in answer and "Chicago Bears" in answer


def test_completed_and_demo_games_are_never_resolved(slate):
    ids = {e["id"] for e in C._candidates(C.local_now(NOW))}
    assert "old1" not in ids          # already played
    assert "demo-x" not in ids        # demo fixture


# ------------------------------------------------------- tool boundary enforcement
def test_wrong_event_id_from_the_model_is_blocked(slate):
    ctx = ctx_for("Touchdown scorers on the Thursday night game?", selected="sun1")
    args = llm.normalize_tool_args("analyze_td_scorers", {"event_id": "sun1"},
                                   "Thursday night", ctx)
    assert args["event_id"] == "thu1"


def test_unresolved_event_does_not_pin_a_tool(slate):
    ctx = ctx_for("Any NFL games tonight?")
    args = llm.normalize_tool_args("analyze_td_scorers", {}, "tonight", ctx)
    assert "event_id" not in args or args.get("event_id") is None


# --------------------------------------------------- OpenAI orchestration (mocked)
class FakeOpenAI:
    """Mocked OpenAI client. Records what the model was told and what it called."""
    name = "openai"
    model = "gpt-4o-mini"

    def __init__(self, outputs):
        self.outputs, self.messages = list(outputs), []

    def chat(self, messages, tool_schemas):
        self.messages.append(messages)
        return self.outputs.pop(0) if self.outputs else {"content": "done", "tool_calls": []}


def _run(monkeypatch, fake, message, selected=None, history=None, tool_result=None):
    seen = []
    monkeypatch.setattr(llm, "provider", lambda: fake)
    monkeypatch.setattr(tools, "call",
                        lambda n, a: (seen.append((n, dict(a))),
                                      tool_result or {"state": "NO QUALIFYING PROP"})[1])
    out = llm.run_chat(message, history or [], None, selected_event_id=selected, now=NOW)
    return out, seen


def test_openai_path_is_pinned_to_the_resolved_event(slate, monkeypatch):
    """The model asks for the selected Sunday game; the boundary redirects it."""
    fake = FakeOpenAI([
        {"content": "", "tool_calls": [{"id": "1", "name": "analyze_td_scorers",
                                        "arguments": json.dumps({"event_id": "sun1"})}]},
        {"content": "Detroit Lions @ Buffalo Bills: here are the candidates.",
         "tool_calls": []},
    ])
    out, seen = _run(monkeypatch, fake,
                     "Who's going to score a touchdown on the Thursday night game?",
                     selected="sun1")
    assert seen[0][1]["event_id"] == "thu1"
    assert out["validation"]["ok"] is True


def test_openai_path_receives_the_resolved_event_in_its_system_context(slate, monkeypatch):
    fake = FakeOpenAI([{"content": "ok", "tool_calls": []}])
    _run(monkeypatch, fake, "Who scores on the Thursday night game?", selected="sun1")
    system = "\n".join(m["content"] for m in fake.messages[0] if m["role"] == "system")
    assert "thu1" in system and "Detroit Lions @ Buffalo Bills" in system
    assert "America/New_York" in system


def test_openai_answer_naming_the_wrong_game_is_corrected_once(slate, monkeypatch):
    fake = FakeOpenAI([
        {"content": "", "tool_calls": [{"id": "1", "name": "analyze_td_scorers",
                                        "arguments": json.dumps({"event_id": "thu1"})}]},
        {"content": "For Carolina Panthers @ Atlanta Falcons, take the Falcons backs.",
         "tool_calls": []},
        {"content": "Detroit Lions @ Buffalo Bills: candidates below.", "tool_calls": []},
    ])
    out, _ = _run(monkeypatch, fake, "Touchdown scorers Thursday night?", selected="sun1")
    assert "Atlanta Falcons" not in out["answer"]
    assert out["validation"]["ok"] is True


def test_openai_answer_that_stays_wrong_returns_a_flagged_partial(slate, monkeypatch):
    wrong = {"content": "Carolina Panthers @ Atlanta Falcons is the play.", "tool_calls": []}
    fake = FakeOpenAI([
        {"content": "", "tool_calls": [{"id": "1", "name": "analyze_td_scorers",
                                        "arguments": json.dumps({"event_id": "thu1"})}]},
        wrong, dict(wrong),
    ])
    out, _ = _run(monkeypatch, fake, "Touchdown scorers Thursday night?", selected="sun1")
    assert out["validation"]["ok"] is False
    assert "could not be validated" in out["answer"]


def test_research_provider_failure_does_not_fabricate(slate, monkeypatch):
    fake = FakeOpenAI([
        {"content": "", "tool_calls": [{"id": "1", "name": "search_player_context",
                                        "arguments": json.dumps({"player": "X"})}]},
        {"content": "Research is unavailable right now.", "tool_calls": []},
    ])
    out, _ = _run(monkeypatch, fake, "What's the latest on roles Thursday night?",
                  selected="thu1", tool_result={"error": "search provider unavailable"})
    assert "unavailable" in out["answer"].lower()


def test_prices_available_without_official_pick_still_answer(slate, monkeypatch):
    board = {"state": "NO QUALIFYING PROP", "official": [], "recommendations": [],
             "prices": [{"player": "A", "price": 150, "book": "dk",
                         "implied_prob": 0.4, "market_prob": None, "model_prob": None}],
             "market": {"books": 6, "priced": 20, "no_vig_players": 0}}
    fake = FakeOpenAI([
        {"content": "", "tool_calls": [{"id": "1", "name": "analyze_td_scorers",
                                        "arguments": json.dumps({"event_id": "thu1"})}]},
        {"content": "No Official Pick. Verified prices: A +150 at dk.", "tool_calls": []},
    ])
    out, _ = _run(monkeypatch, fake, "Who might score Thursday night?", selected="thu1",
                  tool_result=board)
    assert "+150" in out["answer"] or "150" in out["answer"]
    assert out["validation"]["ok"] is True


def test_undated_research_is_not_labelled_current(slate):
    """A Week 1 article with no publication date must not read as current."""
    from edgeterminal.research.sources import relevance_label
    assert relevance_label(None, NOW, topic="role") == "undated"
    assert relevance_label(NOW - 40 * 86400, NOW, topic="role") == "stale"


def test_resolved_event_with_no_props_reports_that_game(slate, monkeypatch):
    """The right game with unavailable prices, not another game's board.

    The failure this guards against is substitution: reporting a different
    fixture's props because the requested one has none, or calling the absence a
    model PASS.
    """
    empty = {"state": "PRICES UNAVAILABLE", "official": [], "recommendations": [],
             "prices": [], "market": {"books": 0, "priced": 0, "no_vig_players": 0}}
    fake = FakeOpenAI([
        {"content": "", "tool_calls": [{"id": "1", "name": "analyze_td_scorers",
                                        "arguments": json.dumps({"event_id": "thu1"})}]},
        {"content": "Detroit Lions @ Buffalo Bills: no anytime-TD prices are posted yet.",
         "tool_calls": []},
    ])
    out, seen = _run(monkeypatch, fake, "Who might score Thursday night?", selected="sun1",
                     tool_result=empty)
    assert seen[0][1]["event_id"] == "thu1"          # asked about the requested game
    assert "Atlanta Falcons" not in out["answer"]    # no substituted fixture
    assert "Bears" not in out["answer"]
    assert out["validation"]["ok"] is True


def test_llm_failure_keeps_the_resolved_event(slate, monkeypatch):
    """A provider exception must not revert to the pre-fix wrong-game behaviour.

    The degraded paths (provider raises, or the model answers with no tool call)
    dropped the validated context, so the deterministic router fell back to the
    selected-event note -- reproducing the original defect whenever OpenAI failed.
    """
    class Boom(FakeOpenAI):
        def chat(self, messages, tool_schemas):
            raise RuntimeError("upstream 503")

    note = ("The user is currently viewing Carolina Panthers @ Atlanta Falcons (NFL), "
            "event id sun1.")
    seen = []
    monkeypatch.setattr(llm, "provider", lambda: Boom([]))
    monkeypatch.setattr(tools, "call",
                        lambda n, a: (seen.append((n, dict(a))),
                                      {"state": "NO QUALIFYING PROP"})[1])
    out = llm.run_chat("Who scores a touchdown on the Thursday night game?", [], note,
                       selected_event_id="sun1", now=NOW)
    assert out["llm_error"] == "RuntimeError"
    assert seen, "the deterministic fallback must still answer"
    assert any(a.get("event_id") == "thu1" or a.get("query") == "thu1" for _, a in seen), seen
    assert "sun1" not in json.dumps(seen)


def test_cached_analysis_cannot_be_served_for_another_game(slate):
    """Analysis context is cached per event id, so one game cannot serve another."""
    from edgeterminal import engine
    engine._ctx_cache.clear()
    engine._ctx_cache["sun1"] = (dt.datetime.now().timestamp(),
                                 {"event": {"id": "sun1", "name": "Carolina Panthers @ "
                                                                 "Atlanta Falcons"}})
    assert engine._ctx_cache.get("thu1") is None
    engine._ctx_cache.clear()


def test_note_pins_the_event_when_context_building_fails(slate, monkeypatch):
    """With no structured context, the selected game must still not be dropped.

    The refactor computed the note-derived id but then pinned it only through the
    context object, so a context-build failure sent prop tools to whichever game
    was next on the schedule.
    """
    seen = []
    monkeypatch.setattr(tools, "call",
                        lambda n, a: (seen.append((n, dict(a))), {"state": "OK"})[1])
    llm.fallback_answer("Who scores a touchdown?", [],
                        "The user is currently viewing X (NFL), event id sun1.", None)
    assert seen[0][1].get("event_id") == "sun1", seen


# ------------------------------------------------- schedule freshness (task 2)
def _set_schedule_age(seconds_old):
    """Stamp the stored schedule's retrieval time relative to the frozen clock."""
    from edgeterminal import engine
    db.kv_set(engine._schedule_key("NFL"),
              dict(at=NOW - seconds_old, source="test-fixture", events=5, ok=True))


def test_stale_schedule_is_refreshed_before_a_day_is_resolved(slate, monkeypatch):
    """A weekday must not be resolved against a schedule past its freshness limit."""
    from edgeterminal import engine
    _set_schedule_age(settings.stale_schedule_s + 60)
    calls = []

    def _refresh(sport="NFL"):
        calls.append(sport)
        db.kv_set(engine._schedule_key(sport),
                  dict(at=NOW, source="refreshed", events=5, ok=True))
        return dict(ok=True, sport=sport, source="refreshed", events=5,
                    changed=[], is_demo=False, error=None)

    monkeypatch.setattr(engine, "refresh_schedule", _refresh)
    ctx = ctx_for("Who scores on the Thursday night game?")
    assert calls == ["NFL"], "exactly one refresh, before resolving"
    assert ctx["schedule"]["refresh_ok"] is True
    assert ctx["schedule"]["uncertain"] is False
    assert ctx["resolution"]["event_id"] == "thu1"


def test_fresh_schedule_is_not_refreshed(slate, monkeypatch):
    from edgeterminal import engine
    _set_schedule_age(10)
    calls = []
    monkeypatch.setattr(engine, "refresh_schedule",
                        lambda sport="NFL": calls.append(sport))
    ctx = ctx_for("Who scores on the Thursday night game?", selected="thu1")
    assert calls == [], "a fresh schedule must not spend a provider call"
    assert ctx["resolution"]["event_id"] == "thu1"


def test_rescheduled_kickoff_changes_which_day_matches(slate, monkeypatch):
    """A game flexed off Thursday must stop answering a Thursday question."""
    from edgeterminal import engine
    _set_schedule_age(settings.stale_schedule_s + 60)

    def _refresh(sport="NFL"):
        # The Thursday game is moved to Sunday by the refresh.
        db.ex("UPDATE events SET start_time=? WHERE id=?", (SUNDAY, "thu1"))
        db.kv_set(engine._schedule_key(sport),
                  dict(at=NOW, source="refreshed", events=5, ok=True))
        return dict(ok=True, sport=sport, source="refreshed", events=5,
                    changed=["thu1"], is_demo=False, error=None)

    monkeypatch.setattr(engine, "refresh_schedule", _refresh)
    ctx = ctx_for("Who scores on the Thursday night game?")
    # No game is on Thursday any more: say so rather than answering about the
    # game that used to be there.
    assert ctx["resolution"]["state"] == "UNAVAILABLE"
    assert "thursday" in ctx["resolution"]["reason"].lower()


def test_refresh_failure_reports_staleness_and_does_not_verify(slate, monkeypatch):
    from edgeterminal import engine
    _set_schedule_age(settings.stale_schedule_s + 500)
    monkeypatch.setattr(engine, "refresh_schedule",
                        lambda sport="NFL": dict(ok=False, sport=sport,
                                                 error="provider unavailable", changed=[],
                                                 events=0))
    ctx = ctx_for("Who scores on the Thursday night game?")
    sched = ctx["schedule"]
    assert sched["refresh_attempted"] is True and sched["refresh_ok"] is False
    assert sched["uncertain"] is True
    assert sched["retrieved_at"] is not None, "the last-known timestamp stays visible"
    assert "provider unavailable" in sched["note"]
    # Stale is never relabelled as newly verified.
    note = C.schedule_note(ctx)
    assert "UNVERIFIED" in note and "refreshed for this question" not in note


def test_refresh_failure_withholds_a_recommendation_on_the_deterministic_path(slate, monkeypatch):
    from edgeterminal import engine
    _set_schedule_age(settings.stale_schedule_s + 500)
    monkeypatch.setattr(engine, "refresh_schedule",
                        lambda sport="NFL": dict(ok=False, sport=sport,
                                                 error="provider unavailable", changed=[],
                                                 events=0))
    called = []
    monkeypatch.setattr(tools, "call", lambda n, a: (called.append(n), {"state": "OK"})[1])
    ctx = ctx_for("What's the best bet on the Thursday night game?")
    out = llm.fallback_answer("What's the best bet on the Thursday night game?", [], None, ctx)
    assert out["recommendation_withheld"] is True
    assert called == [], "no recommendation tool runs against an unverified event"
    assert "could not be verified" in out["answer"]


def test_unverified_schedule_blocks_an_official_pick_on_the_openai_path(slate, monkeypatch):
    from edgeterminal import engine
    _set_schedule_age(settings.stale_schedule_s + 500)
    monkeypatch.setattr(engine, "refresh_schedule",
                        lambda sport="NFL": dict(ok=False, sport=sport,
                                                 error="provider unavailable", changed=[],
                                                 events=0))
    fake = FakeOpenAI([
        {"content": "", "tool_calls": [{"id": "1", "name": "analyze_td_scorers",
                                        "arguments": json.dumps({"event_id": "thu1"})}]},
        {"content": "OFFICIAL PICK: take the Bills backs.", "tool_calls": []},
        {"content": "Detroit Lions @ Buffalo Bills: prices only; the schedule is "
                    "unverified so no staked pick is offered.", "tool_calls": []},
    ])
    out, _ = _run(monkeypatch, fake, "Best bet on the Thursday night game?", selected="thu1")
    assert "OFFICIAL PICK" not in out["answer"]
    assert out["validation"]["ok"] is True
    assert "unverified" in out["answer"].lower()


def test_a_refresh_that_moves_a_kickoff_marks_the_mapping_uncertain(slate, monkeypatch):
    """Re-resolving is not enough: the row must still say what it said."""
    from edgeterminal import engine
    _set_schedule_age(10)
    ctx = ctx_for("Who scores on the Thursday night game?")
    # Simulate the row moving after resolution, then re-validate the mapping.
    db.ex("UPDATE events SET home_team=? WHERE id=?", ("Miami Dolphins", "thu1"))
    problems = C._validate_mapping(ctx["resolution"], ctx["now_local"])
    assert any("home_team changed after refresh" in p for p in problems)


def test_a_kicked_off_game_still_marked_scheduled_triggers_a_refresh(slate, monkeypatch):
    """Stored metadata that contradicts itself is a staleness signal by itself."""
    from edgeterminal import engine
    _set_schedule_age(10)          # timestamp says fresh...
    db.ex("UPDATE events SET start_time=?, status=? WHERE id=?",
          ("2026-09-14T00:15:00Z", "scheduled", "sun2"))   # ...but this row disagrees
    calls = []

    def _refresh(sport="NFL"):
        calls.append(sport)
        db.kv_set(engine._schedule_key(sport),
                  dict(at=NOW, source="refreshed", events=5, ok=True))
        return dict(ok=True, sport=sport, source="refreshed", events=5,
                    changed=[], is_demo=False, error=None)

    monkeypatch.setattr(engine, "refresh_schedule", _refresh)
    ctx = ctx_for("Who scores on the Thursday night game?")
    assert calls == ["NFL"]
    assert "contradicts itself" in (ctx["schedule"]["refresh_reason"] or "")


def test_refresh_is_attempted_at_most_once_per_turn(slate, monkeypatch):
    from edgeterminal import engine
    _set_schedule_age(settings.stale_schedule_s + 60)
    calls = []

    def _refresh(sport="NFL"):
        calls.append(sport)
        # Deliberately does NOT update the timestamp: a second pass would
        # otherwise see it as still stale and loop.
        return dict(ok=True, sport=sport, source="refreshed", events=5,
                    changed=[], is_demo=False, error=None)

    monkeypatch.setattr(engine, "refresh_schedule", _refresh)
    ctx_for("Who scores on the Thursday night game?")
    assert len(calls) == 1, "one attempt per turn, never a retry loop"
