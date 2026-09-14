import time

import pytest

from edgeterminal import parlay
from edgeterminal.agents import pipeline
from edgeterminal.agents import tools
from edgeterminal.confidence import ConfidenceInputs, score


def _signals(risk=-0.1):
    return [
        pipeline._sig("Quantitative Analyst", 0.8, "positive"),
        pipeline._sig("Market Analyst", 0.5, "positive"),
        pipeline._sig("Risk Analyst", risk, "risk"),
    ]


def test_single_book_market_cannot_be_official_pick():
    decision = pipeline.strategist(_signals(), 88, 5.0, "good", False, -125, -110,
                                   "spread", books_quoting=1, sample_size_n=20)
    assert decision["state"] == "WATCHLIST"
    assert "book" in decision["why"].lower()


def test_limited_or_poor_data_cannot_be_official_pick():
    for tier in ("limited", "poor"):
        decision = pipeline.strategist(_signals(), 90, 5.0, tier, False, -125, -110,
                                       "spread", books_quoting=4, sample_size_n=20)
        assert decision["state"] != "OFFICIAL PICK"


def test_unresolved_qb_injury_caps_confidence_below_official_threshold():
    result = score(ConfidenceInputs(edge_pct=8, model_agreement=1, data_quality=.9,
                                    sample_size_n=30, books_quoting=5,
                                    availability_certainty=.5, projection_cv=.2,
                                    context_certainty=.9, unresolved_major_injury=True))
    assert result.score < 80


def test_prior_season_dependency_penalizes_and_caps_confidence():
    baseline = score(ConfidenceInputs(edge_pct=6, model_agreement=.9, data_quality=.85,
                                      sample_size_n=17, books_quoting=4,
                                      availability_certainty=.95, projection_cv=.2,
                                      context_certainty=.9))
    fallback = score(ConfidenceInputs(edge_pct=6, model_agreement=.9, data_quality=.85,
                                      sample_size_n=17, books_quoting=4,
                                      availability_certainty=.95, projection_cv=.2,
                                      context_certainty=.9, prior_season_share=1.0))
    assert fallback.score < baseline.score
    assert fallback.score < 80


def test_conflicting_signals_cap_confidence():
    result = score(ConfidenceInputs(edge_pct=8, model_agreement=.25, data_quality=.9,
                                    sample_size_n=30, books_quoting=5,
                                    availability_certainty=.95, projection_cv=.2,
                                    context_certainty=.9))
    assert result.score < 80


def test_price_beyond_playable_threshold_waits_for_better_price():
    decision = pipeline.strategist(_signals(), 84, 1.0, "good", False, -105, -120,
                                   "spread", books_quoting=4, sample_size_n=20)
    assert decision["state"] == "WAIT FOR PRICE"
    assert decision["trigger"]


def test_parlay_rejects_weak_or_nonofficial_leg():
    base = dict(event_name="A @ B", sport="NFL", line=-2.5, book="fanduel", price=-110,
                model_prob=.57, confidence=84, edge_pct=4.6, data_quality="good", stale=False)
    legs = [dict(base, id="a", event_id="g1", market="spread", selection="A", state="OFFICIAL PICK"),
            dict(base, id="b", event_id="g2", market="total", selection="Over", state="LEAN")]
    assert parlay.build_best(legs, max_legs=2) is None


def test_best_bet_tool_excludes_high_confidence_lean(monkeypatch):
    rows = [dict(state="LEAN", confidence=95, sport="NFL", market="spread", price=-110),
            dict(state="OFFICIAL PICK", confidence=81, sport="NFL", market="spread", price=-110)]
    monkeypatch.setattr(tools.engine, "cached_slate_recs", lambda **kw: rows)
    monkeypatch.setattr(tools, "_slim", lambda r: r)
    result = tools.find_best_bets(sport="NFL")
    assert [r["state"] for r in result["bets"]] == ["OFFICIAL PICK"]
