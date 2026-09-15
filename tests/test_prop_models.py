"""Player usage aggregation and the deterministic touchdown / prop models."""
import pytest

from edgeterminal.analytics import td
from edgeterminal.analytics import usage


PBP = [
    # game 1: RB1 carries, scores from the 3; WR1 gets targets
    dict(game_id="g1", season=2026, week=1, season_type="REG", posteam="KC",
         rusher_player_name="RB One", rusher_player_id="rb1", rush_attempt="1",
         yards_gained="4", yardline_100="3", rush_touchdown="1", touchdown="1",
         td_team="KC"),
    dict(game_id="g1", season=2026, week=1, season_type="REG", posteam="KC",
         rusher_player_name="RB One", rusher_player_id="rb1", rush_attempt="1",
         yards_gained="12", yardline_100="45", rush_touchdown="0", touchdown="0"),
    dict(game_id="g1", season=2026, week=1, season_type="REG", posteam="KC",
         receiver_player_name="WR One", receiver_player_id="wr1", pass_attempt="1",
         complete_pass="1", yards_gained="20", air_yards="12", yardline_100="30",
         pass_touchdown="0", touchdown="0"),
    dict(game_id="g1", season=2026, week=1, season_type="REG", posteam="KC",
         receiver_player_name="WR One", receiver_player_id="wr1", pass_attempt="1",
         complete_pass="1", yards_gained="8", air_yards="6", yardline_100="14",
         pass_touchdown="1", touchdown="1", td_team="KC"),
    # game 2
    dict(game_id="g2", season=2026, week=2, season_type="REG", posteam="KC",
         rusher_player_name="RB One", rusher_player_id="rb1", rush_attempt="1",
         yards_gained="30", yardline_100="60", rush_touchdown="0", touchdown="0"),
    dict(game_id="g2", season=2026, week=2, season_type="REG", posteam="KC",
         receiver_player_name="WR One", receiver_player_id="wr1", pass_attempt="1",
         complete_pass="0", yards_gained="0", air_yards="25", yardline_100="50",
         pass_touchdown="0", touchdown="0"),
    # opponent rows must not be attributed to KC players
    dict(game_id="g1", season=2026, week=1, season_type="REG", posteam="DEN",
         rusher_player_name="RB Two", rusher_player_id="rb2", rush_attempt="1",
         yards_gained="3", yardline_100="2", rush_touchdown="1", touchdown="1"),
]


def _rusher(game_id: str, name: str, player_id: str, carries: int):
    """One game of carries for a named rusher."""
    return [dict(game_id=game_id, season="2026", week="1", season_type="REG", posteam="ARI",
                 rusher_player_id=player_id, rusher_player_name=name, rush_attempt="1",
                 yards_gained="4", yardline_100="30", rush_touchdown="0", touchdown="0")
            for _ in range(carries)]


def test_usage_aggregation_attributes_plays_to_the_offence_only():
    rows = usage.aggregate_player_usage(PBP, season=2026, team="Kansas City Chiefs")
    rb1 = rows["rb1"]
    assert rb1["carries"] == 3
    assert rb1["rush_yards"] == 46
    assert rb1["rush_tds"] == 1
    assert rb1["total_tds"] == 1
    assert rb1["red_zone_touches"] == 1
    assert rb1["goal_line_touches"] == 1
    assert rb1["games"] == 2
    assert "rb2" not in rows
    assert rb1["source"] == "nflverse-pbp"
    # snaps are not in play-by-play: never invent them
    assert rb1["snap_share"] is None


def test_usage_aggregation_counts_receiving_and_air_yards():
    wr1 = usage.aggregate_player_usage(PBP, season=2026)["rec:wr1"]
    assert wr1["targets"] == 3
    assert wr1["receptions"] == 2
    assert wr1["rec_yards"] == 28
    assert wr1["rec_tds"] == 1
    assert wr1["air_yards"] == 43
    assert wr1["red_zone_targets"] == 1


