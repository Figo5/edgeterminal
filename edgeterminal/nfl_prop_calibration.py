"""Walk-forward uncertainty research on fixed NFL prop point-model families.

Calibration uses deterministic synthetic stat thresholds. It does not treat
these thresholds as sportsbook lines, infer a market edge, or rewrite a freeze.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import nfl_prop_lab

FIXED_THRESHOLDS = {
    "passing_yards": (149.5, 199.5, 249.5, 299.5),
    "rushing_yards": (24.5, 49.5, 74.5, 99.5),
    "rushing_attempts": (4.5, 9.5, 14.5, 19.5),
    "receptions": (1.5, 3.5, 5.5, 7.5),
    "receiving_yards": (24.5, 49.5, 74.5, 99.5),
}
METHODS = ("global_normal", "position_normal", "player_rolling_normal", "empirical_quantile")
MIN_PRIOR = 30
MIN_POSITION = 80
MIN_PLAYER = 5
BIAS_BOOTSTRAP_SEED = 23


def synthetic_threshold(market: str, game_id: str, player_id: str) -> float:
    """One half-point threshold per player-game, assigned without outcomes."""
    choices = FIXED_THRESHOLDS[market]
    digest = hashlib.sha256(f"{market}|{game_id}|{player_id}".encode()).digest()
    return choices[int.from_bytes(digest[:4], "big") % len(choices)]


def _sigma(values: list[float]) -> float:
    return max(float(np.std(values, ddof=1)), .25) if len(values) >= 2 else .25


def _normal_over(prediction: float, line: float, sigma: float) -> float:
    return min(max(.5 * math.erfc((line - prediction) / (sigma * math.sqrt(2))), 1e-6), 1 - 1e-6)


def predicted_probabilities(prior: list[dict], row: dict) -> dict[str, float]:
    """All four methods use only residuals from completed prior weeks."""
    if len(prior) < MIN_PRIOR:
        raise ValueError("insufficient prior walk-forward residuals")
    residuals = [r["residual"] for r in prior]
    global_sigma = _sigma(residuals)
    position = [r["residual"] for r in prior if r["position"] == row["position"]]
    position_sigma = _sigma(position) if len(position) >= MIN_POSITION else global_sigma
    player = [r["residual"] for r in prior if r["player_id"] == row["player_id"]][-10:]
    if len(player) >= MIN_PLAYER:
        # Ten global pseudo-observations shrink noisy player sample variance.
        player_sigma = math.sqrt((len(player) * _sigma(player)**2 + 10 * global_sigma**2) / (len(player) + 10))
    else:
        player_sigma = global_sigma
    gap = row["threshold"] - row["prediction"]
    empirical = (sum(residual > gap for residual in residuals) + .5) / (len(residuals) + 1)
    return {"global_normal": _normal_over(row["prediction"], row["threshold"], global_sigma),
            "position_normal": _normal_over(row["prediction"], row["threshold"], position_sigma),
            "player_rolling_normal": _normal_over(row["prediction"], row["threshold"], player_sigma),
            "empirical_quantile": min(max(empirical, 1e-6), 1-1e-6)}


def reliability(pairs: list[tuple[float, int]]) -> list[dict]:
    table = []
    for index in range(10):
        group = [(p, y) for p, y in pairs if min(int(p * 10), 9) == index]
        table.append({"bucket": f"{index/10:.1f}-{(index+1)/10:.1f}",
                      "n": len(group),
                      "mean_predicted": sum(p for p, _ in group) / len(group) if group else None,
                      "realized_over": sum(y for _, y in group) / len(group) if group else None})
    return table


def _scoring(pairs: list[tuple[float, int]]) -> dict:
    if not pairs:
        return {"n": 0, "brier": None, "log_loss": None, "reliability": reliability([])}
    return {"n": len(pairs),
            "brier": sum((p-y)**2 for p, y in pairs) / len(pairs),
            "log_loss": -sum(y * math.log(max(p,1e-6)) + (1-y) * math.log(max(1-p,1e-6)) for p,y in pairs) / len(pairs),
            "reliability": reliability(pairs)}


def choose_method(development: dict, holdout: dict | None = None) -> str:
    """The holdout argument is deliberately ignored during selection."""
    return min(METHODS, key=lambda method: (development[method]["brier"],
                                             development[method]["log_loss"]))


def _walk_forward(rows: list[dict], family: str, market: str) -> list[dict]:
    out = []
    prior_residuals = []
    for season in (2023, 2024, 2025):
        for week in sorted({r["week"] for r in rows if r["season"] == season}):
            train = [r for r in rows if (r["season"], r["week"]) < (season, week)]
            test = [r for r in rows if (r["season"], r["week"]) == (season, week)]
            if len(train) < 100 or not test:
                continue
            if family == "rolling":
                predictions = np.array([r["features"][f"{nfl_prop_lab.LABEL[market]}_last3"] for r in test])
            else:
                x_train, y_train = nfl_prop_lab._matrix(train)
                x_test, _ = nfl_prop_lab._matrix(test)
                predictions = np.maximum(nfl_prop_lab._estimator(family).fit(x_train, y_train).predict(x_test), 0)
            staged = []
            for r, prediction in zip(test, predictions):
                threshold = synthetic_threshold(market, r["game_id"], r["player_id"])
                entry = {"season": season, "week": week, "game_id": r["game_id"],
                         "player_id": r["player_id"], "position": r["position"],
                         "prediction": float(prediction), "actual": r["target"],
                         "threshold": threshold, "outcome_over": int(r["target"] > threshold),
                         "residual": r["target"] - float(prediction)}
                if len(prior_residuals) >= MIN_PRIOR:
                    entry["probabilities"] = predicted_probabilities(prior_residuals, entry)
                else:
                    entry["probabilities"] = None
                staged.append(entry)
            # The week's actuals become available only after every row in it was predicted.
            out.extend(staged)
            prior_residuals.extend({key: entry[key] for key in ("residual", "position", "player_id")}
                                   for entry in staged)
    return out


def _bias_ci(rows: list[dict]) -> list[float] | None:
    if not rows:
        return None
    by_game = defaultdict(lambda: [0.0, 0])
    for row in rows:
        item = by_game[row["game_id"]]
        item[0] += row["prediction"] - row["actual"]
        item[1] += 1
    groups = np.array(list(by_game.values()), dtype=float)
    rng = np.random.default_rng(BIAS_BOOTSTRAP_SEED)
    indices = rng.integers(0, len(groups), size=(1000, len(groups)))
    sampled = groups[indices]
    means = sampled[:,:,0].sum(axis=1) / sampled[:,:,1].sum(axis=1)
    return [float(np.quantile(means, .025)), float(np.quantile(means, .975))]


def analyze(datasets: dict[str, list[dict]], original_validation: dict) -> dict:
    """2023 residual warmup, 2024 method choice, 2025 untouched evaluation."""
    result = {}
    for market in nfl_prop_lab.MARKETS:
        family = original_validation[market]["selected_on_2024"]
        predictions = _walk_forward(datasets[market], family, market)
        for season, split in ((2024, "development_2024"), (2025, "holdout_2025")):
            rows = [r for r in predictions if r["season"] == season]
            replay_mae = sum(abs(r["prediction"]-r["actual"]) for r in rows) / len(rows)
            expected = original_validation[market]["metrics"][f"{split}_{family}"]["mae"]
            if abs(replay_mae - expected) > 1e-7:
                raise ValueError(f"point-model replay changed for {market} {season}")
        split_results = {}
        for season, split in ((2024, "development_2024"), (2025, "holdout_2025")):
            rows = [r for r in predictions if r["season"] == season and r["probabilities"]]
            methods = {method: _scoring([(r["probabilities"][method], r["outcome_over"]) for r in rows])
                       for method in METHODS}
            split_results[split] = {"methods": methods, "point_bias": sum(r["prediction"]-r["actual"] for r in rows) / len(rows),
                                    "point_bias_game_cluster_ci95": _bias_ci(rows)}
        chosen = choose_method(split_results["development_2024"]["methods"],
                               split_results["holdout_2025"]["methods"])
        holdout_rows = [r for r in predictions if r["season"] == 2025]
        frozen_sigma = original_validation[market]["uncertainty_sigma_from_2024"]
        frozen_score = _scoring([(_normal_over(r["prediction"],r["threshold"],frozen_sigma),r["outcome_over"])
                                 for r in holdout_rows])
        result[market] = {"fixed_point_family": family,
                          "selected_uncertainty_on_2024": chosen,
                          "development_2024": split_results["development_2024"],
                          "holdout_2025": split_results["holdout_2025"],
                          "frozen_week3_global_normal_on_2025": frozen_score}
    return {"status": "RETROSPECTIVE_SYNTHETIC_THRESHOLDS_ONLY",
            "methodology": {"point_family_source": "2024 development MAE in frozen 2026 Week 3 lab report",
                            "residual_warmup": "2023 walk-forward predictions; each week's outcomes enter after all that week's predictions",
                            "uncertainty_selection": "lowest 2024 development Brier, log loss tie-break",
                            "final_holdout": "2025 chronological walk-forward; never used for selection",
                            "threshold_assignment": "one predeclared half-point threshold per player-game selected by SHA-256 of market/game/player",
                            "synthetic_thresholds": FIXED_THRESHOLDS,
                            "source_note": "Synthetic thresholds are not sportsbook lines. No historical price or betting-performance claim.",
                            "residual_methods": METHODS},
            "markets": result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline research-only NFL prop probability calibration")
    parser.add_argument("--original-report", default="data/research/nfl_prop_lab_2026_week3.json")
    parser.add_argument("--output", default="data/research/nfl_prop_calibration_2026w3.json")
    args = parser.parse_args()
    original = json.loads(Path(args.original_report).read_text())
    datasets, _, hashes = nfl_prop_lab.build_dataset()
    if hashes != original["dataset_sha256"]:
        raise SystemExit("player-stat input bytes changed from frozen Week 3 report")
    report = analyze(datasets, original["validation"])
    report["input_sha256"] = hashes
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output),
                      "selected": {market: row["selected_uncertainty_on_2024"]
                                   for market, row in report["markets"].items()}}, indent=2))


if __name__ == "__main__":
    main()
