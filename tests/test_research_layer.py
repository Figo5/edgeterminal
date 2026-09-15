"""The web-research layer: classification, caching, bounded features, planning.

Research is evidence, never arithmetic. Everything here produces *structured,
bounded* features with provenance so deterministic model code can decide what
they are worth.
"""
import pytest

from edgeterminal.research import cache as rcache
from edgeterminal.research import features as rfeatures
from edgeterminal.research import planner
from edgeterminal.research import sources


# --------------------------------------------------------------- reliability
def test_source_reliability_follows_the_product_hierarchy():
    assert sources.classify("https://www.nfl.com/injuries")["reliability"] == "PRIMARY"
    assert sources.classify("https://www.chiefs.com/team/depth-chart")["reliability"] == "PRIMARY"
    assert sources.classify("https://www.espn.com/nfl/story/_/id/1/x")["reliability"] == "HIGH"
    assert sources.classify("https://www.rotowire.com/football/player/x")["reliability"] == "MEDIUM"
    assert sources.classify("https://some-betting-blog.example/post")["reliability"] == "LOW"
    assert sources.classify("https://www.reddit.com/r/nfl/comments/1")["reliability"] == "UNVERIFIED"
    assert sources.classify(None)["reliability"] == "UNVERIFIED"


def test_source_classification_reports_why():
    verdict = sources.classify("https://www.espn.com/nfl/injuries")
    assert verdict["domain"] == "espn.com"
    assert verdict["kind"] == "outlet"
    assert verdict["reason"]


def test_official_injury_report_is_primary_and_flagged():
    verdict = sources.classify("https://www.nfl.com/injury-report/week-3")
    assert verdict["reliability"] == "PRIMARY"
    assert "injury" in verdict["topics"]


def test_community_sources_are_only_supporting_context():
    assert sources.is_confirming("https://www.reddit.com/r/fantasyfootball/comments/x") is False
    assert sources.is_confirming("https://www.reuters.com/sports/nfl/x") is True


# --------------------------------------------------------------- freshness
def test_freshness_decays_by_topic():
    day = 86400.0
    now = 100 * day
    assert sources.freshness(now - 3600, now, topic="injury") > 0.9
    assert sources.freshness(now - 3 * day, now, topic="injury") < 0.5
    # a month-old depth chart is background only, never current role confirmation
    assert sources.freshness(now - 30 * day, now, topic="depth_chart") <= 0.2
    assert sources.freshness(None, now, topic="injury") < 1.0


def test_relevance_label_exposes_the_judgement():
    assert sources.relevance_label(0.0, 3600.0, topic="role") == "current"
    assert sources.relevance_label(0.0, 20 * 86400.0, topic="role") in ("background", "stale")


# --------------------------------------------------------------- ttl cache
def test_ttl_cache_expires_and_counts():
    clock = {"t": 100.0}
    cache = rcache.TTLCache(now=lambda: clock["t"])
    cache.set("a", {"v": 1}, ttl=60)
    assert cache.get("a") == {"v": 1}
    cache.get("b")
    clock["t"] += 61
    assert cache.get("a") is None
    stats = cache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 2 and stats["entries"] == 0


def test_ttl_cache_uses_topic_ttls_and_can_store_provenance():
    cache = rcache.TTLCache()
    assert rcache.ttl_for("odds") < rcache.ttl_for("injury") < rcache.ttl_for("role")
    cache.set_topic("role", "query:kc-rb", [{"title": "x"}])
    assert cache.get_topic("role", "query:kc-rb")[0]["title"] == "x"
    assert cache.stats()["entries"] == 1


def test_breaking_injury_news_is_not_overcached_near_kickoff():
    assert rcache.ttl_for("injury") <= 1800
    assert rcache.ttl_for("odds") <= 300


# --------------------------------------------------------------- features
def _item(title, snippet="", url="https://www.espn.com/x", reliability=None, published_at=None,
          retrieved_at=1000.0):
    verdict = sources.classify(url)
    return dict(title=title, snippet=snippet, url=url,
                reliability=reliability or verdict["reliability"],
                domain=verdict["domain"], published_at=published_at,
                retrieved_at=retrieved_at)


def test_confirmed_lead_back_promotion_is_bounded():
    out = rfeatures.extract([
        _item("Chiefs name Xavier Worthy the lead back", "expected lead red-zone back",
              url="https://www.chiefs.com/news/x", reliability="PRIMARY"),
    ], player="Xavier Worthy")
    feature = out["features"]["workload_multiplier"]
    assert 1.0 < feature["value"] <= rfeatures.MAX_MULTIPLIER
    assert feature["provenance"]["reliability"] == "PRIMARY"
    assert feature["provenance"]["url"].endswith("/news/x")
    assert out["uncertainty_penalty"] >= 0.0


def test_snap_limit_is_negative_and_wr_out_redistributes_targets():
    limited = rfeatures.extract([_item("Coach: limited workload for the rookie this week")])
    assert limited["features"]["workload_multiplier"]["value"] < 1.0

    ruled_out = rfeatures.extract([_item("WR1 ruled out for Sunday",
                                         url="https://www.nfl.com/news/wr1-out")],
                                  player="Depth WR")
    assert ruled_out["features"]["target_share_multiplier"]["value"] > 1.0


def test_committee_unchanged_produces_no_meaningful_change():
    out = rfeatures.extract([_item("Coach says backfield committee unchanged")], player="RB")
    assert out["features"]["workload_multiplier"]["value"] == pytest.approx(1.0)


