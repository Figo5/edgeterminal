"""Append-only, research-only grading of immutable NFL player-prop freezes.

No provider call, pick, or product recommendation occurs here. A final game
status plus player-level official-stat evidence is required before settlement.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from collections import defaultdict
from pathlib import Path

from . import db, nfl_prop_lab, nfl_prop_rules

POSTGAME_DELAY_S = 4 * 3600
REPORT_PATH = nfl_prop_lab.ROOT / "data/research/nfl_prop_calibration_2026w3.json"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _freeze(sha: str) -> dict:
    row = db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?", (sha,))
    if not row or hashlib.sha256(row["payload"].encode()).hexdigest() != sha:
        raise ValueError("missing or changed immutable prop freeze")
    payload = json.loads(row["payload"])
    quotes = payload.get("projections") or []
    if not quotes or len({r["quote_id"] for r in quotes}) != len(quotes):
        raise ValueError("invalid prop freeze quote identities")
    return payload


def _number(value: object, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a stat")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid official stat") from exc
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ValueError("invalid official stat")
    return number


def _documented_participation(row: dict) -> bool:
    """A stat row alone does not prove that an all-zero player dressed."""
    fields = (*nfl_prop_lab.USAGE, "completions", "sacks_suffered",
              "kickoff_returns", "punt_returns", "def_tackles_solo",
              "def_tackle_assists", "def_sacks", "def_qb_hits")
    for field in fields:
        value = row.get(field)
        if value not in (None, ""):
            try:
                if math.isfinite(float(value)) and float(value) != 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _source_bytes(file: str) -> bytes | None:
    if file.startswith("db:nfl_prop_grade_source_artifacts:"):
        digest = file.rsplit(":", 1)[-1]
        row = db.q1("SELECT payload FROM nfl_prop_grade_source_artifacts WHERE sha256=?", (digest,))
        return bytes(row["payload"]) if row else None
    if file.startswith("db:nfl_research_grades:"):
        _, _, game_id, revision = file.split(":", 3)
        row = db.q1("SELECT payload FROM nfl_research_grades WHERE game_id=? AND revision=?",
                    (game_id, int(revision)))
        return row["payload"].encode() if row else None
    path = Path(file)
    return path.read_bytes() if path.is_file() else None


def _source(record: object, now: float, label: str) -> dict:
    if not isinstance(record, dict):
        raise ValueError(f"{label} requires source provenance")
    name, file = record.get("source"), record.get("file")
    digest = record.get("sha256")
    when = _number(record.get("retrieved_at"))
    if not isinstance(name, str) or not name or not isinstance(file, str) or not file:
        raise ValueError(f"{label} needs source and file")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{label} needs SHA-256")
    if when > now:
        raise ValueError(f"{label} is from the future")
    body = _source_bytes(file)
    if body is None or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError(f"{label} source file/hash mismatch")
    return record


def _validate_evidence(evidence: dict, frozen_games: set[str], now: float) -> dict:
    if not isinstance(evidence, dict) or evidence.get("game_id") not in frozen_games:
        raise ValueError("evidence game is not in prop freeze")
    status = str(evidence.get("game_status") or "").lower()
    if status not in {"final", "postponed", "suspended", "canceled"}:
        raise ValueError("unrecognized game status")
    final_source = _source(evidence.get("final_game_source"), now, "final game")
    stat_source = _source(evidence.get("player_stat_source"), now, "player stat")
    score = evidence.get("final_score")
    if status == "final":
        if (not isinstance(score, dict) or set(score) != {"home", "away"} or
                any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in score.values())):
            raise ValueError("final game needs official final score")
    retrieved = _number(evidence.get("result_retrieved_at"))
    kickoff = _number(evidence.get("actual_kickoff_utc"))
    if retrieved > now or (kickoff > now and status in {"final", "canceled"}):
        raise ValueError("grade evidence is from the future")
    if retrieved < max(final_source["retrieved_at"], stat_source["retrieved_at"]):
        raise ValueError("grade evidence predates its sources")
    if status == "final" and (final_source["retrieved_at"] < kickoff or
                              stat_source["retrieved_at"] < kickoff):
        raise ValueError("final evidence predates kickoff")
    players = evidence.get("players")
    if not isinstance(players, list):
        raise ValueError("grade evidence needs a player list")
    found = {}
    for player in players:
        if not isinstance(player, dict) or not isinstance(player.get("gsis_player_id"), str):
            raise ValueError("malformed player evidence")
        player_status = player.get("status")
        if player_status not in {"active", "inactive", "unknown"}:
            raise ValueError("malformed player status")
        if player["gsis_player_id"] in found:
            raise ValueError("duplicate official player identity")
        stats = player.get("stats")
        if not isinstance(stats, dict):
            raise ValueError("malformed player stats")
        for market, value in stats.items():
            if market not in nfl_prop_lab.MARKETS:
                raise ValueError("unsupported official player stat")
            _number(value, nonnegative=market != "rushing_yards")
        if player_status == "inactive" or (player_status == "active" and
                any(float(value) == 0 for value in stats.values())):
            participation = player.get("participation_evidence")
            if not isinstance(participation, dict):
                raise ValueError("zero/inactive player needs participation evidence")
            _source(participation, now, "participation")
            if participation["retrieved_at"] > retrieved:
                raise ValueError("grade evidence predates participation source")
            if not isinstance(participation.get("active_roster"), bool) or not isinstance(participation.get("snap_played"), bool):
                raise ValueError("participation needs active roster and snap evidence")
            if player_status == "inactive" and (participation["active_roster"] or participation["snap_played"]):
                raise ValueError("inactive conflicts with participation")
        found[player["gsis_player_id"]] = player
    return {"status": status, "kickoff": kickoff, "retrieved": retrieved,
            "players": found, "final_score": score,
            "final_source": final_source, "stat_source": stat_source}


def _settlement(side: str, actual: float, line: float) -> str:
    if actual == line:
        return "push"
    if side == "over":
        return "won" if actual > line else "lost"
    if side == "under":
        return "won" if actual < line else "lost"
    raise ValueError("invalid frozen prop side")


def implied_probability(price: int) -> float:
    if isinstance(price, bool) or not isinstance(price, int) or abs(price) < 100:
        raise ValueError("invalid frozen American price")
    return -price / (-price + 100) if price < 0 else 100 / (price + 100)


def _grade_row(quote: dict, evidence: dict, checked: dict, now: float,
               rules_sha: str, rules: dict) -> tuple[dict | None, str | None]:
    status = checked["status"]
    player = checked["players"].get(quote["gsis_player_id"])
    if status == "canceled":
        actual, result, player_status = None, "void", "not_applicable"
    elif player is None or player["status"] == "unknown":
        return None, "pending_missing_stat"
    elif player["status"] == "inactive":
        actual, result, player_status = None, "void", "inactive"
    elif quote["market"] not in player["stats"]:
        return None, "pending_missing_stat"
    else:
        actual = _number(player["stats"][quote["market"]],
                         nonnegative=quote["market"] != "rushing_yards")
        result = _settlement(quote["side"], actual, quote["line"])
        player_status = "active"
    participation = player.get("participation_evidence") if player else None
    if player_status == "active" and participation is None:
        # Nonzero official offensive production proves participation.
        participation = {"active_roster": True, "snap_played": True,
                         "source": checked["stat_source"]["source"],
                         "file": checked["stat_source"]["file"],
                         "sha256": checked["stat_source"]["sha256"],
                         "retrieved_at": checked["stat_source"]["retrieved_at"]}
    book = quote["sportsbook"]
    interpretation, scenario = nfl_prop_rules.interpret(book, result, player_status,
                                                        participation, rules)
    if player_status == "inactive":
        participation_class = "confirmed_inactive"
    elif player_status == "not_applicable":
        participation_class = "game_canceled"
    elif actual == 0 and participation and participation.get("snap_played") is False:
        participation_class = "active_zero_no_snap"
    elif actual == 0:
        participation_class = "participated_target_stat_zero"
    else:
        participation_class = "participated_stat_recorded"
    signed = quote["projection"] - actual if actual is not None else None
    probability = quote["probability_over"] if quote["side"] == "over" else quote["probability_under"]
    grade = {"quote_id": quote["quote_id"], "canonical_game_id": quote["canonical_game_id"],
             "player_id": quote["player_id"], "gsis_player_id": quote["gsis_player_id"],
             "player_name": quote["player_name"], "market": quote["market"],
             "actual_stat": actual, "projection": quote["projection"],
             "absolute_error": abs(signed) if signed is not None else None,
             "signed_error": signed, "frozen_line": quote["line"],
             "frozen_side": quote["side"], "frozen_price": quote["price"],
             "implied_probability": implied_probability(quote["price"]),
             "model_probability": probability, "model_probability_over": quote["probability_over"],
             "result": result, "player_status": player_status, "game_status": status,
             "participation_class": participation_class,
             "final_score": checked["final_score"],
             "research_statistical_result": result,
             "sportsbook_settlement_interpretation": interpretation,
             "settlement_scenario": scenario,
             "settlement_basis": "conditional_research_only_not_actual_book_settlement",
             "rules_sha256": rules_sha,
             "rules_version": rules["version"],
             "rule_snapshot": rules["books"].get(book),
             "participation_evidence": participation,
             "sportsbook": quote["sportsbook"], "quote_observed_at": quote["observed_at"],
             "frozen_kickoff_utc": quote["kickoff_utc"],
             "actual_kickoff_utc": checked["kickoff"], "model_version": quote["model_version"],
             "result_source": checked["stat_source"]["source"],
             "result_retrieved_at": checked["retrieved"], "graded_at": now}
    grade["final_game_source"] = checked["final_source"]
    grade["player_stat_source"] = checked["stat_source"]
    return grade, None


def grade_freeze(freeze_sha: str, evidence: list[dict], now: float | None = None,
                 rules_path: str | Path = nfl_prop_rules.DEFAULT_RULES) -> dict:
    """Append evidence and only changed grades; delay and missing data fail closed."""
    now = time.time() if now is None else now
    frozen = _freeze(freeze_sha)
    rules_sha, rules = nfl_prop_rules.load_rules(rules_path)
    quotes = frozen["projections"]
    by_game: dict[str, list[dict]] = defaultdict(list)
    for quote in quotes:
        by_game[quote["canonical_game_id"]].append(quote)
    if not isinstance(evidence, list):
        raise ValueError("grade evidence must be a list")
    checked = [_validate_evidence(item, set(by_game), now) for item in evidence]
    if len({item["game_id"] for item in evidence}) != len(evidence):
        raise ValueError("duplicate game evidence")
    counts = {"freeze_sha": freeze_sha, "evidence_seen": len(evidence),
              "new_revisions": 0, "corrections": 0, "pending_delay": 0,
              "pending_game": 0, "pending_missing_stat": 0}
    with db.tx() as c:
        for item, state in zip(evidence, checked):
            refs = [state["final_source"], state["stat_source"]]
            refs.extend(player["participation_evidence"] for player in state["players"].values()
                        if "participation_evidence" in player)
            for ref in refs:
                raw = _source_bytes(ref["file"])
                if raw is None or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                    raise ValueError("grade source changed before archival")
                c.execute("INSERT OR IGNORE INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)",
                          (ref["sha256"], ref["file"], now, raw))
            body = _canonical(item)
            evidence_sha = hashlib.sha256((freeze_sha + body).encode()).hexdigest()
            c.execute("INSERT OR IGNORE INTO nfl_prop_grade_evidence VALUES(?,?,?,?,?,?,?,?)",
                      (evidence_sha, freeze_sha, item["game_id"], state["status"],
                       state["stat_source"]["source"], state["retrieved"], now, body))
            if state["status"] in {"postponed", "suspended"}:
                counts["pending_game"] += len(by_game[item["game_id"]])
                continue
            if now < state["kickoff"] + POSTGAME_DELAY_S:
                counts["pending_delay"] += len(by_game[item["game_id"]])
                continue
            for quote in by_game[item["game_id"]]:
                grade, reason = _grade_row(quote, item, state, now, rules_sha, rules)
                if grade is None:
                    counts[reason] += 1
                    continue
                old = c.execute("SELECT revision,payload FROM nfl_prop_grade_revisions WHERE "
                    "freeze_sha=? AND quote_id=? ORDER BY revision DESC LIMIT 1",
                    (freeze_sha, quote["quote_id"])).fetchone()
                if old:
                    previous = json.loads(old["payload"])
                    outcome_changed = (previous["actual_stat"], previous["result"],
                                       previous["player_status"]) != (
                                           grade["actual_stat"], grade["result"],
                                           grade["player_status"])
                    if outcome_changed or previous.get("settlement_scenario") == "stat_correction_resettlement_unverified":
                        # A later official-stat change updates research truth, but the
                        # actual bet's prior settlement and resettlement are unknown.
                        grade["sportsbook_settlement_interpretation"] = "undetermined"
                        grade["settlement_scenario"] = "stat_correction_resettlement_unverified"
                    if (previous["actual_stat"], previous["result"], previous["player_status"],
                        previous.get("sportsbook_settlement_interpretation"), previous.get("rules_sha256"),
                        previous.get("final_score")) == (
                        grade["actual_stat"], grade["result"], grade["player_status"],
                        grade["sportsbook_settlement_interpretation"], grade["rules_sha256"],
                        grade["final_score"]):
                        continue
                revision = old["revision"] + 1 if old else 1
                grade["revision"] = revision
                grade["previous_revision"] = old["revision"] if old else None
                grade["previous_evidence_sha"] = previous.get("evidence_sha") if old else None
                grade["evidence_sha"] = evidence_sha
                c.execute("INSERT INTO nfl_prop_grade_revisions VALUES(?,?,?,?,?,?)",
                          (freeze_sha, quote["quote_id"], revision, evidence_sha, now,
                           _canonical(grade)))
                counts["new_revisions"] += 1
                if old:
                    counts["corrections"] += 1
    return counts


def _latest(freeze_sha: str) -> list[dict]:
    rows = db.q("SELECT payload FROM nfl_prop_grade_revisions g WHERE freeze_sha=? AND "
                "revision=(SELECT MAX(z.revision) FROM nfl_prop_grade_revisions z WHERE "
                "z.freeze_sha=g.freeze_sha AND z.quote_id=g.quote_id)", (freeze_sha,))
    return [json.loads(row["payload"]) for row in rows]


def _record(rows: list[dict], field: str = "result") -> dict:
    output = {result: sum(row.get(field) == result for row in rows)
              for result in ("won", "lost", "push", "void", "undetermined")}
    output["n"] = len(rows)
    return output


def _calibration(rows: list[dict]) -> dict:
    usable = [r for r in rows if r["actual_stat"] is not None and r["result"] != "push" and
              r["frozen_side"] == "over"]
    table = []
    for i in range(10):
        items = [r for r in usable if min(int(r["model_probability_over"] * 10), 9) == i]
        table.append({"bucket": f"{i/10:.1f}-{(i+1)/10:.1f}", "n": len(items),
                      "mean_predicted": sum(r["model_probability_over"] for r in items) / len(items) if items else None,
                      "realized_over": sum(r["actual_stat"] > r["frozen_line"] for r in items) / len(items) if items else None})
    return {"n": len(usable), "brier": sum((r["model_probability_over"] - float(r["actual_stat"] > r["frozen_line"]))**2 for r in usable) / len(usable) if usable else None,
            "reliability": table}


def _bucket(grade: dict) -> str:
    sigma = grade.get("uncertainty_sigma")
    if sigma is None or sigma <= 0:
        return "unavailable"
    diff = abs(grade["projection"] - grade["frozen_line"]) / sigma
    if diff < .25: return "0-0.25σ"
    if diff < .5: return "0.25-0.5σ"
    if diff < 1: return "0.5-1σ"
    return "1σ+"


def forward_report(freeze_sha: str | None = None, calibration_path: str | Path | None = None) -> dict:
    """Cumulative 2026 prop report, or one immutable freeze when specified."""
    freeze_shas = ([freeze_sha] if freeze_sha else
                   [row["freeze_sha"] for row in db.q("SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at")])
    frozen_rows, grades, included_shas = [], [], []
    for sha in freeze_shas:
        payload = _freeze(sha)
        selected = [quote for quote in payload["projections"]
                    if quote["canonical_game_id"].startswith("nfl-2026-")]
        if not selected:
            continue
        included_shas.append(sha)
        frozen_rows.extend(selected)
        selected_ids = {quote["quote_id"] for quote in selected}
        grades.extend(grade for grade in _latest(sha) if grade["quote_id"] in selected_ids)
    freeze_shas = included_shas
    quote_map = {r["quote_id"]: r for r in frozen_rows}
    if len(quote_map) != len(frozen_rows):
        raise ValueError("duplicate frozen prop quote identity across weeks")
    for grade in grades:
        grade["uncertainty_sigma"] = quote_map[grade["quote_id"]]["uncertainty_sigma"]
    unique = {}
    settled_keys = set()
    for grade in grades:
        settled_keys.add((grade["canonical_game_id"], grade["player_id"], grade["market"]))
        if grade["actual_stat"] is not None:
            key = (grade["canonical_game_id"], grade["player_id"], grade["market"])
            if key in unique and unique[key]["actual_stat"] != grade["actual_stat"]:
                raise ValueError("inconsistent official stat across frozen books")
            unique[key] = grade
    markets = {}
    for market in nfl_prop_lab.MARKETS:
        player_rows = [r for r in unique.values() if r["market"] == market]
        quote_rows = [r for r in grades if r["market"] == market]
        cal = _calibration(quote_rows)
        markets[market] = {"distinct_player_markets": len(player_rows),
                           "quote_sides_graded": len(quote_rows),
                           "mae": sum(r["absolute_error"] for r in player_rows) / len(player_rows) if player_rows else None,
                           "bias": sum(r["signed_error"] for r in player_rows) / len(player_rows) if player_rows else None,
                           "brier": cal["brier"], "brier_n": cal["n"],
                           "calibration": cal["reliability"],
                           "quote_side_record": _record(quote_rows),
                           "sportsbook_settlement_interpretation": _record(
                               quote_rows, "sportsbook_settlement_interpretation")}
    # One diagnostic direction per complete frozen main-line pair. This is a
    # sign-of-difference analysis, never a pick or staking policy.
    over_rows = [r for r in grades if r["frozen_side"] == "over" and
                 (r["projection"] != r["frozen_line"])]
    diagnostics = []
    for over in over_rows:
        direction = "over" if over["projection"] > over["frozen_line"] else "under"
        key = (over["canonical_game_id"], over["player_id"], over["market"],
               over["sportsbook"], over["frozen_line"])
        companion = next((r for r in grades if (r["canonical_game_id"],r["player_id"],r["market"],r["sportsbook"],r["frozen_line"]) == key and r["frozen_side"] == direction), None)
        if companion:
            diagnostics.append(companion)
    by_book = {book: _record([r for r in diagnostics if r["sportsbook"] == book])
               for book in sorted({r["sportsbook"] for r in diagnostics})}
    settlement_by_book = {book: _record([r for r in diagnostics if r["sportsbook"] == book],
                                        "sportsbook_settlement_interpretation")
                          for book in sorted({r["sportsbook"] for r in diagnostics})}
    by_bucket = {bucket: _record([r for r in diagnostics if _bucket(r) == bucket])
                 for bucket in ("0-0.25σ", "0.25-0.5σ", "0.5-1σ", "1σ+")}
    by_market_direction = {market: _record([r for r in diagnostics if r["market"] == market])
                           for market in nfl_prop_lab.MARKETS}
    latest = {grade["quote_id"]: grade for grade in grades}
    groups = defaultdict(dict)
    for quote in frozen_rows:
        key = (quote["canonical_game_id"],quote["player_id"],quote["market"],
               quote["sportsbook"],quote["line"])
        groups[key][quote["side"]] = quote
    comparisons = []
    for group in groups.values():
        if set(group) != {"over", "under"}:
            continue
        over, under = group["over"], group["under"]
        grade = latest.get(over["quote_id"])
        comparisons.append({"canonical_game_id": over["canonical_game_id"],
                            "player_id": over["player_id"], "player_name": over["player_name"],
                            "market": over["market"], "sportsbook": over["sportsbook"],
                            "projection": over["projection"], "line": over["line"],
                            "projection_line_difference": over["projection"]-over["line"],
                            "model_probability_over": over["probability_over"],
                            "over_price": over["price"], "under_price": under["price"],
                            "implied_probability_over": implied_probability(over["price"]),
                            "implied_probability_under": implied_probability(under["price"]),
                            "quote_observed_at": over["observed_at"],
                            "kickoff_utc": over["kickoff_utc"],
                            "actual_stat": grade["actual_stat"] if grade else None,
                            "over_result": grade["result"] if grade else "pending",
                            "sportsbook_settlement_interpretation": grade.get("sportsbook_settlement_interpretation") if grade else None,
                            "result_source": grade["result_source"] if grade else None})
    retrospective = None
    path = Path(calibration_path) if calibration_path else REPORT_PATH
    if path.exists():
        retrospective = json.loads(path.read_text())
    by_week = {}
    for week in sorted({int(r["canonical_game_id"].split("-")[3]) for r in frozen_rows}):
        week_quotes = [r for r in frozen_rows if int(r["canonical_game_id"].split("-")[3]) == week]
        week_ids = {r["quote_id"] for r in week_quotes}
        by_week[str(week)] = {"frozen_quote_sides": len(week_quotes),
                              "graded_quote_sides": sum(r["quote_id"] in week_ids for r in grades)}
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS",
            "retrospective_validation": retrospective,
            "prospective_2026": {"freeze_sha": freeze_sha, "freeze_shas": freeze_shas,
                                  "weeks": by_week, "frozen_quote_sides": len(frozen_rows),
                                  "quote_sides_graded": len(grades),
                                  "distinct_player_markets_graded": len(settled_keys),
                                  "distinct_player_markets_scored": len(unique),
                                  "pending_quote_sides": len(frozen_rows) - len(grades),
                                  "markets": markets,
                                  "two_sided_market_groups": len(comparisons),
                                  "market_comparisons": comparisons,
                                  "model_direction_by_sportsbook": by_book,
                                  "conditional_settlement_by_sportsbook": settlement_by_book,
                                  "model_direction_by_market": by_market_direction,
                                  "model_direction_by_difference_bucket": by_bucket,
                                  "method_note": "Statistical outcomes and conditional book interpretations are separate. Direction is the sign of projection minus line for research diagnostics only; not picks or profit. Unknown jurisdiction, bet slip, and special terms prevent confirmation of actual sportsbook settlement."}}


def local_evidence(freeze_sha: str, stats_path: str | Path | None = None) -> list[dict]:
    """Use completed game research scores and cached nflverse stats, read-only."""
    frozen = _freeze(freeze_sha)
    seasons = {int(row["canonical_game_id"].split("-")[1]) for row in frozen["projections"]}
    if len(seasons) != 1:
        raise ValueError("a prop freeze must contain one season")
    season = next(iter(seasons))
    stats_path = Path(stats_path) if stats_path else nfl_prop_lab.STATS / f"stats_player_week_{season}.csv"
    raw = stats_path.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    meta_path = stats_path.with_suffix(".metadata.json")
    if not meta_path.exists():
        raise ValueError("player-stat cache lacks source metadata")
    source_meta = json.loads(meta_path.read_text())
    if (source_meta.get("sha256") != source_sha or
            not isinstance(source_meta.get("source_url"), str) or
            not source_meta["source_url"].startswith("https://github.com/nflverse/nflverse-data/releases/download/stats_player/")):
        raise ValueError("player-stat cache source or SHA-256 is unverified")
    stats_retrieved = _number(source_meta.get("retrieved_at"))
    stats = defaultdict(dict)
    for row in csv.DictReader(raw.decode("utf-8-sig").splitlines()):
        if row.get("game_id") and row.get("player_id"):
            stats[row["game_id"]][row["player_id"]] = row
    schedule = nfl_prop_lab._schedule()
    games = {}
    for row in frozen["projections"]:
        games[row["canonical_game_id"]] = row
    evidence = []
    for canonical in games:
        parts = canonical.split("-")
        week = int(parts[3])
        game_id = next((gid for gid, game in schedule.items()
                        if int(game["season"]) == season and int(game["week"]) == week and
                        f"nfl-{season}-reg-{week:02d}-{nfl_prop_lab._team(game['away_team'])}-{nfl_prop_lab._team(game['home_team'])}" == canonical), None)
        if not game_id:
            continue
        grade = db.q1("SELECT * FROM nfl_research_grades WHERE game_id=? ORDER BY revision DESC LIMIT 1", (game_id,))
        if not grade:
            continue
        event = db.q1("SELECT start_time FROM events WHERE id=?", (canonical,))
        if not event:
            continue
        from .nfl_identity import kickoff_utc
        actual_kickoff = kickoff_utc(event["start_time"])
        if not actual_kickoff:
            continue
        player_rows = []
        for gsis, row in stats.get(game_id, {}).items():
            values = {}
            for market, label in nfl_prop_lab.LABEL.items():
                if row.get(label) not in (None, ""):
                    values[market] = _number(row[label], nonnegative=market != "rushing_yards")
            active = _documented_participation(row)
            item = {"gsis_player_id": gsis,
                    "status": "active" if active else "unknown", "stats": values}
            if active:
                item["participation_evidence"] = {
                    "source": source_meta["source_url"], "file": str(stats_path),
                    "sha256": source_sha, "retrieved_at": stats_retrieved,
                    "active_roster": True, "snap_played": True}
            player_rows.append(item)
        game_source = {"source": grade["result_source"],
                       "retrieved_at": grade["source_retrieved_at"],
                       "file": f"db:nfl_research_grades:{game_id}:{grade['revision']}",
                       "sha256": hashlib.sha256(grade["payload"].encode()).hexdigest()}
        stat_source = {"source": source_meta["source_url"],
                       "retrieved_at": stats_retrieved,
                       "file": str(stats_path), "sha256": source_sha}
        evidence.append({"game_id": canonical, "game_status": "final",
                         "actual_kickoff_utc": actual_kickoff.timestamp(),
                         "final_score": {"home": grade["home_score"], "away": grade["away_score"]},
                         "final_game_source": game_source,
                         "player_stat_source": stat_source,
                         "result_retrieved_at": max(grade["source_retrieved_at"], stats_retrieved),
                         "players": player_rows})
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only append-only prop grading")
    parser.add_argument("--freeze-sha", help="default: latest frozen prop batch")
    parser.add_argument("--evidence", help="JSON list of sourced game/player evidence; otherwise local finalized games and cached stats")
    parser.add_argument("--stats-path", help="explicit nflverse weekly stats CSV for local grading")
    parser.add_argument("--report-output", default="data/research/nfl_prop_forward_report.json")
    args = parser.parse_args()
    rows = db.q("SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at")
    freeze_shas = [args.freeze_sha] if args.freeze_sha else [row["freeze_sha"] for row in rows]
    if not freeze_shas:
        raise SystemExit("no prop research freeze")
    if args.evidence and len(freeze_shas) != 1:
        raise SystemExit("--evidence requires --freeze-sha when multiple prop freezes exist")
    counts = []
    for freeze_sha in freeze_shas:
        evidence = json.loads(Path(args.evidence).read_text()) if args.evidence else local_evidence(freeze_sha, args.stats_path)
        counts.append(grade_freeze(freeze_sha, evidence))
    report = forward_report(args.freeze_sha)
    output = Path(args.report_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"counts": counts, "report_output": str(output)}, sort_keys=True))


if __name__ == "__main__":
    main()
