"""Immutable, point-in-time NFL shadow record.

One pregame projection is frozen per canonical game. Result corrections append a
new grade and a separate audit event; neither the freeze nor prior grades can
be changed or deleted through the application schema.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional

from . import db, nfl_identity as NI, nfl_market as NM
from .config import settings
from .integrity import parse_time
from .providers.espn import EspnEvents, SCOREBOARD_URL

PREGAME_WINDOW_S = 7 * 86400
FINAL_WAIT_S = 4 * 3600
CORRECTION_WINDOW_S = 21 * 86400
_sync_lock = threading.Lock()


def _quote(snapshot: Dict, event: Dict, market: str, selection: str,
           prediction_time: float) -> Optional[Dict]:
    rows = [r for r in (snapshot.get("market") or [])
            if not r.get("is_demo") and r.get("captured_at") is not None
            and prediction_time - settings.stale_odds_s <= float(r["captured_at"])
            <= prediction_time + 1]
    valid = NM.valid_game_quotes(rows, event)
    return next((r for r in NM.board_rows(valid, event)
                 if r["market"] == market and r["selection"] == selection), None)


def freeze_pending(now: Optional[float] = None) -> int:
    """Freeze the current model's first eligible pregame view of each game."""
    stamp = time.time() if now is None else now
    inserted = 0
    events = db.q("SELECT * FROM events WHERE sport='NFL' ORDER BY start_time")
    for event in events:
        if db.q1("SELECT 1 FROM nfl_shadow_entries WHERE game_id=?", (event["id"],)):
            continue
        if str(event.get("status") or "").lower() in {
                "status_final", "final", "completed", "post", "closed", "cancelled",
                "status_canceled", "status_cancelled", "status_postponed"}:
            continue
        kickoff = parse_time(event.get("start_time"))
        if kickoff is None or not (stamp < kickoff <= stamp + PREGAME_WINDOW_S):
            continue
        meta = json.loads(event.get("meta") or "{}")
        if meta.get("is_demo") or NI.canonical_id(event) != event["id"]:
            continue
        pred = db.q1(
            "SELECT * FROM nfl_game_predictions WHERE game_id=? "
            "AND model_version=? AND created_at<=? AND created_at<? "
            "ORDER BY created_at DESC LIMIT 1",
            (event["id"], settings.model_version, stamp, kickoff))
        if not pred or pred["created_at"] < kickoff - PREGAME_WINDOW_S:
            continue
        if any(pred[k] is None for k in (
                "projected_home_points", "projected_away_points", "projected_margin",
                "projected_total", "home_win_probability")):
            continue
        saved = db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=? AND game_id=?",
                      (pred["snapshot_id"], event["id"]))
        if not saved:
            continue
        snapshot = json.loads(saved["payload"])
        frozen_game = snapshot.get("game") or {}
        if snapshot.get("is_demo") or any(
                s.get("is_demo") or s.get("trust") == "demo"
                for s in (snapshot.get("sources") or []) if isinstance(s, dict)):
            continue
        if (parse_time(frozen_game.get("kickoff_utc")) != kickoff or
                frozen_game.get("home_team") != event["home_team"] or
                frozen_game.get("away_team") != event["away_team"]):
            continue
        spread = _quote(snapshot, event, "spread", event["home_team"], pred["created_at"])
        total = _quote(snapshot, event, "total", "Over", pred["created_at"])
        season_type = {"pre": 1, "reg": 2, "post": 3}[event["id"].split("-")[2]]
        with db.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO nfl_shadow_entries("
                "game_id,prediction_id,snapshot_id,model_version,feature_version,"
                "season,season_type,week,home_team,away_team,kickoff_utc,frozen_at,"
                "designation,projected_home_points,projected_away_points,"
                "projected_margin,projected_total,home_win_probability,"
                "home_spread_line,home_spread_book,home_spread_price,home_spread_captured_at,"
                "total_line,total_book,over_price,total_captured_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event["id"], pred["id"], pred["snapshot_id"], pred["model_version"],
                 pred["feature_version"], int(meta["season"]), season_type,
                 int(meta["week"]), event["home_team"], event["away_team"],
                 kickoff, stamp, "PASS", pred["projected_home_points"],
                 pred["projected_away_points"], pred["projected_margin"],
                 pred["projected_total"], pred["home_win_probability"],
                 spread.get("line") if spread else None,
                 spread.get("book") if spread else None,
                 spread.get("price") if spread else None,
                 spread.get("captured_at") if spread else None,
                 total.get("line") if total else None,
                 total.get("book") if total else None,
                 total.get("price") if total else None,
                 total.get("captured_at") if total else None))
            inserted += cur.rowcount
    return inserted


def _outcome(pick: float, actual: float) -> str:
    if abs(actual) < 1e-9:
        return "push"
    if abs(pick) < 1e-9:
        return "pass"
    return "win" if pick * actual > 0 else "loss"


