from edgeterminal.providers.nflverse import aggregate_team_features, merge_feature_seasons


def test_nflverse_aggregation_produces_reproducible_bounded_team_features():
    plays = [
        {"posteam": "BUF", "defteam": "KC", "epa": "0.4", "success": "1",
         "qb_dropback": "1", "rush_attempt": "0", "cpoe": "5", "pass_oe": "8",
         "game_id": "g1", "play_id": "1", "no_huddle": "0"},
        {"posteam": "BUF", "defteam": "KC", "epa": "-0.2", "success": "0",
         "qb_dropback": "0", "rush_attempt": "1", "cpoe": "", "pass_oe": "-4",
         "game_id": "g1", "play_id": "2", "no_huddle": "1"},
        {"posteam": "KC", "defteam": "BUF", "epa": "0.1", "success": "1",
         "qb_dropback": "1", "rush_attempt": "0", "cpoe": "2", "pass_oe": "3",
         "game_id": "g1", "play_id": "3", "no_huddle": "0"},
    ]
    out = aggregate_team_features(plays, season=2026, retrieved_at=123.0)
    buffalo = out["Buffalo Bills"]
    assert buffalo["epa_play"] == 0.1
    assert buffalo["epa_dropback"] == 0.4
    assert buffalo["epa_rush"] == -0.2
    assert buffalo["success_rate"] == 0.5
    assert buffalo["cpoe"] == 5.0
    assert buffalo["proe"] == 2.0
    assert buffalo["games"] == 1
    assert buffalo["source"] == "nflverse-pbp"
    assert buffalo["season"] == 2026
    assert buffalo["retrieved_at"] == 123.0


def test_merge_feature_seasons_fills_early_season_gaps_from_the_prior_year():
    """A team yet to play this season must still get EPA inputs, clearly labelled."""
    current = {"Buffalo Bills": dict(epa_play=0.1, success_rate=0.5, games=1, season=2026)}
    prior = {"Buffalo Bills": dict(epa_play=0.0, success_rate=0.47, games=17, season=2025),
             "Kansas City Chiefs": dict(epa_play=0.12, success_rate=0.52, games=17, season=2025)}
    merged = merge_feature_seasons(current, prior, season=2026)
    assert merged["Buffalo Bills"]["feature_season"] == 2026
    assert merged["Buffalo Bills"]["prior_season_features"] is False
    assert merged["Kansas City Chiefs"]["feature_season"] == 2025
    assert merged["Kansas City Chiefs"]["prior_season_features"] is True
    assert merged["Kansas City Chiefs"]["epa_play"] == 0.12
    # ESPN keeps ownership of the game count that drives shrinkage.
    assert "games" not in merged["Kansas City Chiefs"]


def test_merge_feature_seasons_is_empty_safe():
    assert merge_feature_seasons({}, {}, season=2026) == {}
    merged = merge_feature_seasons({}, {"Denver Broncos": dict(epa_play=0.05, games=17)}, season=2026)
    assert merged["Denver Broncos"]["prior_season_features"] is True


def test_prior_feature_note_flags_only_the_event_teams():
    from edgeterminal import engine

    event = {"home_team": "Kansas City Chiefs", "away_team": "Denver Broncos"}
    stats = {"Kansas City Chiefs": dict(prior_season_features=True, epa_play=0.05),
             "Denver Broncos": dict(prior_season_features=True, epa_play=0.02),
             "Buffalo Bills": dict(prior_season_features=False)}
    note = engine.prior_feature_note(stats, event)
    assert "Kansas City Chiefs" in note and "Denver Broncos" in note
    assert "Buffalo Bills" not in note
    assert engine.prior_feature_note({"Denver Broncos": dict(prior_season_features=False)},
                                    event) is None
