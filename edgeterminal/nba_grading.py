"""Research-only NBA statistical grading of saved forecasts and selections.

RESEARCH_ONLY / PASS. Pure functions over caller-supplied saved forecasts and
official results: no provider calls, no database, no storage, no live picks.
Grading is final-only with a postgame wait. Missing data is never interpreted
as zero; a player zero grades only with proof the player actually played.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .nba_model import (DESIGNATION, MARKETS, MODEL_VERSION,
                        PARAMETER_FINGERPRINT, POSTGAME_DELAY_S, READINESS,
                        parse_time, valid_number)

GRADING_VERSION = "nba-grading-1.1.0"
GAME_STATUSES = ("final", "postponed", "suspended", "canceled")
PLAYER_STATUSES = ("active", "inactive", "dnp", "unknown")
MARKET_SIDES = {"spread": ("home", "away"), "total": ("over", "under"),
                "moneyline": ("home", "away")}


def _base() -> Dict:
    return {"grading_version": GRADING_VERSION,
            "model_version": MODEL_VERSION,
            "parameter_fingerprint": PARAMETER_FINGERPRINT,
            "readiness": READINESS, "designation": DESIGNATION}


def _status(official_result: Dict) -> str:
    status = str(official_result.get("game_status") or "").lower()
    if status not in GAME_STATUSES:
        raise ValueError(f"unrecognized game status {status!r}")
    return status


def _gate(saved: Dict, official_result: Dict, now) -> Optional[str]:
    """A pending/void reason forced by game state, or None when gradeable."""
    if official_result.get("game_id") != saved.get("game_id"):
        raise ValueError("official result does not match the saved game")
    tip = parse_time(saved.get("tipoff"))
    now_ts = parse_time(now)
    if tip is None or now_ts is None:
        raise ValueError("grading requires a saved tipoff and a valid now")
    status = _status(official_result)
    if status in ("postponed", "suspended"):
        return "game_not_final"
    if now_ts < tip + POSTGAME_DELAY_S:
        return "postgame_wait"
    if status == "canceled":
        return "game_canceled"
    return None


def _settle(side: str, actual: float, line: float) -> str:
    if actual == line:
        return "push"
    if side == "over":
        return "won" if actual > line else "lost"
    return "won" if actual < line else "lost"


# ------------------------------------------------------------------- game side

def _grade_market(entry: Dict, home_score: Optional[float],
                  away_score: Optional[float]) -> Dict:
    market, side = entry.get("market"), entry.get("side")
    grade = {"market": market, "side": side, "line": entry.get("line"),
             "result": None, "reason": None,
             "actual_margin": None, "actual_total": None}
    if home_score is None or away_score is None:
        grade.update(result="pending", reason="missing_official_score")
        return grade
    margin, total = home_score - away_score, home_score + away_score
    grade["actual_margin"], grade["actual_total"] = margin, total
    if market == "moneyline":
        if margin == 0:
            grade.update(result="pending", reason="inconsistent_final")
        elif (margin > 0) == (side == "home"):
            grade["result"] = "won"
        else:
            grade["result"] = "lost"
        return grade
    line = valid_number(entry.get("line"))
    if line is None:
        raise ValueError("a saved spread/total market needs a numeric line")
    if market == "spread":
        adjusted = margin + line if side == "home" else -margin + line
        grade["result"] = ("push" if adjusted == 0
                           else ("won" if adjusted > 0 else "lost"))
    elif market == "total":
        grade["result"] = _settle(side, total, line)
    else:
        raise ValueError(f"unsupported saved market {market!r}")
    return grade


def grade_game(saved_forecast: Dict, official_result: Dict, now) -> Dict:
    """Grade a saved game's frozen markets against an official result.

    won/lost/push on final scores after the postgame wait; postponed or
    suspended stays pending; canceled is void once the wait has passed.
    A missing final score is pending, never zero.
    """
    out = _base()
    out.update(game_id=saved_forecast.get("game_id"),
               game_status=_status(official_result),
               graded_at=parse_time(now))
    gate = _gate(saved_forecast, official_result, now)
    grades: List[Dict] = []
    markets = saved_forecast.get("markets") or []
    if gate == "game_canceled":
        grades = [dict(_grade_shell(m), result="void", reason="game_canceled")
                  for m in markets]
    elif gate is not None:
        grades = [dict(_grade_shell(m), result="pending", reason=gate)
                  for m in markets]
    else:
        home_score = valid_number(official_result.get("home_score"),
                                  nonnegative=True)
        away_score = valid_number(official_result.get("away_score"),
                                  nonnegative=True)
        # Fail closed on contradictory NBA finals: a final cannot be tied or
        # scoreless. Such evidence is pending for every market, never graded.
        inconsistent = (home_score is not None and away_score is not None and
                        (home_score == 0 or away_score == 0 or
                         home_score == away_score))
        for entry in markets:
            if entry.get("market") not in MARKET_SIDES:
                raise ValueError("unsupported saved market")
            if entry.get("side") not in MARKET_SIDES[entry["market"]]:
                raise ValueError("unsupported saved side for market")
            if inconsistent:
                grades.append(dict(_grade_shell(entry), result="pending",
                                   reason="inconsistent_final"))
            else:
                grades.append(_grade_market(entry, home_score, away_score))
    out["grades"] = grades
    if any(g["result"] == "pending" for g in grades):
        out["status"] = "pending"
    elif grades and all(g["result"] == "void" for g in grades):
        out["status"] = "void"
    else:
        out["status"] = "graded"
    return out


def _grade_shell(entry: Dict) -> Dict:
    return {"market": entry.get("market"), "side": entry.get("side"),
            "line": entry.get("line"), "result": None, "reason": None,
            "actual_margin": None, "actual_total": None}


# ------------------------------------------------------------------ prop side

def _official_stat(player: Dict, market: str) -> Optional[float]:
    stats = player.get("stats")
    if not isinstance(stats, dict):
        return None
    if market == "pra":
        if stats.get("pra") is not None:
            return valid_number(stats.get("pra"), nonnegative=True)
        parts = [stats.get(k) for k in ("points", "rebounds", "assists")]
        if all(valid_number(p, nonnegative=True) is not None for p in parts):
            return sum(float(p) for p in parts)
        return None
    if stats.get(market) is None:
        return None
    return valid_number(stats.get(market), nonnegative=True)


def _proof_of_play(player: Dict) -> bool:
    """A zero grades only with evidence the player actually appeared."""
    minutes = valid_number(player.get("minutes"))
    if minutes is not None and minutes > 0:
        return True
    participation = player.get("participation")
    return (isinstance(participation, dict)
            and participation.get("played") is True)


def grade_prop(saved_selection: Dict, official_result: Dict, now) -> Dict:
    """Grade one saved player-prop selection against an official result.

    DNP and inactive are explicit voids. An official stat of zero grades only
    with proof of play (documented minutes or participation evidence); without
    it the selection stays pending. Postponed/suspended stay pending, canceled
    is void after the postgame wait, and missing stats are never zeros.
    """
    out = _base()
    out.update(game_id=saved_selection.get("game_id"),
               player_id=saved_selection.get("player_id"),
               market=saved_selection.get("market"),
               side=saved_selection.get("side"),
               line=saved_selection.get("line"),
               game_status=_status(official_result),
               graded_at=parse_time(now),
               result=None, reason=None, actual=None)
    market = saved_selection.get("market")
    if market not in MARKETS:
        raise ValueError(f"unsupported saved market {market!r}")
    if saved_selection.get("side") not in ("over", "under"):
        raise ValueError("a saved selection needs an over/under side")
    line = valid_number(saved_selection.get("line"))
    if line is None:
        raise ValueError("a saved selection needs a numeric line")

    gate = _gate(saved_selection, official_result, now)
    if gate == "game_canceled":
        out.update(result="void", reason="game_canceled")
        return out
    if gate is not None:
        out.update(result="pending", reason=gate)
        return out

    players = official_result.get("players")
    if not isinstance(players, list):
        out.update(result="pending", reason="missing_player_stat")
        return out
    # Fail closed on duplicate official player rows instead of taking the
    # first match: contradictory evidence stays pending.
    seen_ids = [str(p.get("player_id")) for p in players if isinstance(p, dict)]
    if len(seen_ids) != len(set(seen_ids)):
        out.update(result="pending", reason="duplicate_player_rows")
        return out
    player = next((p for p in players if isinstance(p, dict) and
                   str(p.get("player_id")) == str(saved_selection.get("player_id"))),
                  None)
    if player is None:
        out.update(result="pending", reason="missing_player_stat")
        return out
    availability = str(player.get("status") or "").lower()
    if availability not in PLAYER_STATUSES:
        raise ValueError(f"unrecognized player status {availability!r}")
    if availability in ("dnp", "inactive"):
        out.update(result="void", reason=availability)
        return out
    if availability == "unknown":
        out.update(result="pending", reason="unknown_availability")
        return out

    actual = _official_stat(player, market)
    if actual is None:
        out.update(result="pending", reason="missing_stat")
        return out
    if actual == 0 and not _proof_of_play(player):
        out.update(result="pending", reason="unproven_zero")
        return out
    out["actual"] = actual
    out["result"] = _settle(saved_selection["side"], actual, line)
    return out
