"""Walk-forward NFL backtest coverage.

The replay must never look at a game it is projecting: features for week W come
only from weeks < W (or a labelled prior-season basis).
"""
import pytest

from edgeterminal import backtest
from edgeterminal.analytics.football import project_game


PBP = [
    dict(game_id="g1", season=2025, week=1, season_type="REG", posteam="KC", epa=1.0, success=1.0),
    dict(game_id="g1", season=2025, week=1, season_type="REG", posteam="BUF", epa=-1.0, success=0.0),
    dict(game_id="g2", season=2025, week=2, season_type="REG", posteam="KC", epa=2.0, success=1.0),
    dict(game_id="g2", season=2025, week=2, season_type="REG", posteam="BUF", epa=-2.0, success=0.0),
]

GAMES = [
    dict(game_id="g1", season=2025, week=1, game_type="REG", home_team="KC", away_team="BUF",
         home_score=24.0, away_score=17.0, spread_line=-3.0, total_line=44.0),
    dict(game_id="g2", season=2025, week=2, game_type="REG", home_team="BUF", away_team="KC",
         home_score=20.0, away_score=27.0, spread_line=1.5, total_line=45.5),
]


def test_team_features_exclude_the_projected_week():
    feats = backtest.team_features(PBP, GAMES, season=2025, upto_week=2)
    kc = feats["Kansas City Chiefs"]
    assert kc["games"] == 1
    assert kc["epa_play"] == 1.0
    assert kc["points_for"] == 24.0
    assert kc["points_against"] == 17.0
    assert set(feats) == {"Kansas City Chiefs", "Buffalo Bills"}


def test_replay_grades_against_the_market_line():
    rows = backtest.replay(PBP, GAMES, season=2025, weeks=[2])
    assert len(rows) == 1
    row = rows[0]
    assert row["home"] == "Buffalo Bills" and row["away"] == "Kansas City Chiefs"
    assert row["basis"] == "current_season"
    assert row["actual_margin"] == -7.0
    # nflverse `spread_line` is the home handicap, positive = home favourite.
    # BUF (home) was favoured by 1.5, lost by 7, so the road side covered.
    assert row["pick"] == "away"
    assert row["ats"] == "win"
    assert row["total_pick"] == "under"
    assert row["total_result"] == "loss"
    assert row["proj_margin"] < -1.5


def test_ats_truth_table_matches_the_verified_nflverse_convention():
    # Home favoured by 7 (spread_line +7) and wins by 10 -> home covers.
    assert backtest._grade_ats("home", 10.0, 7.0) == "win"
    assert backtest._grade_ats("away", 10.0, 7.0) == "loss"
    # Home favoured by 7 but wins by only 3 -> away covers.
    assert backtest._grade_ats("away", 3.0, 7.0) == "win"
    # Exact landing is a push.
    assert backtest._grade_ats("home", 7.0, 7.0) == "push"


def test_replay_uses_prior_season_basis_when_current_sample_is_empty():
    prior = [dict(game_id="p1", season=2024, week=1, season_type="REG", posteam="KC", epa=0.5, success=0.5),
             dict(game_id="p1", season=2024, week=1, season_type="REG", posteam="BUF", epa=0.1, success=0.4)]
    prior_games = [dict(game_id="p1", season=2024, week=1, game_type="REG", home_team="KC", away_team="BUF",
                        home_score=21.0, away_score=20.0, spread_line=-2.0, total_line=43.0)]
    rows = backtest.replay(prior + PBP, prior_games + GAMES, season=2025, weeks=[1], prior_season=2024)
    assert [r["basis"] for r in rows] == ["prior_season"]


def test_summarize_reports_error_market_and_calibration():
    rows = [dict(proj_margin=3.0, actual_margin=1.0, proj_total=45.0, actual_total=48.0,
                 spread_line=-3.0, total_line=44.0, home_win_prob=0.6, home_won=True,
                 ats="win", total_result="loss"),
            dict(proj_margin=-2.0, actual_margin=-4.0, proj_total=40.0, actual_total=38.0,
                 spread_line=1.0, total_line=41.0, home_win_prob=0.4, home_won=False,
                 ats="loss", total_result="win")]
    out = backtest.summarize(rows)
    assert out["n"] == 2
    assert out["margin_mae"] == pytest.approx(2.0)
    assert out["total_mae"] == pytest.approx(2.5)
    assert out["ats_hit_rate"] == 0.5
    assert out["over_hit_rate"] == 0.5
    assert out["brier_score"] == pytest.approx(0.16)


def test_replay_projects_with_the_production_model():
    """The replay must call the same project_game used in production."""
    rows = backtest.replay(PBP, GAMES, season=2025, weeks=[2])
    feats = backtest.team_features(PBP, GAMES, season=2025, upto_week=2)
    expected = project_game("NFL", "Buffalo Bills", "Kansas City Chiefs", feats)
    assert rows[0]["proj_margin"] == expected["margin"]
    assert rows[0]["proj_total"] == expected["total"]
    assert rows[0]["home_win_prob"] == expected["home_win_prob"]
