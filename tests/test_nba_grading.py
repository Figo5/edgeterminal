"""Research-only NBA grading: final-only, explicit voids, missing is never zero."""
from datetime import datetime, timezone

import pytest

from edgeterminal import nba_grading


def ts(y, m, d, hh=19, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()


TIP = ts(2026, 1, 31)                    # 2026-01-31T19:00:00Z
AFTER = TIP + nba_grading.POSTGAME_DELAY_S + 600
BEFORE_DELAY = TIP + 3600


@pytest.fixture
def forecast():
    return {"game_id": "g1", "home": "BOS", "away": "MIA", "tipoff": TIP,
            "markets": [{"market": "spread", "side": "home", "line": -5.5},
                        {"market": "total", "side": "over", "line": 210.5},
                        {"market": "moneyline", "side": "home"}]}


@pytest.fixture
def final_result():
    return {"game_id": "g1", "game_status": "final", "home_score": 112,
            "away_score": 100, "retrieved_at": TIP + 3 * 3600}


@pytest.fixture
def selection():
    return {"game_id": "g1", "player_id": "p1", "player": "Player One",
            "market": "points", "line": 20.5, "side": "over", "tipoff": TIP}


def _prop_result(players, status="final", **kw):
    out = {"game_id": "g1", "game_status": status,
           "retrieved_at": TIP + 3 * 3600, "players": players}
    out.update(kw)
    return out


# ------------------------------------------------------------------ grade_game

def test_grade_game_final_all_markets(forecast, final_result):
    out = nba_grading.grade_game(forecast, final_result, AFTER)
    assert out["status"] == "graded"
    assert out["designation"] == "PASS" and out["readiness"] == "RESEARCH_ONLY"
    results = {g["market"]: g["result"] for g in out["grades"]}
    assert results == {"spread": "won", "total": "won", "moneyline": "won"}
    total = next(g for g in out["grades"] if g["market"] == "total")
    assert total["actual_total"] == 212
    spread = next(g for g in out["grades"] if g["market"] == "spread")
    assert spread["actual_margin"] == 12


def test_grade_game_push_on_exact_line(forecast, final_result):
    forecast["markets"] = [{"market": "total", "side": "over", "line": 212.0},
                           {"market": "spread", "side": "home", "line": -12.0},
                           {"market": "spread", "side": "away", "line": 12.0}]
    out = nba_grading.grade_game(forecast, final_result, AFTER)
    assert [g["result"] for g in out["grades"]] == ["push", "push", "push"]


def test_grade_game_losses(forecast, final_result):
    forecast["markets"] = [{"market": "spread", "side": "away", "line": 10.5},
                           {"market": "total", "side": "under", "line": 200.5},
                           {"market": "moneyline", "side": "away"}]
    out = nba_grading.grade_game(forecast, final_result, AFTER)
    assert [g["result"] for g in out["grades"]] == ["lost", "lost", "lost"]


@pytest.mark.parametrize("status", ["postponed", "suspended"])
def test_grade_game_postponed_suspended_pending(forecast, status):
    result = {"game_id": "g1", "game_status": status,
              "retrieved_at": TIP + 3 * 3600}
    out = nba_grading.grade_game(forecast, result, AFTER + 86400)
    assert out["status"] == "pending"
    assert all(g["result"] == "pending" for g in out["grades"])
    assert all(g["reason"] == "game_not_final" for g in out["grades"])


def test_grade_game_canceled_void_after_delay(forecast):
    result = {"game_id": "g1", "game_status": "canceled",
              "retrieved_at": TIP + 3 * 3600}
    out = nba_grading.grade_game(forecast, result, AFTER)
    assert out["status"] == "void"
    assert all(g["result"] == "void" and g["reason"] == "game_canceled"
               for g in out["grades"])


def test_grade_game_postgame_wait(forecast, final_result):
    out = nba_grading.grade_game(forecast, final_result, BEFORE_DELAY)
    assert out["status"] == "pending"
    assert all(g["reason"] == "postgame_wait" for g in out["grades"])


def test_grade_game_missing_score_never_zero(forecast):
    result = {"game_id": "g1", "game_status": "final", "home_score": 112,
              "away_score": None, "retrieved_at": TIP + 3 * 3600}
    out = nba_grading.grade_game(forecast, result, AFTER)
    assert out["status"] == "pending"
    assert all(g["reason"] == "missing_official_score" for g in out["grades"])


def test_grade_game_identity_mismatch_rejected(forecast, final_result):
    final_result["game_id"] = "other"
    with pytest.raises(ValueError):
        nba_grading.grade_game(forecast, final_result, AFTER)


@pytest.mark.parametrize("home,away", [(100, 100), (112, 0), (0, 98)])
def test_grade_game_contradictory_final_fails_closed(forecast, home, away):
    """A tied or scoreless NBA final is contradictory evidence: all pending."""
    result = {"game_id": "g1", "game_status": "final", "home_score": home,
              "away_score": away, "retrieved_at": TIP + 3 * 3600}
    out = nba_grading.grade_game(forecast, result, AFTER)
    assert out["status"] == "pending"
    assert all(g["result"] == "pending" for g in out["grades"])
    assert all(g["reason"] == "inconsistent_final" for g in out["grades"])


# ------------------------------------------------------------------ grade_prop

def test_grade_prop_won_lost_push(selection):
    players = [{"player_id": "p1", "status": "active", "minutes": 34,
                "stats": {"points": 25}}]
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "won" and out["actual"] == 25

    players[0]["stats"]["points"] = 18
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "lost"

    selection["line"] = 18.0
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "push"


@pytest.mark.parametrize("status,expected", [("dnp", "dnp"), ("inactive", "inactive")])
def test_grade_prop_dnp_inactive_explicit_void(selection, status, expected):
    players = [{"player_id": "p1", "status": status, "minutes": 0, "stats": {}}]
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "void"
    assert out["reason"] == expected
    assert out["actual"] is None


def test_grade_prop_zero_requires_proof_of_play(selection):
    selection["line"] = 0.5
    # Zero minutes and no participation proof: zero is not evidence of play.
    players = [{"player_id": "p1", "status": "active", "minutes": 0,
                "stats": {"points": 0}}]
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "pending"
    assert out["reason"] == "unproven_zero"
    assert out["actual"] is None
    # Documented minutes played prove participation: the zero is real.
    players[0]["minutes"] = 12
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "lost" and out["actual"] == 0
    # Explicit participation proof also settles the zero.
    players[0] = {"player_id": "p1", "status": "active",
                  "stats": {"points": 0}, "participation": {"played": True}}
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "lost" and out["actual"] == 0


def test_grade_prop_missing_stat_is_pending_never_zero(selection):
    players = [{"player_id": "p1", "status": "active", "minutes": 30,
                "stats": {"rebounds": 7}}]
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "pending"
    assert out["reason"] == "missing_stat"
    assert out["actual"] is None
    # Player absent from the box score entirely.
    out = nba_grading.grade_prop(selection, _prop_result([]), AFTER)
    assert out["result"] == "pending"
    assert out["reason"] == "missing_player_stat"


def test_grade_prop_unknown_availability_pending(selection):
    players = [{"player_id": "p1", "status": "unknown", "stats": {}}]
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "pending"
    assert out["reason"] == "unknown_availability"


def test_grade_prop_duplicate_player_rows_fail_closed(selection):
    """Duplicate official rows never settle on the first match."""
    players = [{"player_id": "p1", "status": "active", "minutes": 34,
                "stats": {"points": 25}},
               {"player_id": "p1", "status": "active", "minutes": 34,
                "stats": {"points": 12}}]
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "pending"
    assert out["reason"] == "duplicate_player_rows"
    assert out["actual"] is None


def test_grade_prop_pra_from_components(selection):
    selection["market"] = "pra"
    selection["line"] = 17.5
    players = [{"player_id": "p1", "status": "active", "minutes": 30,
                "stats": {"points": 10, "rebounds": 5, "assists": 3}}]
    out = nba_grading.grade_prop(selection, _prop_result(players), AFTER)
    assert out["result"] == "won" and out["actual"] == 18


def test_grade_prop_game_not_final_and_wait(selection):
    players = [{"player_id": "p1", "status": "active", "minutes": 30,
                "stats": {"points": 25}}]
    for status in ("postponed", "suspended"):
        out = nba_grading.grade_prop(selection, _prop_result(players, status=status),
                                     AFTER + 86400)
        assert out["result"] == "pending" and out["reason"] == "game_not_final"
    out = nba_grading.grade_prop(selection, _prop_result(players), BEFORE_DELAY)
    assert out["result"] == "pending" and out["reason"] == "postgame_wait"
    out = nba_grading.grade_prop(selection, _prop_result(players, status="canceled"),
                                 AFTER)
    assert out["result"] == "void" and out["reason"] == "game_canceled"


def test_grade_prop_identity_mismatch_rejected(selection):
    result = _prop_result([])
    result["game_id"] = "other"
    with pytest.raises(ValueError):
        nba_grading.grade_prop(selection, result, AFTER)
