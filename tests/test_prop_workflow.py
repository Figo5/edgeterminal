"""The prop workflow end to end, with every external service mocked.

Nothing here touches the network or a paid API: the Odds API, injuries, nflverse
play-by-play and web research are all injected.
"""
import pytest

from edgeterminal import props
from edgeterminal.agents import llm
from edgeterminal.agents import tools
from edgeterminal.config import settings


BIG_BEN = "Big Ben Rusher"
WIDEOUT = "Wide Out One"
OPP_RB = "Opp Rusher"


def _play(**kw):
    base = dict(game_id="g1", season=2026, week=1, season_type="REG", posteam="KC",
                td_team="", touchdown="0", rush_attempt="0", rush_touchdown="0",
                pass_attempt="0", complete_pass="0", pass_touchdown="0", interception="0",
                yards_gained="0", air_yards="0", yardline_100="50")
    base.update(kw)
    return base


def _rb_plays():
    rows = []
    for game in ("g1", "g2", "g3", "g4", "g5"):
        for i in range(14):
            rz = i < 3
            # Red-zone work is not all goal-line work: three RZ carries a game but
            # only one from inside the five.
            yardline = {0: "18", 1: "12", 2: "3"}.get(i, "40")
            rows.append(_play(game_id=game, week=1 if game in ("g1", "g2") else 2,
                              posteam="KC", rush_attempt="1",
                              rusher_player_id="rb-ben", rusher_player_name=BIG_BEN,
                              yards_gained="4", yardline_100=yardline if rz else "40",
                              rush_touchdown="1" if i == 0 else "0",
                              touchdown="1" if i == 0 else "0",
                              td_team="KC" if i == 0 else ""))
        for i in range(6):
            rows.append(_play(game_id=game, week=1 if game in ("g1", "g2") else 2,
                              posteam="KC", pass_attempt="1", complete_pass="1",
                              receiver_player_id="wr-one", receiver_player_name=WIDEOUT,
                              passer_player_id="qb-one", passer_player_name="Qb One",
                              yards_gained="11", air_yards="8",
                              yardline_100="15" if i == 0 else "45",
                              pass_touchdown="1" if i == 0 else "0",
                              touchdown="1" if i == 0 else "0",
                              td_team="KC" if i == 0 else ""))
    return rows


def _paired_no(yes_price, hold=0.06):
    """The No price a book would post alongside this Yes, carrying `hold` vig."""
    from edgeterminal.pricing import american_to_prob, prob_to_american
    fair_yes = american_to_prob(yes_price) / (1.0 + hold)
    return prob_to_american(min(0.98, (1.0 - fair_yes) * (1.0 + hold)))


def _td_rows(prices=None, two_sided=True):
    """Anytime-TD quotes.

    `two_sided=True` posts the No alongside each Yes, which is what makes a
    same-player no-vig probability (and therefore a recommendation) possible.
    With `two_sided=False` only Yes exists, so the board must fall back to
    verified-price browsing.
    """
    prices = prices or {"Big Ben Rusher": [120, 130], "Wide Out One": [150, 160],
                        "Qb One": [400, 420], "Opp Rusher": [280, 300],
                        "Depth Guy": [900, 950]}
    rows = []
    for player, books in prices.items():
        for i, price in enumerate(books):
            book = "fanduel" if i == 0 else "bet365"
            rows.append(dict(event_id="evt1", sport="NFL", book=book,
                             market="player_anytime_td", api_market="player_anytime_td",
                             selection=player, player=player, side="yes", line=None,
                             price=price, captured_at=1000.0))
            if two_sided:
                rows.append(dict(event_id="evt1", sport="NFL", book=book,
                                 market="player_anytime_td", api_market="player_anytime_td",
                                 selection=player, player=player, side="no", line=None,
                                 price=_paired_no(price), captured_at=1000.0))
    return rows


def _market_rows():
    rows = []
    for book, over, under, line in (("fanduel", -115, -105, 68.5), ("bet365", -110, -110, 68.5)):
        rows.append(dict(event_id="evt1", sport="NFL", book=book, market="player_reception_yards",
                         api_market="player_reception_yds", selection=WIDEOUT, player=WIDEOUT,
                         side="over", line=line, price=over, captured_at=1000.0))
        rows.append(dict(event_id="evt1", sport="NFL", book=book, market="player_reception_yards",
                         api_market="player_reception_yds", selection=WIDEOUT, player=WIDEOUT,
                         side="under", line=line, price=under, captured_at=1000.0))
    return rows


