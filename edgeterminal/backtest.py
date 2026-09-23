"""Deterministic evaluation of frozen NFL prediction rows and walk-forward replay.

Two jobs live here:

1. `evaluate_rows` scores already-settled prediction rows (grading + scoring).
2. `replay` re-runs the *production* projection model over historical games with
   strictly point-in-time features and compares it with the closing market line.

Neither job tunes production thresholds: the backtest measures the model, it
does not fit it.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import httpx

from .analytics.football import project_game
from .analytics.ratings import shrink_stats
from .nfl_baselines import baselines_before
from .nfl_market import nflverse_to_home_spread
from .pricing import american_to_decimal
from .pricing import norm_cdf
from .providers.normalize import normalize_team

SCHEDULES_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
                 "schedules/games.csv.gz")

PBP_COLUMNS = ("game_id", "season", "week", "season_type", "game_type", "posteam",
               "epa", "success", "qb_dropback", "rush_attempt")


def _bucket(confidence: float) -> str:
    c = int(float(confidence))
    if c >= 95:
        return "95+"
    lo = max(0, c // 5 * 5)
    return f"{lo}-{lo + 4}"


def evaluate_rows(rows: Iterable[Dict]) -> Dict:
    settled: List[Dict] = [dict(r) for r in rows if r.get("won") in (0, 1, False, True)]
    if not settled:
        return {"n": 0, "brier_score": None, "log_loss": None, "roi_pct": None,
                "avg_clv_pct": None, "calibration": []}
    brier = 0.0
    logloss = 0.0
    staked = profit = 0.0
    clv = []
    buckets: Dict[str, List[Dict]] = {}
    for row in settled:
        y = 1.0 if row["won"] else 0.0
        p = max(1e-6, min(1 - 1e-6, float(row["model_prob"])))
        brier += (p - y) ** 2
        logloss -= y * math.log(p) + (1 - y) * math.log(1 - p)
        units = float(row.get("units") or 1.0)
        staked += units
        profit += units * (american_to_decimal(row["price"]) - 1.0 if y else -1.0)
        if row.get("clv_pct") is not None:
            clv.append(float(row["clv_pct"]))
        buckets.setdefault(_bucket(row.get("confidence", p * 100)), []).append({"p": p, "y": y})
    calibration = []
    for bucket, vals in sorted(buckets.items()):
        calibration.append({"bucket": bucket, "n": len(vals),
                            "predicted": round(sum(v["p"] for v in vals) / len(vals), 6),
                            "observed": round(sum(v["y"] for v in vals) / len(vals), 6)})
    n = len(settled)
    return {"n": n, "brier_score": brier / n, "log_loss": logloss / n,
            "roi_pct": profit / staked * 100 if staked else None,
            "avg_clv_pct": sum(clv) / len(clv) if clv else None,
            "calibration": calibration}


# ------------------------------------------------------------------ walk-forward
def _num(value):
    try:
        return float(value) if value not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


def load_schedules(cache_dir: Path, refresh: bool = False, timeout: float = 90.0) -> List[Dict]:
    """Fetch the CC-BY nflverse schedule once; keep source/hash provenance locally."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / "games.csv.gz"
    metadata = cache_dir / "games.metadata.json"
    if refresh or not archive.exists():
        response = httpx.get(SCHEDULES_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        archive.write_bytes(response.content)
        metadata.write_text(json.dumps({"source_url": SCHEDULES_URL, "retrieved_at": time.time(),
                                        "sha256": hashlib.sha256(response.content).hexdigest(),
                                        "license": "CC-BY-4.0"}, indent=2))
    with gzip.open(archive, "rt", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def regular_season_games(games: Iterable[Dict], season: int) -> List[Dict]:
    out = []
    for g in games:
        if _num(g.get("season")) != float(season):
            continue
        if str(g.get("game_type") or "REG").upper() not in ("REG", "REGULAR"):
            continue
        out.append(dict(g))
    return out


def team_features(plays: Iterable[Dict], games: Iterable[Dict], season: int,
                  upto_week: int) -> Dict[str, Dict]:
    """Point-in-time team rates: only regular-season games strictly before `upto_week`."""
    pooled = regular_season_games(games, season)
    eligible = {}
    for g in pooled:
        week = _num(g.get("week"))
        if week is None or week >= float(upto_week):
            continue
        eligible[str(g.get("game_id"))] = g

    scored = defaultdict(list)
    against = defaultdict(list)
    for g in eligible.values():
        home = normalize_team(None, g.get("home_team"))
        away = normalize_team(None, g.get("away_team"))
        hs, as_ = _num(g.get("home_score")), _num(g.get("away_score"))
        if hs is None or as_ is None:
            continue
        if home:
            scored[home].append(hs); against[home].append(as_)
        if away:
            scored[away].append(as_); against[away].append(hs)

    epa = defaultdict(list)
    success = defaultdict(list)
    for row in plays:
        if _num(row.get("season")) != float(season):
            continue
        if str(row.get("season_type") or "REG").upper() != "REG":
            continue
        if str(row.get("game_id")) not in eligible:
            continue
        team = normalize_team(None, row.get("posteam"))
        if not team:
            continue
        e, s = _num(row.get("epa")), _num(row.get("success"))
        if e is not None:
            epa[team].append(e)
        if s is not None:
            success[team].append(s)

    out: Dict[str, Dict] = {}
    for team, pf in scored.items():
        out[team] = dict(
            points_for=round(sum(pf) / len(pf), 4),
            points_against=round(sum(against[team]) / len(against[team]), 4),
            epa_play=round(sum(epa[team]) / len(epa[team]), 6) if epa[team] else 0.0,
            success_rate=round(sum(success[team]) / len(success[team]), 6) if success[team] else 0.45,
            games=len(pf), season=int(season), source="nflverse-pbp+schedules")
    return out


def _has(features: Dict[str, Dict], home: str, away: str) -> bool:
    return bool(features.get(home) and features.get(away))


def _grade_ats(pick: str, actual_margin: float, spread_line: float) -> Optional[str]:
    # nflverse `spread_line` is the HOME team's handicap and a POSITIVE number
    # means the home team was the favourite (verified against 2025 results:
    # corr(spread_line, actual_margin) = +0.50). Home covers when its margin
    # exceeds that handicap.
    cover = actual_margin - spread_line
    if abs(cover) < 1e-9:
        return "push"
    home_covered = cover > 0
    return "win" if (pick == "home") == home_covered else "loss"


def _grade_total(pick: str, actual_total: float, total_line: float) -> Optional[str]:
    if abs(actual_total - total_line) < 1e-9:
        return "push"
    return "win" if (pick == "over") == (actual_total > total_line) else "loss"


def replay(plays: Iterable[Dict], games: Iterable[Dict], season: int,
           weeks: Optional[Iterable[int]] = None, prior_season: Optional[int] = None,
           sport: str = "NFL") -> List[Dict]:
    """Project every graded game in `season` using only information available then."""
    if prior_season is not None and prior_season >= season:
        raise ValueError("prior_season must end before the evaluated season")
    plays = list(plays)
    games = list(games)
    wanted = None if weeks is None else {int(w) for w in weeks}
    targets = [g for g in regular_season_games(games, season)
               if wanted is None or int(_num(g.get("week")) or 0) in wanted]
    rows: List[Dict] = []
    # Every game in a week has the same strict prior-week cutoff. Reusing the
    # frozen feature set also prevents a later game in that week from seeing an
    # earlier final score simply because replay iteration order changed.
    feature_cache: Dict[tuple[int, int], Dict[str, Dict]] = {}

    def features_before(feature_season: int, cutoff_week: int) -> Dict[str, Dict]:
        key = (feature_season, cutoff_week)
        if key not in feature_cache:
            feature_cache[key] = team_features(plays, games, feature_season, cutoff_week)
        return feature_cache[key]

    for game in sorted(targets, key=lambda g: (int(_num(g.get("week")) or 0), str(g.get("game_id")))):
        week = int(_num(game.get("week")) or 0)
        home = normalize_team(None, game.get("home_team"))
        away = normalize_team(None, game.get("away_team"))
        if not home or not away:
            continue
        hs, as_ = _num(game.get("home_score")), _num(game.get("away_score"))
        if hs is None or as_ is None:
            continue
        features = features_before(season, week)
        basis = "current_season"
        if not _has(features, home, away) and prior_season:
            features = features_before(prior_season, max(week, 19))
            basis = "prior_season"
        if not _has(features, home, away):
            continue
        # Live NFL projections shrink sparse team scoring rates. Replay must
        # apply the same transform, with only prior-week inputs.
        model_features = shrink_stats(features, sport)[0]
        neutral = str(game.get("location") or "").lower() == "neutral"
        proj = project_game(sport, home, away, model_features, neutral=neutral)
        baselines = baselines_before(games, season, week, home, away, neutral=neutral)
        margin = hs - as_
        total = hs + as_
        spread_line = _num(game.get("spread_line"))
        total_line = _num(game.get("total_line"))
        row = dict(
            season=int(season), week=week, game_id=str(game.get("game_id")),
            home=home, away=away, basis=basis, neutral=neutral,
            proj_margin=proj["margin"], proj_total=proj["total"],
            actual_margin=margin, actual_total=total, home_win_prob=proj["home_win_prob"],
            home_won=None if margin == 0 else margin > 0,
            spread_line=spread_line,
            home_spread=nflverse_to_home_spread(spread_line) if spread_line is not None else None,
            total_line=total_line,
            sample_size_n=proj["sample_size_n"], uncertainty=proj["uncertainty"],
            baselines=baselines,
        )
        if spread_line is not None:
            row["pick"] = "home" if proj["margin"] - spread_line > 0 else "away"
            row["ats"] = _grade_ats(row["pick"], margin, spread_line)
            row["spread_price"] = _num(game.get(
                "home_spread_odds" if row["pick"] == "home" else "away_spread_odds"))
        if total_line is not None:
            row["total_pick"] = "over" if proj["total"] > total_line else "under"
            row["total_result"] = _grade_total(row["total_pick"], total, total_line)
            row["total_price"] = _num(game.get(
                "over_odds" if row["total_pick"] == "over" else "under_odds"))
        rows.append(row)
    return rows


def _rate(rows: List[Dict], key: str) -> Optional[float]:
    graded = [r for r in rows if r.get(key) in ("win", "loss")]
    if not graded:
        return None
    return round(sum(1 for r in graded if r[key] == "win") / len(graded), 4)


def _wilson(wins: int, n: int) -> Optional[List[float]]:
    """95% Wilson interval for a binary rate (pushes are excluded)."""
    if not n:
        return None
    z = 1.959963984540054
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def _paired_mae_delta(pairs: List[tuple[float, float]]) -> Dict:
    """Model minus baseline absolute error; deterministic paired bootstrap CI."""
    if not pairs:
        return {"n": 0, "delta": None, "ci95": None}
    diffs = [model_error - baseline_error for model_error, baseline_error in pairs]
    n = len(diffs)
    rng = random.Random(20260923)
    samples = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n
                     for _ in range(2000))
    return {"n": n, "delta": round(sum(diffs) / n, 4),
            "ci95": [round(samples[49], 4), round(samples[1949], 4)]}


def _probability_score(rows: List[Dict], probability) -> Dict:
    outcomes = [(max(1e-6, min(1 - 1e-6, probability(r))),
                 1.0 if r["home_won"] else 0.0)
                for r in rows if r.get("home_won") is not None and probability(r) is not None]
    if not outcomes:
        return {"n": 0, "brier_score": None, "log_loss": None}
    return {"n": len(outcomes),
            "brier_score": round(sum((p - y) ** 2 for p, y in outcomes) / len(outcomes), 6),
            "log_loss": round(sum(-(y * math.log(p) + (1-y) * math.log(1-p))
                                  for p, y in outcomes) / len(outcomes), 6)}


def _priced_record(rows: List[Dict], outcome_key: str, price_key: str) -> Dict:
    priced = [(r[outcome_key], _num(r.get(price_key))) for r in rows
              if r.get(outcome_key) in {"win", "loss", "push"}]
    priced = [(outcome, price) for outcome, price in priced
              if price is not None and (price <= -100 or price >= 100)]
    profit = sum(american_to_decimal(price) - 1 if outcome == "win" else
                 0 if outcome == "push" else -1 for outcome, price in priced)
    return {"n": len(priced), "roi_pct": round(100 * profit / len(priced), 2)
            if priced else None, "price_timing_verified": False}


def _edge_bucket(edge: float) -> str:
    value = abs(edge)
    if value < 1:
        return "0-1"
    if value < 2:
        return "1-2"
    if value < 3:
        return "2-3"
    if value < 4:
        return "3-4"
    if value < 6:
        return "4-6"
    return "6+"


def _edge_buckets(rows: List[Dict], prediction: str, market: str,
                  result: str) -> List[Dict]:
    order = ("0-1", "1-2", "2-3", "3-4", "4-6", "6+")
    groups = defaultdict(list)
    for row in rows:
        if row.get(market) is not None and row.get(result) in {"win", "loss", "push"}:
            groups[_edge_bucket(row[prediction] - row[market])].append(row[result])
    return [dict(bucket=b, n=len(groups[b]), wins=groups[b].count("win"),
                 losses=groups[b].count("loss"), pushes=groups[b].count("push"),
                 hit_rate=round(groups[b].count("win") /
                                (groups[b].count("win") + groups[b].count("loss")), 4)
                 if groups[b].count("win") + groups[b].count("loss") else None,
                 hit_rate_ci95=_wilson(groups[b].count("win"),
                                       groups[b].count("win") + groups[b].count("loss")))
            for b in order]


def _win_calibration(rows: List[Dict]) -> List[Dict]:
    groups = defaultdict(list)
    for row in rows:
        if row.get("home_won") is None:
            continue
        p = max(float(row["home_win_prob"]), 1.0 - float(row["home_win_prob"]))
        bucket = ("50-55" if p < .55 else "55-60" if p < .60 else
                  "60-65" if p < .65 else "65-70" if p < .70 else "70+")
        favored_home = row["home_win_prob"] >= .5
        groups[bucket].append((p, float(bool(row["home_won"]) == favored_home)))
    return [dict(bucket=b, n=len(groups[b]),
                 predicted=round(sum(p for p, _ in groups[b]) / len(groups[b]), 4)
                 if groups[b] else None,
                 observed=round(sum(y for _, y in groups[b]) / len(groups[b]), 4)
                 if groups[b] else None,
                 observed_ci95=_wilson(int(sum(y for _, y in groups[b])), len(groups[b])))
            for b in ("50-55", "55-60", "60-65", "65-70", "70+")]


def summarize(rows: Iterable[Dict]) -> Dict:
    rows = [dict(r) for r in rows]
    n = len(rows)
    if not n:
        return {"n": 0}
    margin_err = [abs(r["proj_margin"] - r["actual_margin"]) for r in rows]
    total_err = [abs(r["proj_total"] - r["actual_total"]) for r in rows]
    bias = [r["proj_margin"] - r["actual_margin"] for r in rows]
    win_rows = [r for r in rows if r.get("home_won") is not None]
    model_probability = _probability_score(rows, lambda r: r["home_win_prob"])
    correct = sum(1 for r in win_rows if (r["home_win_prob"] > 0.5) == bool(r["home_won"]))
    bases = {r.get("basis") for r in rows if r.get("basis")}
    comparisons = {}
    for name in ("home_league", "elo", "rolling", "market_close"):
        if name == "market_close":
            margin_pairs = [(r["spread_line"], r["actual_margin"], r["proj_margin"]) for r in rows
                            if r.get("spread_line") is not None]
            total_pairs = [(r["total_line"], r["actual_total"], r["proj_total"]) for r in rows
                           if r.get("total_line") is not None]
        else:
            margin_pairs = [(r["baselines"][name]["margin"], r["actual_margin"], r["proj_margin"])
                            for r in rows if name in r.get("baselines", {})]
            total_pairs = [(r["baselines"][name]["total"], r["actual_total"], r["proj_total"])
                           for r in rows if name in r.get("baselines", {})]
        comparisons[name] = dict(
            margin_n=len(margin_pairs), total_n=len(total_pairs),
            margin_mae=round(sum(abs(p - a) for p, a, _ in margin_pairs) / len(margin_pairs), 4)
                       if margin_pairs else None,
            margin_rmse=round(math.sqrt(sum((p - a) ** 2 for p, a, _ in margin_pairs) /
                                        len(margin_pairs)), 4) if margin_pairs else None,
            total_mae=round(sum(abs(p - a) for p, a, _ in total_pairs) / len(total_pairs), 4)
                      if total_pairs else None,
            total_rmse=round(math.sqrt(sum((p - a) ** 2 for p, a, _ in total_pairs) /
                                       len(total_pairs)), 4) if total_pairs else None,
            margin_mae_delta_model_minus_baseline=_paired_mae_delta(
                [(abs(model - actual), abs(base - actual))
                 for base, actual, model in margin_pairs]),
            total_mae_delta_model_minus_baseline=_paired_mae_delta(
                [(abs(model - actual), abs(base - actual))
                 for base, actual, model in total_pairs]),
        )
    comparisons["elo"]["win_probability"] = _probability_score(
        rows, lambda r: r.get("baselines", {}).get("elo", {}).get("home_win_prob"))
    comparisons["market_close"]["win_probability"] = _probability_score(
        rows, lambda r: norm_cdf(r["spread_line"] / 13.2)
        if r.get("spread_line") is not None else None)
    return dict(
        n=n,
        margin_mae=round(sum(margin_err) / n, 4),
        total_mae=round(sum(total_err) / n, 4),
        margin_rmse=round(math.sqrt(sum(e ** 2 for e in margin_err) / n), 4),
        total_rmse=round(math.sqrt(sum(e ** 2 for e in total_err) / n), 4),
        margin_bias=round(sum(bias) / n, 4),
        brier_score=model_probability["brier_score"],
        log_loss=model_probability["log_loss"],
        win_prob_n=model_probability["n"],
        ties_excluded_from_win_probability=n-model_probability["n"],
        win_calibration=_win_calibration(rows),
        spread_edge_buckets=_edge_buckets(rows, "proj_margin", "spread_line", "ats"),
        total_edge_buckets=_edge_buckets(rows, "proj_total", "total_line", "total_result"),
        home_win_accuracy=round(correct / len(win_rows), 4) if win_rows else None,
        ats_n=len([r for r in rows if r.get("ats") in ("win", "loss")]),
        ats_hit_rate=_rate(rows, "ats"),
        ats_hit_rate_ci95=_wilson(sum(r.get("ats") == "win" for r in rows),
                                  sum(r.get("ats") in {"win", "loss"} for r in rows)),
        total_pick_hit_rate=_rate(rows, "total_result"),
        total_pick_hit_rate_ci95=_wilson(
            sum(r.get("total_result") == "win" for r in rows),
            sum(r.get("total_result") in {"win", "loss"} for r in rows)),
        benchmark_breakeven_at_minus_110=round(110/210, 6),
        spread_closing_price_record=_priced_record(rows, "ats", "spread_price"),
        total_closing_price_record=_priced_record(rows, "total_result", "total_price"),
        basis=dict(sorted({b: sum(1 for r in rows if r.get("basis") == b)
                           for b in bases}.items())),
        baselines=comparisons,
    )


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    from .config import settings
    from .logs import install_redaction

    install_redaction()
    parser = argparse.ArgumentParser(description="Walk-forward NFL backtest (read-only).")
    parser.add_argument("--seasons", default="2024,2025")
    parser.add_argument("--prior-season", type=int, default=None)
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cache = Path(args.cache_dir or settings.nflverse_cache_dir)
    schedules = load_schedules(cache, refresh=args.refresh)
    seasons = [int(s) for s in str(args.seasons).split(",") if s.strip()]
    report = {}
    for season in seasons:
        from .providers.nflverse import pbp_rows
        plays = list(pbp_rows(season, cache, refresh=args.refresh, columns=PBP_COLUMNS))
        if args.prior_season is not None:
            if args.prior_season >= season:
                parser.error("--prior-season must be earlier than every evaluated season")
            plays += pbp_rows(args.prior_season, cache, refresh=args.refresh,
                             columns=PBP_COLUMNS)
        rows = replay(plays, schedules, season, prior_season=args.prior_season)
        report[str(season)] = summarize(rows)
    print(json.dumps(report, indent=2) if args.json else report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
