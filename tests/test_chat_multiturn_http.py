"""The user's actual three-turn browser conversation, over real HTTP.

These drive `POST /api/chat` through FastAPI's serialization exactly as the
browser does, with one conversation carried across all three turns. Nothing here
seeds a turn with the correct event: whatever Turn 1 returns is what Turn 2 must
send back, and Turn 2's result is what Turn 3 must send back. A test that seeds
each turn independently would pass against the broken build, which is precisely
how this defect was previously reported as fixed.

The reported failure:
  T1 "Who might score a touchdown in the Thursday night game" -> Lions @ Bills
  T2 "What about receiving yards in that game"                -> Vikings @ Bears
  T3 "Show me the available prices ..."                       -> Vikings moneyline

Providers and the model are mocked; no test makes a paid call.
"""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from edgeterminal import db
from edgeterminal.agents import llm, tools
from edgeterminal.config import settings

# Tue 15 Sep 2026, 08:00 America/New_York.
NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()
THU = "2026-09-18T00:15:00Z"      # Thu 20:15 ET -- Friday's UTC date
SUN = "2026-09-20T17:00:00Z"

THU_ID = "thu-lions-bills"
SUN_ID = "sun-vikings-bears"
SUN2_ID = "sun-panthers-falcons"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'multi.db'}")
    monkeypatch.setattr(settings, "demo_mode", False)
    db.init()
    rows = [
        (THU_ID, "Detroit Lions @ Buffalo Bills", "Buffalo Bills", "Detroit Lions", THU),
        (SUN_ID, "Minnesota Vikings @ Chicago Bears", "Chicago Bears",
         "Minnesota Vikings", SUN),
        (SUN2_ID, "Carolina Panthers @ Atlanta Falcons", "Atlanta Falcons",
         "Carolina Panthers", SUN),
    ]
    for eid, name, home, away, start in rows:
        db.ex("INSERT OR REPLACE INTO events(id,sport,league,name,home_team,away_team,"
              "start_time,venue,status,meta,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (eid, "NFL", "NFL", name, home, away, start, "", "scheduled", "{}", NOW))

    from edgeterminal import engine
    db.kv_set(engine._schedule_key("NFL"),
              dict(at=NOW, source="test-fixture", events=len(rows), ok=True))
    monkeypatch.setattr(engine, "refresh_schedule",
                        lambda sport="NFL": dict(ok=True, sport=sport, source="test-fixture",
                                                 events=len(rows), changed=[], error=None))
    # The whole suite runs on a frozen clock so "Thursday" is deterministic.
    monkeypatch.setattr(llm, "_now_override", NOW, raising=False)
    monkeypatch.setattr(llm.time, "time", lambda: NOW)

    with TestClient(app_under_test()) as c:
        yield c
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def app_under_test():
    from edgeterminal.api import app
    return app


class Recorder:
    """Mocked model. Scripts one reply per round and records tool calls."""

    name = "openai"
    model = "gpt-4o-mini"

    def __init__(self):
        self.script = []
        self.calls = []          # tool calls the model asked for
        self.systems = []        # system context each turn received

    def queue(self, *replies):
        self.script.extend(replies)

    def chat(self, messages, tool_schemas):
        self.systems.append("\n".join(m["content"] for m in messages
                                      if m["role"] == "system"))
        return self.script.pop(0) if self.script else {"content": "done", "tool_calls": []}


def tool_call(name, args, cid="1"):
    return {"content": "", "tool_calls": [
        {"id": cid, "name": name, "arguments": json.dumps(args)}]}


@pytest.fixture()
def model(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(llm, "provider", lambda: rec)
    return rec


@pytest.fixture()
def executed(monkeypatch):
    """Every tool actually executed, with its *effective* validated arguments."""
    seen = []

    def _call(name, args):
        seen.append((name, dict(args or {})))
        return {"state": "NO QUALIFYING PROP", "prices": [], "event": {"id": args.get("event_id")}}

    monkeypatch.setattr(tools, "call", _call)
    return seen


def post(client, message, *, context=None, event_id=SUN_ID, history=None):
    """One browser turn. `event_id` is the dashboard selection, which the real
    frontend attaches to EVERY payload whether or not the user touched it."""
    body = {"message": message, "history": history or [], "event_id": event_id}
    if context is not None:
        body["context"] = context
    r = client.post("/api/chat", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def conversation(client, model, executed, turns, event_id=SUN_ID):
    """Run turns as a browser would: carry history and context forward."""
    history, context, out = [], None, []
    for message, replies in turns:
        model.queue(*replies)
        history.append({"role": "user", "content": message})
        res = post(client, message, context=context, event_id=event_id, history=history[-8:])
        history.append({"role": "assistant", "content": res["answer"]})
        # Whatever the server hands back is what the next turn sends. This is
        # the turn boundary the previous tests skipped.
        context = res.get("context")
        out.append(res)
    return out


# ---------------------------------------------------------------- the defect
def test_three_turn_conversation_keeps_the_event_and_the_market(client, model, executed):
    """Turn 1 resolves Thursday; turns 2 and 3 must stay on it."""
    results = conversation(client, model, executed, [
        ("Who might score a touchdown in the Thursday night game",
         [tool_call("analyze_td_scorers", {"event_id": THU_ID}),
          {"content": "Detroit Lions @ Buffalo Bills anytime-TD prices below.",
           "tool_calls": []}]),
        ("What about receiving yards in that game",
         [tool_call("run_prop_analysis", {"query": SUN_ID, "category": "all"}),
          {"content": "Receiving yardage for Detroit Lions @ Buffalo Bills.",
           "tool_calls": []}]),
        ("Show me the available prices and when they were updated, even if nothing qualifies",
         [tool_call("get_player_props", {"event_id": SUN_ID}),
          {"content": "Prices for Detroit Lions @ Buffalo Bills receiving yardage.",
           "tool_calls": []}]),
    ])

    # Every executed tool ran against the Thursday game, never the dashboard one.
    for name, args in executed:
        used = args.get("event_id") or args.get("query")
        assert used == THU_ID, f"{name} ran against {used!r}, expected the Thursday game"

    # Turn 2 switched market without switching event.
    assert results[1]["context"]["event_id"] == THU_ID
    assert results[1]["context"]["market"] == "player_reception_yards"

    # Turn 3 kept BOTH: asking for prices must not reset the market to moneyline.
    assert results[2]["context"]["event_id"] == THU_ID
    assert results[2]["context"]["market"] == "player_reception_yards"
    assert "Vikings" not in results[2]["answer"] and "Bears" not in results[2]["answer"]


def test_turn_two_does_not_inherit_the_sticky_dashboard_selection(client, model, executed):
    """'that game' means the conversation's game, not the unchanged selection."""
    conversation(client, model, executed, [
        ("Who might score a touchdown in the Thursday night game",
         [tool_call("analyze_td_scorers", {"event_id": THU_ID}),
          {"content": "Detroit Lions @ Buffalo Bills.", "tool_calls": []}]),
        ("What about receiving yards in that game",
         [tool_call("run_prop_analysis", {"query": SUN_ID}),
          {"content": "Detroit Lions @ Buffalo Bills receiving yardage.", "tool_calls": []}]),
    ])
    assert executed[-1][1].get("event_id") == THU_ID or executed[-1][1].get("query") == THU_ID


def test_receiving_yards_resolves_to_the_receiving_market_not_category_all(client, model, executed):
    conversation(client, model, executed, [
        ("Who might score a touchdown in the Thursday night game",
         [tool_call("analyze_td_scorers", {"event_id": THU_ID}),
          {"content": "Detroit Lions @ Buffalo Bills.", "tool_calls": []}]),
        ("What about receiving yards in that game",
         [tool_call("run_prop_analysis", {"query": SUN_ID, "category": "all"}),
          {"content": "Receiving yardage.", "tool_calls": []}]),
    ])
    name, args = executed[-1]
    assert args.get("category") != "all", "'receiving yards' is not the whole prop board"
    assert args.get("category") == "receiving" or \
        args.get("market") == "player_reception_yards", args


# ------------------------------------------------------------- other paths
def test_an_explicit_switch_to_another_game_is_honoured(client, model, executed):
    conversation(client, model, executed, [
        ("Who might score a touchdown in the Thursday night game",
         [tool_call("analyze_td_scorers", {"event_id": THU_ID}),
          {"content": "Detroit Lions @ Buffalo Bills.", "tool_calls": []}]),
        ("Now show me the Panthers game instead",
         [tool_call("analyze_td_scorers", {"event_id": THU_ID}),
          {"content": "Carolina Panthers @ Atlanta Falcons.", "tool_calls": []}]),
    ])
    assert executed[-1][1].get("event_id") == SUN2_ID


def test_an_intentional_new_dashboard_selection_is_honoured(client, model, executed):
    """A selection the user actually changed outranks the conversation event."""
    history, context = [], None
    model.queue(tool_call("analyze_td_scorers", {"event_id": THU_ID}),
                {"content": "Detroit Lions @ Buffalo Bills.", "tool_calls": []})
    history.append({"role": "user", "content": "touchdowns in the Thursday night game"})
    r1 = post(client, "touchdowns in the Thursday night game", event_id=SUN_ID,
              history=history)
    history.append({"role": "assistant", "content": r1["answer"]})

    # The user clicks a DIFFERENT game, then asks about "this game".
    model.queue(tool_call("analyze_td_scorers", {"event_id": THU_ID}),
                {"content": "Carolina Panthers @ Atlanta Falcons.", "tool_calls": []})
    r2 = post(client, "who scores in this game", context=r1["context"],
              event_id=SUN2_ID, history=history)
    assert r2["context"]["event_id"] == SUN2_ID
    assert executed[-1][1].get("event_id") == SUN2_ID


def test_explicit_switch_back_to_moneyline_changes_market_not_event(client, model, executed):
    results = conversation(client, model, executed, [
        ("What about receiving yards in the Thursday night game",
         [tool_call("run_prop_analysis", {"query": THU_ID}),
          {"content": "Detroit Lions @ Buffalo Bills receiving yardage.", "tool_calls": []}]),
        ("show me the moneyline instead",
         [tool_call("run_game_analysis", {"query": THU_ID}),
          {"content": "Detroit Lions @ Buffalo Bills moneyline.", "tool_calls": []}]),
    ])
    assert results[-1]["context"]["event_id"] == THU_ID
    assert results[-1]["context"]["market"] in (None, "moneyline")


def test_separate_conversations_do_not_share_an_active_event(client, model, executed):
    """Two browser tabs. One resolves Thursday; the other must be unaffected."""
    model.queue(tool_call("analyze_td_scorers", {"event_id": THU_ID}),
                {"content": "Detroit Lions @ Buffalo Bills.", "tool_calls": []})
    a1 = post(client, "touchdowns in the Thursday night game", event_id=SUN_ID,
              history=[{"role": "user", "content": "touchdowns in the Thursday night game"}])
    assert a1["context"]["event_id"] == THU_ID

    # Tab B has its own (empty) context and its own selection.
    model.queue(tool_call("analyze_td_scorers", {"event_id": THU_ID}),
                {"content": "Minnesota Vikings @ Chicago Bears.", "tool_calls": []})
    b1 = post(client, "who scores in this game", context=None, event_id=SUN_ID,
              history=[{"role": "user", "content": "who scores in this game"}])
    assert b1["context"]["event_id"] == SUN_ID, "tab B must not inherit tab A's event"
    assert a1["context"]["conversation_id"] != b1["context"]["conversation_id"]


def test_openai_outage_on_turn_two_preserves_the_conversation_event(client, model, executed):
    model.queue(tool_call("analyze_td_scorers", {"event_id": THU_ID}),
                {"content": "Detroit Lions @ Buffalo Bills.", "tool_calls": []})
    history = [{"role": "user", "content": "touchdowns in the Thursday night game"}]
    r1 = post(client, "touchdowns in the Thursday night game", event_id=SUN_ID,
              history=history)
    history.append({"role": "assistant", "content": r1["answer"]})

    def boom(messages, schemas):
        raise RuntimeError("upstream 503")

    model.chat = boom
    r2 = post(client, "what about receiving yards in that game", context=r1["context"],
              event_id=SUN_ID, history=history)
    assert r2["context"]["event_id"] == THU_ID, "an outage must not drop the event"
    used = executed[-1][1]
    assert (used.get("event_id") or used.get("query")) == THU_ID


def test_a_rejected_wrong_event_answer_does_not_poison_the_next_turn(client, model, executed):
    """Context is committed only after validation."""
    model.queue(
        tool_call("analyze_td_scorers", {"event_id": THU_ID}),
        # The model narrates the wrong game; validation catches it.
        {"content": "Minnesota Vikings @ Chicago Bears look strong.", "tool_calls": []},
        {"content": "Detroit Lions @ Buffalo Bills candidates below.", "tool_calls": []})
    history = [{"role": "user", "content": "touchdowns in the Thursday night game"}]
    r1 = post(client, "touchdowns in the Thursday night game", event_id=SUN_ID,
              history=history)
    assert r1["context"]["event_id"] == THU_ID, \
        "the rejected Vikings answer must never become the conversation's event"


def test_missing_receiving_prices_report_the_right_game(client, model, executed, monkeypatch):
    monkeypatch.setattr(tools, "call", lambda n, a: (
        executed.append((n, dict(a or {}))),
        {"state": "PRICES UNAVAILABLE", "prices": [],
         "event": {"id": a.get("event_id"), "name": "Detroit Lions @ Buffalo Bills"}})[1])
    results = conversation(client, model, executed, [
        ("Who might score a touchdown in the Thursday night game",
         [tool_call("analyze_td_scorers", {"event_id": THU_ID}),
          {"content": "Detroit Lions @ Buffalo Bills.", "tool_calls": []}]),
        ("What about receiving yards in that game",
         [tool_call("run_prop_analysis", {"query": SUN_ID}),
          {"content": "No receiving-yardage prices are posted for "
                      "Detroit Lions @ Buffalo Bills.", "tool_calls": []}]),
    ])
    answer = results[-1]["answer"]
    assert "Bills" in answer or "Lions" in answer
    assert "moneyline" not in answer.lower(), "never substitute a moneyline board"


# ------------------------------------------------- tool contract (executed)
def test_run_prop_analysis_contract_actually_executes(client, monkeypatch):
    """Executes the real function, not a mock of it.

    Every other test in this file mocks `tools.call`, which cannot catch a
    NameError inside the tool body -- and did not: the rewritten
    run_prop_analysis shipped with `propsmod` out of scope and every mocked test
    still passed.
    """
    captured = {}

    def _fake_analyze(event_id, **kw):
        captured.update(event_id=event_id, **kw)
        return {"state": "NO QUALIFYING PROP", "recommendations": [], "prices": []}

    import edgeterminal.props as propsmod
    monkeypatch.setattr(propsmod, "analyze_props", _fake_analyze)

    out = tools.run_prop_analysis(event_id=THU_ID, market="player_reception_yards")
    assert "error" not in out, out
    assert captured["event_id"] == THU_ID
    assert captured["category"] == "receiving", "a market request is not the whole board"
    assert out["requested_market"] == "player_reception_yards"


def test_run_prop_analysis_refuses_a_non_prop_market(client):
    out = tools.run_prop_analysis(event_id=THU_ID, market="moneyline")
    assert "not a player prop market" in out["error"]


def test_run_prop_analysis_needs_an_event(client):
    assert "needs an event_id" in tools.run_prop_analysis().get("error", "")


def test_run_prop_analysis_ignores_unknown_model_kwargs(client, monkeypatch):
    """The model sent category='all'; an unknown kwarg must not raise TypeError."""
    import edgeterminal.props as propsmod
    monkeypatch.setattr(propsmod, "analyze_props",
                        lambda event_id, **kw: {"state": "OK", "recommendations": []})
    out = tools.run_prop_analysis(event_id=THU_ID, category="all", nonsense=1)
    assert "error" not in out, out
