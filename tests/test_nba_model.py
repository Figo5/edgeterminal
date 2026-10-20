"""Research-only NBA baseline model: eligibility, leakage, uncertainty, selection.

Every projection must come from sourced pre-tipoff boxscore rows. Missing or
thin history is unavailable, never an invented league-mean forecast.
"""
import math
import statistics
from datetime import datetime, timezone

import pytest

from edgeterminal import nba_model
from edgeterminal.pricing import norm_cdf


def ts(y, m, d, hh=19, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()


TIP = ts(2026, 1, 31)  # 2026-01-31T19:00:00Z


def team_row(team, opp, pts, opp_pts, day, game_id, home, possessions=None):
    row = {"date": f"2026-01-{day:02d}", "team": team, "opponent": opp,
           "points": pts, "opponent_points": opp_pts, "home": home,
           "game_id": game_id}
    if possessions is not None:
        row["possessions"] = possessions
    return row


def prop_row(pid, day, minutes, points, rebounds, assists, threes, game_id,
             team="BOS", name="Player One", hour=19):
    return {"played_at": f"2026-01-{day:02d}T{hour:02d}:00:00Z",
            "player_id": pid, "player": name, "team": team,
            "minutes": minutes, "points": points, "rebounds": rebounds,
            "assists": assists, "threes": threes, "game_id": game_id}


@pytest.fixture
def flat_history():
    """BOS always wins 110-100; 12 shared games, date-only rows, normal rest."""
    rows = []
    days = [2, 4, 6, 9, 11, 13, 16, 18, 20, 23, 25, 27]
    for i, d in enumerate(days):
        rows.append(team_row("BOS", "MIA", 110, 100, d, f"g-{i}", i % 2 == 0))
        rows.append(team_row("MIA", "BOS", 100, 110, d, f"g-{i}", i % 2 != 0))
    return rows


@pytest.fixture
def six_game_player():
    rows = []
    days = [2, 5, 8, 11, 14, 17]
    minutes = [30, 32, 34, 30, 32, 34]
    points = [21, 22, 23, 21, 22, 23]
    rebounds = [5, 6, 7, 5, 6, 7]
    assists = [4, 5, 6, 4, 5, 6]
    threes = [1, 2, 3, 1, 2, 3]
    for i, d in enumerate(days):
        rows.append(prop_row("p1", d, minutes[i], points[i], rebounds[i],
                             assists[i], threes[i], f"pg-{i}"))
    return rows


# ------------------------------------------------------------------ project_game

def test_project_game_flat_sourced_history(flat_history):
    out = nba_model.project_game("BOS", "MIA", flat_history, TIP)
    assert out["status"] == "ok"
    assert out["designation"] == "PASS" and out["readiness"] == "RESEARCH_ONLY"
    # (110 off vs 110 allowed)/2 + HFA/2 ; (100 vs 100)/2 - HFA/2
    assert out["proj_home"] == pytest.approx(111.1, abs=0.01)
    assert out["proj_away"] == pytest.approx(98.9, abs=0.01)
    assert out["margin"] == pytest.approx(12.2, abs=0.01)
    assert out["total"] == pytest.approx(210.0, abs=0.01)
    expected_p = 1 - norm_cdf(-out["margin"] / out["sd_margin"])
    assert out["home_win_probability"] == pytest.approx(expected_p, abs=0.002)
    assert 0 < out["home_win_probability"] < 1
    assert out["sd_margin"] > 0 and out["sd_total"] > 0
    assert out["observations"] == {"home": 12, "away": 12}
    assert out["parameter_fingerprint"] == nba_model.PARAMETER_FINGERPRINT
    assert out["model_version"] == nba_model.MODEL_VERSION
    assert out["pace_adjusted"] is False


def test_project_game_post_tipoff_rows_do_not_leak(flat_history):
    base = nba_model.project_game("BOS", "MIA", flat_history, TIP)
    poisoned = flat_history + [
        {"played_at": TIP + 1, "team": "BOS", "opponent": "MIA",
         "points": 400, "opponent_points": 0, "home": True, "game_id": "x1"},
        {"played_at": TIP + 3600, "team": "MIA", "opponent": "BOS",
         "points": 400, "opponent_points": 0, "home": False, "game_id": "x1"},
    ]
    assert nba_model.project_game("BOS", "MIA", poisoned, TIP) == base


def test_project_game_tipoff_boundary(flat_history):
    base = nba_model.project_game("BOS", "MIA", flat_history, TIP)
    at = flat_history + [{"played_at": TIP, "team": "BOS", "opponent": "MIA",
                          "points": 130, "opponent_points": 90, "home": True,
                          "game_id": "at-tip"}]
    # A record exactly at tipoff is not pre-tipoff: excluded.
    assert nba_model.project_game("BOS", "MIA", at, TIP) == base
    # A game inside the 4h finish lag could not have been final: excluded.
    unfinished = flat_history + [{"played_at": TIP - 14399, "team": "BOS",
                                  "opponent": "MIA", "points": 130,
                                  "opponent_points": 90, "home": True,
                                  "game_id": "unfinished"}]
    assert nba_model.project_game("BOS", "MIA", unfinished, TIP) == base
    # A game exactly at tipoff - 4h could have finished: eligible.
    finished = flat_history + [{"played_at": TIP - 14400, "team": "BOS",
                                "opponent": "MIA", "points": 130,
                                "opponent_points": 90, "home": True,
                                "game_id": "finished"}]
    changed = nba_model.project_game("BOS", "MIA", finished, TIP)
    assert changed["status"] == "ok"
    assert changed["proj_home"] != base["proj_home"]
    assert changed["observations"]["home"] == 13


def test_project_game_insufficient_history_unavailable(flat_history):
    out = nba_model.project_game("BOS", "MIA", flat_history[:18], TIP)  # MIA has 9
    assert out["status"] == "unavailable"
    assert out["reason"] == "insufficient_team_history"
    assert "proj_home" not in out and "margin" not in out


def test_project_game_missing_history_is_not_league_mean():
    out = nba_model.project_game("BOS", "MIA", [], TIP)
    assert out["status"] == "unavailable"
    # No invented forecast: no projection keys at all.
    for key in ("proj_home", "proj_away", "margin", "total", "home_win_probability"):
        assert key not in out


def test_project_game_unknown_team_unavailable(flat_history):
    out = nba_model.project_game("BOS", "SEA", flat_history, TIP)
    assert out["status"] == "unavailable"
    assert out["reason"] == "unknown_team"


def test_project_game_pace_adjusted_when_possessions_sourced():
    rows = []
    days = [2, 4, 6, 9, 11, 13, 16, 18, 20, 23, 25, 27]
    for i, d in enumerate(days):
        # BOS: 114.4 on 104 poss (110.0/100), allows 104 on 104 (100.0/100)
        rows.append(team_row("BOS", "MIA", 114.4, 104, d, f"p-{i}",
                             i % 2 == 0, possessions=104))
        # MIA: 100 on 100 poss (100.0/100), allows 110 on 100 (110.0/100)
        rows.append(team_row("MIA", "BOS", 100, 110, d, f"p-{i}",
                             i % 2 != 0, possessions=100))
    out = nba_model.project_game("BOS", "MIA", rows, TIP)
    assert out["status"] == "ok"
    assert out["pace_adjusted"] is True
    assert out["expected_pace"] == pytest.approx(102.0, abs=0.01)
    assert out["proj_home"] == pytest.approx(110 * 102 / 100 + nba_model.HFA / 2, abs=0.01)
    assert out["proj_away"] == pytest.approx(100 * 102 / 100 - nba_model.HFA / 2, abs=0.01)


def test_project_game_rest_from_timestamped_dates(flat_history):
    # Move BOS's last game to 24h before tipoff with a full timestamp.
    b2b = [dict(r) for r in flat_history]
    b2b[22] = {"played_at": "2026-01-30T19:00:00Z", "team": "BOS",
               "opponent": "MIA", "points": 110, "opponent_points": 100,
               "home": True, "game_id": "g-11"}
    out = nba_model.project_game("BOS", "MIA", b2b, TIP)
    assert out["status"] == "ok"
    assert out["rest"]["home"] == "b2b"
    assert out["rest"]["away"] == "normal"
    assert out["proj_home"] == pytest.approx(111.1 + nba_model.B2B_HOME_ADJ, abs=0.01)


def test_project_game_missing_rest_context_is_unknown_never_healthy(flat_history):
    b2b = [dict(r) for r in flat_history]
    b2b[22] = {"played_at": "2026-01-30T19:00:00Z", "team": "BOS",
               "opponent": "MIA", "points": 110, "opponent_points": 100,
               "home": True, "game_id": "g-11"}
    out = nba_model.project_game("BOS", "MIA", b2b, TIP, history_complete=False)
    assert out["status"] == "ok"
    assert out["rest"]["home"] == "unknown"
    # Unknown rest is not assumed healthy: no b2b adjustment, no penalty either.
    assert out["proj_home"] == pytest.approx(111.1, abs=0.01)


# ------------------------------------------------------------------ project_prop

def test_project_prop_minutes_aware_baseline(six_game_player):
    out = nba_model.project_prop("p1", "points", six_game_player, TIP, line=20.5)
    assert out["status"] == "ok"
    rates = [21/30, 22/32, 23/34, 21/30, 22/32, 23/34]
    mu = statistics.mean(rates) * 32.0
    assert out["projection"] == pytest.approx(mu, abs=0.01)
    assert out["projected_minutes"] == pytest.approx(32.0, abs=0.01)
    assert out["n"] == 6
    # sample sd ~0.89 is below the documented floor
    assert out["sd"] == nba_model.PROP_SD_FLOOR["points"]
    assert out["probability_over"] == pytest.approx(
        1 - norm_cdf((20.5 - mu) / out["sd"]), abs=0.002)
    assert out["probability_over"] + out["probability_under"] == pytest.approx(1.0)
    assert out["probability_push"] == 0.0  # half line carries no push mass
    assert out["parameter_fingerprint"] == nba_model.PARAMETER_FINGERPRINT


def test_project_prop_raw_projection_without_line(six_game_player):
    out = nba_model.project_prop("p1", "points", six_game_player, TIP)
    assert out["status"] == "ok"
    assert out["probability_over"] is None
    assert out["probability_under"] is None
    assert out["probability_push"] is None
    assert out["projection"] is not None


def test_project_prop_pra_and_count_market(six_game_player):
    pra = nba_model.project_prop("p1", "pra", six_game_player, TIP, line=32.5)
    assert pra["status"] == "ok"
    assert pra["projection"] == pytest.approx(32.96, abs=0.02)
    threes = nba_model.project_prop("p1", "threes", six_game_player, TIP, line=1.5)
    assert threes["status"] == "ok"
    assert 0 < threes["probability_over"] < 1


def test_project_prop_requires_five_games(six_game_player):
    out = nba_model.project_prop("p1", "points", six_game_player[:4], TIP)
    assert out["status"] == "unavailable"
    assert out["reason"] == "insufficient_games"
    assert out["n"] == 4
    assert "projection" not in out


def test_project_prop_unknown_minutes_rows_excluded(six_game_player):
    rows = [dict(r) for r in six_game_player]
    rows[0]["minutes"] = None
    rows[1]["minutes"] = ""
    out = nba_model.project_prop("p1", "points", rows, TIP)
    assert out["status"] == "unavailable"
    assert out["n"] == 4


def test_project_prop_rejects_non_numeric_and_leaking_rows(six_game_player):
    base = nba_model.project_prop("p1", "points", six_game_player, TIP)
    rows = [dict(r) for r in six_game_player] + [
        dict(prop_row("p1", 19, 34, 200, 8, 8, 4, "leak"), played_at=TIP + 1),
        dict(prop_row("p1", 19, 34, 200, 8, 8, 4, "at-tip"), played_at=TIP),
        dict(prop_row("p1", 19, 34, float("nan"), 8, 8, 4, "nan-row")),
        dict(prop_row("p2", 19, 34, 99, 8, 8, 4, "other-player")),
    ]
    assert nba_model.project_prop("p1", "points", rows, TIP) == base


def test_project_prop_unknown_market_rejected(six_game_player):
    with pytest.raises(ValueError):
        nba_model.project_prop("p1", "steals", six_game_player, TIP)


def test_project_prop_integer_line_push_mass_count_market(six_game_player):
    # Poisson branch (sd floor 1.3, variance below mean*1.05)
    mu = statistics.mean([1/30, 2/32, 3/34, 1/30, 2/32, 3/34]) * 32.0
    out = nba_model.project_prop("p1", "threes", six_game_player, TIP, line=2.0)
    assert out["status"] == "ok"
    e = math.exp(-mu)
    assert out["probability_over"] == pytest.approx(
        1 - e * (1 + mu + mu * mu / 2), abs=0.002)
    assert out["probability_push"] == pytest.approx(e * mu * mu / 2, abs=0.002)
    assert out["probability_under"] == pytest.approx(e * (1 + mu), abs=0.002)
    total = (out["probability_over"] + out["probability_under"]
             + out["probability_push"])
    assert total == pytest.approx(1.0, abs=0.001)
    # Push mass is excluded from under, not folded into it.
    assert out["probability_under"] < 1 - out["probability_over"]


def test_project_prop_integer_line_continuity_correction_points(six_game_player):
    mu = statistics.mean([21/30, 22/32, 23/34, 21/30, 22/32, 23/34]) * 32.0
    sd = nba_model.PROP_SD_FLOOR["points"]
    out = nba_model.project_prop("p1", "points", six_game_player, TIP, line=22.0)
    assert out["status"] == "ok"
    assert out["probability_over"] == pytest.approx(
        1 - norm_cdf((22.5 - mu) / sd), abs=0.002)
    assert out["probability_under"] == pytest.approx(
        norm_cdf((21.5 - mu) / sd), abs=0.002)
    assert out["probability_push"] == pytest.approx(
        norm_cdf((22.5 - mu) / sd) - norm_cdf((21.5 - mu) / sd), abs=0.002)
    assert out["probability_push"] > 0
    assert (out["probability_over"] + out["probability_under"]
            + out["probability_push"]) == pytest.approx(1.0, abs=0.001)


def test_project_game_duplicate_game_ids_and_conflicts(flat_history):
    base = nba_model.project_game("BOS", "MIA", flat_history, TIP)
    # Identical duplicate rows collapse: nothing changes.
    identical = flat_history + [dict(flat_history[0]), dict(flat_history[1])]
    assert nba_model.project_game("BOS", "MIA", identical, TIP) == base
    # A conflicting duplicate taints its game for that team: it is rejected.
    conflict = flat_history + [dict(flat_history[0], points=130)]
    out = nba_model.project_game("BOS", "MIA", conflict, TIP)
    assert out["status"] == "ok"
    assert out["observations"]["home"] == 11
    assert out["input_conflicts"] == 1
    # Conflicting rows never count toward sufficiency.
    thin = flat_history[:18] + [dict(flat_history[0], points=130),
                                dict(flat_history[2], points=131),
                                dict(flat_history[4], points=132)]
    out = nba_model.project_game("BOS", "MIA", thin, TIP)
    assert out["status"] == "unavailable"
    assert out["observations"]["home"] == 6


def test_project_prop_duplicate_game_ids_and_conflicts(six_game_player):
    base = nba_model.project_prop("p1", "points", six_game_player, TIP)
    identical = six_game_player + [dict(six_game_player[0])]
    assert nba_model.project_prop("p1", "points", identical, TIP) == base
    one_conflict = six_game_player + [dict(six_game_player[0], points=99)]
    out = nba_model.project_prop("p1", "points", one_conflict, TIP)
    assert out["status"] == "ok"
    assert out["n"] == 5
    assert out["input_conflicts"] == 1
    two_conflicts = one_conflict + [dict(six_game_player[1], minutes=40)]
    out = nba_model.project_prop("p1", "points", two_conflicts, TIP)
    assert out["status"] == "unavailable"
    assert out["n"] == 4


# ------------------------------------------------------------------ select_props

def _player_history(pid, team, game_id, mean_points):
    rows = []
    for i, d in enumerate([2, 5, 8, 11, 14, 17]):
        rows.append({"played_at": f"2026-01-{d:02d}T19:00:00Z",
                     "player_id": pid, "player": f"Player {pid}", "team": team,
                     "minutes": 32, "points": mean_points + (i % 2),
                     "rebounds": 6, "assists": 5, "threes": 2,
                     "game_id": f"{game_id}-{i}"})
    return rows


def _quote(pid, team, game_id, market, line, side, book="bookA",
           observed=TIP - 3600, verified=True, price=-110,
           line_type="main", is_main=True):
    q = {"game_id": game_id, "player_id": pid, "player": f"Player {pid}",
         "team": team, "market": market, "line": line, "side": side,
         "price": price, "sportsbook": book, "observed_at": observed,
         "identity_verified": verified}
    if line_type is not None:
        q["line_type"] = line_type
    if is_main is not None:
        q["is_main"] = is_main
    return q


@pytest.fixture
def slate():
    """Six verified players on six games; overs supported on depressed lines."""
    history, quotes = [], []
    means = {"p1": 22, "p2": 24, "p3": 20, "p4": 26, "p5": 18, "p6": 25}
    teams = ["BOS", "MIA", "NYK", "CHI", "ATL", "DEN"]
    for i, (pid, mean) in enumerate(means.items()):
        gid = f"g{i}"
        history += _player_history(pid, teams[i], gid, mean)
        line = mean - 2.5
        quotes.append(_quote(pid, teams[i], gid, "points", line, "over"))
        quotes.append(_quote(pid, teams[i], gid, "points", line, "under"))
    return history, quotes


def test_select_props_five_distinct_strongest_side(slate):
    history, quotes = slate
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert out["status"] == "ok"
    assert len(out["selections"]) == 5
    keys = {(s["player_id"], s["market"]) for s in out["selections"]}
    assert len(keys) == 5  # distinct player/stat markets
    probs = [s["probability"] for s in out["selections"]]
    assert probs == sorted(probs, reverse=True)  # ranked by model probability
    for sel in out["selections"]:
        assert sel["side"] == "over"  # lines sit 2.5 below projections
        assert sel["probability"] > 0.5
        # Implied difference stays descriptive, never a ranking/filter input.
        assert sel["implied_probability"] == pytest.approx(110 / 210, abs=0.001)
        assert sel["edge"] == pytest.approx(
            sel["probability"] - sel["implied_probability"], abs=0.001)
        assert sel["identity_verified"] is True
        assert sel["observed_at"] < TIP
        assert sel["sportsbook"] and sel["price"] == -110
        assert sel["parameter_fingerprint"] == nba_model.PARAMETER_FINGERPRINT


def test_select_props_probability_not_edge_ranks(slate):
    """A juiced weak quote must not outrank a stronger model probability."""
    history, quotes = slate
    # p6 has the same model probability as the others but a far better price;
    # ranking by edge would promote it, ranking by probability must not.
    for q in quotes:
        if q["player_id"] == "p6":
            q["price"] = 2000  # huge implied difference, descriptive only
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert out["status"] == "ok"
    probs = [s["probability"] for s in out["selections"]]
    assert probs == sorted(probs, reverse=True)
    p6 = next(c for c in out["candidates"] if c["player_id"] == "p6")
    assert p6["edge"] > 0.3  # descriptive field still reports the difference


def test_select_props_main_line_provenance_required(slate):
    history, quotes = slate
    for q in quotes:
        if q["player_id"] == "p4":
            q["line_type"], q["is_main"] = "alternate", False
        if q["player_id"] == "p5":
            q["line_type"], q["is_main"] = "main", False  # conflicting
        if q["player_id"] == "p6":
            q.pop("line_type"); q.pop("is_main")          # undocumented
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert out["status"] == "wait"
    reasons = {(e["player_id"], e["reason"]) for e in out["excluded"]}
    assert ("p4", "alternate_line") in reasons
    assert ("p5", "conflicting_main_line_metadata") in reasons
    assert ("p6", "missing_main_line_provenance") in reasons


def test_select_props_main_line_single_flag_suffices(slate):
    history, quotes = slate
    for q in quotes:
        if q["player_id"] == "p6":
            q.pop("is_main")  # line_type="main" alone is explicit enough
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert out["status"] == "ok"
    assert len(out["selections"]) == 5


def test_select_props_stale_quotes_rejected(slate):
    history, quotes = slate
    for q in quotes:
        if q["player_id"] in ("p5", "p6"):
            q["observed_at"] = TIP - 7200  # 5400s old at now: beyond 3600s
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert out["status"] == "wait"
    assert ("p5", "stale_quote") in {(e["player_id"], e["reason"])
                                     for e in out["excluded"]}
    assert ("p6", "stale_quote") in {(e["player_id"], e["reason"])
                                     for e in out["excluded"]}


def test_select_props_now_must_precede_tipoff(slate):
    history, quotes = slate
    with pytest.raises(ValueError):
        nba_model.select_props(quotes, history, TIP, TIP)
    with pytest.raises(ValueError):
        nba_model.select_props(quotes, history, TIP, TIP + 1)


def test_select_props_waits_below_five(slate):
    history, quotes = slate
    cut = [q for q in quotes if q["player_id"] not in ("p5", "p6")]
    out = nba_model.select_props(cut, history, TIP, TIP - 1800)
    assert out["status"] == "wait"
    assert out["selections"] == []
    assert out["eligible_candidates"] == 4


def test_select_props_requires_verified_identity_and_pre_tipoff(slate):
    history, quotes = slate
    for q in quotes:
        if q["player_id"] == "p5":
            q["identity_verified"] = False
        if q["player_id"] == "p6":
            q["observed_at"] = TIP + 60  # after tipoff
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert out["status"] == "wait"
    reasons = {(e["player_id"], e["reason"]) for e in out["excluded"]}
    assert ("p5", "identity_unverified") in reasons
    assert ("p6", "after_tipoff") in reasons


def test_select_props_rejects_future_quotes(slate):
    history, quotes = slate
    for q in quotes:
        if q["player_id"] == "p6":
            q["observed_at"] = TIP - 600  # after `now`
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert ("p6", "future_quote") in {(e["player_id"], e["reason"]) for e in out["excluded"]}


def test_select_props_caps_two_markets_per_player(slate):
    history, quotes = slate
    for market, line in (("rebounds", 3.5), ("assists", 2.5), ("threes", 0.5)):
        quotes.append(_quote("p1", "BOS", "g0", market, line, "over"))
        quotes.append(_quote("p1", "BOS", "g0", market, line, "under"))
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800)
    assert out["status"] == "ok"
    assert len(out["selections"]) == 5
    assert sum(s["player_id"] == "p1" for s in out["selections"]) <= 2