def test_usage_is_point_in_time_and_reports_availability():
    rows = usage.aggregate_player_usage(PBP, season=2026, upto_week=2)
    assert rows["rb1"]["games"] == 1
    empty = usage.aggregate_player_usage(PBP, season=2025)
    assert empty == {}


def test_usage_report_marks_missing_metrics_explicitly():
    report = usage.usage_report(PBP, season=2026, player="rb1")
    assert report["available"] is True
    assert report["missing"] == ["snap_share", "route_participation"]
    assert report["red_zone_share"] is not None
    assert report["role_stability"] in ("stable", "volatile")


def test_usage_report_is_honest_when_the_player_is_absent():
    report = usage.usage_report(PBP, season=2026, player="nobody")
    assert report["available"] is False
    assert report["reason"]


def test_player_identity_resolves_nflverse_abbreviations():
    """nflverse play-by-play stores 'J.Conner', not 'James Conner'."""
    plays = [
        *(play for play in _rusher("g1", "J.Conner", "00-0033281", 12)),
        *(play for play in _rusher("g2", "J.Conner", "00-0033281", 15)),
        *(play for play in _rusher("g3", "K.Walker", "00-0037139", 9)),
        *(play for play in _rusher("g4", "A.St. Brown", "00-0036960", 7)),
    ]
    rows = usage.aggregate_player_usage(plays, season=2026)
    assert usage.find_player(rows, "James Conner")["name"] == "J.Conner"
    assert usage.find_player(rows, "Kenneth Walker III")["name"] == "K.Walker"
    assert usage.find_player(rows, "Amon-Ra St. Brown")["name"] == "A.St. Brown"
    assert usage.find_player(rows, "Conner")["name"] == "J.Conner"
    assert usage.find_player(rows, "Nonexistent Player") is None
    # a surname shared by two players resolves to the busier one
    plays += [*(play for play in _rusher("g5", "M.Walker", "00-0000001", 4))]
    rows = usage.aggregate_player_usage(plays, season=2026)
    assert usage.find_player(rows, "Walker")["name"] == "K.Walker"


# ------------------------------------------------------------------ team TD env
def test_team_td_environment_scales_with_implied_total():
    league = td.team_td_environment(implied_total=22.5, opponent_td_allow=1.0)
    good = td.team_td_environment(implied_total=28.0, opponent_td_allow=1.0)
    tough = td.team_td_environment(implied_total=28.0, opponent_td_allow=0.8)
    assert good["projected_team_tds"] > league["projected_team_tds"]
    assert tough["projected_team_tds"] < good["projected_team_tds"]
    assert tough["opponent_factor"] < good["opponent_factor"]


def test_team_td_environment_is_bounded_and_explained():
    env = td.team_td_environment(implied_total=30.0, opponent_td_allow=1.6)
    assert 1.0 <= env["projected_team_tds"] <= 6.0
    assert env["points_per_td"] >= 6.0
    assert env["notes"]


# ------------------------------------------------------------------ player TD
def test_anytime_td_probability_uses_a_poisson_opportunity_model():
    low = td.anytime_td_probability(team_tds=2.5, opportunity_share=0.20)
    high = td.anytime_td_probability(team_tds=3.5, opportunity_share=0.35)
    assert 0 < low["prob"] < high["prob"] <= 0.75
    assert high["expected_tds"] == pytest.approx(3.5 * 0.35, abs=0.01)
    assert high["prob"] == pytest.approx(1 - pow(2.718281828459045, -high["expected_tds"]), abs=0.005)


def test_bounded_multipliers_scale_the_td_projection():
    base = td.anytime_td_probability(team_tds=3.0, opportunity_share=0.25)
    limited = td.anytime_td_probability(
        team_tds=3.0, opportunity_share=0.25,
        multipliers={"workload_multiplier": 0.85, "availability_multiplier": 0.9})
    promoted = td.anytime_td_probability(
        team_tds=3.0, opportunity_share=0.25, multipliers={"workload_multiplier": 1.15})
    assert limited["prob"] < base["prob"] < promoted["prob"]
    assert limited["adjustments"]