def _closing(game_id: str, book: Optional[str], market: str, selection: str,
             event: Dict, after: float, kickoff: float) -> Optional[float]:
    if not book:
        return None
    # A line observed days before kickoff is not a closing comparator.
    candidates = db.q(
        "SELECT * FROM odds_snapshots WHERE event_id=? AND book=? AND market=? "
        "AND selection=? AND captured_at>? AND captured_at>=? AND captured_at<? "
        "AND is_demo=0 ORDER BY captured_at DESC, id DESC LIMIT 30",
        (game_id, book, market, selection, after, kickoff-6*3600, kickoff))
    for row in candidates:
        batch = db.q(
            "SELECT * FROM odds_snapshots WHERE event_id=? AND book=? AND market=? "
            "AND captured_at=? AND is_demo=0",
            (game_id, book, market, row["captured_at"]))
        if any(valid["id"] == row["id"] for valid in NM.valid_game_quotes(batch, event)):
            return float(row["line"])
    return None


def _score(value) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) and score >= 0 and score.is_integer() else None


def _grade(entry: Dict, result: Dict, now: float) -> Optional[Dict]:
    if str(result.get("status") or "").upper() not in {"STATUS_FINAL", "FINAL"}:
        return None
    if db.mapped_nfl_game("espn", str(result.get("id") or "")) != entry["game_id"]:
        return None
    if NI.canonical_id(result) != entry["game_id"]:
        return None
    if abs((parse_time(result.get("start_time")) or 0) - entry["kickoff_utc"]) > 4*3600:
        return None
    meta = result.get("meta") or {}
    home, away = _score(meta.get("home_score")), _score(meta.get("away_score"))
    if home is None or away is None:
        return None
    margin, total = home - away, home + away
    p_margin = float(entry["projected_margin"])
    p_total = float(entry["projected_total"])
    spread = entry.get("home_spread_line")
    total_line = entry.get("total_line")
    ats = (_outcome(p_margin + spread, margin + spread)
           if spread is not None else None)
    total_result = (_outcome(p_total - total_line, total - total_line)
                    if total_line is not None else None)
    su = _outcome(p_margin, margin)
    p_home = float(entry["home_win_probability"])
    brier = (p_home - (1.0 if margin > 0 else 0.0))**2 if margin != 0 else None
    home_err = home - entry["projected_home_points"]
    away_err = away - entry["projected_away_points"]
    event = {"home_team": entry["home_team"], "away_team": entry["away_team"]}
    close_spread = _closing(entry["game_id"], entry.get("home_spread_book"),
                            "spread", entry["home_team"], event,
                            entry["frozen_at"], entry["kickoff_utc"])
    close_total = _closing(entry["game_id"], entry.get("total_book"), "total", "Over",
                           event, entry["frozen_at"], entry["kickoff_utc"])
    spread_clv = ((spread - close_spread) * (1 if p_margin + spread > 0 else -1)
                  if spread is not None and close_spread is not None and
                  p_margin + spread != 0 else None)
    total_clv = ((close_total - total_line) * (1 if p_total > total_line else -1)
                 if total_line is not None and close_total is not None and
                 p_total != total_line else None)
    return dict(home_score=home, away_score=away, su_result=su, ats_result=ats,
                total_result=total_result,
                score_mae=(abs(home_err)+abs(away_err))/2,
                score_rmse=math.sqrt((home_err**2+away_err**2)/2),
                margin_error=margin-p_margin, total_error=total-p_total,
                brier=brier, closing_home_spread=close_spread,
                closing_total=close_total, spread_clv_points=spread_clv,
                total_clv_points=total_clv,
                result_source=meta.get("source_url") or SCOREBOARD_URL,
                source_retrieved_at=float(meta.get("retrieved_at") or now), graded_at=now)


_GRADE_FIELDS = ("home_score", "away_score", "su_result", "ats_result",
                 "total_result", "score_mae", "score_rmse", "margin_error",
                 "total_error", "brier", "closing_home_spread", "closing_total",
                 "spread_clv_points", "total_clv_points", "result_source",
                 "source_retrieved_at", "graded_at")


def sync_results(provider=None, now: Optional[float] = None) -> Dict:
    """Fetch final ESPN weeks and append first grades or explicit corrections."""
    with _sync_lock:
        return _sync_results(provider, now)


