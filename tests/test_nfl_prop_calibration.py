"""Synthetic-threshold calibration tests; never sportsbook backtests."""
import math

from edgeterminal.nfl_prop_calibration import (
    FIXED_THRESHOLDS, choose_method, predicted_probabilities, reliability,
    synthetic_threshold,
)


def test_threshold_is_deterministic_and_predeclared():
    first=synthetic_threshold("receiving_yards","game-a","player-a")
    assert first in FIXED_THRESHOLDS["receiving_yards"]
    assert first==synthetic_threshold("receiving_yards","game-a","player-a")


def test_uncertainty_methods_only_use_prior_residuals():
    prior=[{"residual":float(i-10),"position":"WR","player_id":"p" if i<8 else "other"}
           for i in range(30)]
    row={"prediction":40.0,"threshold":49.5,"position":"WR","player_id":"p"}
    result=predicted_probabilities(prior,row)
    assert set(result)=={"global_normal","position_normal","player_rolling_normal","empirical_quantile"}
    assert all(0 < value < 1 for value in result.values())
    assert result==predicted_probabilities(prior,row)


def test_selection_uses_development_not_holdout():
    dev={"global_normal":{"brier":.18,"log_loss":.55},
         "position_normal":{"brier":.17,"log_loss":.58},
         "player_rolling_normal":{"brier":.20,"log_loss":.56},
         "empirical_quantile":{"brier":.19,"log_loss":.54}}
    holdout={"global_normal":{"brier":.10},"position_normal":{"brier":.30}}
    assert choose_method(dev,holdout)=="position_normal"
    table=reliability([(0.15,0),(0.25,1),(0.95,1)])
    assert sum(bucket["n"] for bucket in table)==3
    assert table[1]["realized_over"]==0