def test_first_td_is_a_race_share_and_is_smaller_than_anytime():
    players = [dict(name="A", expected_tds=0.9), dict(name="B", expected_tds=0.5),
               dict(name="C", expected_tds=0.3)]
    race = td.first_td_probabilities(players)
    assert sum(race.values()) <= 1.0
    assert race["A"] > race["B"] > race["C"]
    assert race["A"] < td.anytime_td_probability(team_tds=0.9, opportunity_share=1.0)["prob"]


# ------------------------------------------------------------------ no-vig
def _yes(book, price):
    return dict(book=book, side="yes", price=price)


def _no(book, price):
    return dict(book=book, side="no", price=price)


def test_anytime_td_probabilities_are_never_normalised_across_players():
    """Anytime-TD outcomes are not mutually exclusive: they must not sum to 1."""
    quotes = {"A": [_yes("dk", 120)], "B": [_yes("dk", 250)],
              "C": [_yes("dk", 400)], "D": [_yes("dk", 900)]}
    out = td.no_vig_td_market(quotes)
    assert out["method"] == "same-player-two-sided"
    # Raw implied probabilities are reported as-is, vig included.
    assert out["raw_probs"]["A"] == pytest.approx(0.4545, abs=0.001)
    assert sum(out["raw_probs"].values()) > 1.0   # emphatically not normalised


def test_adding_an_unrelated_player_cannot_change_another_players_probability():
    """The bug that motivated this: every number moved when the field changed."""
    one = td.no_vig_td_market({"A": [_yes("dk", 120), _no("dk", -160)],
                               "B": [_yes("dk", 250)]})
    two = td.no_vig_td_market({"A": [_yes("dk", 120), _no("dk", -160)],
                               "B": [_yes("dk", 250)],
                               "Z": [_yes("dk", 300)], "Y": [_yes("dk", -200)]})
    assert one["players"]["A"]["no_vig"] == two["players"]["A"]["no_vig"]
    assert one["raw_probs"]["B"] == two["raw_probs"]["B"]


def test_same_player_yes_no_devig_is_used_when_both_sides_exist():
    out = td.no_vig_td_market({"A": [_yes("dk", 120), _no("dk", -160)],
                               "B": [_yes("dk", 250), _no("dk", -320)],
                               "C": [_yes("dk", 400)]})
    p_yes, p_no = 100 / 220, 160 / 260
    assert out["players"]["A"]["no_vig"] == pytest.approx(p_yes / (p_yes + p_no), abs=1e-6)
    assert out["players"]["A"]["basis"] == "two_sided_devig"
    assert out["players"]["A"]["no_vig"] < out["players"]["A"]["raw_implied"]
    assert out["no_vig_players"] == 2


def test_a_missing_no_price_stays_missing_and_is_never_fabricated():
    out = td.no_vig_td_market({"A": [_yes("dk", 120), _yes("fd", 125), _yes("mgm", 130)]})
    assert out["players"]["A"]["no_vig"] is None
    assert out["players"]["A"]["basis"] == "raw_implied_with_vig"
    assert out["probs"] == {}          # no fair probability is offered
    assert out["players"]["A"]["raw_implied"] == pytest.approx(0.4444, abs=0.001)  # median of 3 books
    assert "unavailable" in out["reason"]


def test_yes_quotes_from_other_books_do_not_supply_a_missing_no():
    """A No from book X must not de-vig a Yes from book Y."""
    out = td.no_vig_td_market({"A": [_yes("dk", 120), _no("fd", -160)]})
    assert out["players"]["A"]["no_vig"] is None
    assert out["players"]["A"]["two_sided_books"] == 0


def test_no_vig_reports_when_nothing_is_priced():
    out = td.no_vig_td_market({})
    assert out["usable"] is False
    assert out["reason"]


# ------------------------------------------------------------------ board
def _market(**quotes):
    return td.no_vig_td_market(quotes)


