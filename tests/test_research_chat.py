"""Conversation-level research chat behavior with stored-view fixtures only."""
import datetime as dt

import pytest

from edgeterminal import db, product_views, paper_research
from edgeterminal.agents import context, llm
from edgeterminal.config import settings

NOW = dt.datetime(2026, 9, 24, 18, tzinfo=dt.timezone.utc).timestamp()
GAME = "nfl-2026-reg-03-atl-gb"


def _prop(player, market, line, probability, projection, book="espnbet"):
    return dict(player_id=player, player=player, team="Atlanta Falcons", market=market,
                sportsbook=book, line=line, over_price=-110, under_price=-110,
                projection=projection, probability_over=1-probability,
                probability_under=probability, quote_observed_at=NOW-600,
                quote_is_stale=False, paper_position=None)


@pytest.fixture
def chat(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'research-chat.db'}")
    monkeypatch.setattr(settings, "demo_mode", False)
    db.init()
    db.ex("INSERT INTO events(id,sport,league,name,home_team,away_team,start_time,venue,status,meta,updated_at) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (GAME, "NFL", "NFL", "Atlanta Falcons @ Green Bay Packers", "Green Bay Packers",
           "Atlanta Falcons", "2026-09-25T00:15Z", "", "scheduled", "{}", NOW))
    monkeypatch.setattr(context, "_ensure_fresh_schedule", lambda ctx, allow_refresh=True: {"uncertain": False})
    monkeypatch.setattr(llm, "_now_override", NOW, raising=False)
    game = dict(id=GAME, away_team="Atlanta Falcons", home_team="Green Bay Packers",
                projection=dict(away_score=20.4, home_score=27.8, home_margin=7.4,
                                total=48.2, home_win_probability=.69,
                                away_win_probability=.31),
                market=dict(moneyline=dict(home=dict(price=-200, book="draftkings", captured_at=NOW-600),
                                           away=dict(price=170, book="draftkings", captured_at=NOW-600)),
                            spread=dict(home=dict(line=-3.5, price=-110, book="draftkings", captured_at=NOW-600),
                                        home_cover_probability=.58),
                            total=dict(over=dict(line=47.5, price=-110, book="draftkings", captured_at=NOW-600),
                                       over_probability=.52)),
                market_is_stale=False, market_observed_at=NOW-600)
    rows = [_prop("Drake London", "receptions", 5.5, .83, 3.68),
            _prop("Michael Penix Jr.", "passing_yards", 204.5, .77, 144.71),
            _prop("Jonnu Smith", "receptions", 1.5, .34, 2.2),
            _prop("Bijan Robinson", "rushing_yards", 75.5, .73, 63.0)]
    monkeypatch.setattr(product_views, "matchup", lambda game_id: {
        "game": game, "props": {"rows": rows}} if game_id == GAME else (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr(paper_research, "dashboard", lambda: {
        "current_bankroll": 1000, "open": [dict(player_name="Drake London", side="under",
            line=5.5, market="receptions", sportsbook="espnbet", odds=-110, stake=10)]})

    def ask(message, prior=None):
        return llm.run_chat(message, prior_context=prior, selected_event_id=GAME, now=NOW)

    yield ask
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_best_threshold_and_context(chat):
    first = chat("best bet for falcons packers")
    assert "Drake London" in first["answer"]
    assert first["answer"].index("Drake London") < first["answer"].index("PASS")
    more = chat("anything else?", first["context"])
    assert "Drake London" not in more["answer"]
    assert more["context"]["event_id"] == GAME
    second = chat("anything 70% confidence or higher", first["context"])
    assert second["context"]["event_id"] == GAME
    assert "Drake London" in second["answer"] and "Michael Penix Jr." in second["answer"]
    assert "Jonnu Smith" not in second["answer"]
    assert second["context"]["threshold"] == .7


def test_parlay_and_followups(chat):
    parlay = chat("create a 3 leg parlay for tnf")
    assert "Research Parlay" in parlay["answer"]
    assert "3 model-backed legs" in parlay["answer"]
    assert "Correlation" in parlay["answer"]
    assert "Combined odds and joint win probability are unavailable" in parlay["answer"]
    assert parlay["context"]["parlay_size"] == 3
    receiving = chat("best receiving prop", parlay["context"])
    assert "Drake London" in receiving["answer"]
    penix = chat("what about Penix", receiving["context"])
    assert "Michael Penix Jr." in penix["answer"]
    assert "Drake London" not in penix["answer"]
    assert penix["context"]["player"] == "Michael Penix Jr."
    total = chat("over under?", penix["context"])
    assert "market 47.5" in total["answer"] and "Over 52%" in total["answer"]
    again = chat("anything else?", total["context"])
    assert "Green Bay Packers" in again["answer"] and "total" in again["answer"]


def test_paper_only_when_asked(chat):
    ordinary = chat("best bet for falcons packers")
    assert "Drake London" in ordinary["answer"]
    paper = chat("paper picks?", ordinary["context"])
    assert "PAPER portfolio" in paper["answer"]
    assert "Drake London" in paper["answer"]
    assert "Michael Penix Jr." not in paper["answer"]