def test_select_props_injury_blocks_and_availability_surfaces(slate):
    history, quotes = slate
    injuries = [{"player_id": "p4", "status": "inactive"},
                {"player_id": "p5", "status": "out"},
                {"player_id": "p6", "status": "doubtful"},
                {"player_id": "p1", "status": "questionable"},
                {"player_id": "p2", "status": "unknown"}]
    out = nba_model.select_props(quotes, history, TIP, TIP - 1800, injuries=injuries)
    assert out["status"] == "wait"  # only three eligible remain
    reasons = {(e["player_id"], e["reason"]) for e in out["excluded"]}
    assert ("p4", "injury_out_or_doubtful") in reasons
    assert ("p5", "injury_out_or_doubtful") in reasons
    assert ("p6", "injury_out_or_doubtful") in reasons
    p1 = next(c for c in out["candidates"] if c["player_id"] == "p1")
    assert "questionable" in p1["flags"]
    p2 = next(c for c in out["candidates"] if c["player_id"] == "p2")
    assert "availability_unknown" in p2["flags"]


# ------------------------------------------------------------- evaluate_history

def _season_rows(games=30):
    rows = []
    for g in range(1, games + 1):
        day = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() + g * 86400
        bos_home = g % 2 == 1
        bos_pts, mia_pts = 108 + (g % 5), 102 + (g % 3)
        rows.append({"played_at": day, "team": "BOS", "opponent": "MIA",
                     "points": bos_pts, "opponent_points": mia_pts,
                     "home": bos_home, "game_id": f"s-{g}"})
        rows.append({"played_at": day, "team": "MIA", "opponent": "BOS",
                     "points": mia_pts, "opponent_points": bos_pts,
                     "home": not bos_home, "game_id": f"s-{g}"})
    return rows


