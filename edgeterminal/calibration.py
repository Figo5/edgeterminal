"""Grading, CLV and confidence calibration.

Calibration deltas are derived statistically from graded predictions. The LLM
cannot write into this table -- confidence is only ever moved by observed
results with an adequate sample.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from . import db
from .config import settings
from .confidence import CONF_BUCKETS, bucket_of
from .pricing import american_to_prob, clv_pct, ev_per_unit
from .backtest import evaluate_rows

MIN_BUCKET_N = 25          # below this we report, but never adjust
MAX_DOWN, MAX_UP = -8.0, 4.0


# ------------------------------------------------------------------- grading
def record_closing_line(event_id: str, market: str, selection: str, book: str,
                        line: Optional[float], price: float) -> None:
    db.ex("INSERT OR REPLACE INTO closing_lines(event_id,market,selection,book,line,price,"
          "captured_at) VALUES(?,?,?,?,?,?,?)",
          (event_id, market, selection, book, line, price, time.time()))


def snapshot_closing_lines(event_id: str) -> int:
    """Freeze the latest stored odds for an event as its closing line."""
    rows = db.latest_odds(event_id=event_id)
    for r in rows:
        record_closing_line(event_id, r["market"], r["selection"], r["book"],
                            r.get("line"), r["price"])
    return len(rows)


def grade_prediction(prediction_id: str, won: Optional[bool], push: bool = False,
                     actual_value: Optional[float] = None,
                     closing_price: Optional[float] = None,
                     closing_line: Optional[float] = None,
                     notes: str = "") -> Dict:
    pred = db.q1("SELECT * FROM predictions WHERE id=?", (prediction_id,))
    if not pred:
        raise ValueError(f"unknown prediction {prediction_id}")
    if closing_price is None:
        cl = db.q1("SELECT price, line FROM closing_lines WHERE event_id=? AND market=? "
                   "AND selection=? ORDER BY captured_at DESC LIMIT 1",
                   (pred["event_id"], pred["market"], pred["selection"]))
        if cl:
            closing_price, closing_line = cl["price"], cl["line"]
    clv = clv_pct(pred["price"], closing_price) if closing_price else None
    outcome = "push" if push else ("win" if won else "loss") if won is not None else "void"
    db.ex("INSERT OR REPLACE INTO results(prediction_id,event_id,outcome,won,push,actual_value,"
          "closing_price,closing_line,clv_pct,graded_at,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (prediction_id, pred["event_id"], outcome, 1 if won else 0, 1 if push else 0,
           actual_value, closing_price, closing_line,
           round(clv, 3) if clv is not None else None, time.time(), notes))
    return dict(prediction_id=prediction_id, outcome=outcome, clv_pct=clv)


# --------------------------------------------------------------- performance
def _joined(where: str = "", params: Optional[List] = None) -> List[Dict]:
    rows = db.q(f"""SELECT p.*, r.outcome, r.won, r.push, r.clv_pct, r.closing_price
                    FROM predictions p JOIN results r ON r.prediction_id = p.id
                    {where}""", params or [])
    # Quarantined predictions (wrong event / wrong matchup) must not be scored,
    # but they are kept in place rather than deleted or rewritten.
    blocked = db.quarantined_ids("prediction")
    return [row for row in rows if str(row["id"]) not in blocked] if blocked else rows


def performance(scope: Optional[str] = None) -> Dict:
    rows = _joined()
    graded = [r for r in rows if r["outcome"] in ("win", "loss")]
    picks = [r for r in graded if r["state"] == "OFFICIAL PICK"]

    def summarise(rs: List[Dict]) -> Dict:
        n = len(rs)
        if not n:
            return dict(n=0, hit_rate=None, roi_pct=None, avg_clv=None, units=0.0, profit_units=0.0)
        wins = sum(1 for r in rs if r["won"])
        staked = sum(float(r["units"] or 0) for r in rs)
        profit = sum((float(r["units"] or 0) *
                      (ev_per_unit(1.0, r["price"]) if r["won"] else -1.0)) for r in rs)
        clvs = [r["clv_pct"] for r in rs if r["clv_pct"] is not None]
        return dict(n=n, hit_rate=round(wins / n, 4),
                    roi_pct=round(profit / staked * 100, 2) if staked else None,
                    avg_clv=round(sum(clvs) / len(clvs), 3) if clvs else None,
                    units=round(staked, 2), profit_units=round(profit, 2))

    by: Dict[str, Dict] = {}
    for key_fn, label in ((lambda r: r["sport"], "by_sport"),
                          (lambda r: r["market"], "by_market"),
                          (lambda r: r["confidence_bucket"], "by_confidence"),
                          (lambda r: r["model_version"], "by_model_version"),
                          (lambda r: _odds_band(r["price"]), "by_odds_band")):
        groups: Dict[str, List[Dict]] = {}
        for r in graded:
            groups.setdefault(str(key_fn(r)), []).append(r)
        by[label] = {k: summarise(v) for k, v in sorted(groups.items())}

    scoring = evaluate_rows(graded)
    return dict(total_predictions=len(db.q("SELECT id FROM predictions")),
                graded=len(graded), official_picks=len(picks),
                overall=summarise(graded), picks_only=summarise(picks),
                scoring=scoring, calibration=calibration_table(), **by)


def _odds_band(price: float) -> str:
    p = float(price)
    if p <= -200:
        return "<= -200"
    if p <= -130:
        return "-200..-130"
    if p < 0:
        return "-130..-100"
    if p < 150:
        return "+100..+150"
    if p < 300:
        return "+150..+300"
    return ">= +300"


def calibration_table(scope_by: str = "confidence_bucket") -> List[Dict]:
    """Observed hit rate vs the win probability the model claimed, per bucket."""
    rows = _joined()
    graded = [r for r in rows if r["outcome"] in ("win", "loss")]
    groups: Dict[str, List[Dict]] = {}
    for r in graded:
        groups.setdefault(r[scope_by] or "?", []).append(r)
    out = []
    for bucket, rs in groups.items():
        n = len(rs)
        wins = sum(1 for r in rs if r["won"])
        expected = sum(float(r["model_prob"] or 0) for r in rs) / n
        observed = wins / n
        clvs = [r["clv_pct"] for r in rs if r["clv_pct"] is not None]
        out.append(dict(bucket=bucket, n=n, expected=round(expected, 4),
                        observed=round(observed, 4), gap=round(observed - expected, 4),
                        avg_clv=round(sum(clvs) / len(clvs), 3) if clvs else None,
                        sufficient=n >= MIN_BUCKET_N))
    out.sort(key=lambda r: r["bucket"])
    return out


def recompute(model_version: Optional[str] = None) -> List[Dict]:
    """Derive confidence deltas per sport/market scope and store them.

    A scope is only adjusted with >= MIN_BUCKET_N graded results, and the delta
    is clamped. Deliberately conservative: this exists to catch a systematically
    over-confident model, not to curve-fit the past.
    """
    rows = _joined()
    graded = [r for r in rows if r["outcome"] in ("win", "loss")]
    now = time.time()
    written: List[Dict] = []
    scopes: Dict[str, List[Dict]] = {}
    for r in graded:
        scopes.setdefault(r["sport"], []).append(r)
        scopes.setdefault(f"{r['sport']}:{r['market']}", []).append(r)
    for scope, rs in scopes.items():
        n = len(rs)
        expected = sum(float(r["model_prob"] or 0) for r in rs) / n
        observed = sum(1 for r in rs if r["won"]) / n
        gap = observed - expected           # negative = over-confident
        delta = 0.0
        if n >= MIN_BUCKET_N:
            # 1 probability point of miscalibration -> ~0.6 confidence points
            delta = max(MAX_DOWN, min(MAX_UP, gap * 100 * 0.6))
        db.ex("INSERT INTO calibration(scope,bucket,n,hit_rate,expected,delta,model_version,"
              "computed_at) VALUES(?,?,?,?,?,?,?,?)",
              (scope, "all", n, round(observed, 4), round(expected, 4), round(delta, 3),
               model_version or settings.model_version, now))
        written.append(dict(scope=scope, n=n, observed=round(observed, 4),
                            expected=round(expected, 4), delta=round(delta, 3),
                            applied=n >= MIN_BUCKET_N))
    return written
