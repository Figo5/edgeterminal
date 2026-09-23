"""The research-only policy must survive chat formatting and quote display."""
from edgeterminal.agents import llm, tools
from edgeterminal import engine, nfl_market
from edgeterminal.api import performance
from edgeterminal.integrity import parse_time
from edgeterminal.providers.theoddsapi import TheOddsApi


def test_chat_pass_explains_policy_not_a_missing_edge():
    result = llm.format_result("run_game_analysis", {
        "event": "Atlanta Falcons at Green Bay Packers",
        "recommendations": [{"state": "PASS", "reasoning": [
            "Research-only model: NFL pick thresholds are not validated"]}],
    })
    assert "NFL pick thresholds are not validated" in result
    assert "No market in this event clears the edge threshold" not in result
    empty = llm.format_result("find_best_bets", {
        "bets": [], "note": "NFL markets remain PASS by policy"})
    assert "lower the confidence filter" not in empty
    assert "remain PASS" in empty


def test_market_board_exposes_quote_provenance_and_actual_book_count():
    event = {"home_team": "Green Bay Packers", "away_team": "Atlanta Falcons"}
    rows = [dict(event_id="g", market="moneyline", selection="Green Bay Packers",
                 book="draftkings", price=-110, line=None, captured_at=10.0,
                 provider_updated_at=9.0),
            dict(event_id="g", market="moneyline", selection="Green Bay Packers",
                 book="fanduel", price=-105, line=None, captured_at=11.0,
                 provider_updated_at=8.0)]
    board = nfl_market.board_rows(rows, event)
    assert board[0]["book"] == "fanduel"
    assert board[0]["books_quoted"] == 2
    assert board[0]["captured_at"] == 11.0
    assert board[0]["provider_updated_at"] == 8.0


def test_performance_tool_does_not_claim_live_nfl_grading():
    result = tools.get_model_performance()
    assert result["forward_shadow_status"] == "NOT_STARTED"
    assert "not been implemented" in result["note"]
    assert performance()["graded_nfl_game_predictions"] == 0


def test_book_update_time_survives_market_flattening_and_analysis():
    provider = object.__new__(TheOddsApi)
    row = provider._flatten({
        "id": "event-1", "bookmakers": [{"key": "fanduel",
        "last_update": "2026-09-23T10:00:00Z", "markets": [{
            "key": "h2h", "last_update": "2026-09-23T10:05:00Z",
            "outcomes": [{"name": "Buffalo Bills", "price": -110}],
        }]}],
    }, "NFL")[0]
    expected = parse_time("2026-09-23T10:05:00Z")
    assert row["provider_updated_at"] == expected
    assert engine._quotes_by_selection([row])[("moneyline", "Buffalo Bills")][0][
        "provider_updated_at"] == expected


def test_nfl_best_prop_tool_does_not_designate_a_play(monkeypatch):
    monkeypatch.setattr(tools, "_resolve_event", lambda event_id=None: ("event-1", None))
    monkeypatch.setattr(tools, "_props", lambda event_id, category=None: {
        "event": {"id": "event-1", "sport": "NFL"},
        "recommendations": [{"state": "PASS", "edge_pct": 9.0, "confidence": 99}],
    })
    result = tools.find_best_player_props(event_id="event-1")
    assert result["recommendations"] == []
    assert result["official_pick_count"] == 0
    assert "remain PASS" in result["note"]