@pytest.fixture()
def event(tmp_path, monkeypatch):
    from edgeterminal import db
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'props.db'}")
    db.init()
    db.ex("INSERT OR REPLACE INTO events(id,sport,league,name,home_team,away_team,start_time,"
          "venue,status,meta,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         ("evt1", "NFL", "NFL", "Denver Broncos @ Kansas City Chiefs",
          "Kansas City Chiefs", "Denver Broncos", "2026-09-20T17:00Z",
          "Arrowhead Stadium", "scheduled", "{}", 1000.0))
    for market, selection, line, price in (("total", "Over", 44.5, -110),
                                           ("spread", "Kansas City Chiefs", -3.0, -110)):
        db.store_odds([dict(event_id="evt1", sport="NFL", book="fanduel", market=market,
                            selection=selection, line=line, price=price,
                            captured_at=1000.0, source="test")], source="test")
    yield "evt1"
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _deps(rows, research_items=None, usage=True):
    return dict(
        fetch_props=lambda eid, markets, **kw: dict(event_id=eid, rows=rows,
                                                    source="theoddsapi", is_demo=False,
                                                    cache="miss", error=None, quota={}),
        injuries=lambda: [dict(team="Kansas City Chiefs", player=BIG_BEN, position="RB",
                               status="Questionable")],
        plays=(lambda: _rb_plays()) if usage else (lambda: []),
        season=2026,
        research=lambda plan: dict(steps=[], items=research_items or [], queries_used=0),
    )


def test_prop_workflow_prices_td_markets_and_separates_likelihood_from_value(event):
    out = props.analyze_props(event, category="td", deps=_deps(_td_rows()), now=1000.0)
    assert out["event"]["name"].endswith("Kansas City Chiefs")
    assert out["market"]["usable"] is True
    assert out["market"]["books"] == 2
    assert out["most_likely"]["player"] == BIG_BEN
    if out["best_value"]:
        assert out["best_value"]["ev_per_unit"] >= 0
    for rec in out["recommendations"]:
        assert rec["fair_price"] is not None
        assert rec["sources"]
        assert 0 <= rec["confidence"] <= settings.prop_confidence_cap
        assert rec["units"] <= settings.prop_max_units["player_anytime_td"]


def test_prop_workflow_never_promotes_without_market_coverage(event):
    single = [r for r in _td_rows() if r["book"] == "fanduel"]
    out = props.analyze_props(event, category="td", deps=_deps(single), now=1000.0)
    assert out["market"]["coverage"] == "thin"
    assert out["official"] == []
    # One book is never enough to grade a prop: nothing may leave INSUFFICIENT DATA.
    assert all(r["state"] == "INSUFFICIENT DATA" for r in out["recommendations"])
    assert out["state"] != "OFFICIAL PICK"


def test_prop_workflow_is_insufficient_data_without_usage(event):
    out = props.analyze_props(event, category="td", deps=_deps(_td_rows(), usage=False), now=1000.0)
    assert out["recommendations"] == []
    assert out["state"] == "INSUFFICIENT DATA"


def test_prop_confidence_is_capped_below_game_market_ceiling(event):
    out = props.analyze_props(event, category="td", deps=_deps(_td_rows()), now=1000.0)
    caps = {rec["confidence_cap"] for rec in out["recommendations"]}
    assert caps <= {settings.prop_confidence_cap}


def test_yardage_prop_uses_an_opportunity_first_projection(event):
    out = props.analyze_props(event, category="receiving",
                              deps=_deps(_td_rows() + _market_rows()), now=1000.0)
    receiving = [r for r in out["recommendations"] if r["market"] == "player_reception_yards"]
    assert receiving
    row = receiving[0]
    assert row["projection"]["component"]["catches_per_game"] > 0
    assert row["projection"]["component"]["yards_per_catch"] > 0
    assert row["line"] == 68.5
    assert 0 < row["model_prob"] < 1
    assert row["fair_price"] is not None