def test_evaluate_history_chronological_no_leakage():
    rows = _season_rows()
    cutoff = ts(2026, 1, 17, 0)  # game 16 (Jan 17 00:00) is the first forward game
    out = nba_model.evaluate_history(rows, cutoff)
    assert out["status"] == "ok"
    assert out["training_cutoff"] == cutoff
    assert out["training_rows"] == 30  # 15 games x 2 team rows before cutoff
    assert out["forward_games"] == 15
    assert out["projected_games"] == 15
    assert len(out["per_game"]) == 15
    assert out["mae_margin"] >= 0 and out["rmse_margin"] >= out["mae_margin"]
    assert out["mae_total"] >= 0
    assert 0 <= out["brier_home_win"] <= 1
    # forward games are chronological and every projection is fingerprinted
    times = [g["tipoff"] for g in out["per_game"]]
    assert times == sorted(times)
    assert all(g["parameter_fingerprint"] == nba_model.PARAMETER_FINGERPRINT
               for g in out["per_game"])
    # Mutating the final forward game must not change earlier projections.
    poisoned = [dict(r) for r in rows]
    poisoned[-2]["points"] = 500
    poisoned[-1]["opponent_points"] = 500
    again = nba_model.evaluate_history(poisoned, cutoff)
    assert again["per_game"][:-1] == out["per_game"][:-1]
    assert again["per_game"][-1] != out["per_game"][-1]