def _sync_results(provider=None, now: Optional[float] = None) -> Dict:
    stamp = time.time() if now is None else now
    source = provider or EspnEvents()
    entries = db.q("SELECT * FROM nfl_shadow_entries WHERE kickoff_utc<=?",
                   (stamp - FINAL_WAIT_S,))
    due = []
    for entry in entries:
        latest = db.q1("SELECT * FROM nfl_shadow_grades WHERE game_id=? "
                       "ORDER BY revision DESC LIMIT 1", (entry["game_id"],))
        if latest and stamp - entry["kickoff_utc"] > CORRECTION_WINDOW_S:
            continue
        due.append((entry, latest))
    groups = defaultdict(list)
    for entry, latest in due:
        groups[(entry["season"], entry["season_type"], entry["week"])].append(
            (entry, latest))
    output = dict(graded=0, corrected=0,
                  pending=sum(latest is None for _, latest in due),
                  checked_for_corrections=sum(latest is not None for _, latest in due),
                  errors=[])
    for (season, season_type, week), batch in groups.items():
        try:
            games = source.list_events_for_week(season, season_type, week)
        except Exception as exc:  # noqa: BLE001 - keep all pending records intact
            output["errors"].append(f"{season} week {week}: {type(exc).__name__}")
            continue
        by_id = {NI.canonical_id(g): g for g in games if NI.canonical_id(g)}
        for entry, latest in batch:
            game = by_id.get(entry["game_id"])
            grade = _grade(entry, game, stamp) if game else None
            if not grade:
                continue
            if latest and (latest["home_score"], latest["away_score"]) == (
                    grade["home_score"], grade["away_score"]):
                continue
            revision = (latest["revision"] + 1) if latest else 1
            values = tuple(grade[name] for name in _GRADE_FIELDS)
            with db.tx() as c:
                c.execute(
                    "INSERT INTO nfl_shadow_grades(game_id,revision," +
                    ",".join(_GRADE_FIELDS) + ") VALUES(" +
                    ",".join("?" for _ in range(len(_GRADE_FIELDS)+2)) + ")",
                    (entry["game_id"], revision) + values)
                if latest:
                    c.execute(
                        "INSERT INTO nfl_shadow_audit(game_id,prior_revision,new_revision,"
                        "old_home_score,old_away_score,new_home_score,new_away_score,"
                        "reason,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (entry["game_id"], latest["revision"], revision,
                         latest["home_score"], latest["away_score"],
                         grade["home_score"], grade["away_score"],
                         "ESPN final-score correction", grade["result_source"], stamp))
            output["corrected" if latest else "graded"] += 1
    return output


def _record_summary(rows: List[Dict]) -> Dict:
    graded = [r for r in rows if r.get("revision") is not None]
    if not graded:
        return dict(n=0, score_mae=None, score_rmse=None, margin_mae=None,
                    total_mae=None, brier=None,
                    su=dict(w=0, l=0, p=0), ats=dict(w=0, l=0, p=0),
                    totals=dict(w=0, l=0, p=0),
                    spread_clv_n=0, spread_clv_mean=None,
                    total_clv_n=0, total_clv_mean=None)
    def tally(key):
        values = [r.get(key) for r in graded]
        return dict(w=values.count("win"), l=values.count("loss"),
                    p=values.count("push"))
    def mean(values):
        return round(sum(values)/len(values), 4) if values else None
    spread_clv = [r["spread_clv_points"] for r in graded
                  if r.get("spread_clv_points") is not None]
    total_clv = [r["total_clv_points"] for r in graded
                 if r.get("total_clv_points") is not None]
    # RMSE is pooled over both team-score errors, not an average of game RMSEs.
    score_mse = [r["score_rmse"]**2 for r in graded]
    return dict(n=len(graded), score_mae=mean([r["score_mae"] for r in graded]),
                score_rmse=round(math.sqrt(sum(score_mse)/len(score_mse)), 4),
                margin_mae=mean([abs(r["margin_error"]) for r in graded]),
                total_mae=mean([abs(r["total_error"]) for r in graded]),
                brier=mean([r["brier"] for r in graded if r["brier"] is not None]),
                su=tally("su_result"), ats=tally("ats_result"),
                totals=tally("total_result"),
                spread_clv_n=len(spread_clv), spread_clv_mean=mean(spread_clv),
                total_clv_n=len(total_clv), total_clv_mean=mean(total_clv))


def performance() -> Dict:
    """All frozen projections, latest grade per game, with pick states separate."""
    rows = db.q(
        "SELECT e.*,g.* FROM nfl_shadow_entries e LEFT JOIN nfl_shadow_grades g "
        "ON g.game_id=e.game_id AND g.revision=(SELECT MAX(revision) "
        "FROM nfl_shadow_grades WHERE game_id=e.game_id) "
        "ORDER BY e.season,e.season_type,e.week,e.kickoff_utc")
    weekly = defaultdict(list)
    versions = defaultdict(list)
    for row in rows:
        season_type = {1: "PRE", 2: "REG", 3: "POST"}.get(row["season_type"], "UNK")
        weekly[f"{row['season']} {season_type} W{row['week']:02d}"].append(row)
        versions[row["model_version"]].append(row)
    needs_review = [r["game_id"] for r in rows
                    if r.get("revision") is None and
                    r["kickoff_utc"] < time.time() - 24*3600]
    return dict(readiness="RESEARCH_ONLY",
                forward_shadow_status="TRACKING" if rows else "NOT_STARTED",
                frozen_nfl_games=len(rows),
                graded_nfl_game_predictions=sum(r.get("revision") is not None for r in rows),
                all_projections=_record_summary(rows),
                official_picks=dict(n=0), leans=dict(n=0),
                weekly={key: _record_summary(value) for key, value in weekly.items()},
                model_versions={key: dict(frozen=len(value), **_record_summary(value))
                                for key, value in versions.items()},
                needs_review=needs_review,
                corrections=db.q1("SELECT COUNT(*) n FROM nfl_shadow_audit")["n"],
                note="One immutable pregame prediction per NFL game. PASS is the only "
                     "designation; CLV points are observed same-book line movement "
                     "toward the frozen model side, not an attainable return.")
