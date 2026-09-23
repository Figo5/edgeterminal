"""Predeclared, point-in-time NFL comparison models.

These simple baselines are evaluators, not pick generators. Only final scores
from earlier weeks enter a prediction. Closing market lines are deliberately
absent from this module and are used only as an external benchmark.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List

from .providers.normalize import normalize_team

HOME_FIELD_POINTS = 1.7
LEAGUE_TOTAL = 44.0
ELO_HOME_POINTS = 45.0
ELO_K = 20.0
ELO_POINTS_PER_RATING = 28.0


def _number(value):
    try:
        return float(value) if value not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


def baselines_before(games: Iterable[Dict], season: int, week: int,
                     home: str, away: str,
                     neutral: bool = False) -> Dict[str, Dict[str, float]]:
    """Return fixed home-field, Elo and rolling-form predictions before week W.

    The previous season is used in full; the current season is cut off at W.
    Scores from other games in W remain invisible, even if they kicked off
    earlier. That conservative rule also makes replay ordering irrelevant.
    """
    past: List[Dict] = []
    for game in games:
        year, played_week = _number(game.get("season")), _number(game.get("week"))
        if year not in (float(season - 1), float(season)) or played_week is None:
            continue
        if year == float(season) and played_week >= week:
            continue
        if str(game.get("game_type") or "REG").upper() not in {"REG", "REGULAR"}:
            continue
        hs, as_ = _number(game.get("home_score")), _number(game.get("away_score"))
        h = normalize_team(None, game.get("home_team"))
        a = normalize_team(None, game.get("away_team"))
        if hs is None or as_ is None or not h or not a or h == a:
            continue
        past.append(dict(season=int(year), week=int(played_week), game_id=str(game.get("game_id")),
                         home=h, away=a, home_score=hs, away_score=as_,
                         neutral=str(game.get("location") or "").lower() == "neutral"))
    past.sort(key=lambda g: (g["season"], g["week"], g["game_id"]))

    ratings = defaultdict(lambda: 1500.0)
    scored = defaultdict(list)
    allowed = defaultdict(list)
    for game in past:
        h, a = game["home"], game["away"]
        expected = 1.0 / (1.0 + 10.0 ** (-(ratings[h] +
                        (0.0 if game["neutral"] else ELO_HOME_POINTS) - ratings[a]) / 400.0))
        result = 1.0 if game["home_score"] > game["away_score"] else (
            0.0 if game["home_score"] < game["away_score"] else 0.5)
        change = ELO_K * (result - expected)
        ratings[h] += change
        ratings[a] -= change
        scored[h].append(game["home_score"])
        allowed[h].append(game["away_score"])
        scored[a].append(game["away_score"])
        allowed[a].append(game["home_score"])

    elo_gap = ratings[home] + (0.0 if neutral else ELO_HOME_POINTS) - ratings[away]
    elo_prob = 1.0 / (1.0 + 10.0 ** (-elo_gap / 400.0))

    def recent(team: str, rows: Dict[str, List[float]], prior: float) -> float:
        values = rows[team][-8:]
        return (sum(values) + 4.0 * prior) / (len(values) + 4.0)

    home_for, home_against = recent(home, scored, 22.0), recent(home, allowed, 22.0)
    away_for, away_against = recent(away, scored, 22.0), recent(away, allowed, 22.0)
    rolling_home = (home_for + away_against) / 2.0
    rolling_away = (away_for + home_against) / 2.0
    return {
        "home_league": {"margin": 0.0 if neutral else HOME_FIELD_POINTS, "total": LEAGUE_TOTAL},
        "elo": {"margin": elo_gap / ELO_POINTS_PER_RATING,
                "total": LEAGUE_TOTAL, "home_win_prob": elo_prob},
        "rolling": {"margin": rolling_home - rolling_away +
                    (0.0 if neutral else HOME_FIELD_POINTS),
                    "total": rolling_home + rolling_away},
    }
