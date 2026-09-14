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
