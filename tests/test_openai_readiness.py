import json

from edgeterminal.agents import llm, tools


class FakeProvider:
    name = "openai"
    model = "test-model"

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.messages = []

    def chat(self, messages, tool_schemas):
        self.messages.append(messages)
        return next(self.outputs)


def test_openai_executes_tool_before_returning_factual_answer(monkeypatch):
    fake = FakeProvider([
        {"content": "", "tool_calls": [{"id": "1", "name": "get_provider_status", "arguments": "{}"}]},
        {"content": "Grounded response", "tool_calls": []},
    ])
    monkeypatch.setattr(llm, "provider", lambda: fake)
    monkeypatch.setattr(tools, "call", lambda name, args: {"providers": [], "last_refresh": None})
    result = llm.run_chat("Are providers live?")
    assert result["answer"] == "Grounded response"
    assert result["tool_trace"][0]["tool"] == "get_provider_status"
    tool_payload = fake.messages[-1][-1]["content"]
    assert json.loads(tool_payload) == {"providers": [], "last_refresh": None}


def test_openai_direct_factual_answer_is_replaced_by_grounded_fallback(monkeypatch):
    fake = FakeProvider([{"content": "The odds are definitely -110", "tool_calls": []}])
    monkeypatch.setattr(llm, "provider", lambda: fake)
    monkeypatch.setattr(tools, "call", lambda name, args: {"error": "No current odds available"})
    result = llm.run_chat("What are today's NFL odds?")
    assert result["provider"] == "deterministic-router"
    assert "definitely -110" not in result["answer"]
    assert result["tool_trace"]


def test_tool_schemas_require_mandatory_arguments():
    by_name = {s["name"]: s["parameters"] for s in tools.SCHEMAS}
    assert "query" in by_name["get_current_odds"]["required"]
    assert "query" in by_name["run_game_analysis"]["required"]
    assert set(by_name["get_line_history"]["required"]) == {"query", "market", "selection"}
    assert "sport" in by_name["get_team_stats"]["required"]


def test_large_tool_payload_remains_valid_json(monkeypatch):
    huge = {"rows": ["x" * 1000 for _ in range(100)]}
    fake = FakeProvider([
        {"content": "", "tool_calls": [{"id": "1", "name": "get_provider_status", "arguments": "{}"}]},
        {"content": "done", "tool_calls": []},
    ])
    monkeypatch.setattr(llm, "provider", lambda: fake)
    monkeypatch.setattr(tools, "call", lambda name, args: huge)
    llm.run_chat("provider status")
    assert json.loads(fake.messages[-1][-1]["content"])


def test_openai_failure_fallback_does_not_expose_exception_text(monkeypatch):
    class Broken:
        name = "openai"
        model = "test-model"
        def chat(self, messages, tool_schemas):
            raise RuntimeError("secret-token-must-not-leak")
    monkeypatch.setattr(llm, "provider", lambda: Broken())
    monkeypatch.setattr(tools, "call", lambda name, args: {"error": "No data"})
    result = llm.run_chat("What are the odds?")
    assert "secret-token-must-not-leak" not in json.dumps(result)
    assert result["llm_error"] == "RuntimeError"


def _captured_openai_args(monkeypatch, prompt, supplied_args):
    fake = FakeProvider([
        {"content": "", "tool_calls": [{"id": "1", "name": "find_best_bets",
                                          "arguments": json.dumps(supplied_args)}]},
        {"content": "grounded", "tool_calls": []},
    ])
    captured = {}
    monkeypatch.setattr(llm, "provider", lambda: fake)
    monkeypatch.setattr(tools, "call", lambda name, args: captured.update(name=name, args=args) or
                        {"bets": [], "official_pick_count": 0, "threshold": 80})
    result = llm.run_chat(prompt)
    return captured, result


def test_best_nfl_bet_cannot_lower_production_threshold(monkeypatch):
    captured, result = _captured_openai_args(
        monkeypatch, "What's the best NFL bet right now?",
        {"min_confidence": 60, "sport": "NFL", "limit": 1})
    assert captured["name"] == "find_best_bets"
    assert captured["args"]["min_confidence"] == 80
    assert captured["args"].get("include_leans") is not True
    assert result["tool_trace"][0]["arguments"]["min_confidence"] == 80


def test_strongest_play_cannot_lower_production_threshold(monkeypatch):
    captured, _ = _captured_openai_args(monkeypatch, "What's your strongest play?",
                                         {"min_confidence": 65, "limit": 1})
    assert captured["args"]["min_confidence"] == 80
    assert captured["args"].get("include_leans") is not True


def test_explicit_60_plus_request_preserves_lower_threshold(monkeypatch):
    captured, _ = _captured_openai_args(monkeypatch, "Show me 60+ confidence NFL bets.",
                                         {"min_confidence": 60, "sport": "NFL"})
    assert captured["args"]["min_confidence"] == 60
    assert captured["args"]["include_leans"] is True


def test_explicit_lean_request_preserves_broader_search(monkeypatch):
    captured, _ = _captured_openai_args(monkeypatch, "Show me leans.",
                                         {"min_confidence": 60, "sport": "NFL"})
    assert captured["args"]["include_leans"] is True
    assert captured["args"]["only_leans"] is True


def test_active_chat_tools_expose_only_nfl_sport():
    sport_schemas = []
    for schema in tools.SCHEMAS:
        sport = schema.get("parameters", {}).get("properties", {}).get("sport")
        if sport:
            sport_schemas.append(sport["enum"])
    assert sport_schemas
    assert all(enum == ["NFL"] for enum in sport_schemas)


def test_deterministic_chat_refuses_dormant_sports(monkeypatch):
    monkeypatch.setattr(llm, "provider", lambda: None)
    result = llm.run_chat("Show me PGA outrights")
    assert result["provider"] == "scope-router"
    assert result["tool_trace"] == []
    assert "NFL-only" in result["answer"]
    assert "run_golf_analysis" not in tools.REGISTRY


def test_rendered_tools_receive_the_application_rendering_in_valid_json(monkeypatch):
    """The model must see the app's own numbers, and the payload must stay JSON."""
    board = {"state": "NO QUALIFYING PROP", "event": {"name": "A @ B"},
             "market": {"books": 6, "coverage": "full", "priced": 32, "no_vig_players": 0},
             "recommendations": [], "prices": [], "official": []}
    fake = FakeProvider([
        {"content": "", "tool_calls": [{"id": "1", "name": "analyze_td_scorers",
                                        "arguments": "{}"}]},
        {"content": "Grounded", "tool_calls": []},
    ])
    monkeypatch.setattr(llm, "provider", lambda: fake)
    monkeypatch.setattr(tools, "call", lambda name, args: board)
    llm.run_chat("best td scorer")
    payload = json.loads(fake.messages[-1][-1]["content"])
    assert "_authoritative_rendering" in payload
    assert "32 player(s) priced across 6 book(s)" in payload["_authoritative_rendering"]
    assert "overround" not in payload["_authoritative_rendering"].lower()
