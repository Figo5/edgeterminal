#!/usr/bin/env python3
"""Read-only diagnostics of actual saved NBA spread comparisons through Oct 28.

No network, production imports, model fitting, revised picks, or database writes.
Only market_diagnostics.json beside this script is written.
"""
from collections import Counter
import json
from pathlib import Path
import sqlite3
from statistics import mean, median


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DAY = "2026-10-28"


def metrics(rows):
    return {
        "n": len(rows),
        "record": dict(Counter(r["result"] for r in rows)),
        "margin_mae": mean(r["pred_abs_error"] for r in rows),
        "market_margin_mae": mean(r["market_abs_error"] for r in rows),
        "winner_accuracy": mean(r["winner_correct"] for r in rows),
        "average_age_h": mean(r["forecast_age_h"] for r in rows),
        "median_absolute_market_disagreement": median(abs(r["edge_home"]) for r in rows),
        "home_picks": sum(r["selected_side"] == "home" for r in rows),
        "home_margin_bias": mean(r["predicted_home_margin"] - r["actual_home_margin"] for r in rows),
    }


def run():
    con = sqlite3.connect((ROOT / "data/edgeterminal.db").as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    rows = []
    for game in con.execute("SELECT * FROM nba_games WHERE status='final' "
                            "AND season_start_year=2026 AND game_date_et<=? ORDER BY game_date_et,game_id", (DAY,)):
        forecasts = list(con.execute("SELECT * FROM nba_forecasts WHERE game_id=? ORDER BY created_at,forecast_id", (game["game_id"],)))
        by_kind = {}
        for record in forecasts:
            payload = json.loads(record["payload"])
            by_kind.setdefault(payload.get("kind"), (record, payload))
        if "game" not in by_kind:
            continue
        saved, payload = by_kind["game"]
        market = payload.get("market") or {}
        if market.get("home_spread") is None:
            market = by_kind.get("spread-line", (None, {}))[1].get("market") or {}
        line = market.get("home_spread")
        if line is None:
            continue
        assert saved["created_at"] < game["tipoff_ts"]
        projection = payload["projection"]
        pred = projection["home_margin"]
        actual = game["home_score"] - game["away_score"]
        edge = pred + line
        side = "home" if edge >= 0 else "away"
        cushion = (actual + line) * (1 if side == "home" else -1)
        result = "won" if cushion > 0 else "lost" if cushion < 0 else "push"
        grade = con.execute("SELECT payload FROM nba_game_grade_revisions WHERE game_id=? ORDER BY id DESC LIMIT 1", (game["game_id"],)).fetchone()
        assert grade is not None and json.loads(grade[0])["spread"] == result
        rows.append({
            "game_id": game["game_id"], "day": game["game_date_et"],
            "home": game["home_team"], "away": game["away_team"],
            "predicted_home_margin": pred, "actual_home_margin": actual,
            "line": line, "market_expected_margin": -line, "edge_home": edge,
            "selected_side": side, "selected_team": game[side + "_team"],
            "selected_line": line if side == "home" else -line,
            "cover_margin": cushion, "result": result, "stored_result": result,
            "winner_correct": (pred > 0) == (actual > 0),
            "forecast_age_h": (game["tipoff_ts"] - saved["created_at"]) / 3600,
            "forecast_at": saved["created_at"], "tipoff_at": game["tipoff_ts"],
            "pred_abs_error": abs(pred - actual), "market_abs_error": abs(-line - actual),
        })
    con.close()
    last = [r for r in rows if r["day"] == DAY]
    prior = [r for r in rows if r["day"] < DAY]
    assert len(last) == 12
    buckets = {
        "0to2": [r for r in prior if abs(r["edge_home"]) < 2],
        "2to5": [r for r in prior if 2 <= abs(r["edge_home"]) < 5],
        "5plus": [r for r in prior if abs(r["edge_home"]) >= 5],
    }
    report = {
        "scope": "Actual immutable saved comparisons through 2026-10-28; no retuning or hypothetical lines",
        "last_night": metrics(last), "prior_days": metrics(prior), "all_days": metrics(rows),
        "daily": {day: metrics([r for r in rows if r["day"] == day]) for day in sorted({r["day"] for r in rows})},
        "oct28_games": last,
        "prior_diagnostic_edge_buckets": {name: metrics(values) for name, values in buckets.items()},
    }
    (HERE / "market_diagnostics.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: report[k] for k in ("last_night", "prior_days", "all_days")}))


if __name__ == "__main__":
    run()