def test_research_can_change_a_projection_but_only_through_bounded_features(event):
    items = [dict(title=f"{BIG_BEN} expected to handle a limited workload",
                  snippet="coach said snap count", url="https://www.espn.com/nfl/story/x",
                  reliability="HIGH", published_at=995.0, retrieved_at=1000.0)]
    base = props.analyze_props(event, category="td", deps=_deps(_td_rows()), now=1000.0)
    researched = props.analyze_props(event, category="td",
                                     deps=_deps(_td_rows(), research_items=items), now=1000.0)
    def prob(out):
        return next(r["final_prob"] for r in out["recommendations"] if r["player"] == BIG_BEN)
    assert prob(researched) < prob(base)
    row = next(r for r in researched["recommendations"] if r["player"] == BIG_BEN)
    assert all(v <= 1.0 for v in row["adjustments"].values())
    assert any(abs(v - 1.0) > 0.001 for v in row["adjustments"].values())
    assert row["research_context"]
    assert researched["research"]["citations"]


def test_research_conflicts_are_recorded_and_penalised(event):
    items = [
        dict(title=f"{BIG_BEN} listed questionable", url="https://www.nfl.com/injury-report/x",
             reliability="PRIMARY", published_at=999.0, retrieved_at=1000.0),
        dict(title=f"{BIG_BEN} expected to play and handle a full workload",
             url="https://www.espn.com/nfl/story/y", reliability="HIGH",
             published_at=999.0, retrieved_at=1000.0),
    ]
    out = props.analyze_props(event, category="td", deps=_deps(_td_rows(), research_items=items),
                              now=1000.0)
    row = next(r for r in out["recommendations"] if r["player"] == BIG_BEN)
    assert out["research"]["conflicts"]
    assert row["source_agreement"] < 1.0
    assert row["state"] in ("WATCHLIST", "LEAN", "INSUFFICIENT DATA", "PASS")


def test_stale_prop_prices_are_flagged_not_bet(event):
    rows = _td_rows()
    for row in rows:
        row["captured_at"] = 1000.0 - 4 * 3600
    out = props.analyze_props(event, category="td", deps=_deps(rows), now=1000.0)
    states = {r["state"] for r in out["recommendations"]}
    assert states <= {"STALE", "INSUFFICIENT DATA", "PASS", "WATCHLIST"}
    assert out["official"] == []


def test_prop_state_machine_requires_coverage_freshness_and_edge():
    assert props.prop_state(confidence=80.0, edge_pct=5.0, books=3, age_s=30.0, agreement=1.0,
                            usage_available=True, is_demo=False) == "OFFICIAL PICK"
    assert props.prop_state(confidence=80.0, edge_pct=5.0, books=1, age_s=30.0, agreement=1.0,
                            usage_available=True, is_demo=False) == "INSUFFICIENT DATA"
    assert props.prop_state(confidence=80.0, edge_pct=5.0, books=3, age_s=99999.0, agreement=1.0,
                            usage_available=True, is_demo=False) == "STALE"
    assert props.prop_state(confidence=80.0, edge_pct=5.0, books=3, age_s=30.0, agreement=0.85,
                            usage_available=True, is_demo=False) == "WATCHLIST"
    assert props.prop_state(confidence=80.0, edge_pct=-1.0, books=3, age_s=30.0, agreement=1.0,
                            usage_available=True, is_demo=False) == "PASS"
    assert props.prop_state(confidence=40.0, edge_pct=5.0, books=3, age_s=30.0, agreement=1.0,
                            usage_available=True, is_demo=False) in ("LEAN", "WATCHLIST",
                                                                     "WAIT FOR PRICE")


def test_a_prior_season_baseline_cannot_become_an_official_pick():
    """Role evidence from last season can inform a lean, never a staked pick."""
    common = dict(confidence=90.0, edge_pct=8.0, books=3, age_s=30.0, agreement=1.0,
                  usage_available=True, is_demo=False)
    assert props.prop_state(**common) == "OFFICIAL PICK"
    assert props.prop_state(**common, usage_basis="prior_season") == "LEAN"
    assert props.prop_state(**dict(common, confidence=50.0),
                            usage_basis="prior_season") == "WATCHLIST"


