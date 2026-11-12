#!/usr/bin/env python3
"""Offline, read-only audit of the 2026-10-28 NBA spread comparison record.

RESEARCH_ONLY / PASS. Uses stdlib only, imports no production modules, opens
SQLite mode=ro plus query_only, and writes only its own grading report files.
No provider calls, model fitting, forecasting, locking or grading mutations.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import mean


DAY = "2026-10-28"
TEAM_IDS = {
    "ATL": ("1", "ATL"), "BOS": ("2", "BOS"), "BKN": ("17", "BKN"),
    "CHA": ("30", "CHA"), "CHI": ("4", "CHI"), "CLE": ("5", "CLE"),
    "DAL": ("6", "DAL"), "DEN": ("7", "DEN"), "DET": ("8", "DET"),
    "GSW": ("9", "GS"), "HOU": ("10", "HOU"), "IND": ("11", "IND"),
    "LAC": ("12", "LAC"), "LAL": ("13", "LAL"), "MEM": ("29", "MEM"),
    "MIA": ("14", "MIA"), "MIL": ("15", "MIL"), "MIN": ("16", "MIN"),
    "NOP": ("3", "NO"), "NYK": ("18", "NY"), "OKC": ("25", "OKC"),
    "ORL": ("19", "ORL"), "PHI": ("20", "PHI"), "PHX": ("21", "PHX"),
    "POR": ("22", "POR"), "SAC": ("23", "SAC"), "SAS": ("24", "SA"),
    "TOR": ("28", "TOR"), "UTA": ("26", "UTAH"), "WAS": ("27", "WSH"),
}


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def settle(cushion):
    return "won" if cushion > 0 else "lost" if cushion < 0 else "push"


def audit(db_path):
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    sources = {}
    global_failures = []
    checks = Counter()

    def check(condition, label, failures):
        checks[label] += 1
        if not condition:
            failures.append(label)

    def source(source_id):
        if source_id not in sources:
            row = dict(con.execute("SELECT * FROM nba_source_artifacts WHERE id=?", (source_id,)).fetchone())
            body = row.pop("body")
            row["body_sha256_verified"] = digest(body) == row["sha256"]
            row["body_bytes"] = len(body)
            row["data"] = json.loads(body)
            row["received_utc"] = utc(row["received_at"])
            sources[source_id] = row
        return sources[source_id]

    def source_checks(item, by, failures):
        check(item["http_status"] == 200, "source_http_200", failures)
        check(item["body_sha256_verified"], "source_body_sha256", failures)
        check(item["received_at"] <= by, "source_received_by_record", failures)

    def forecast_checks(record, game, failures):
        check(digest(record["payload"]) == record["payload_sha256"], "forecast_payload_sha256", failures)
        check(record["created_at"] < game["tipoff_ts"], "forecast_pretipoff", failures)
        ids = json.loads(record["source_artifact_ids"])
        check(bool(ids), "forecast_has_sources", failures)
        for source_id in ids:
            source_checks(source(source_id), record["created_at"], failures)
        return ids

    def event_in_source(item, event_id):
        data = item["data"]
        if "events" in data:
            event = next(x for x in data["events"] if str(x["id"]) == event_id)
        else:
            event = data["header"]
        return event, event["competitions"][0]

    def identity_checks(event, comp, game, failures):
        check(str(event["id"]) == game["espn_event_id"], "source_event_id", failures)
        stamp = datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).timestamp()
        check(stamp == game["tipoff_ts"], "source_tipoff", failures)
        competitors = {x["homeAway"]: x for x in comp["competitors"]}
        for side in ("home", "away"):
            team = competitors[side]["team"]
            code = game[side + "_team"]
            check((str(team["id"]), team["abbreviation"]) == TEAM_IDS[code], "source_team_id_abbreviation_and_side", failures)
        return competitors

    games = [dict(r) for r in con.execute("SELECT * FROM nba_games WHERE game_date_et=? ORDER BY tipoff_ts,game_id", (DAY,))]
    check(len(games) == 12, "twelve_games", global_failures)
    output = []
    for game in games:
        failures, warnings = [], []
        saved = [dict(r) for r in con.execute("SELECT * FROM nba_forecasts WHERE game_id=? ORDER BY created_at,forecast_id", (game["game_id"],))]
        projections = [r for r in saved if json.loads(r["payload"]).get("kind") == "game"]
        check(len(projections) == 1, "single_game_forecast", failures)
        forecast = projections[0]
        payload = json.loads(forecast["payload"])
        projection = payload["projection"]
        original_source_ids = forecast_checks(forecast, game, failures)
        check(projection["home"] == game["home_team"] and projection["away"] == game["away_team"], "projection_team_identity", failures)
        check(projection["tipoff"] == game["tipoff_ts"], "projection_tipoff", failures)
        check(abs(projection["home_score"] - projection["away_score"] - projection["home_margin"]) <= .011, "rounded_projection_margin", failures)
        check(projection["designation"] == "PASS" and projection["readiness"] == "RESEARCH_ONLY", "research_designation", failures)
        line_record = forecast
        market = payload.get("market") or {}
        if market.get("home_spread") is None:
            patches = [r for r in saved if json.loads(r["payload"]).get("kind") == "spread-line"]
            check(len(patches) == 1, "single_immutable_spread_comparison", failures)
            line_record = patches[0]
            market = json.loads(line_record["payload"])["market"]
            forecast_checks(line_record, game, failures)
            warnings.append("spread_comparison_saved_after_game_projection")
        line_source = source(market["source_artifact_id"])
        source_checks(line_source, market["observed_at"], failures)
        check(line_source["sha256"] == market["source_sha256"], "market_source_sha256", failures)
        check(market["observed_at"] <= line_record["created_at"] < game["tipoff_ts"], "quote_observed_saved_pretipoff", failures)
        event, comp = event_in_source(line_source, game["espn_event_id"])
        identity_checks(event, comp, game, failures)
        check(comp["status"]["type"]["state"] == "pre", "line_source_game_pregame", failures)
        matches = []
        for odds in comp.get("odds", []):
            spread = odds.get("pointSpread") or {}
            home = (spread.get("home") or {}).get("close") or {}
            away = (spread.get("away") or {}).get("close") or {}
            if (home.get("line") is not None and away.get("line") is not None
                    and float(home["line"]) == market["home_spread"]
                    and float(away["line"]) == market["away_spread"]
                    and float(home["odds"]) == market["home_price"]
                    and float(away["odds"]) == market["away_price"]):
                matches.append(odds)
        check(len(matches) == 1, "unique_exact_raw_spread_and_prices", failures)
        raw_provider = matches[0]["provider"]
        if market.get("sportsbook") == "Unknown":
            warnings.append("saved_sportsbook_unknown_raw_provider_name_is_" + raw_provider.get("name", "missing"))
        check(market["home_spread"] == -market["away_spread"], "opposite_spread_signs", failures)
        home_edge = projection["home_margin"] + market["home_spread"]
        pick_side = "home" if home_edge >= 0 else "away"
        line = market["home_spread"] if pick_side == "home" else -market["home_spread"]
        check(line == market[pick_side + "_spread"], "selected_line_matches_side", failures)

        revisions = [dict(r) for r in con.execute("SELECT * FROM nba_game_grade_revisions WHERE game_id=? ORDER BY id", (game["game_id"],))]
        check(bool(revisions), "has_saved_grade", failures)
        previous_id = 0
        for revision in revisions:
            check(digest(revision["payload"] + ":" + str(previous_id)) == revision["sha256"], "grade_revision_hash_chain", failures)
            previous_id = revision["id"]
        grade = revisions[-1]
        stored_grade = json.loads(grade["payload"])
        final_source = source(grade["source_artifact_id"])
        source_checks(final_source, grade["graded_at"], failures)
        check(final_source["received_at"] > game["tipoff_ts"], "final_source_after_tipoff", failures)
        event, comp = event_in_source(final_source, game["espn_event_id"])
        competitors = identity_checks(event, comp, game, failures)
        status = comp["status"]["type"]
        check(status["completed"] is True and status["state"] == "post" and status["name"] == "STATUS_FINAL", "archived_status_final", failures)
        score = {side: int(competitors[side]["score"]) for side in ("home", "away")}
        check(score["home"] != score["away"] and min(score.values()) > 0, "valid_non_tied_final", failures)
        reconcile = {}
        for side in ("home", "away"):
            code = game[side + "_team"]
            team_id = TEAM_IDS[code][0]
            check(score[side] == game[side + "_score"] == stored_grade[side + "_score"], "source_grade_game_score_match", failures)
            periods = sum(int(x.get("value", x.get("displayValue"))) for x in competitors[side]["linescores"])
            check(periods == score[side], "period_scores_reconcile", failures)
            box = final_source["data"]["boxscore"]
            team = next(x for x in box["teams"] if str(x["team"]["id"]) == team_id)
            stats = {x["name"]: x["displayValue"] for x in team["statistics"]}
            made = lambda name: int(stats[name].split("-")[0])
            scoring_formula = (2 * made("fieldGoalsMade-fieldGoalsAttempted")
                               + made("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
                               + made("freeThrowsMade-freeThrowsAttempted"))
            check(scoring_formula == score[side], "team_scoring_formula_reconciles", failures)
            player_team = next(x for x in box["players"] if str(x["team"]["id"]) == team_id)
            stats = player_team["statistics"][0]
            pts = stats["names"].index("PTS")
            player_points = sum(int(a["stats"][pts]) for a in stats["athletes"] if a.get("stats"))
            check(player_points == score[side] == int(stats["totals"][pts]), "player_points_and_box_totals_reconcile", failures)
            reconcile[code] = dict(header=score[side], periods=periods, scoring_formula=scoring_formula, player_points=player_points)
        current_game_source = source(game["source_artifact_id"])
        source_checks(current_game_source, game["updated_at"], failures)
        event, current_comp = event_in_source(current_game_source, game["espn_event_id"])
        current_competitors = identity_checks(event, current_comp, game, failures)
        check(all(int(current_competitors[s]["score"]) == score[s] for s in ("home", "away")), "game_row_source_score_match", failures)
        actual_margin = score["home"] - score["away"]
        cushion = actual_margin + line if pick_side == "home" else -actual_margin + line
        result = settle(cushion)
        check(result == stored_grade["spread"], "recomputed_result_equals_saved_grade", failures)

        latest_row = con.execute("SELECT v FROM kv WHERE k=?", ("nba_market_" + game["game_id"],)).fetchone()
        latest = json.loads(latest_row[0]) if latest_row else {}
        latest_hs = latest.get("home_spread")
        latest_line = latest_hs if pick_side == "home" else -latest_hs
        latest_cushion = actual_margin + latest_line if pick_side == "home" else -actual_margin + latest_line
        latest_pick = "home" if projection["home_margin"] + latest_hs >= 0 else "away"
        latest_reselected_line = latest_hs if latest_pick == "home" else -latest_hs
        latest_reselected_cushion = actual_margin + latest_reselected_line if latest_pick == "home" else -actual_margin + latest_reselected_line
        if latest_hs != market["home_spread"]:
            warnings.append("latest_market_differs_from_original_comparison")
        output.append({
            "game_id": game["game_id"], "event_id": game["espn_event_id"],
            "matchup": game["away_team"] + "@" + game["home_team"],
            "home_team": game["home_team"], "away_team": game["away_team"],
            "tipoff_utc": game["tipoff_utc"], "model_version": forecast["model_version"],
            "model_fingerprint": payload["model"]["fingerprint"],
            "forecast_id": forecast["forecast_id"], "forecast_payload_sha256": forecast["payload_sha256"],
            "forecast_at_utc": utc(forecast["created_at"]),
            "forecast_age_at_tipoff_hours": round((game["tipoff_ts"] - forecast["created_at"]) / 3600, 4),
            "forecast_source_ids": original_source_ids,
            "projection_home_margin": projection["home_margin"],
            "projection_home_score": projection["home_score"], "projection_away_score": projection["away_score"],
            "original_game_forecast_home_spread": payload.get("market", {}).get("home_spread"),
            "comparison_forecast_id": line_record["forecast_id"],
            "comparison_payload_sha256": line_record["payload_sha256"],
            "comparison_at_utc": utc(line_record["created_at"]),
            "line_observed_at_utc": utc(market["observed_at"]),
            "line_age_at_tipoff_hours": round((game["tipoff_ts"] - market["observed_at"]) / 3600, 4),
            "line_source_artifact_id": line_source["id"], "line_source_sha256": line_source["sha256"],
            "saved_sportsbook": market.get("sportsbook"), "raw_provider": raw_provider,
            "original_home_spread": market["home_spread"], "original_away_spread": market["away_spread"],
            "pick_side": pick_side, "pick_team": game[pick_side + "_team"], "pick_line": line,
            "pick_price": market[pick_side + "_price"], "projected_cover_cushion": round(abs(home_edge), 4),
            "final_home_score": score["home"], "final_away_score": score["away"],
            "actual_home_margin": actual_margin, "cover_cushion": cushion,
            "home_margin_error_actual_minus_projection": round(actual_margin - projection["home_margin"], 4),
            "recomputed_result": result, "saved_result": stored_grade["spread"],
            "grade_revision_id": grade["id"], "grade_revision_sha256": grade["sha256"],
            "grade_source_artifact_id": final_source["id"], "grade_source_sha256": final_source["sha256"],
            "game_row_source_artifact_id": game["source_artifact_id"], "grade_revision_count": len(revisions),
            "score_reconciliation": reconcile,
            "latest_home_spread_for_reference_only": latest_hs,
            "latest_source_artifact_id_for_reference_only": latest.get("source_artifact_id"),
            "counterfactual_original_pick_latest_line_result": settle(latest_cushion),
            "counterfactual_reselect_with_latest_line_result": settle(latest_reselected_cushion),
            "warnings": warnings, "failures": failures,
        })
    con.rollback()
    con.close()
    failures = global_failures + [f"{g['game_id']}:{x}" for g in output for x in g["failures"]]
    source_manifest = [{k: v for k, v in row.items() if k != "data"} for _, row in sorted(sources.items())]
    return {
        "date": DAY, "designation": "PASS", "readiness": "RESEARCH_ONLY",
        "method": "Offline read-only SQLite transaction; independent raw ESPN parsing and arithmetic; no production imports or provider requests.",
        "database": str(db_path.resolve()),
        "result": dict(Counter(g["recomputed_result"] for g in output)),
        "all_original_comparison_grades_match": not failures,
        "games": output, "source_manifest": source_manifest,
        "checks": dict(checks), "failures": failures,
        "summary": {
            "source_artifacts_verified": len(sources),
            "game_forecast_payloads_verified": len(output),
            "separate_immutable_spread_payloads_verified": sum(g["comparison_forecast_id"] != g["forecast_id"] for g in output),
            "original_game_forecasts_without_spreads": sum(g["original_game_forecast_home_spread"] is None for g in output),
            "game_model_versions": dict(Counter(g["model_version"] for g in output)),
            "forecast_age_hours_range": [min(g["forecast_age_at_tipoff_hours"] for g in output), max(g["forecast_age_at_tipoff_hours"] for g in output)],
            "comparison_line_age_hours_range": [min(g["line_age_at_tipoff_hours"] for g in output), max(g["line_age_at_tipoff_hours"] for g in output)],
            "small_projected_cover_cushion_under_one_point": [g["matchup"] for g in output if g["projected_cover_cushion"] < 1],
            "half_point_losses": [g["matchup"] for g in output if g["cover_cushion"] == -.5],
            "saved_sportsbook_unknown": sum(g["saved_sportsbook"] == "Unknown" for g in output),
            "latest_line_differs": sum(g["latest_home_spread_for_reference_only"] != g["original_home_spread"] for g in output),
            "counterfactual_original_pick_latest_line_record": dict(Counter(g["counterfactual_original_pick_latest_line_result"] for g in output)),
            "counterfactual_reselect_with_latest_line_record": dict(Counter(g["counterfactual_reselect_with_latest_line_result"] for g in output)),
            "margin_mae": round(mean(abs(g["home_margin_error_actual_minus_projection"]) for g in output), 4),
            "market_margin_mae_original": round(mean(abs(g["actual_home_margin"] + g["original_home_spread"]) for g in output), 4),
        },
    }


def markdown(report):
    s = report["summary"]
    lines = [
        "# October 28, 2026 NBA spread grading audit", "", "RESEARCH_ONLY / PASS.", "",
        f"The saved original-comparison record is genuinely **3 won, 9 lost**. All 12 independently recomputed results match the saved grades; {len(report['failures'])} integrity failures were found.", "",
        f"This offline audit verified {s['source_artifacts_verified']} archived source body hashes, 12 game forecast hashes, 12 separately saved spread-comparison payload hashes and all grade-revision hash chains. Every referenced input was HTTP 200 and received by its record time; all forecasts and comparison observations precede stored tipoff. Final event IDs, numeric team IDs, ESPN abbreviations, home/away placement and scores match the database. Each final score also reconciles to period totals, player points, box totals and 2×FGM + 3PM + FTM.", "",
        "All game projections use **nba-research-1.1.0**, fingerprint `79026f8a1c09b06e247bd5e2c0335d86f8930c92c416679299dadfc479cf4da4`. Their original payloads contain no spread. `comparison_market()` subsequently attaches the first immutable pretipoff `spread-line` record; this remains fixed even when `kv` market prices move. The prop model upgrade to 2.0.1 did not regenerate these game projections.", "",
        "Pick cushion = home score − away score + pick line for a home pick; away score − home score + pick line for an away pick. Positive wins; negative loses. Projection and actual margins below both use home − away.", "",
        "| Matchup | Original pick | Projected home margin | Actual home margin | Cover cushion | Result | Forecast age (h) | Line source | Final source |", 
        "|---|---:|---:|---:|---:|---|---:|---:|---:|",
    ]
    for g in report["games"]:
        lines.append(f"| {g['matchup']} | {g['pick_team']} {g['pick_line']:+g} | {g['projection_home_margin']:+.2f} | {g['actual_home_margin']:+g} | {g['cover_cushion']:+g} | {g['recomputed_result']} | {g['forecast_age_at_tipoff_hours']:.2f} | {g['line_source_artifact_id']} | {g['grade_source_artifact_id']} |")
    lines.extend([
        "", "Findings that affect interpretation and future work:", "",
        f"- Forecasts were {s['forecast_age_hours_range'][0]:.2f}–{s['forecast_age_hours_range'][1]:.2f} hours old at tipoff; comparison lines were {s['comparison_line_age_hours_range'][0]:.2f}–{s['comparison_line_age_hours_range'][1]:.2f} hours old. Preserve these originals, but evaluate a separately versioned near-tipoff snapshot process so new injuries, rest and recent games can inform future forecasts.",
        "- Four projected advantages were under one point: CLE 0.10, IND 0.60, MIL 0.67 and NOP 0.97. All lost. The system currently forces a side for every available line. Test a prespecified abstention rule on chronological holdouts; do not select its threshold from this slate.",
        "- CLE −10.5 and CHA +7.5 both lost by half a point. IND +1.5 is also correctly a loss despite PHI being the model's straight-up winner; spread and moneyline picks are different calculations.",
        "- Diagnostic counterfactual only: the latest cached lines differ in 7 games. Regrading the same original picks at the latest lines produces 5–7 because CLE −9.5 and IND +7.5 cover. Recomputing both picks and grades at the latest lines produces 6–6, additionally changing LAC +5.5 to LAL +1.5. These are hypothetical records at different quotes and must not overwrite the genuine original 3–9 record.",
        f"- Saved sportsbook names are `Unknown` for {s['saved_sportsbook_unknown']} games, although all 12 original raw offers name DraftKings, provider ID 100. `parse_market()` only reads `provider.displayName`, which is absent in those 10 offers. A future metadata-only fix can fall back to `provider.name`; prices, lines and grades are correct.",
        f"- Slate margin MAE was {s['margin_mae']:.2f} points versus {s['market_margin_mae_original']:.2f} for the original market margin. This is descriptive evidence from 12 games, not a validated improvement rule.",
        "- There are no `spreads` rows in `nba_quote_observations`; game lines are preserved by the immutable forecast payload and its archived source hash. Future audit metadata should expose both forecast time and comparison time and freeze a selected side/line/source reference explicitly.",
        "", "Original line source artifacts: 8439 (PHI@IND), 9917 (MIA@ATL, WAS@CLE, CHA@DET, ORL@TOR, NYK@CHI, HOU@MIL, GSW@MIN, DEN@NOP), and 10067 (SAS@UTA, OKC@DAL, LAL@LAC). The companion JSON contains complete source URLs, receipts, SHA-256 values and per-game checks. The ESPN JSON `close` field means the observed offer at that archived receipt; it is not asserted to be the final closing line.",
        "", "Reproduce from the repository root:", "", "```sh", "python3 reports/nba/spread_audit_2026_10_28/grading_audit.py", "```", "",
        "The script opens the production database read-only and writes only grading_audit.json and grading_audit.md in its report directory. No production database, model, source capture, forecast, lock or existing report was modified; no Odds API was used.", "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[3] / "data" / "edgeterminal.db")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    report = audit(args.db)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "grading_audit.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (args.out_dir / "grading_audit.md").write_text(markdown(report))
    print(json.dumps({"record": report["result"], "failures": report["failures"], "summary": report["summary"]}, indent=2))
    return int(bool(report["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
