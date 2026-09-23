"""Unvalidated live context must not silently change the NFL numeric model."""

from edgeterminal.analytics.football import project_game


def test_nfl_injury_weather_and_rest_are_context_only():
    teams = {
        "Home": {"points_for": 24, "points_against": 20, "games": 8},
        "Away": {"points_for": 21, "points_against": 23, "games": 8},
    }
    base = project_game("NFL", "Home", "Away", teams)
    contextual = project_game(
        "NFL", "Home", "Away", teams,
        injuries=[{"team": "Home", "player": "Starter", "position": "QB",
                   "status": "questionable"}],
        weather={"indoor": False, "wind_mph": 27, "precip_chance": 90,
                 "temp_f": 12},
        home_rest=3, away_rest=11,
    )
    for key in ("proj_home", "proj_away", "margin", "total", "home_win_prob"):
        assert contextual[key] == base[key]
    assert contextual["availability_certainty"] < base["availability_certainty"]
    assert contextual["weather_points"] == 0
    assert all("pts" not in note for note in contextual["notes"])