def test_units_stay_conservative_for_props():
    assert props.prop_units(80.0, 6.0, "player_anytime_td") <= settings.prop_max_units["player_anytime_td"]
    assert props.prop_units(80.0, 6.0, "player_1st_td") <= settings.prop_max_units["player_1st_td"]
    assert props.prop_units(80.0, 6.0, "player_reception_yards") <= settings.prop_default_max_units
    assert props.prop_units(30.0, 6.0, "player_reception_yards") == 0.0
    assert props.prop_units(80.0, -1.0, "player_anytime_td") == 0.0


def test_implied_team_totals_split_the_game_total_by_the_handicap():
    event_row = dict(home_team="Kansas City Chiefs", away_team="Denver Broncos")
    rows = [dict(market="total", selection="Over", line=44.0),
            dict(market="spread", selection="Kansas City Chiefs", line=-3.0)]
    env = props.implied_team_totals(event_row, rows)
    assert env["available"] is True
    assert env["totals"]["Kansas City Chiefs"] == pytest.approx(23.5)
    assert env["totals"]["Denver Broncos"] == pytest.approx(20.5)


# ------------------------------------------------------------------- chat tools
def test_explicit_market_list_limits_the_fetch_to_that_market(event):
    seen = {}

    def fetch(eid, markets, **kw):
        seen["markets"] = list(markets)
        return dict(event_id=eid, rows=_td_rows(), source="theoddsapi", is_demo=False,
                    cache="miss", error=None, quota={},
                    identity=dict(state="OK", provider_id="prov-1", reasons=[]))

    deps = _deps(_td_rows())
    deps["fetch_props"] = fetch
    props.analyze_props(event, category=None, markets=["player_anytime_td"],
                        deps=deps, now=1000.0)
    assert seen["markets"] == ["player_anytime_td"]


def test_a_one_game_usage_sample_is_never_priced(event):
    thin = [p for p in _rb_plays() if p["game_id"] == "g1"]
    deps = _deps(_td_rows())
    deps["plays"] = lambda: thin
    deps["prior_plays"] = lambda: []
    out = props.analyze_props(event, category="td", deps=deps, now=1000.0)
    assert out["recommendations"] == []
    reasons = " ".join(str(s.get("reason")) for s in out["skipped"])
    assert "too small to price" in reasons


def test_a_thin_current_season_falls_back_to_the_labelled_prior_season(event):
    thin = [p for p in _rb_plays() if p["game_id"] == "g1"]
    deps = _deps(_td_rows())
    deps["plays"] = lambda: thin
    deps["prior_plays"] = lambda: [dict(p, season=None) for p in _rb_plays()]
    out = props.analyze_props(event, category="td", deps=deps, now=1000.0)
    rows = [r for r in out["recommendations"] if r["usage_basis"] == "prior_season"]
    assert rows, "expected the prior-season baseline to be used and labelled"
    assert all(0 < r["final_prob"] < 0.99 for r in rows)


def test_chat_tools_are_registered_and_return_grounded_prop_data(event, monkeypatch):
    for name in ("get_player_props", "get_player_usage", "search_player_context",
                 "analyze_td_scorers", "analyze_player_prop", "find_best_player_props"):
        assert name in tools.REGISTRY
    monkeypatch.setattr(tools, "_props", lambda *a, **kw: props.analyze_props(
        event, category=kw.get("category"), deps=_deps(_td_rows() + _market_rows()), now=1000.0))
    td = tools.analyze_td_scorers(event_id=event)
    assert td["most_likely"]["player"] == BIG_BEN
    assert td["sources"] or td["most_likely"]["sources"]
    prop = tools.analyze_player_prop(event_id=event, player=WIDEOUT)
    assert prop["recommendations"] or prop["note"]
    listed = tools.find_best_player_props(event_id=event)
    assert "recommendations" in listed


def test_chat_tool_schemas_are_nfl_props_only():
    schemas = {s["name"]: s for s in tools.SCHEMAS}
    for name in ("get_player_props", "analyze_td_scorers", "analyze_player_prop",
                 "find_best_player_props", "get_player_usage", "search_player_context"):
        assert name in schemas
    assert "sport" not in schemas["analyze_td_scorers"]["parameters"]["properties"]


