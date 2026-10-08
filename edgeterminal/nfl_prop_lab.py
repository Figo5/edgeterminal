"""Point-in-time, research-only NFL player prop projections.

Weekly nflverse stats are labels. Every feature is calculated before updating the
state with that week's results. Archived sportsbook quotes are joined only after
model fitting and never enter a feature or model-selection decision.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from . import db, nfl_identity, nfl_props

MARKETS = ("passing_yards", "rushing_yards", "rushing_attempts", "receptions", "receiving_yards")
LABEL = {"passing_yards": "passing_yards", "rushing_yards": "rushing_yards",
         "rushing_attempts": "carries", "receptions": "receptions",
         "receiving_yards": "receiving_yards"}
USAGE = ("targets", "receptions", "receiving_yards", "carries", "rushing_yards",
         "attempts", "passing_yards")
ROOT = Path(__file__).resolve().parents[1]
STATS = ROOT / "data/nflverse/player_stats"
SCHEDULE = ROOT / "data/nflverse/pbp/games.csv.gz"
VERSION = "prop-lab-v1"
FEATURE_VERSION = "player-prior3-prior5-team-opp-v1"
FEATURES = tuple([f"{key}_last3" for key in USAGE] +
                 [f"{key}_last5" for key in USAGE] +
                 ["prior_games", "target_share_last3", "carry_share_last3",
                  "team_pass_attempts_last3", "team_carries_last3",
                  "opponent_allowed_last3", "home", "rest_days", "days_since_played",
                  "week", "position_qb", "position_rb", "position_wr", "position_te"])


def _num(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _team(code: str) -> str:
    return {"LA": "lar", "JAX": "jax", "WAS": "wsh"}.get(code, code.lower())


def _norm_name(name: str) -> str:
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name.lower())
    return re.sub(r"[^a-z0-9]", "", name)


def _schedule() -> dict[str, dict]:
    with gzip.open(SCHEDULE, "rt", newline="") as stream:
        return {r["game_id"]: r for r in csv.DictReader(stream)
                if 2022 <= int(r["season"]) <= 2026 and r["game_type"] == "REG"}


def _read_stats(stats_dir: Path | None = None) -> tuple[list[dict], dict[str, str]]:
    rows, hashes = [], {}
    for year in range(2022, 2027):
        path = (stats_dir if stats_dir is not None and year == 2026 else STATS) / f"stats_player_week_{year}.csv"
        body = path.read_bytes()
        hashes[path.name] = hashlib.sha256(body).hexdigest()
        for row in csv.DictReader(body.decode("utf-8-sig").splitlines()):
            if row["season_type"] == "REG" and row["player_id"] and row["game_id"]:
                rows.append(row)
    return rows, hashes


def _avg(rows: list[dict], key: str, n: int = 3) -> float:
    values = [_num(r.get(key)) for r in rows[-n:]]
    return sum(values) / len(values) if values else 0.0


def _eligible(market: str, prior: list[dict]) -> bool:
    """Select a quoted-player-like cohort using earlier opportunity only."""
    if not prior:
        return False
    field, minimum = (("attempts", 10) if market == "passing_yards" else
                      ("carries", 1) if market in {"rushing_yards", "rushing_attempts"} else
                      ("targets", 1))
    return _avg(prior, field) >= minimum


def _feature(row: dict, history: dict, schedule: dict[str, dict]) -> dict | None:
    game = schedule.get(row["game_id"])
    if not game:
        return None
    player = history["player"].get(row["player_id"], [])
    team = _team(row["team"])
    opponent = _team(row["opponent_team"])
    team_hist = history["team"].get(team, [])
    opp_hist = history["defense"].get(opponent, [])
    position = row["position"]
    is_home = _team(game["home_team"]) == team
    rest = game["home_rest"] if is_home else game["away_rest"]
    date = datetime.fromisoformat(game["gameday"]).date()
    last_date = player[-1]["_date"] if player else None
    feature = {f"{key}_last{span}": _avg(player, key, span)
               for key in USAGE for span in (3, 5)}
    player_targets = sum(_num(x["targets"]) for x in player[-3:])
    player_carries = sum(_num(x["carries"]) for x in player[-3:])
    team_pass = sum(_num(x["attempts"]) for x in team_hist[-3:])
    team_carries = sum(_num(x["carries"]) for x in team_hist[-3:])
    feature.update(prior_games=len(player), target_share_last3=player_targets / team_pass if team_pass else 0,
                   carry_share_last3=player_carries / team_carries if team_carries else 0,
                   team_pass_attempts_last3=_avg(team_hist, "attempts"),
                   team_carries_last3=_avg(team_hist, "carries"),
                   opponent_allowed_last3=0, home=float(is_home),
                   rest_days=_num(rest, 7), days_since_played=min((date-last_date).days, 35) if last_date else 35,
                   week=int(row["week"]), position_qb=float(position == "QB"),
                   position_rb=float(position == "RB"), position_wr=float(position == "WR"),
                   position_te=float(position == "TE"))
    return {"feature": feature, "player": player, "opp_hist": opp_hist,
            "date": date, "team": team, "opponent": opponent, "game": game}


def build_dataset(cutoff: tuple[int, int] = (2026, 3), stats_dir: Path | None = None) -> tuple[dict[str, list[dict]], dict[str, dict], dict[str, str]]:
    """Generate every row from prior weeks only; return state after 2026 Week 2."""
    raw, hashes = _read_stats(stats_dir)
    schedule = _schedule()
    by_week: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for r in raw:
        if r["game_id"] in schedule and (int(r["season"]), int(r["week"])) < cutoff:
            by_week[(int(r["season"]), int(r["week"]))].append(r)
    history: dict[str, dict] = {"player": defaultdict(list), "team": defaultdict(list),
                               "defense": defaultdict(list)}
    datasets: dict[str, list[dict]] = {market: [] for market in MARKETS}
    for (season, week), rows in sorted(by_week.items()):
        staged = []
        for row in rows:
            prepared = _feature(row, history, schedule)
            if not prepared:
                continue
            staged.append((row, prepared))
            prior = prepared["player"]
            for market in MARKETS:
                if market == "passing_yards" and row["position"] != "QB":
                    continue
                if market != "passing_yards" and row["position"] not in {"QB", "RB", "WR", "TE"}:
                    continue
                if not _eligible(market, prior):
                    continue
                if market in {"receptions", "receiving_yards"} and row["position"] == "QB":
                    continue
                features = dict(prepared["feature"])
                features["opponent_allowed_last3"] = _avg(prepared["opp_hist"], LABEL[market])
                datasets[market].append({"season": season, "week": week, "game_id": row["game_id"],
                                         "player_id": row["player_id"], "player_name": row["player_display_name"],
                                         "team": prepared["team"], "position": row["position"],
                                         "target": _num(row[LABEL[market]]), "features": features})
        # Commit an entire week together, so Thursday/Sunday/Monday games never leak
        # into other games from the same scheduled week.
        team_games: dict[tuple[str, str], dict] = defaultdict(lambda: {key: 0.0 for key in USAGE})
        defense_games: dict[tuple[str, str], dict] = defaultdict(lambda: {key: 0.0 for key in USAGE})
        for row, prepared in staged:
            state = {key: _num(row[key]) for key in USAGE}
            state["_date"] = prepared["date"]
            history["player"][row["player_id"]].append(state)
            for key in USAGE:
                team_games[(prepared["team"], row["game_id"])][key] += state[key]
                defense_games[(prepared["opponent"], row["game_id"])][key] += state[key]
        for (team, _), state in team_games.items():
            history["team"][team].append(state)
        for (team, _), state in defense_games.items():
            history["defense"][team].append(state)
    return datasets, history, hashes


def _matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    return np.array([[r["features"][key] for key in FEATURES] for r in rows], dtype=float), np.array([r["target"] for r in rows])


def _estimator(family: str):
    if family == "ridge":
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=100.0))
    if family == "boosted":
        return HistGradientBoostingRegressor(max_iter=60, max_leaf_nodes=15,
                                             min_samples_leaf=40, l2_regularization=10,
                                             learning_rate=0.06, random_state=13)
    raise ValueError(family)


def evaluate(datasets: dict[str, list[dict]]) -> dict:
    """2024 development and 2025 untouched holdout; re-fit before each week."""
    result = {}
    for market, rows in datasets.items():
        estimates = {f"{split}_{family}": [] for split in ("development_2024", "holdout_2025")
                     for family in ("rolling", "ridge", "boosted")}
        for season in (2024, 2025):
            split = "development_2024" if season == 2024 else "holdout_2025"
            for week in sorted({r["week"] for r in rows if r["season"] == season}):
                train = [r for r in rows if (r["season"], r["week"]) < (season, week)]
                test = [r for r in rows if (r["season"], r["week"]) == (season, week)]
                if len(train) < 100 or not test:
                    continue
                y = np.array([r["target"] for r in test])
                rolling = np.array([r["features"][f"{LABEL[market]}_last3"] for r in test])
                estimates[f"{split}_rolling"].extend(zip(y.tolist(), rolling.tolist()))
                x_train, y_train = _matrix(train)
                x_test, _ = _matrix(test)
                for family in ("ridge", "boosted"):
                    model = _estimator(family).fit(x_train, y_train)
                    pred = np.maximum(model.predict(x_test), 0)
                    estimates[f"{split}_{family}"].extend(zip(y.tolist(), pred.tolist()))
        metrics = {}
        for key, pairs in estimates.items():
            errors = [pred - actual for actual, pred in pairs]
            metrics[key] = {"n": len(pairs), "mae": sum(abs(e) for e in errors) / len(errors),
                            "rmse": math.sqrt(sum(e*e for e in errors) / len(errors)),
                            "bias": sum(errors) / len(errors),
                            "residual_sigma": statistics.stdev(errors) if len(errors) > 1 else 0}
        winner = min(("rolling", "ridge", "boosted"), key=lambda f: metrics[f"development_2024_{f}"]["mae"])
        result[market] = {"metrics": metrics, "selected_on_2024": winner,
                          "uncertainty_sigma_from_2024": metrics[f"development_2024_{winner}"]["residual_sigma"],
                          "training_rows": len(rows)}
    return result


def _week3_features(history: dict[str, dict], season: int = 2026, week: int = 3,
                    stats_dir: Path | None = None) -> dict[tuple[str, str], dict]:
    schedule = _schedule()
    recent, _ = _read_stats(stats_dir)
    candidates: dict[tuple[str, str], dict] = {}
    for row in recent:
        if int(row["season"]) < season - 1 or (int(row["season"]), int(row["week"])) >= (season, week):
            continue
        if row["position"] not in {"QB", "RB", "WR", "TE"}:
            continue
        key = (row["player_id"], _team(row["team"]))
        if key not in candidates or (int(row["season"]), int(row["week"])) > (int(candidates[key]["season"]), int(candidates[key]["week"])):
            candidates[key] = row
    output = {}
    for game_id, game in schedule.items():
        if int(game["season"]) != season or int(game["week"]) != week:
            continue
        for row in candidates.values():
            team = _team(row["team"])
            if team not in {_team(game["home_team"]), _team(game["away_team"])}:
                continue
            opponent = _team(game["away_team"]) if team == _team(game["home_team"]) else _team(game["home_team"])
            virtual = dict(row, game_id=game_id, season=str(season), week=str(week), opponent_team=opponent)
            prepared = _feature(virtual, history, schedule)
            if prepared and prepared["player"]:
                output[(row["player_id"], team)] = prepared
    return output


def _pair_quotes(batch_id: str) -> tuple[list[dict], dict]:
    quotes = db.q("SELECT * FROM nfl_prop_snapshots WHERE batch_id=? AND market IN (?,?,?,?,?)",
                  (batch_id, *MARKETS))
    groups: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    counts = {"all_observed": len(quotes), "alternate_observed": 0, "invalid_main": 0,
              "one_sided_main": 0, "duplicate_main_sides": 0,
              "two_sided_main_groups": 0}
    for quote in quotes:
        if quote["line_type"] != "main":
            counts["alternate_observed"] += 1
            continue
        if quote["line"] is None or not math.isfinite(quote["line"]) or not quote["price"] or abs(quote["price"]) < 100:
            counts["invalid_main"] += 1
            continue
        key = (quote["canonical_game_id"], quote["player_id"], quote["market"],
               quote["sportsbook"], quote["line"])
        if quote["side"] in {"over", "under"}:
            groups[key][quote["side"]].append(quote)
    pairs = []
    for group in groups.values():
        if any(len(side) != 1 for side in group.values()):
            counts["duplicate_main_sides"] += 1
        elif set(group) == {"over", "under"}:
            pairs.extend(group["over"] + group["under"])
            counts["two_sided_main_groups"] += 1
        else:
            counts["one_sided_main"] += 1
    return pairs, counts


def _prob_over(line: float, projection: float, sigma: float) -> float:
    if sigma <= 0:
        return float(projection > line)
    return 0.5 * math.erfc((line - projection) / (sigma * math.sqrt(2)))


def project_week3(datasets: dict[str, list[dict]], history: dict[str, dict], validation: dict,
                  batch_id: str, *, season: int = 2026, week: int = 3,
                  stats_dir: Path | None = None) -> dict:
    pairs, coverage = _pair_quotes(batch_id)
    identities = db.q("SELECT DISTINCT player_id,player_name,team FROM nfl_prop_snapshots WHERE batch_id=?",
                      (batch_id,))
    by_name: dict[tuple[str, str], set[str]] = defaultdict(set)
    recent, _ = _read_stats(stats_dir)
    for r in recent:
        if int(r["season"]) >= season - 1 and (int(r["season"]), int(r["week"])) < (season, week):
            by_name[(_norm_name(r["player_display_name"]), _team(r["team"]))].add(r["player_id"])
    mapping, unmatched, ambiguous = {}, [], []
    for item in identities:
        team = nfl_identity.team_code(item["team"])
        matches = by_name.get((_norm_name(item["player_name"]), team), set())
        if len(matches) == 1:
            mapping[item["player_id"]] = next(iter(matches))
        elif len(matches) > 1:
            ambiguous.append(item["player_name"])
        else:
            unmatched.append(item["player_name"])
    week3 = _week3_features(history, season, week, stats_dir)
    fitted = {}
    for market in MARKETS:
        family = validation[market]["selected_on_2024"]
        if family != "rolling":
            rows = [r for r in datasets[market] if (r["season"], r["week"]) < (2026, 3)]
            x, y = _matrix(rows)
            fitted[market] = _estimator(family).fit(x, y)
    projections = []
    for quote in pairs:
        gsis = mapping.get(quote["player_id"])
        team = nfl_identity.team_code(quote["team"])
        prepared = week3.get((gsis, team))
        if not prepared or not _eligible(quote["market"], prepared["player"]):
            continue
        market = quote["market"]
        feature = dict(prepared["feature"])
        feature["opponent_allowed_last3"] = _avg(prepared["opp_hist"], LABEL[market])
        family = validation[market]["selected_on_2024"]
        if family == "rolling":
            point = feature[f"{LABEL[market]}_last3"]
        else:
            point = float(fitted[market].predict(np.array([[feature[k] for k in FEATURES]]))[0])
        point = max(0.0, point)
        sigma = validation[market]["uncertainty_sigma_from_2024"]
        over = _prob_over(quote["line"], point, sigma)
        projections.append({"quote_id": quote["id"], "batch_id": batch_id,
                            "canonical_game_id": quote["canonical_game_id"],
                            "player_id": quote["player_id"], "gsis_player_id": gsis,
                            "player_name": quote["player_name"], "market": market,
                            "model_version": VERSION + ":" + family, "projection": round(point, 4),
                            "uncertainty_sigma": round(sigma, 4),
                            "sportsbook": quote["sportsbook"], "line": quote["line"],
                            "side": quote["side"], "price": quote["price"],
                            "observed_at": quote["retrieved_at"], "kickoff_utc": quote["kickoff_utc"],
                            "probability_over": round(over, 6), "probability_under": round(1-over, 6),
                            "input_context": {"prior_games": feature["prior_games"],
                                              "stat_last3": feature[f"{LABEL[market]}_last3"],
                                              "opponent_allowed_last3": feature["opponent_allowed_last3"],
                                              "opportunities_last3": feature["attempts_last3" if market == "passing_yards" else
                                                                               "carries_last3" if market in {"rushing_yards", "rushing_attempts"} else
                                                                               "targets_last3"],
                                              "opportunity_kind": "pass attempts" if market == "passing_yards" else
                                                                  "carries" if market in {"rushing_yards", "rushing_attempts"} else "targets"},
                            "raw_difference": round(point-quote["line"], 4)})
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS", "batch_id": batch_id,
            "model_selection": {m: validation[m]["selected_on_2024"] for m in MARKETS},
            "identity_matched": len(mapping), "identity_unmatched": sorted(unmatched),
            "identity_ambiguous": sorted(ambiguous), "coverage": coverage,
            "paired_quotes_with_projection": len(projections), "projections": projections}


def seal_forward(report: dict, dataset_hashes: dict[str, str], created_at: float | None = None) -> dict:
    """Write a separate, immutable prop freeze before every represented kickoff."""
    created_at = time.time() if created_at is None else created_at
    rows = report["projections"]
    first = db.q1("SELECT MIN(kickoff_utc) AS kickoff FROM nfl_prop_snapshots WHERE batch_id=?",
                  (report["batch_id"],))
    if (not rows or not dataset_hashes or not first or first["kickoff"] is None or
            created_at >= first["kickoff"] or
            any(created_at >= r["kickoff_utc"] or created_at < r["observed_at"] or
                r["observed_at"] >= r["kickoff_utc"] for r in rows)):
        raise ValueError("prop freeze requires pre-kickoff projections and observations")
    paired_ids = {quote["id"] for quote in _pair_quotes(report["batch_id"])[0]}
    if len({row["quote_id"] for row in rows}) != len(rows) or any(row["quote_id"] not in paired_ids for row in rows):
        raise ValueError("prop freeze requires unique complete main-line quote pairs")
    for row in rows:
        quote = db.q1("SELECT * FROM nfl_prop_snapshots WHERE id=? AND batch_id=?",
                      (row["quote_id"], report["batch_id"]))
        if not quote or quote["line_type"] != "main" or any(row.get(field) != quote[source] for field, source in
                            (("canonical_game_id", "canonical_game_id"), ("player_id", "player_id"),
                             ("market", "market"), ("sportsbook", "sportsbook"), ("line", "line"),
                             ("side", "side"), ("price", "price"),
                             ("observed_at", "retrieved_at"), ("kickoff_utc", "kickoff_utc"))):
            raise ValueError("prop freeze quote does not match immutable observation")
    metadata = {"model_version": VERSION, "feature_version": FEATURE_VERSION,
                "feature_names": FEATURES, "input_sha256": dataset_hashes,
                "selection_basis": "2024 development MAE", "validation_holdout": "2025",
                "source_batch_id": report["batch_id"], "created_at": created_at}
    body = json.dumps({"metadata": metadata, "projections": rows}, sort_keys=True, separators=(",", ":"))
    freeze_sha = hashlib.sha256(body.encode()).hexdigest()
    with db.tx() as c:
        if c.execute("SELECT 1 FROM nfl_prop_research_freezes WHERE source_batch_id=?", (report["batch_id"],)).fetchone():
            raise ValueError("source batch has already been frozen")
        c.execute("INSERT INTO nfl_prop_research_freezes(freeze_sha,source_batch_id,frozen_at,payload) VALUES(?,?,?,?)",
                  (freeze_sha, report["batch_id"], created_at, body))
    return {"freeze_sha": freeze_sha, "quote_count": len(rows), "frozen_at": created_at}


def research_board(game_id: str, market: str | None = None) -> dict:
    """Enrich archived observations with frozen research estimates only."""
    board = nfl_props.board(game_id, market)
    row = db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE source_batch_id=?",
                (board["batch_id"],)) if board["batch_id"] else None
    projections = {r["quote_id"]: r for r in json.loads(row["payload"])["projections"]} if row else {}
    for quote in board["quotes"]:
        projection = projections.get(quote["id"])
        quote["research_estimate"] = ({key: projection[key] for key in
            ("model_version", "projection", "uncertainty_sigma", "raw_difference",
             "probability_over", "probability_under")} if projection else None)
    board["note"] = "Research estimates only; no recommendations, edge thresholds, or picks. Only complete main-line pairs receive estimates."
    board["projected_quote_count"] = sum(q["research_estimate"] is not None for q in board["quotes"])
    return board


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline NFL prop lab; reads archived market quotes")
    parser.add_argument("--output", default="data/research/nfl_prop_lab_2026_week3.json")
    parser.add_argument("--seal", action="store_true", help="append immutable pre-kickoff prop freeze")
    args = parser.parse_args()
    datasets, history, hashes = build_dataset()
    validation = evaluate(datasets)
    batch = db.q1("SELECT batch_id FROM nfl_prop_capture_batches ORDER BY retrieved_at DESC LIMIT 1")
    if not batch:
        raise SystemExit("no archived SportsGameOdds capture")
    forward = project_week3(datasets, history, validation, batch["batch_id"])
    report = {"readiness": "RESEARCH_ONLY", "designation": "PASS", "feature_version": FEATURE_VERSION,
              "features": FEATURES, "dataset_sha256": hashes,
              "validation": validation, "forward": forward}
    if args.seal:
        report["forward_freeze"] = seal_forward(forward, hashes)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(path), "training_rows": {m: validation[m]["training_rows"] for m in MARKETS},
                      "paired_quotes_with_projection": forward["paired_quotes_with_projection"],
                      "forward_freeze": report.get("forward_freeze")}, indent=2))


if __name__ == "__main__":
    main()
