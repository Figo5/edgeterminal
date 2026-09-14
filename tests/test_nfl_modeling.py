import math

import pytest

from edgeterminal.analytics.nfl_features import (
    blend_market_model,
    effective_sample_weight,
    opponent_adjusted_rate,
    recency_weighted_rate,
    ranking_score,
    select_best_model_quote,
)
from edgeterminal.backtest import evaluate_rows
from edgeterminal.pricing import devig_american, prob_over
from edgeterminal.analytics.football import project_game


def test_no_vig_two_sided_market_sums_to_one():
    home, away = devig_american([-110, -110])
    assert home == pytest.approx(0.5)
    assert away == pytest.approx(0.5)
    assert home + away == pytest.approx(1.0)


def test_recency_weighting_prioritizes_recent_games_without_discarding_old_games():
    # Newest game is last. A half-life of two games keeps history but weights 30 most.
    rate = recency_weighted_rate([10, 10, 10, 30], half_life_games=2)
    assert 15 < rate < 25


def test_prior_season_weight_decays_as_current_sample_grows():
    early = effective_sample_weight(current_games=2, prior_games=17, prior_equivalent_games=6)
    late = effective_sample_weight(current_games=12, prior_games=17, prior_equivalent_games=6)
    assert early["prior_weight"] > late["prior_weight"]
    assert early["current_weight"] < late["current_weight"]


def test_small_samples_increase_uncertainty_and_shrink_to_prior():
    early = effective_sample_weight(2, 17, 6)
    late = effective_sample_weight(14, 17, 6)
    assert early["uncertainty"] > late["uncertainty"]
    assert early["reliability"] < late["reliability"]


def test_opponent_adjustment_discounts_results_against_weak_opponents():
    weak_schedule = opponent_adjusted_rate(raw_rate=0.14, opponent_strength=-0.08)
    strong_schedule = opponent_adjusted_rate(raw_rate=0.14, opponent_strength=0.08)
    assert weak_schedule < strong_schedule
    assert weak_schedule < 0.14 < strong_schedule


def test_market_model_blend_uses_market_as_prior_without_copying_it():
    result = blend_market_model(model_prob=0.62, market_prob=0.52,
                                model_reliability=0.7, market_depth=3)
    assert 0.52 < result["final_prob"] < 0.62
    assert result["final_prob"] != pytest.approx(0.52)
    assert result["model_weight"] > 0


def test_distribution_probability_responds_to_variance():
    tight = prob_over(mean=45, sd=7, line=50)
    wide = prob_over(mean=45, sd=14, line=50)
    assert tight < wide < 0.5


def test_ranking_rewards_value_and_quality_but_penalizes_uncertainty():
    strong = ranking_score(confidence=84, edge_pct=4, ev_pct=7, data_quality=0.9,
                           market_depth=4, uncertainty=0.15)
    uncertain = ranking_score(confidence=90, edge_pct=4, ev_pct=7, data_quality=0.6,
                              market_depth=1, uncertainty=0.6)
    assert strong > uncertain


def test_backtest_reports_brier_log_loss_roi_and_clv():
    rows = [
        {"model_prob": 0.70, "won": 1, "price": -110, "units": 1, "clv_pct": 2.0,
         "market": "spread", "confidence": 82},
        {"model_prob": 0.60, "won": 0, "price": -110, "units": 1, "clv_pct": -1.0,
         "market": "spread", "confidence": 78},
    ]
    out = evaluate_rows(rows)
    assert out["n"] == 2
    assert out["brier_score"] == pytest.approx(((0.7-1)**2 + (0.6-0)**2) / 2)
    assert out["log_loss"] == pytest.approx(-(math.log(0.7) + math.log(0.4)) / 2)
    assert out["avg_clv_pct"] == pytest.approx(0.5)
    assert out["roi_pct"] < 0
    assert out["calibration"][0]["bucket"] in {"75-79", "80-84"}


def test_nfl_projection_widens_distribution_for_small_samples():
    base = {
        "A": {"points_for": 24, "points_against": 20, "games": 2},
        "B": {"points_for": 20, "points_against": 24, "games": 2},
    }
    mature = {team: dict(row, games=14) for team, row in base.items()}
    early = project_game("NFL", "A", "B", base)
    late = project_game("NFL", "A", "B", mature)
    assert early["sd_margin"] > late["sd_margin"]
    assert early["uncertainty"] > late["uncertainty"]
    assert early["sample_size_n"] == 2


def test_nfl_advanced_efficiency_is_bounded_and_explained():
    stats = {
        "A": {"points_for": 23, "points_against": 22, "games": 10,
              "epa_play": 0.25, "success_rate": 0.54, "proe": 0.08},
        "B": {"points_for": 23, "points_against": 22, "games": 10,
              "epa_play": -0.15, "success_rate": 0.42, "proe": -0.04},
    }
    projection = project_game("NFL", "A", "B", stats)
    assert 0 < projection["feature_groups"]["efficiency_points"] <= 3.0
    assert projection["feature_version"] == "nfl-features-1"


def test_quote_selection_uses_blended_ev_not_raw_model_ev():
    candidates = [
        {"quote": {"line": -3.0, "price": 110, "book": "A"},
         "raw_model_prob": 0.55, "market_prob": 0.45},
        {"quote": {"line": -2.5, "price": -110, "book": "B"},
         "raw_model_prob": 0.56, "market_prob": 0.54},
    ]
    picked = select_best_model_quote(candidates, model_reliability=0.3, market_depth=2)
    assert picked["quote"]["book"] == "B"
    assert picked["final_ev"] > 0