def test_deterministic_router_can_answer_prop_questions_without_a_key(monkeypatch):
    monkeypatch.setattr(llm, "provider", lambda: None)
    tool, args = llm._route("best TD scorer Chiefs Broncos")
    assert tool in ("analyze_td_scorers", "find_best_player_props")
    tool, args = llm._route("who is most likely to score")
    assert tool == "analyze_td_scorers"
    tool, args = llm._route("show me the best receiving prop")
    assert tool in ("find_best_player_props", "analyze_player_prop")


def test_openai_web_search_returns_only_cited_results():
    class FakeResponses:
        def create(self, **kw):
            class Chunk:
                annotations = [type("A", (), {"url": "https://www.nfl.com/injury-report/x",
                                              "title": "Injury report"})()]
            class Item:
                content = [Chunk()]
            return type("R", (), {"output_text": "text", "output": [Item()]})()

    class FakeProvider(llm.LLMProvider):
        name = "openai"
        def __init__(self):
            self.client = type("C", (), {"responses": FakeResponses()})()
            self.search_model = "test-model"

    found = llm.web_search("chief injury report", provider_instance=llm.OpenAIProvider.__new__(llm.OpenAIProvider)) \
        if False else None
    provider = FakeProvider()
    provider.web_search = llm.OpenAIProvider.web_search.__get__(provider)
    rows = provider.web_search("chief injury report", limit=3)
    assert rows and rows[0]["url"].startswith("https://www.nfl.com/")
    assert rows[0]["backend"] == "openai-web-search"


def test_missing_llm_means_no_llm_research(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert llm.web_search("anything") == []


def test_prop_fetch_authenticates_and_records_quota(monkeypatch):
    """Regression: the prop request must carry the key, or the API answers 401."""
    from edgeterminal.providers import theoddsapi as oddsmod
    from edgeterminal.research import cache as rcache

    captured = {}

    def fake(url, params=None, provider="http"):
        captured.update(url=url, params=dict(params or {}))
        return dict(bookmakers=[]), {"x-requests-remaining": "342", "x-requests-used": "158"}

    monkeypatch.setattr(oddsmod, "get_json_meta", fake)
    client = oddsmod.TheOddsApi(api_key="test-key")
    out = client.fetch_event_props("evt1", markets=["player_anytime_td"], force=True,
                                   cache=rcache.TTLCache())
    assert captured["params"]["apiKey"] == "test-key"
    assert captured["params"]["markets"] == "player_anytime_td"
    assert out["quota"]["remaining"] == "342"
    assert out["quota"]["used"] == "158"

def test_yes_only_td_market_browses_prices_but_recommends_nothing(event):
    """No book posts a No price -> no fair probability, so no recommendation."""
    out = props.analyze_props(event, category="td",
                              deps=_deps(_td_rows(two_sided=False)), now=1000.0)
    assert out["market"]["usable"] is True          # prices are still usable to look at
    assert out["market"]["no_vig_players"] == 0
    assert out["recommendations"] == []
    assert out["official"] == []
    assert out["prices"], "verified prices must still be browsable"
    for row in out["prices"]:
        assert row["state"] == "NOT RECOMMENDABLE"
        assert row["market_prob"] is None
        assert 0 < row["implied_prob"] < 1
    # "most likely by the market" is still answerable without any model.
    assert out["market_favourite"]["player"] == BIG_BEN


def test_first_td_prices_never_enter_the_anytime_board(event):
    """player_1st_td settles differently and is disabled for this release."""
    first_td = [dict(r, market="player_1st_td", api_market="player_1st_td",
                     price=r["price"] * 4 if r["price"] > 0 else r["price"])
                for r in _td_rows(two_sided=False)]
    baseline = props.analyze_props(event, category="td", deps=_deps(_td_rows()), now=1000.0)
    polluted = props.analyze_props(event, category="td",
                                   deps=_deps(_td_rows() + first_td), now=1000.0)

    def _probs(out):
        return {r["player"]: r["market_prob"] for r in out["recommendations"]}

    assert _probs(baseline) == _probs(polluted), "first-TD quotes moved anytime probabilities"
    emitted = [r["market"] for r in polluted["recommendations"] + polluted["prices"]]
    assert "player_1st_td" not in emitted
