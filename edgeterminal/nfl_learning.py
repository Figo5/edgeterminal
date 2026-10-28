"""Weekly NFL updates with separate fit, calibration and real quoted holdouts.

This adapter never changes the pinned model or any saved forecast. Training is
explicit and reads local, receipt-backed sources only. The common registry owns
promotion; HTTP prediction serves JSON coefficients without fitting anything.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import time

import numpy as np

from . import db, nfl_identity, nfl_prop_grading as grading
from . import nfl_prop_lab as lab, nfl_prop_model as baseline

SCHEMA = "nfl-weekly-json-ridge-v1"
PARAMETERS = {"family": "ridge_role", "alpha": 100., "calibration_weeks": 2,
              "validation_weeks": 3, "minimum_calibration_rows": 30,
              "tail_pseudocount_each_side": .5,
              "selection": "top-five-confidence-v1"}


def _active():
    from . import model_learning_store
    return model_learning_store.load_active("NFL")


def _digest(value) -> str:
    body = value if isinstance(value, bytes) else json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(body).hexdigest()


def split_rows(datasets: dict, validation_week: tuple[int, int]) -> tuple[dict, dict]:
    """Never split a game/week across fit, calibration and validation."""
    weeks = sorted({(int(r["season"]), int(r["week"]))
                    for rows in datasets.values() for r in rows
                    if (int(r["season"]), int(r["week"])) < validation_week})
    if len(weeks) <= PARAMETERS["calibration_weeks"]:
        raise ValueError("insufficient whole weeks before NFL validation")
    boundary = weeks[-PARAMETERS["calibration_weeks"]]
    fit = {m: [r for r in datasets[m] if (r["season"], r["week"]) < boundary]
           for m in lab.MARKETS}
    calibration = {m: [r for r in datasets[m]
                       if boundary <= (r["season"], r["week"]) < validation_week]
                   for m in lab.MARKETS}
    return fit, calibration


def fit_market(fit: list, calibration: list, market: str) -> dict:
    if not fit or len(calibration) < PARAMETERS["minimum_calibration_rows"]:
        raise ValueError("insufficient NFL fit/calibration coverage")
    if any(not math.isfinite(float(r["target"])) for r in fit + calibration):
        raise ValueError("NFL training labels must be finite")
    estimator = baseline._fit(fit, market, "ridge_role", "ridge")
    scaler, ridge = estimator.steps[0][1], estimator.steps[1][1]
    coefficients = ridge.coef_ / scaler.scale_
    intercept = float(ridge.intercept_ - np.dot(coefficients, scaler.mean_))
    points = baseline._predict(estimator, calibration, market, "ridge_role")
    residuals = [float(row["target"] - point) for row, point in zip(calibration, points)]
    return {"coefficients": coefficients.tolist(), "intercept": intercept,
            "residuals": residuals, "fit_rows": len(fit),
            "calibration_rows": len(calibration)}


def predict(config: dict, features: dict, market: str, line: float | None = None):
    derived = baseline.derived_features({"features": features}, market)
    coefficients = config.get("coefficients", [])
    residuals = config.get("residuals", [])
    values = list(coefficients) + [config.get("intercept")]
    if (len(coefficients) != len(baseline.FEATURES) or
            any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                not math.isfinite(x) for x in values)):
        raise ValueError("NFL JSON coefficients must be finite and complete")
    point = config["intercept"] + sum(
        coefficient * derived[name] for name, coefficient in zip(baseline.FEATURES, coefficients))
    if not math.isfinite(point):
        raise ValueError("NFL JSON prediction must be finite")
    point = max(0., point)
    if line is None:
        return point, None
    if (isinstance(line, bool) or not math.isfinite(line) or
            len(residuals) < PARAMETERS["minimum_calibration_rows"] or
            any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                not math.isfinite(x) for x in residuals)):
        raise ValueError("NFL probability requires finite calibration")
    outcomes = np.rint(point + np.array(residuals, dtype=float))
    if market in {"receptions", "rushing_attempts"}:
        outcomes = np.maximum(0., outcomes)
    denominator = len(outcomes) + 1.
    probability = {"over": float((np.sum(outcomes > line) + .5) / denominator),
                   "under": float((np.sum(outcomes < line) + .5) / denominator),
                   "push": float(np.sum(outcomes == line) / denominator)}
    return point, probability


def _validated_active():
    active = _active()
    if active is None:
        return None
    artifact = active["artifact"]
    if (artifact.get("schema") != SCHEMA or artifact.get("features") != list(baseline.FEATURES) or
            artifact.get("parameters") != PARAMETERS or
            set(artifact.get("configurations", {})) != set(lab.MARKETS)):
        raise ValueError("NFL adaptive artifact contract mismatch")
    return active


def model_info() -> dict:
    active = _validated_active()
    if active is None:
        return baseline.model_info()
    artifact = active["artifact"]
    return {"version": active["version"], "training_cutoff": artifact["fit_boundary_week"],
            "training_cutoff_at": active["training_cutoff"],
            "fingerprint": artifact["parameter_fingerprint"],
            "manifest_sha256": active["artifact_sha256"],
            "candidate_id": active["candidate_id"]}


def load_artifact():
    active = _validated_active()
    return baseline.load_artifact() if active is None else (active, active["artifact"])


def _feature_for(prepared: dict, market: str) -> dict:
    features = dict(prepared["feature"])
    features["opponent_allowed_last3"] = lab._avg(prepared["opp_hist"], lab.LABEL[market])
    return features


def _metadata(active: dict) -> dict:
    artifact = active["artifact"]
    return {
        "model_version": active["version"], "feature_version": SCHEMA,
        "feature_names": list(baseline.FEATURES),
        "parameter_fingerprint": artifact["parameter_fingerprint"],
        "model_manifest_sha256": active["artifact_sha256"],
        "training_cutoff": artifact["fit_boundary_week"],
        "training_cutoff_at": active["training_cutoff"],
        "calibration_cutoff_at": artifact["calibration_cutoff"],
        "learning_candidate_id": active["candidate_id"],
        "selection_basis": "Fixed Ridge; prior whole-week residual calibration; validated registry promotion",
        "validation_holdout": artifact["validation_description"]}


def project_current(datasets: dict, history: dict, validation: dict, batch_id: str,
                    *, season: int = 2026, week: int = 3, stats_dir: Path | None = None) -> dict:
    active = _validated_active()
    report = baseline.project_current(datasets, history, validation, batch_id,
                                     season=season, week=week, stats_dir=stats_dir)
    if active is None:
        return report
    if active["training_cutoff"] >= time.time():
        raise ValueError("NFL model training cutoff is not prior to forecast")
    artifact = active["artifact"]
    inputs = lab._week3_features(history, season, week, stats_dir)
    for row in report["projections"]:
        quote = db.q1("SELECT team FROM nfl_prop_snapshots WHERE id=?", (row["quote_id"],))
        prepared = inputs.get((row["gsis_player_id"], nfl_identity.team_code(quote["team"]))) if quote else None
        if prepared is None:
            raise ValueError("adaptive NFL forecast has no matching prior-only inputs")
        market = row["market"]
        config = artifact["configurations"][market]
        point, probability = predict(config, _feature_for(prepared, market), market, float(row["line"]))
        row.update(model_version=active["version"] + ":ridge_role",
                   parameter_fingerprint=artifact["parameter_fingerprint"],
                   model_manifest_sha256=active["artifact_sha256"], projection=round(point, 4),
                   uncertainty_sigma=round(float(np.std(config["residuals"], ddof=1)), 4),
                   raw_difference=round(point - row["line"], 4),
                   probability_over=round(probability["over"], 6),
                   probability_under=round(probability["under"], 6),
                   probability_push=round(probability["push"], 6),
                   learning_candidate_id=active["candidate_id"],
                   training_cutoff_at=active["training_cutoff"],
                   calibration_cutoff_at=artifact["calibration_cutoff"])
        row["input_context"]["probability_method"] = "weekly_empirical"
    report["model_selection"] = {m: "ridge_role" for m in lab.MARKETS}
    report["model_metadata"] = _metadata(active)
    return report


def _canonical_game(game: dict) -> str:
    return (f"nfl-{game['season']}-reg-{int(game['week']):02d}-"
            f"{nfl_identity.team_code(game['away_team'])}-{nfl_identity.team_code(game['home_team'])}")


def _kickoff(game: dict) -> float:
    # nflverse's gametime is America/New_York, including daylight saving time.
    from zoneinfo import ZoneInfo
    value = datetime.fromisoformat(game["gameday"] + "T" + (game.get("gametime") or "23:59"))
    return value.replace(tzinfo=ZoneInfo("America/New_York")).timestamp()


def _catalog(now: float) -> dict:
    proofs = {}
    for record in db.q("SELECT * FROM nfl_prop_research_freezes ORDER BY frozen_at"):
        if record["frozen_at"] > now:
            continue
        if _digest(record["payload"].encode()) != record["freeze_sha"]:
            raise ValueError("NFL source freeze checksum mismatch")
        for digest in json.loads(record["payload"])["metadata"].get("input_sha256", {}).values():
            proofs.setdefault(digest, record["frozen_at"])
    catalog = {}
    paths = list(lab.STATS.glob("stats_player_week_202*.csv"))
    paths += list((lab.ROOT / "data/nflverse/prop_live").glob("*.csv"))
    for path in paths:
        body = path.read_bytes()
        digest = _digest(body)
        receipt = proofs.get(digest)
        if receipt is not None and path.stat().st_mtime <= receipt:
            catalog[digest] = {"body": body, "available_at": receipt, "source": {
                "kind": "file", "path": str(path.relative_to(lab.ROOT)),
                "sha256": digest, "received_at": receipt}}
    for record in db.q("SELECT * FROM nfl_prop_grade_source_artifacts WHERE captured_at<=?", (now,)):
        body = bytes(record["payload"])
        if _digest(body) != record["sha256"]:
            raise ValueError("NFL official source checksum mismatch")
        source = {"kind": "nfl_prop_grade_source_artifact", "sha256": record["sha256"],
                  "received_at": record["captured_at"]}
        old = catalog.get(record["sha256"])
        if old is None or record["captured_at"] < old["source"]["received_at"]:
            catalog[record["sha256"]] = {"body": body, "source": source,
                "available_at": min(record["captured_at"], proofs.get(record["sha256"], now)),
                "url": record["source_file"]}
    return catalog


def _current_snapshot(catalog: dict, before: float, season: int) -> dict:
    eligible = []
    for item in catalog.values():
        if item["source"]["received_at"] >= before:
            continue
        body = item["body"]
        if not body.startswith(b"player_id,") and not body.startswith(b"\xef\xbb\xbfplayer_id,"):
            continue
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        if rows and {"season", "week", "season_type", "game_id", *lab.USAGE} <= rows[0].keys():
            current = [r for r in rows if r["season"] == str(season) and r["season_type"] == "REG"]
            if current:
                eligible.append((item["source"]["received_at"], item))
    if not eligible:
        raise ValueError("no receipt-backed NFL stats snapshot before cutoff")
    return max(eligible, key=lambda pair: pair[0])[1]


def _dataset(body: bytes, cutoff: tuple[int, int]):
    with TemporaryDirectory(prefix="edgeterminal-nfl-learning-") as directory:
        path = Path(directory)
        (path / "stats_player_week_2026.csv").write_bytes(body)
        return lab.build_dataset(cutoff, stats_dir=path)


def _require_completed_history(body: bytes, boundary: tuple[int, int], schedule: dict) -> None:
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    present = {r["game_id"] for r in rows if r.get("season_type") == "REG"}
    expected = {game_id for game_id, game in schedule.items()
                if int(game["season"]) == boundary[0] and int(game["week"]) < boundary[1]}
    if expected - present:
        raise ValueError("NFL pre-cutoff player history has incomplete game coverage")


def _validation_records(now: float):
    from . import nfl_top_five
    board = nfl_top_five.view()
    schedule = lab._schedule()
    complete = set()
    by_week = {}
    for game in board["games"]:
        key = tuple(int(x) for x in (game["game_id"].split("-")[1], game["game_id"].split("-")[3]))
        by_week.setdefault(key, []).append(game)
    for week, games in by_week.items():
        scheduled = [g for g in schedule.values() if (int(g["season"]), int(g["week"])) == week]
        if (scheduled and max(_kickoff(g) for g in scheduled) + 8 * 3600 < now and
                all(r["result"] in {"won", "lost", "push", "void"} for g in games for r in g["rows"])):
            complete.add(week)
    weeks = sorted(complete)[-PARAMETERS["validation_weeks"]:]
    if len(weeks) < PARAMETERS["validation_weeks"]:
        return [], {}, []
    freezes, output, sources = {}, [], {}
    for week in weeks:
        for game in by_week[week]:
            freeze_sha = game["freeze_sha"]
            frozen = freezes.setdefault(freeze_sha, grading._freeze(freeze_sha))
            for row in game["rows"]:
                if row["result"] not in {"won", "lost"}:
                    continue
                grade_row = db.q1("SELECT * FROM nfl_prop_grade_revisions WHERE freeze_sha=? AND quote_id=? "
                                   "ORDER BY revision DESC LIMIT 1", (freeze_sha, row["quote_id"]))
                evidence_row = db.q1("SELECT * FROM nfl_prop_grade_evidence WHERE evidence_sha=?",
                                      (grade_row["evidence_sha"],))
                evidence_payload = json.loads(evidence_row["payload"]) if evidence_row else None
                if (not evidence_row or _digest((freeze_sha + grading._canonical(evidence_payload)).encode())
                        != evidence_row["evidence_sha"]):
                    raise ValueError("NFL validation evidence checksum mismatch")
                evidence = grading._validate_evidence(evidence_payload, {game["game_id"]}, now)
                official = evidence["players"].get(row["gsis_player_id"], {})
                if (evidence["status"] != "final" or official.get("status") != "active" or
                        official.get("stats", {}).get(row["market"]) != row["actual"] or
                        evidence["retrieved"] > now or row["graded_at"] > now):
                    raise ValueError("NFL validation grade conflicts with official result")
                for source in (evidence["stat_source"], evidence["final_source"]):
                    if not source["file"].startswith("db:nfl_prop_grade_source_artifacts:"):
                        raise ValueError("NFL validation source has no archived official receipt")
                    saved = db.q1("SELECT captured_at FROM nfl_prop_grade_source_artifacts WHERE sha256=?",
                                   (source["sha256"],))
                    sources[source["sha256"]] = {"kind": "nfl_prop_grade_source_artifact",
                        "sha256": source["sha256"], "received_at": saved["captured_at"], "role": "validation"}
                output.append({**row, "game_id": game["game_id"], "season": week[0], "week": week[1],
                               "kickoff_at": row["kickoff_utc"], "result_observed_at": grade_row["graded_at"],
                               "freeze_sha": freeze_sha, "frozen_metadata": frozen["metadata"]})
    return output, freezes, list(sources.values())


def _baseline_points(records: list, active):
    if active:
        for row in records:
            row["baseline_prediction"], probability = predict(active["artifact"]["configurations"][row["market"]],
                row["features"], row["market"], row["line"])
            row["baseline_probability"] = probability[row["side"]]
        return
    manifest, models = baseline.load_artifact()
    for market in lab.MARKETS:
        rows = [r for r in records if r["market"] == market]
        if not rows:
            continue
        config = manifest["configurations"][market]
        points = baseline._predict(models[market], rows, market, config["family"])
        for row, point in zip(rows, points):
            if config["probability_method"] == "empirical":
                probability = baseline.probabilities(row["line"], float(point), config, market)
            else:
                over = lab._prob_over(row["line"], point, config["baseline_sigma"])
                probability = {"over": over, "under": 1. - over}
            row["baseline_prediction"] = float(point)
            row["baseline_probability"] = probability[row["side"]]


def build_candidate(now: float | None = None) -> dict:
    """Build a proposed weekly update; the registry independently gates promotion."""
    stamp = time.time() if now is None else now
    records, _, sources = _validation_records(stamp)
    if not records:
        return {"status": "waiting", "reason": "Three completed NFL weeks with verified selected main lines are required."}
    validation_start = min(r["kickoff_at"] for r in records)
    active = _active()
    if active and active["training_cutoff"] >= validation_start:
        return {"status": "waiting", "reason": "Current NFL baseline needs a new untouched whole-week validation window."}
    season, week = min((r["season"], r["week"]) for r in records)
    if season != 2026 or week < 3:
        return {"status": "waiting", "reason": "NFL local feature source supports the 2026 regular season."}
    fit_boundary = (season, week - PARAMETERS["calibration_weeks"])
    schedule = lab._schedule()
    whole_week_start = min(_kickoff(g) for g in schedule.values()
                           if (int(g["season"]), int(g["week"])) == (season, week))
    if active and active["training_cutoff"] >= whole_week_start:
        return {"status": "waiting", "reason": "Current NFL baseline needs a new untouched whole-week validation window."}
    training_cutoff = min(_kickoff(g) for g in schedule.values()
                          if (int(g["season"]), int(g["week"])) == fit_boundary) - 1.
    calibration_cutoff = whole_week_start - 1.
    catalog = _catalog(stamp)
    fit_snapshot = _current_snapshot(catalog, training_cutoff, season)
    calibration_snapshot = _current_snapshot(catalog, calibration_cutoff, season)
    _require_completed_history(fit_snapshot["body"], fit_boundary, schedule)
    _require_completed_history(calibration_snapshot["body"], (season, week), schedule)
    fit, _, fit_hashes = _dataset(fit_snapshot["body"], fit_boundary)
    all_calibration, _, cal_hashes = _dataset(calibration_snapshot["body"], (season, week))
    _, calibration = split_rows(all_calibration, (season, week))
    configs = {market: fit_market(fit[market], calibration[market], market) for market in lab.MARKETS}
    # Exact archived pregame input bytes, never a refreshed postgame feature file.
    feature_cache = {}
    for row in records:
        hashes = row["frozen_metadata"]["input_sha256"]
        digest = hashes.get("features:stats_player_week_2026.csv", hashes.get("stats_player_week_2026.csv"))
        if digest not in catalog:
            raise ValueError("NFL quoted validation is missing its frozen feature source")
        source = catalog[digest]
        if not source["available_at"] <= row["frozen_metadata"]["created_at"] < row["kickoff_at"]:
            raise ValueError("NFL validation feature source postdates its immutable pregame forecast")
        key = digest, row["season"], row["week"]
        if key not in feature_cache:
            with TemporaryDirectory(prefix="edgeterminal-nfl-learning-") as directory:
                path = Path(directory)
                (path / "stats_player_week_2026.csv").write_bytes(source["body"])
                _, history, _ = lab.build_dataset((row["season"], row["week"]), path)
                feature_cache[key] = lab._week3_features(history, row["season"], row["week"], path)
            sources.append({**source["source"], "role": "validation"})
        quote = db.q1("SELECT team,line_type FROM nfl_prop_snapshots WHERE id=?", (row["quote_id"],))
        if not quote or quote["line_type"] != "main":
            raise ValueError("NFL validation requires original main-line quotes")
        prepared = feature_cache[key].get((row["gsis_player_id"], nfl_identity.team_code(quote["team"])))
        if prepared is None:
            raise ValueError("NFL validation identity has no frozen prior-only features")
        row["features"] = _feature_for(prepared, row["market"])
    _baseline_points(records, active)
    evaluation_rows = []
    for row in records:
        point, probability = predict(configs[row["market"]], row["features"], row["market"], row["line"])
        evaluation_rows.append({key: row[key] for key in (
            "game_id", "player_id", "market", "quote_id", "line", "side", "observed_at", "kickoff_at",
            "result_observed_at", "actual", "baseline_prediction", "baseline_probability")})
        evaluation_rows[-1].update(candidate_prediction=point, candidate_probability=probability[row["side"]])
    for role, hashes, snapshot in (("training", fit_hashes, fit_snapshot),
                                   ("calibration", cal_hashes, calibration_snapshot)):
        for name, digest in hashes.items():
            item = snapshot if name.endswith("2026.csv") else catalog.get(digest)
            if item is None:
                raise ValueError("NFL historical source lacks an availability receipt")
            sources.append({**item["source"], "role": role})
    sources.append({"kind": "file", "path": str(lab.SCHEDULE.relative_to(lab.ROOT)),
                    "sha256": _digest(lab.SCHEDULE.read_bytes()),
                    "received_at": lab.SCHEDULE.stat().st_mtime, "role": "training"})
    fit_games = {r["game_id"] for rows in fit.values() for r in rows}
    calibration_games = {r["game_id"] for rows in calibration.values() for r in rows}
    training = {
        "fit_game_ids": sorted(_canonical_game(schedule[g]) for g in fit_games),
        "calibration_game_ids": sorted(_canonical_game(schedule[g]) for g in calibration_games),
        "fit_label_max_at": max(s["received_at"] for s in sources if s["role"] == "training"),
        "calibration_label_max_at": max(s["received_at"] for s in sources if s["role"] == "calibration")}
    last_week = max(r["week"] for r in records)
    artifact = {"schema": SCHEMA, "parameters": PARAMETERS,
                "features": list(baseline.FEATURES), "configurations": configs,
                "fit_boundary_week": list(fit_boundary), "training_cutoff": training_cutoff,
                "calibration_cutoff": calibration_cutoff, "training": training,
                "validation_description": f"2026 weeks {week}-{last_week}; locked actual main lines; never fitted",
                "parameter_fingerprint": _digest({"parameters": PARAMETERS, "configurations": configs}),
                "code_sha256": {str(Path(module.__file__).relative_to(lab.ROOT)): _digest(Path(module.__file__).read_bytes())
                                for module in (baseline, lab)}}
    artifact["code_sha256"]["edgeterminal/nfl_learning.py"] = _digest(Path(__file__).read_bytes())
    evaluation = {"kind": "actual_main_lines", "validation_start": validation_start,
                  "validation_end": max(r["result_observed_at"] for r in records),
                  "calibration_cutoff": calibration_cutoff,
                  "baseline_candidate_id": active["candidate_id"] if active else None,
                  "rows": evaluation_rows}
    unique = {(s["kind"], s.get("path"), s["sha256"], s["role"]): s for s in sources}
    sources = sorted(unique.values(), key=lambda source: (source["role"], source["kind"], source["sha256"]))
    # The same source/label/code revision is idempotent across worker retries.
    revision = _digest({"artifact": artifact, "evaluation": evaluation, "sources": sources})[:16]
    version = f"nfl-weekly-ridge-1.2026w{last_week:02d}-{revision}"
    artifact["version"] = version
    return {"status": "ready", "sport": "NFL", "version": version,
            "training_cutoff": training_cutoff, "artifact": artifact,
            "evaluation": evaluation, "sources": sources}


def _saved_metrics(rows: list) -> dict:
    if not rows:
        return {"n": 0}
    absolute, brier, log_loss, buckets = [], [], [], {}
    wins = 0
    for row in rows:
        actual, point = float(row["actual"]), float(row["projection"])
        probability = float(row["probability_" + row["side"]])
        if not all(math.isfinite(v) for v in (actual, point, probability)) or not 0 <= probability <= 1:
            raise ValueError("NFL saved forecast diagnostics require finite estimates")
        won = float(actual > row["line"]) if row["side"] == "over" else float(actual < row["line"])
        wins += int(won)
        absolute.append(abs(actual - point))
        brier.append((probability - won) ** 2)
        clipped = max(1e-9, min(1 - 1e-9, probability))
        log_loss.append(-won * math.log(clipped) - (1 - won) * math.log1p(-clipped))
        buckets.setdefault(min(9, int(probability * 10)), []).append((probability, won))
    reliability = [{"lower": key / 10., "upper": (key + 1) / 10., "n": len(values),
                    "mean_probability": sum(p for p, _ in values) / len(values),
                    "observed_win_rate": sum(y for _, y in values) / len(values)}
                   for key, values in sorted(buckets.items())]
    return {"n": len(rows), "wins": wins, "losses": len(rows) - wins,
            "win_rate": wins / len(rows), "mae": sum(absolute) / len(rows),
            "brier": sum(brier) / len(rows), "log_loss": sum(log_loss) / len(rows),
            "reliability": reliability}


def nightly_diagnostics(now: float | None = None) -> dict:
    """Verified saved-forecast metrics; never train or fetch on a page load."""
    stamp = time.time() if now is None else now
    rows, _, _ = _validation_records(stamp)
    return {"status": "ok" if rows else "waiting", "sport": "NFL", "observed_at": stamp, "readiness": "RESEARCH_ONLY",
            "verified_validation_rows": len(rows), "verified_games": len({r["game_id"] for r in rows}),
            "market_coverage": dict(Counter(r["market"] for r in rows)),
            "saved_forecast_metrics": {
                "window": "latest three completed weeks with resolved locked selections; pushes/voids excluded",
                "by_market": {market: _saved_metrics([r for r in rows if r["market"] == market])
                              for market in lab.MARKETS},
                "by_model_version": {version: _saved_metrics([r for r in rows if r["model_version"] == version])
                                     for version in sorted({r["model_version"] for r in rows})}},
            "active_model": model_info()}


def seal_forward(report: dict, dataset_hashes: dict, created_at: float | None = None) -> dict:
    """Retain legacy sealing; adaptive freezes also retain precise version cutoffs."""
    metadata = report.get("model_metadata", {})
    if "learning_candidate_id" not in metadata:
        return lab.seal_forward(report, dataset_hashes, created_at)
    stamp = time.time() if created_at is None else created_at
    active = _validated_active()
    if not active or active["candidate_id"] != metadata["learning_candidate_id"]:
        raise ValueError("NFL freeze requires the verified active candidate")
    if (metadata != _metadata(active) or
            not metadata["training_cutoff_at"] < metadata["calibration_cutoff_at"] < stamp):
        raise ValueError("NFL freeze model identity/cutoff mismatch")
    rows = report["projections"]
    first = db.q1("SELECT MIN(kickoff_utc) AS kickoff FROM nfl_prop_snapshots WHERE batch_id=?", (report["batch_id"],))
    paired_ids = {q["id"] for q in lab._pair_quotes(report["batch_id"])[0]}
    if (not rows or not dataset_hashes or not first or first["kickoff"] is None or stamp >= first["kickoff"] or
            len({r["quote_id"] for r in rows}) != len(rows)):
        raise ValueError("NFL freeze requires unique pre-kickoff quotes and sources")
    for row in rows:
        quote = db.q1("SELECT * FROM nfl_prop_snapshots WHERE id=? AND batch_id=?", (row["quote_id"], report["batch_id"]))
        if (not quote or quote["line_type"] != "main" or row["quote_id"] not in paired_ids or
                not row["observed_at"] <= stamp < row["kickoff_utc"] or
                row["model_version"] != metadata["model_version"] + ":ridge_role" or
                any(row.get(key) != metadata[key] for key in ("parameter_fingerprint", "model_manifest_sha256",
                    "learning_candidate_id", "training_cutoff_at", "calibration_cutoff_at")) or
                any(row.get(field) != quote[source] for field, source in (
                    ("canonical_game_id", "canonical_game_id"), ("player_id", "player_id"), ("market", "market"),
                    ("sportsbook", "sportsbook"), ("line", "line"), ("side", "side"), ("price", "price"),
                    ("observed_at", "retrieved_at"), ("kickoff_utc", "kickoff_utc")))):
            raise ValueError("NFL freeze quote/model does not match immutable observation")
    body = json.dumps({"metadata": {**metadata, "input_sha256": dataset_hashes,
                                   "source_batch_id": report["batch_id"], "created_at": stamp},
                       "projections": rows}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = _digest(body.encode())
    with db.tx() as connection:
        connection.execute("INSERT INTO nfl_prop_research_freezes(freeze_sha,source_batch_id,frozen_at,payload) VALUES(?,?,?,?)",
                           (digest, report["batch_id"], stamp, body))
    return {"freeze_sha": digest, "quote_count": len(rows), "frozen_at": stamp}
