"""Historical comparators are fixed and cannot see the evaluated week."""

from edgeterminal.nfl_baselines import baselines_before


def game(year, week, gid, home, away, hs, as_):
    return dict(season=year, week=week, game_id=gid, game_type="REG",
                home_team=home, away_team=away, home_score=hs, away_score=as_)


def test_same_week_and_future_scores_do_not_change_baselines():
    past = [game(2024, 17, "p", "KC", "BUF", 21, 17),
            game(2025, 1, "w1", "KC", "BUF", 28, 7)]
    target = baselines_before(past, 2025, 2, "Kansas City Chiefs", "Buffalo Bills")
    future = past + [game(2025, 2, "w2a", "KC", "BUF", 0, 60),
                     game(2025, 12, "w12", "KC", "BUF", 0, 70),
                     game(2026, 1, "next", "KC", "BUF", 0, 80)]
    assert baselines_before(future, 2025, 2, "Kansas City Chiefs", "Buffalo Bills") == target
    assert target["home_league"] == {"margin": 1.7, "total": 44.0}


def test_prior_results_move_elo_and_rolling_in_expected_direction():
    no_history = baselines_before([], 2025, 2, "Kansas City Chiefs", "Buffalo Bills")
    history = [game(2025, 1, "w1", "KC", "BUF", 42, 7)]
    with_history = baselines_before(history, 2025, 2,
                                    "Kansas City Chiefs", "Buffalo Bills")
    assert with_history["elo"]["margin"] > no_history["elo"]["margin"]
    assert with_history["rolling"]["margin"] > no_history["rolling"]["margin"]