def test_evaluate_history_without_forward_records_unavailable():
    rows = _season_rows()
    out = nba_model.evaluate_history(rows, ts(2026, 3, 1))
    assert out["status"] == "unavailable"
    assert out["reason"] == "no_forward_records"


def test_evaluate_history_without_source_data_makes_no_validation_claim():
    out = nba_model.evaluate_history([], ts(2026, 1, 17))
    assert out["status"] == "unavailable"
    assert out["designation"] == "PASS"
    assert out["readiness"] == "RESEARCH_ONLY"


# ------------------------------------------------------ originals are unchanged

def test_original_analytics_module_unchanged():
    """Golden master: the pre-existing analytics/nba.py behavior is untouched."""
    from edgeterminal.analytics import nba
    stats = {
        "BOS": {"pace": 100.0, "off_rating": 116.0, "def_rating": 110.0},
        "MIA": {"pace": 98.0, "off_rating": 112.0, "def_rating": 112.0},
    }
    out = nba.project_game("BOS", "MIA", stats)
    assert out == {"availability_certainty": 1.0, "away": "MIA",
                   "fair_home_spread": -5.0, "fair_total": 223.0, "home": "BOS",
                   "home_win_prob": 0.6721, "margin": 5.17, "notes": [],
                   "pace": 99.0, "proj_away": 108.79, "proj_home": 113.96,
                   "sd_margin": 11.6, "sd_total": 16.5, "sport": "NBA",
                   "total": 222.75}
    prop = nba.project_prop({"name": "X", "team": "BOS",
                             "usage": {"minutes": 34, "games": 10},
                             "props": {"points": (24.0, 6.0)}}, "points")
    assert prop == {"adjustments": [], "availability_certainty": 1.0,
                    "cv": 0.25, "mean": 24.0, "n": 10, "sd": 6.0,
                    "usage": {"games": 10, "minutes": 34}}
    assert nba.prop_prob({"mean": 24.0, "sd": 6.0}, "over", 21.5, "points") == \
        pytest.approx(0.6615388804893103)
    assert nba.spread_prob(out, "BOS", -5.5) == pytest.approx(0.48865231059723147)
