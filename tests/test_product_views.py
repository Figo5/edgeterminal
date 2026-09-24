"""Read-only product composition over sealed research records."""
import hashlib
import json
import time

from edgeterminal import product_views


def _game():
    return {"id": "nfl-2026-reg-03-atl-gb", "away_team": "Atlanta Falcons",
            "home_team": "Green Bay Packers", "start_time": "2026-09-25T00:15Z",
            "venue": "Lambeau Field", "status": "scheduled", "markets": []}


def test_weekly_board_uses_stored_pregame_quotes_and_marks_stale(monkeypatch):
    game = _game()
    kickoff = 1790295300.0
    monkeypatch.setattr(product_views.engine, "slate", lambda *a, **k: [game])
    monkeypatch.setattr(product_views, "_prediction", lambda g: (
        {"proj_away": 19.86, "proj_home": 24.39, "margin": 4.53,
         "total": 44.25, "home_win_prob": 0.6254}, 1790200000.0))
    rows = [
        {"id": n, "book": "draftkings", "market": market, "selection": selection,
         "line": line, "price": price, "captured_at": kickoff - 3600, "source": "espn-odds"}
        for n, (market, selection, line, price) in enumerate([
            ("moneyline", game["home_team"], None, -245),
            ("spread", game["home_team"], -4.5, -115),
            ("total", "Over", 42.5, -118)], 1)]
    monkeypatch.setattr(product_views.db, "q", lambda *a, **k: rows)
    monkeypatch.setattr(product_views.time, "time", lambda: kickoff - 900)
    board = product_views.weekly_board()
    item = board["games"][0]
    assert board["readiness"] == "RESEARCH_ONLY"
    assert item["designation"] == "PASS"
    assert item["projection"]["home_win_probability"] == 0.6254
    assert item["market"]["spread"]["home"]["line"] == -4.5
    assert item["market"]["moneyline"]["home"]["price"] == -245
    assert item["market_is_stale"]
    assert item["market_observed_at"] == kickoff - 3600


def test_missing_probability_stays_missing(monkeypatch):
    game = _game()
    monkeypatch.setattr(product_views, "_prediction", lambda g: (
        {"proj_away": 20, "proj_home": 24, "margin": 4, "total": 44,
         "home_win_prob": 0.6}, 1790200000.0))
    monkeypatch.setattr(product_views.db, "q", lambda *a, **k: [])
    item = product_views._game(game)
    assert item["market"]["spread"]["home_cover_probability"] is None
    assert item["market"]["total"]["over_probability"] is None
    assert item["market_is_stale"]


def test_prop_pairings_keep_full_board_and_paper_badge(monkeypatch):
    game_id = _game()["id"]
    monkeypatch.setattr(product_views.nfl_props, "board", lambda g: {"batch_id": "batch"})
    quote = {"projection": 3.6829, "probability_over": 0.170496,
             "probability_under": 0.829504}
    payload = json.dumps({"quote": quote})
    freeze_payload = "{}"
    freeze_sha = hashlib.sha256(freeze_payload.encode()).hexdigest()
    base = {"payload": payload, "pair_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "freeze_sha": freeze_sha, "player_id": "drake", "player_name": "Drake London",
            "team": "Atlanta Falcons", "market": "receptions", "sportsbook": "espn bet",
            "line": 5.5, "provider_updated_at": None, "retrieved_at": 1790200000.0}
    rows = [{**base, "side": "over", "price": 105},
            {**base, "side": "under", "price": -135}]
    monkeypatch.setattr(product_views.db, "q", lambda *a, **k: rows)
    monkeypatch.setattr(product_views.db, "q1", lambda sql, *a, **k:
                        {"start_time": "2026-09-25T00:15Z"} if "FROM events" in sql
                        else {"payload": freeze_payload})
    monkeypatch.setattr(product_views, "_open_paper_positions",
                        lambda g: {("drake", "receptions", "espn bet", 5.5, "under"):
                                   {"id": "immutable-paper", "side": "under"}})
    result = product_views.player_props(game_id)
    assert result["count"] == result["two_sided_count"] == 1
    row = result["rows"][0]
    assert row["confidence"] == row["probability_under"] == 0.829504
    assert row["stronger_side"] == "under"
    assert row["over_price"] == 105 and row["under_price"] == -135
    assert row["paper_position"]["id"] == "immutable-paper"


def test_product_performance_keeps_forward_summary_separate(tmp_path, monkeypatch):
    path = tmp_path / "data/research/nfl_prop_forward_report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "prospective_2026": {"distinct_player_markets_graded": 2,
                             "quote_sides_graded": 4, "pending_quote_sides": 8,
                             "markets": {"receptions": {"mae": 1.2}},
                             "market_comparisons": [{"player": "private detail"}]},
        "retrospective_validation": {"receptions": {"mae": 1.0}},
    }))
    monkeypatch.setattr(product_views, "ROOT", tmp_path)
    monkeypatch.setattr(product_views.shadow, "performance", lambda: {"all_projections": {"n": 0}})
    monkeypatch.setattr(product_views.paper_research, "dashboard", lambda: {
        "starting_bankroll": 1000, "current_bankroll": 1000, "open_positions": 3,
        "settled_positions": 0, "record": {}, "profit_loss": 0, "roi": None})
    result = product_views.performance()
    assert result["prospective_2026"]["player_props"]["quote_sides_graded"] == 4
    assert "market_comparisons" not in result["prospective_2026"]["player_props"]
    assert result["retrospective_research"]["available"]
    assert result["prospective_2026"]["paper"]["open_positions"] == 3


def test_matchup_context_is_from_stored_pregame_snapshot(monkeypatch):
    game_id = _game()["id"]
    monkeypatch.setattr(product_views, "weekly_board", lambda: {
        "games": [{"id": game_id, "kickoff_utc": "2026-09-25T00:15Z"}],
        "as_of": 1790200000.0})
    monkeypatch.setattr(product_views, "player_props", lambda g: {"rows": [], "count": 0})
    payload = json.dumps({"weather": {"conditions": "Overcast", "retrieved_at": 1790200000.0},
                          "injuries": [{"player": "Example", "status": "Out"}]})
    monkeypatch.setattr(product_views.db, "q1", lambda *a, **k: {
        "payload": payload, "created_at": 1790200000.0})
    result = product_views.matchup(game_id)
    assert result["context"]["weather"]["conditions"] == "Overcast"
    assert result["context"]["injuries"][0]["status"] == "Out"
    assert result["context"]["observed_at"] == 1790200000.0
    assert result["designation"] == "PASS"
