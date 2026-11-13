"""Weekly NFL updates with separate fit, calibration and real quoted holdouts.

This adapter never changes the pinned model or any saved forecast. Training is
explicit and reads local, receipt-backed sources only. The common registry owns
promotion; HTTP prediction serves JSON coefficients without fitting anything.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
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
from . import nfl_prop_roles as roles

QUALITY_PROTOCOL = "nfl-props-quality-v2"
ROLE_SCHEMA = "nfl-selective-role-composite-v2"
ROLE_PARAMETERS = {"family": roles.SCHEMA, "alpha": roles.ALPHA,
                   "residual_calibration_weeks": 1, "market_selection_weeks": 1,
                   "validation_weeks": 3, "minimum_calibration_rows": 30,
                   "selection": "top-five-confidence-v1",
                   "quote_unit": "one-over-book-lexicographic-per-game-player-market"}

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
    legacy = (artifact.get("schema") == SCHEMA and artifact.get("features") == list(baseline.FEATURES)
              and artifact.get("parameters") == PARAMETERS)
    composite = (artifact.get("schema") == ROLE_SCHEMA and artifact.get("features") == list(roles.FEATURES)
                 and artifact.get("parameters") == ROLE_PARAMETERS)
    if (not (legacy or composite) or set(artifact.get("configurations", {})) != set(lab.MARKETS)):
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
        "model_version": active["version"], "feature_version": artifact["schema"],
        "feature_names": artifact["features"],
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
    role_inputs = {}
    if artifact["schema"] == ROLE_SCHEMA:
        raw, _ = lab._read_stats(stats_dir)
        role_inputs = roles.current_features(inputs, raw, season, week)
    for row in report["projections"]:
        quote = db.q1("SELECT team FROM nfl_prop_snapshots WHERE id=?", (row["quote_id"],))
        prepared = inputs.get((row["gsis_player_id"], nfl_identity.team_code(quote["team"]))) if quote else None
        if prepared is None:
            raise ValueError("adaptive NFL forecast has no matching prior-only inputs")
        market = row["market"]
        config = artifact["configurations"][market]
        if config.get("kind") == "frozen_baseline":
            _check_baseline_config(config)
            continue  # Preserve the entire pinned nonlinear baseline row exactly.
        feature = _feature_for(prepared, market)
        if config.get("kind") == roles.SCHEMA:
            feature.update(role_inputs[(row["gsis_player_id"], lab._team(quote["team"]), market)])
        point, probability = _json_predict(config, feature, market, float(row["line"]))
        row.update(model_version=active["version"] + ":" + ("role_v2" if config.get("kind") == roles.SCHEMA else "ridge_role"),
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
    if artifact.get("price_anchor"):
        from . import nfl_prop_anchor
        for row in report["projections"]:
            nfl_prop_anchor.apply(row, artifact)
            row["price_anchor_candidate_id"] = active["candidate_id"]
    report["model_selection"] = {m: artifact["configurations"][m].get("kind", "ridge_role") for m in lab.MARKETS}
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
    if not board["games"]:
        return [], {}, []
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
                not _seal_model_matches(row, metadata, active) or
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


def _baseline_config():
    return dict(kind='frozen_baseline', version=baseline.VERSION,
                manifest_sha256=baseline.MANIFEST_SHA256,
                parameter_fingerprint=baseline.PARAMETER_FINGERPRINT)


def _check_baseline_config(config):
    if config != _baseline_config():
        raise ValueError('retained NFL baseline pin changed')


def _json_predict(config, features, market, line):
    return (roles.predict(config, features, market, line) if config.get('kind') == roles.SCHEMA
            else predict(config, features, market, line))


def _seal_model_matches(row, metadata, active):
    config = active['artifact']['configurations'][row['market']]
    if config.get('kind') == 'frozen_baseline':
        _check_baseline_config(config)
        return (row.get('model_version', '').startswith(baseline.VERSION + ':') and
                row.get('parameter_fingerprint') == baseline.PARAMETER_FINGERPRINT and
                row.get('model_manifest_sha256') == baseline.MANIFEST_SHA256 and
                'learning_candidate_id' not in row)
    suffix = ':role_v2' if config.get('kind') == roles.SCHEMA else ':ridge_role'
    return (row.get('model_version') == metadata['model_version'] + suffix and
            all(row.get(key) == metadata[key] for key in ('parameter_fingerprint',
                'model_manifest_sha256', 'learning_candidate_id', 'training_cutoff_at', 'calibration_cutoff_at')))


def _quality_window(now):
    """Choose scheduled whole weeks without inspecting any result label."""
    records = db.q('SELECT * FROM nfl_top_five_selections ORDER BY selected_at,game_id')
    if not records:
        return [], [], {}
    schedule = lab._schedule()
    grouped = {}
    for record in records:
        if _digest(record['payload'].encode()) != record['sha256']:
            raise ValueError('NFL policy selection checksum mismatch')
        value = json.loads(record['payload'])
        if any(value.get(k) != record[k] for k in ('game_id', 'freeze_sha', 'selected_at')):
            raise ValueError('NFL policy selection metadata changed')
        parts = record['game_id'].split('-')
        week = int(parts[1]), int(parts[3])
        grouped.setdefault(week, []).append(dict(record, season=week[0], week=week[1]))
    completed = [week for week in sorted(grouped) if any(
        (int(g['season']), int(g['week'])) == week for g in schedule.values()) and
        max(_kickoff(g) for g in schedule.values()
            if (int(g['season']), int(g['week'])) == week) + 8 * 3600 < now]
    outer = completed[-ROLE_PARAMETERS['validation_weeks']:]
    return [r for w in outer for r in grouped[w]], outer, schedule


def _pregame_universe(group):
    """Reconstruct the complete source universe and only pregame exclusions."""
    from . import nfl_top_five, nfl_prop_availability
    record = db.q1('SELECT * FROM nfl_top_five_selections WHERE game_id=?', (group['game_id'],))
    if (not record or record['freeze_sha'] != group['freeze_sha'] or
            record['selected_at'] != group['selected_at'] or
            _digest(record['payload'].encode()) != record['sha256']):
        raise ValueError('NFL policy does not match its immutable pregame selection')
    payload = json.loads(record['payload'])
    if (payload.get('policy') != nfl_top_five.POLICY or any(
            payload.get(k) != record[k] for k in ('game_id', 'freeze_sha', 'selected_at'))):
        raise ValueError('NFL policy selection source changed')
    frozen = grading._freeze(group['freeze_sha'])
    seal = db.q1('SELECT frozen_at FROM nfl_prop_research_freezes WHERE freeze_sha=?', (group['freeze_sha'],))
    stamp = group['selected_at']
    if not seal or not seal['frozen_at'] <= stamp:
        raise ValueError('NFL policy selection predates freeze')
    universe = [dict(r) for r in frozen['projections'] if r['canonical_game_id'] == group['game_id']]
    ids = [r['quote_id'] for r in universe]
    if not universe or len(set(ids)) != len(ids):
        raise ValueError('NFL policy universe has missing or duplicate quotes')
    eligible, exclusions, availability = [], {}, {}
    pairs = {r['id'] for r in lab._pair_quotes(frozen['metadata']['source_batch_id'])[0]}
    for row in universe:
        quote = db.q1('SELECT * FROM nfl_prop_snapshots WHERE id=?', (row['quote_id'],))
        if (not quote or quote['line_type'] != 'main' or row['quote_id'] not in pairs or
                any(row[k] != quote[q] for k, q in (
                    ('canonical_game_id', 'canonical_game_id'), ('player_id', 'player_id'),
                    ('player_name', 'player_name'), ('market', 'market'), ('line', 'line'),
                    ('side', 'side'), ('price', 'price'), ('sportsbook', 'sportsbook'),
                    ('observed_at', 'retrieved_at'), ('kickoff_utc', 'kickoff_utc')))):
            raise ValueError('NFL policy frozen quote differs from main-line source')
        if not row['observed_at'] <= stamp < row['kickoff_utc']:
            exclusions[str(row['quote_id'])] = 'outside_pregame_capture'
            continue
        if stamp - row['observed_at'] > 86400:
            exclusions[str(row['quote_id'])] = 'stale_before_selection'
            continue
        identity = row['player_name'], row['market'], row['kickoff_utc']
        if identity not in availability:
            availability[identity] = nfl_prop_availability.view(row['player_name'], group['game_id'],
                row['kickoff_utc'], row['market'], now=stamp)
        status = availability[identity]
        if status['blocked']:
            exclusions[str(row['quote_id'])] = 'pregame_' + status['status']
        else:
            eligible.append(dict(row, team=quote['team']))
    # Verify the saved selector and availability too; it remains historical evidence,
    # while the incumbent is independently rescored on these same input bytes.
    selected = nfl_top_five.rank(eligible)
    clean = [{k: v for k, v in r.items() if k != 'team'} for r in selected]
    if clean != payload.get('selections'):
        raise ValueError('NFL historical top-five selector/availability cannot be reconstructed')
    if len(selected) != 5:
        raise ValueError('NFL common policy universe lacks five player markets')
    return universe, eligible, exclusions, frozen['metadata']


def _graded_universe(group, now):
    """Exclude a whole game for any unresolved eligible offer; never cherry-pick."""
    universe, eligible, exclusions, metadata = _pregame_universe(group)
    revisions = db.q('SELECT * FROM nfl_prop_grade_revisions WHERE freeze_sha=? AND graded_at<=? '
                     'ORDER BY quote_id,revision', (group['freeze_sha'], now))
    latest = {r['quote_id']: r for r in revisions}
    sources, evidence_cache, rows = {}, {}, []
    for frozen in eligible:
        saved = latest.get(frozen['quote_id'])
        grade = json.loads(saved['payload']) if saved else None
        if not grade or grade.get('research_statistical_result') not in ('won', 'lost', 'push'):
            return None, [], 'eligible_universe_unresolved_or_void'
        sha = saved['evidence_sha']
        if sha not in evidence_cache:
            proof = db.q1('SELECT * FROM nfl_prop_grade_evidence WHERE evidence_sha=?', (sha,))
            value = json.loads(proof['payload']) if proof else None
            if not proof or _digest((group['freeze_sha'] + grading._canonical(value)).encode()) != sha:
                raise ValueError('NFL policy grade evidence checksum mismatch')
            verified = grading._validate_evidence(value, {group['game_id']}, now)
            for source in (verified['stat_source'], verified['final_source']):
                if not source['file'].startswith('db:nfl_prop_grade_source_artifacts:'):
                    raise ValueError('NFL policy has no immutable official source receipt')
                archive = db.q1('SELECT captured_at FROM nfl_prop_grade_source_artifacts WHERE sha256=?', (source['sha256'],))
                if not archive or archive['captured_at'] > saved['graded_at']:
                    raise ValueError('NFL policy grade predates source receipt')
                sources[source['sha256']] = dict(kind='nfl_prop_grade_source_artifact',
                    sha256=source['sha256'], received_at=archive['captured_at'], role='validation')
            evidence_cache[sha] = verified
        proof = evidence_cache[sha]
        player = proof['players'].get(frozen['gsis_player_id'], {})
        actual = grade.get('actual_stat')
        if (proof['status'] != 'final' or player.get('status') != 'active' or
                player.get('stats', {}).get(frozen['market']) != actual or
                proof['retrieved'] > saved['graded_at'] or
                grade['research_statistical_result'] != grading._settlement(frozen['side'], actual, frozen['line'])):
            raise ValueError('NFL policy actual conflicts with official final evidence')
        rows.append(dict(frozen, game_id=group['game_id'], season=group.get('season', 2026),
            week=group.get('week', int(group['game_id'].split('-')[3])),
            kickoff_at=frozen['kickoff_utc'], actual=actual,
            result_observed_at=saved['graded_at'], frozen_metadata=metadata))
    detail = dict(game_id=group['game_id'], freeze_sha=group['freeze_sha'],
                  selected_at=group['selected_at'],
                  universe_quote_ids=sorted(r['quote_id'] for r in universe),
                  eligible_quote_ids=sorted(r['quote_id'] for r in eligible), exclusions=exclusions,
                  rows=rows)
    return detail, list(sources.values()), None


def _role_dataset(body, cutoff):
    with TemporaryDirectory(prefix='edgeterminal-nfl-role-fit-') as directory:
        path = Path(directory)
        (path / 'stats_player_week_2026.csv').write_bytes(body)
        datasets, history, hashes = lab.build_dataset(cutoff, stats_dir=path)
        raw, role_hashes = lab._read_stats(path)
        if hashes != role_hashes:
            raise ValueError('NFL role feature source revisions differ')
        return roles.augment_datasets(datasets, raw), history, hashes


def _attach_features(groups, catalog, sources):
    """Rebuild prior-only inputs from exact freeze-pinned bytes, never latest CSV."""
    cache = {}
    for group in groups:
        for row in group['rows']:
            metadata = row['frozen_metadata']
            hashes = metadata['input_sha256']
            digest = hashes.get('features:stats_player_week_2026.csv', hashes.get('stats_player_week_2026.csv'))
            source = catalog.get(digest)
            if not source or not source['available_at'] <= metadata['created_at'] < row['kickoff_at']:
                raise ValueError('NFL policy missing receipt-backed frozen feature bytes')
            key = digest, row['season'], row['week']
            if key not in cache:
                with TemporaryDirectory(prefix='edgeterminal-nfl-policy-input-') as directory:
                    path = Path(directory)
                    (path / 'stats_player_week_2026.csv').write_bytes(source['body'])
                    _, history, read_hashes = lab.build_dataset((row['season'], row['week']), path)
                    raw, _ = lab._read_stats(path)
                    for name, expected in read_hashes.items():
                        pinned = hashes.get('features:' + name, hashes.get(name))
                        if pinned != expected:
                            raise ValueError('NFL policy historical feature prefix hash changed')
                    inputs = lab._week3_features(history, row['season'], row['week'], path)
                    cache[key] = inputs, roles.current_features(inputs, raw, row['season'], row['week'])
                sources.append(dict(source['source'], role='validation'))
            inputs, role_values = cache[key]
            identity = row['gsis_player_id'], lab._team(row['team'])
            prepared = inputs.get(identity)
            if prepared is None:
                raise ValueError('NFL policy identity lacks frozen prior-only inputs')
            row['features'] = {**_feature_for(prepared, row['market']),
                **role_values[(*identity, row['market'])]}


def _score(rows, artifact=None):
    """One scorer dispatch for serving-equivalent rounded policy replay."""
    output = [dict(r) for r in rows]
    pinned = None
    for row in output:
        market = row['market']
        config = artifact['configurations'][market] if artifact else _baseline_config()
        if config.get('kind') == 'frozen_baseline':
            _check_baseline_config(config)
            if pinned is None:
                pinned = baseline.load_artifact()
            manifest, models = pinned
            base_config = manifest['configurations'][market]
            point = float(baseline._predict(models[market], [row], market, base_config['family'])[0])
            if base_config['probability_method'] == 'empirical':
                probabilities = baseline.probabilities(row['line'], point, base_config, market)
            else:
                over = lab._prob_over(row['line'], point, base_config['baseline_sigma'])
                probabilities = dict(over=over, under=1. - over, push=0.)
        else:
            point, probabilities = _json_predict(config, row['features'], market, float(row['line']))
        row.update(projection=round(point, 4), probability_over=round(probabilities['over'], 6),
                   probability_under=round(probabilities['under'], 6), probability_push=round(probabilities['push'], 6))
        if artifact and artifact.get('price_anchor'):
            from . import nfl_prop_anchor
            nfl_prop_anchor.apply(row, artifact)
    return output


def _conditional_side(row, side):
    mass = 1. - row['probability_push']
    if mass <= 0:
        raise ValueError('NFL decided probability has no non-push mass')
    value = row['probability_' + side] / mass
    if not math.isfinite(value) or value < 0 or value > 1. + 1.01e-6:
        raise ValueError('NFL conditional probability is invalid')
    return min(1., value)


def _paired(row, before, after):
    value = {key: row[key] for key in ('game_id', 'player_id', 'market', 'quote_id', 'line', 'side',
             'observed_at', 'kickoff_at', 'result_observed_at', 'actual')}
    value.update(baseline_prediction=before['projection'], candidate_prediction=after['projection'],
                 baseline_probability=_conditional_side(before, row['side']),
                 candidate_probability=_conditional_side(after, row['side']))
    return value


def _policy(group, incumbent, candidate):
    from .nfl_top_five import rank
    before = {r['quote_id']: r for r in _score(group['rows'], incumbent)}
    after = {r['quote_id']: r for r in _score(group['rows'], candidate)}
    result = {k: deepcopy(v) for k, v in group.items() if k != 'rows'}
    result['baseline'] = [_paired(r, r, after[r['quote_id']]) for r in rank(list(before.values()))]
    result['candidate'] = [_paired(r, before[r['quote_id']], r) for r in rank(list(after.values()))]
    if any(len(result[key]) != 5 for key in ('baseline', 'candidate')):
        raise ValueError('NFL replay must choose five distinct player/stat markets')
    return result


def _quote_units(groups):
    """One deterministic observed over quote per game/player/stat, before labels."""
    seen, output = set(), []
    rows = sorted((r for g in groups for r in g['rows']),
                  key=lambda r: (r['game_id'], str(r['player_id']), r['market'], r['sportsbook'], r['quote_id']))
    for row in rows:
        identity = row['game_id'], row['player_id'], row['market']
        if row['side'] == 'over' and identity not in seen:
            seen.add(identity); output.append(row)
    return output


def _incumbent(candidate_id):
    if candidate_id is None:
        return None
    from . import model_learning_store as store
    candidate = store._candidate(candidate_id)
    if not candidate or candidate['sport'] != 'NFL':
        raise ValueError('NFL policy incumbent is missing')
    return store._load(candidate)['artifact']


def verify_policy_universe(group, artifact=None):
    """Registry adapter: rederive frozen universe, eligibility, scores and top five."""
    detail, _, reason = _graded_universe(group, time.time())
    if reason:
        raise ValueError('NFL policy universe is no longer fully resolved: ' + reason)
    for key in ('universe_quote_ids', 'eligible_quote_ids', 'exclusions'):
        if detail[key] != group.get(key):
            raise ValueError('NFL policy frozen universe or pregame exclusions changed')
    sources = []
    _attach_features([detail], _catalog(time.time()), sources)
    incumbent = _incumbent(group.get('baseline_candidate_id'))
    if artifact is None:
        raise ValueError('NFL candidate artifact required for full policy re-verification')
    expected = _policy(detail, incumbent, artifact)
    for key in ('baseline', 'candidate'):
        if expected[key] != group[key]:
            raise ValueError('NFL policy scorer or top-five membership changed: ' + key)
    return True


def _fit_role_proposals(fit, residual_cal, incumbent):
    """Sparse markets retain exact configs instead of blocking supported markets."""
    proposal, coverage = {}, {}
    for market in lab.MARKETS:
        supported = bool(fit[market]) and len(residual_cal[market]) >= roles.MIN_CALIBRATION
        coverage[market] = dict(fit_rows=len(fit[market]),
            residual_calibration_rows=len(residual_cal[market]), supported=supported)
        proposal[market] = (roles.fit_market(fit[market], residual_cal[market], market) if supported
                           else deepcopy(incumbent['configurations'][market] if incumbent else _baseline_config()))
    return proposal, coverage


def build_quality_candidate(now: float | None = None) -> dict:
    """Evaluated outer weeks are spent for every lane, so the price anchor is tried first."""
    from . import nfl_prop_anchor
    anchor = nfl_prop_anchor.build(now)
    if anchor['status'] == 'ready':
        return anchor
    result = build_candidate(now)
    if result.get('status') != 'ready':
        result['anchor_proposal'] = {k: v for k, v in anchor.items() if k not in {'artifact', 'sources', 'evaluation'}}
    return result


def build_candidate(now: float | None = None) -> dict:
    """Propose sourced role updates chosen before an untouched full-policy holdout."""
    from . import model_learning_store as store, prop_calibration
    stamp = time.time() if now is None else now
    records, weeks, schedule = _quality_window(stamp)
    if len(weeks) < ROLE_PARAMETERS['validation_weeks']:
        return dict(status='waiting', reason='Three completed NFL weeks with immutable quote universes are required.',
                    protocol=QUALITY_PROTOCOL)
    season, first_week = min(weeks)
    if season != 2026 or first_week < 4:
        return dict(status='waiting', reason='NFL role fitting needs earlier whole fit, calibration and selection weeks.',
                    protocol=QUALITY_PROTOCOL)
    game_ids = [r['game_id'] for r in records]
    # Preflight before reading any outer grade labels, including rejected attempts.
    if not store.holdout_available('NFL', QUALITY_PROTOCOL, game_ids):
        return dict(status='waiting', reason='This NFL outer cohort was already evaluated; waiting for a wholly fresh window.',
                    protocol=QUALITY_PROTOCOL)
    start = min(_kickoff(g) for g in schedule.values()
                if (int(g['season']), int(g['week'])) == min(weeks))
    active = _validated_active()
    if active and (active['training_cutoff'] >= start or
                   active['artifact'].get('calibration_cutoff', active['training_cutoff']) >= start):
        return dict(status='waiting', reason='Current NFL baseline needs a new untouched whole-week validation window.',
                    protocol=QUALITY_PROTOCOL)
    incumbent = active['artifact'] if active else None
    baseline_id = active['candidate_id'] if active else None
    fit_boundary = season, first_week - 2
    selection_week = season, first_week - 1
    training_cutoff = min(_kickoff(g) for g in schedule.values()
        if (int(g['season']), int(g['week'])) == fit_boundary) - 1.
    probability_cutoff = min(_kickoff(g) for g in schedule.values()
        if (int(g['season']), int(g['week'])) == selection_week) - 1.
    calibration_cutoff = start - 1.
    sources, coverage = [], {}
    all_records = db.q('SELECT * FROM nfl_top_five_selections ORDER BY selected_at,game_id')
    inner, quoted_cal = [], []
    for record in all_records:
        parts = record['game_id'].split('-')
        week = int(parts[1]), int(parts[3])
        if week not in (fit_boundary, selection_week):
            continue
        record = dict(record, season=week[0], week=week[1])
        limit = probability_cutoff if week == fit_boundary else calibration_cutoff
        group, proof, reason = _graded_universe(record, limit)
        if reason:
            coverage[record['game_id']] = reason
            continue
        (quoted_cal if week == fit_boundary else inner).append(group)
        sources.extend(dict(s, role='calibration') for s in proof)
    cal_units, inner_units = _quote_units(quoted_cal), _quote_units(inner)
    quote_coverage = dict(calibration_games=len(quoted_cal), calibration_rows=len(cal_units),
                          market_selection_games=len(inner), market_selection_rows=len(inner_units))
    if not cal_units or not inner_units:
        return dict(status='waiting', reason='Earlier NFL actual-line calibration and inner market-choice quotes are missing.',
                    waiting_kind='coverage_wait', protocol=QUALITY_PROTOCOL,
                    quote_coverage=quote_coverage, excluded_games=coverage)
    catalog = _catalog(stamp)
    fit_snapshot = _current_snapshot(catalog, training_cutoff, season)
    cal_snapshot = _current_snapshot(catalog, probability_cutoff, season)
    _require_completed_history(fit_snapshot['body'], fit_boundary, schedule)
    _require_completed_history(cal_snapshot['body'], selection_week, schedule)
    fit, _, fit_hashes = _role_dataset(fit_snapshot['body'], fit_boundary)
    prior, _, cal_hashes = _role_dataset(cal_snapshot['body'], selection_week)
    residual_cal = {m: [r for r in prior[m] if (r['season'], r['week']) == fit_boundary] for m in lab.MARKETS}
    # The fixed opportunity scorer is fitted once. No family/alpha search touches
    # the inner selector or outer results; retained market configs are deep copies.
    proposal, fit_coverage = _fit_role_proposals(fit, residual_cal, incumbent)
    if not any(c['supported'] for c in fit_coverage.values()):
        return dict(status='waiting', reason='NFL role training/calibration sample coverage is incomplete.',
                    waiting_kind='coverage_wait', protocol=QUALITY_PROTOCOL, role_fit_coverage=fit_coverage,
                    quote_coverage=quote_coverage, excluded_games=coverage)
    _attach_features(quoted_cal + inner, catalog, sources)
    cal_units, inner_units = _quote_units(quoted_cal), _quote_units(inner)
    role_artifact = dict(configurations=proposal)
    role_cal = _score(cal_units, role_artifact)
    incumbent_cal = _score(cal_units, incumbent)
    incumbent_maps = {}
    for market in lab.MARKETS:
        def calibration_rows(scored):
            return [dict(row, probability_over=row['probability_over'],
                         probability_under=row['probability_under'], probability_push=row['probability_push'])
                    for row in scored if row['market'] == market]
        if fit_coverage[market]['supported']:
            proposal[market]['actual_line_calibration'] = prop_calibration.fit_calibrator(
                calibration_rows(role_cal), cutoff=probability_cutoff)
        incumbent_maps[market] = prop_calibration.fit_calibrator(
            calibration_rows(incumbent_cal), cutoff=probability_cutoff)
    before = {r['quote_id']: r for r in _score(inner_units, incumbent)}
    after = _score(inner_units, role_artifact)
    inner_rows = [_paired(row, before[row['quote_id']], row) for row in after]
    selection = prop_calibration.choose_market_updates(inner_rows, lab.MARKETS, cutoff=calibration_cutoff)
    changed = selection['changed_markets']
    if not changed:
        return dict(status='waiting', reason='No NFL market passed the separate inner actual-line improvement requirements.',
                    protocol=QUALITY_PROTOCOL, market_selection=selection, quote_coverage=quote_coverage, excluded_games=coverage)
    configs = {m: deepcopy(proposal[m] if m in changed else
        incumbent['configurations'][m] if incumbent else _baseline_config()) for m in lab.MARKETS}
    artifact = dict(schema=ROLE_SCHEMA, evaluation_family=QUALITY_PROTOCOL, parameters=ROLE_PARAMETERS,
        probability_contract='conditional_side_given_no_push',
        features=list(roles.FEATURES), configurations=configs, changed_markets=changed, role_fit_coverage=fit_coverage,
        ranking_changed=True, market_selection=selection, fit_boundary_week=list(fit_boundary),
        training_cutoff=training_cutoff, calibration_cutoff=calibration_cutoff,
        probability_calibration_cutoff=probability_cutoff,
        validation_description=f'2026 weeks {first_week}-{max(w[1] for w in weeks)}; full frozen offers; untouched',
        parameter_fingerprint=_digest(dict(parameters=ROLE_PARAMETERS, configurations=configs)),
        selector_sha256=_digest(Path(__file__).with_name('nfl_top_five.py').read_bytes()))
    # Only now, after the market choice is sealed, read outer labels and rebuild
    # features. These labels cannot change the selected market/configuration list.
    outer = []
    for record in records:
        group, proof, reason = _graded_universe(record, stamp)
        if reason:
            coverage[record['game_id']] = reason
        else:
            outer.append(group); sources.extend(proof)
    if len(outer) < 30 or sum(len(g['rows']) for g in outer) < 100:
        return dict(status='waiting', reason='Thirty wholly resolved common NFL offer universes are required.',
                    protocol=QUALITY_PROTOCOL, covered_games=len(outer), excluded_games=coverage)
    _attach_features(outer, catalog, sources)
    policies = []
    ablations = dict(role_only=[], calibration_only=[], combined=[])
    raw_role = dict(configurations={m: {k: v for k, v in c.items()
        if not (fit_coverage[m]['supported'] and k == 'actual_line_calibration')} for m, c in proposal.items()})
    for group in outer:
        policy = _policy(group, incumbent, artifact)
        policy['baseline_candidate_id'] = baseline_id
        policies.append(policy)
        units = _quote_units([group])
        baseline_units = {r['quote_id']: r for r in _score(units, incumbent)}
        for name, candidate in (('role_only', raw_role), ('combined', role_artifact)):
            ablations[name].extend(_paired(r, baseline_units[r['quote_id']], r) for r in _score(units, candidate))
        # Calibration-only ablation retains the exact incumbent selected side and
        # five-player/stat membership; it is diagnostic, never a new rank policy.
        scored = {r['quote_id']: r for r in _score(group['rows'], incumbent)}
        for selected in policy['baseline']:
            row = scored[selected['quote_id']]
            probability = prop_calibration.apply_calibrator(
                {s: row['probability_' + s] for s in ('over', 'under', 'push')}, incumbent_maps[row['market']])
            after_row = dict(row, **{'probability_' + s: round(probability[s], 6) for s in probability})
            ablations['calibration_only'].append(_paired(row, row, after_row))
    for role, hashes, snapshot in (('training', fit_hashes, fit_snapshot), ('calibration', cal_hashes, cal_snapshot)):
        for name, digest in hashes.items():
            item = snapshot if name.endswith('2026.csv') else catalog.get(digest)
            if item is None:
                raise ValueError('NFL role history has no immutable availability receipt')
            sources.append(dict(item['source'], role=role))
    sources.append(dict(kind='file', path=str(lab.SCHEDULE.relative_to(lab.ROOT)),
        sha256=_digest(lab.SCHEDULE.read_bytes()), received_at=lab.SCHEDULE.stat().st_mtime, role='training'))
    artifact['training'] = dict(
        fit_game_ids=sorted({_canonical_game(schedule[r['game_id']]) for rows in fit.values() for r in rows}),
        calibration_game_ids=sorted({_canonical_game(schedule[r['game_id']]) for rows in residual_cal.values() for r in rows}),
        fit_label_max_at=max(s['received_at'] for s in sources if s['role'] == 'training'),
        calibration_label_max_at=max(s['received_at'] for s in sources if s['role'] == 'calibration'))
    from . import nfl_top_five, nfl_prop_availability
    artifact['code_sha256'] = {str(Path(m.__file__).relative_to(lab.ROOT)): _digest(Path(m.__file__).read_bytes())
                               for m in (baseline, lab, roles, prop_calibration, nfl_top_five, nfl_prop_availability)}
    artifact['code_sha256']['edgeterminal/nfl_learning.py'] = _digest(Path(__file__).read_bytes())
    def ablation_metrics(rows):
        # The pure shared metric implementation is the same one used by the gate;
        # these outer ablations describe effects and never choose deployed markets.
        decided = [r for r in rows if r['actual'] != r['line']]
        return dict(n=len(decided), **(store._metrics(decided) if decided else {}))
    evaluation = dict(kind='actual_main_lines', validation_start=start,
        validation_end=max(r['result_observed_at'] for g in outer for r in g['rows']),
        calibration_cutoff=calibration_cutoff, baseline_candidate_id=baseline_id,
        rows=[r for p in policies for r in p['candidate']],
        selection_policy=dict(kind='common_full_offer_universe', games=policies),
        coverage=dict(included_games=len(outer), excluded_games=coverage),
        ablations={name: ablation_metrics(rows) for name, rows in ablations.items()})
    unique = {(s['kind'], s.get('path'), s['sha256'], s['role']): s for s in sources}
    sources = sorted(unique.values(), key=lambda s: (s['role'], s['kind'], s['sha256']))
    revision = _digest(dict(artifact=artifact, evaluation=evaluation, sources=sources))[:16]
    version = f'nfl-roles-2.2026w{max(w[1] for w in weeks):02d}-{revision}'
    artifact['version'] = version
    return dict(status='ready', sport='NFL', version=version, training_cutoff=training_cutoff,
                artifact=artifact, evaluation=evaluation, sources=sources)