def test_td_board_separates_most_likely_from_best_value():
    board = td.td_board(
        players=[dict(name="Chalk", team="KC", opportunity_share=0.35,
                      team_tds=3.0, best_price=120, book="fanduel"),
                 dict(name="Value", team="KC", opportunity_share=0.20,
                      team_tds=3.0, best_price=400, book="bet365")],
        market=_market(Chalk=[_yes("dk", 120), _no("dk", -160), _yes("fd", 125)],
                       Value=[_yes("dk", 400), _no("dk", -520), _yes("fd", 420)]),
        model_reliability=0.6)
    assert board["state"] == "OK"
    assert board["most_likely"]["player"] == "Chalk"
    assert board["most_likely"] is not board["best_value"]
    for row in board["rows"]:
        assert 0 < row["final_prob"] < 1
        assert 0 < row["implied_prob"] < 1
        assert row["fair_price"] is not None
        assert row["recommendable"] is True


def test_board_still_shows_verified_prices_when_no_vig_is_unavailable():
    """Browsing must survive the absence of a model-grade market."""
    board = td.td_board(
        players=[dict(name="A", team="KC", opportunity_share=0.3, team_tds=3.0,
                      best_price=200, book="fanduel")],
        market=_market(A=[_yes("fanduel", 200)]), model_reliability=0.5)
    assert board["state"] == "OK"
    row = board["rows"][0]
    assert row["best_price"] == 200          # the price is visible
    assert row["implied_prob"] == pytest.approx(1 / 3, abs=1e-6)
    assert row["market_prob"] is None        # but no fair market probability
    assert row["recommendable"] is False
    assert row["edge_pct"] is None and row["ev_per_unit"] is None
    assert board["best_value"] is None
    assert board["most_likely_market"]["player"] == "A"


def test_missing_usage_cannot_manufacture_a_model_probability_or_an_edge():
    board = td.td_board(
        players=[dict(name="NoUsage", team="KC", opportunity_share=0.0, team_tds=0.0,
                      best_price=1500, book="fanduel")],
        market=_market(NoUsage=[_yes("dk", 1500), _no("dk", -2500)]),
        model_reliability=0.9)
    row = board["rows"][0]
    assert row["model_prob"] is None
    assert row["edge_pct"] is None and row["ev_per_unit"] is None
    assert row["recommendable"] is False
    assert board["best_value"] is None


def test_market_only_rows_never_produce_edge_from_their_own_vig():
    """no_vig - implied would otherwise hand back the vig as fake edge."""
    board = td.td_board(
        players=[dict(name="A", team="KC", opportunity_share=0.0, team_tds=0.0,
                      best_price=120, book="dk")],
        market=_market(A=[_yes("dk", 120), _no("dk", -160)]))
    row = board["rows"][0]
    assert row["market_prob"] is not None
    assert row["probability_basis"] == "market-only"
    assert row["edge_pct"] is None


def test_board_probabilities_and_fair_prices_stay_in_bounds():
    board = td.td_board(
        players=[dict(name=f"P{i}", team="KC", opportunity_share=share, team_tds=3.2,
                      best_price=price, book="dk")
                 for i, (share, price) in enumerate(
                     [(0.05, 2000), (0.2, 300), (0.45, -140), (0.8, -600)])],
        market=_market(**{f"P{i}": [_yes("dk", price), _no("dk", -price if price > 0 else 100)]
                          for i, (share, price) in enumerate(
                              [(0.05, 2000), (0.2, 300), (0.45, -140), (0.8, -600)])}),
        model_reliability=0.7)
    for row in board["rows"]:
        assert 0 < row["implied_prob"] < 1
        assert row["market_prob"] is None or 0 < row["market_prob"] < 1
        assert row["model_prob"] is None or 0 < row["model_prob"] <= 0.75
        assert row["final_prob"] is None or 0 < row["final_prob"] < 1
        if row["fair_price"] is not None:
            assert abs(row["fair_price"]) >= 100