def test_low_reliability_sources_cannot_move_a_projection_much():
    weak = rfeatures.extract([_item("Blog: lead back promotion expected",
                                    url="https://some-betting-blog.example/post")])
    strong = rfeatures.extract([_item("lead back promotion confirmed",
                                      url="https://www.nfl.com/news/x")])
    weak_delta = abs(weak["features"]["workload_multiplier"]["value"] - 1.0)
    strong_delta = abs(strong["features"]["workload_multiplier"]["value"] - 1.0)
    assert weak_delta < strong_delta
    assert weak_delta <= rfeatures.WEAK_SOURCE_LIMIT


def test_all_multipliers_are_bounded():
    out = rfeatures.extract([
        _item("lead back promotion confirmed", url="https://www.nfl.com/news/a"),
        _item("expected to be limited with a snap count", url="https://www.espn.com/b"),
    ])
    for feature in out["features"].values():
        assert rfeatures.MIN_MULTIPLIER <= feature["value"] <= rfeatures.MAX_MULTIPLIER


def test_conflicting_sources_increase_uncertainty_instead_of_picking_a_side():
    out = rfeatures.extract([
        _item("Player X listed questionable", url="https://www.nfl.com/injury-report/x",
              reliability="PRIMARY"),
        _item("Player X expected to play and handle a full workload", url="https://www.espn.com/x"),
        _item("Player X limited workload possible", url="https://www.si.com/x"),
    ], player="Player X")
    assert out["conflicts"]
    assert out["uncertainty_penalty"] > 0.0
    assert out["features"]["availability_multiplier"]["value"] <= 1.0
    assert out["source_agreement"] < 1.0


def test_agreement_produces_no_penalty():
    out = rfeatures.extract([
        _item("Player Y: full practice participant, no injury designation",
              url="https://www.nfl.com/injury-report/y", reliability="PRIMARY"),
        _item("Player Y full practice, expected to handle his usual role",
              url="https://www.espn.com/y"),
    ], player="Player Y")
    assert out["uncertainty_penalty"] == 0.0
    assert out["source_agreement"] == 1.0


# --------------------------------------------------------------- web odds
def test_web_fallback_odds_are_never_mixed_with_structured_odds():
    row = rfeatures.web_fallback_quote({
        "player": "P", "market": "player_anytime_td", "price": 185,
        "url": "https://www.espn.com/odds/x", "retrieved_at": 1000.0,
        "source_label": "ESPN odds page", "page_timestamp": None})
    assert row["source_type"] == "WEB_FALLBACK"
    assert row["structured"] is False
    assert row["reliability"] in ("HIGH", "MEDIUM")
    # no verifiable page timestamp -> cannot support an Official Pick
    assert row["eligible_for_official"] is False


def test_stale_web_odds_are_rejected():
    assert rfeatures.web_fallback_quote({
        "player": "P", "market": "player_anytime_td", "price": 185,
        "url": "https://www.espn.com/odds/x", "retrieved_at": 1000.0,
        "page_timestamp": 1000.0 - 6 * 3600})["eligible_for_official"] is False
    fresh = rfeatures.web_fallback_quote({
        "player": "P", "market": "player_anytime_td", "price": 185,
        "url": "https://www.espn.com/odds/x", "retrieved_at": 1000.0,
        "page_timestamp": 1000.0 - 120, "now": 1000.0, "parsed": True})
    assert fresh["eligible_for_official"] is True


def test_web_odds_without_parse_validation_are_ineligible():
    assert rfeatures.web_fallback_quote({
        "player": "P", "market": "player_anytime_td", "price": 185,
        "url": "https://www.espn.com/odds/x", "retrieved_at": 1000.0,
        "page_timestamp": 990.0, "now": 1000.0, "parsed": False})["eligible_for_official"] is False


# --------------------------------------------------------------- planner
def test_planner_asks_only_for_missing_inputs():
    plan = planner.plan_prop_research(
        request=dict(kind="td_scorers", event_name="Denver Broncos @ Kansas City Chiefs"),
        known=dict(has_odds=True, has_injuries=False, has_usage=True, usage_games=9,
                   has_role=False, has_implied_total=True, has_weather=False),
        players=["Patrick Mahomes"], team="Kansas City Chiefs")
    gaps = {step["gap"] for step in plan}
    assert "injuries" in gaps
    assert "role" in gaps
    assert "odds" not in gaps
    assert "usage" not in gaps
    assert all(step["query"] for step in plan)
    assert all(step["priority"] in ("high", "medium", "low") for step in plan)


def test_planner_skips_role_research_when_usage_sample_is_large_and_stable():
    plan = planner.plan_prop_research(
        request=dict(kind="prop", market="player_reception_yds"),
        known=dict(has_odds=True, has_injuries=True, has_usage=True, usage_games=14,
                   has_role=True, has_implied_total=True, has_weather=True,
                   role_stable=True, player="P", team="Kansas City Chiefs"),
        players=["P"], team="Kansas City Chiefs")
    assert plan == []


def test_planner_deduplicates_similar_queries():
    plan = planner.plan_prop_research(
        request=dict(kind="td_scorers"),
        known=dict(has_odds=False, has_injuries=False, has_usage=False,
                   has_role=False, has_implied_total=False, has_weather=False),
        players=["A", "B"], team="Kansas City Chiefs")
    queries = [step["query"] for step in plan]
    assert len(queries) == len(set(queries))
    assert len([q for q in queries if "A" in q or "B" in q]) <= len(queries)


def test_planner_flags_weather_as_low_priority():
    plan = planner.plan_prop_research(
        request=dict(kind="td_scorers"),
        known=dict(has_odds=True, has_injuries=True, has_usage=True, has_role=True,
                   has_implied_total=True, has_weather=False),
        players=[], team="Kansas City Chiefs")
    weather = [s for s in plan if s["gap"] == "weather"]
    assert weather and weather[0]["priority"] == "low"