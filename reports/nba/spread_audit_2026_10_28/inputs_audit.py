"""Offline exact-input audit. Reads SQLite through mode=ro; writes only this folder.

No provider calls, model fitting, application db helpers, or production mutations.
Run from repository root: .venv/bin/python reports/nba/spread_audit_2026_10_28/inputs_audit.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from edgeterminal import nba_model, nba_sources  # pure parsing/projection only

OUT = Path(__file__).resolve().parent
CON = sqlite3.connect(f"file:{ROOT / 'data/edgeterminal.db'}?mode=ro", uri=True)
CON.row_factory = sqlite3.Row
CON.execute("PRAGMA query_only=ON")


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def artifact(artifact_id):
    return CON.execute("SELECT * FROM nba_source_artifacts WHERE id=?", (artifact_id,)).fetchone()


PARSED = {}


def owner_rows(a):
    """Match load_team_history: each schedule supplies ONLY its requested team."""
    if a["id"] not in PARSED:
        body = json.loads(a["body"])
        owner = nba_sources.team(body.get("team", {}).get("abbreviation"))
        rows = [dict(r, source_artifact_id=a["id"]) for r in
                nba_sources.team_history(body, a["sha256"]) if r["team"] == owner]
        PARSED[a["id"]] = (owner, rows)
    return PARSED[a["id"]]


def unique(rows):
    """Same strict duplicate comparison as nba_ops.unique_history, without db access."""
    seen = {}
    for row in rows:
        key = (str(row["game_id"]), str(row["team"]))
        plain = lambda r: {k: v for k, v in r.items()
                           if k not in {"source_sha256", "source_artifact_id"}}
        if key in seen and plain(row) != plain(seen[key]):
            raise ValueError(f"NBA history conflict: {key}")
        seen[key] = row
    return list(seen.values())


def used_window(rows, code, tipoff):
    selected, conflicts = nba_model._team_rows(rows, code, tipoff)
    lookup = {(r["team"], str(r["game_id"])): r for r in rows}
    return [dict(lookup[(code, r["game_id"])], ts=r["ts"]) for r in selected], conflicts


def source_meta(a, created, tipoff):
    owner, rows = owner_rows(a) if a["kind"] == "team-history" else (None, [])
    times = [nba_model.parse_time(r["played_at"]) for r in rows]
    return dict(id=a["id"], kind=a["kind"], url=a["url"], owner=owner,
                season_query=parse_qs(urlparse(a["url"]).query).get("season", [None])[0],
                received_at=a["received_at"], received_at_utc=iso(a["received_at"]),
                receipt_age_at_forecast_hours=round((created-a["received_at"])/3600, 4),
                receipt_age_at_tipoff_hours=round((tipoff-a["received_at"])/3600, 4),
                http_status=a["http_status"], body_sha256=a["sha256"],
                body_sha256_matches=hashlib.sha256(a["body"]).hexdigest() == a["sha256"],
                completed_owner_rows=len(rows),
                latest_completed_game_utc=iso(max(times)) if times else None)


def first_complete_receipt(url, game_id, tipoff):
    for a in CON.execute("SELECT * FROM nba_source_artifacts WHERE url=? AND http_status=200 "
                         "AND received_at<? ORDER BY received_at,id", (url, tipoff)):
        if any(str(r["game_id"]) == game_id for r in owner_rows(a)[1]):
            return dict(artifact_id=a["id"], received_at=a["received_at"], utc=iso(a["received_at"]))
    return None


def spread_side(margin, line):
    return "home" if margin + line >= 0 else "away"


def spread_result(side, actual_margin, line):
    adjusted = actual_margin + line
    return "push" if adjusted == 0 else "won" if (adjusted > 0) == (side == "home") else "lost"


records = []
numeric_fields = ("margin", "total", "proj_home", "proj_away", "home_win_probability",
                  "rest", "rest_days", "observations", "input_conflicts", "pace_adjusted",
                  "expected_pace", "sd_margin", "sd_total")
for f in CON.execute("SELECT f.*,g.home_team,g.away_team,g.tipoff_ts,g.tipoff_utc,"
                     "g.home_score,g.away_score FROM nba_forecasts f JOIN nba_games g USING(game_id) "
                     "WHERE g.game_date_et='2026-10-28' AND json_extract(f.payload,'$.kind')='game' "
                     "ORDER BY g.tipoff_ts,f.game_id"):
    p = json.loads(f["payload"])
    exact, before_tipoff, sources, later_sources = [], [], [], []
    current_urls = {}
    for source_id in json.loads(f["source_artifact_ids"]):
        a = artifact(source_id)
        sources.append(source_meta(a, f["created_at"], f["tipoff_ts"]))
        if a["kind"] != "team-history":
            continue
        owner, rows = owner_rows(a)
        exact.extend(rows)
        season = parse_qs(urlparse(a["url"]).query).get("season", [None])[0]
        if season == "2027":
            current_urls[owner] = a["url"]
        later = CON.execute("SELECT * FROM nba_source_artifacts WHERE url=? AND received_at<? "
                            "AND http_status=200 ORDER BY received_at DESC,id DESC LIMIT 1",
                            (a["url"], f["tipoff_ts"])).fetchone()
        before_tipoff.extend(owner_rows(later)[1])
        later_sources.append(source_meta(later, f["created_at"], f["tipoff_ts"]))
    exact, before_tipoff = unique(exact), unique(before_tipoff)
    replay = nba_model.project_game(f["home_team"], f["away_team"], exact, f["tipoff_utc"])
    later_replay = nba_model.project_game(f["home_team"], f["away_team"], before_tipoff, f["tipoff_utc"])
    teams = {}
    for side, code in [("home", f["home_team"]), ("away", f["away_team"])]:
        window, conflicts = used_window(exact, code, f["tipoff_ts"])
        later_window, later_conflicts = used_window(before_tipoff, code, f["tipoff_ts"])
        ids = {str(r["game_id"]) for r in window}
        added = []
        for row in later_window:
            if str(row["game_id"]) in ids:
                continue
            receipt = first_complete_receipt(current_urls[code], str(row["game_id"]), f["tipoff_ts"])
            added.append(dict(row,
                              kickoff_relative_to_forecast_hours=round((row["ts"]-f["created_at"])/3600, 4),
                              first_archived_owner_schedule_final=receipt))
        current_count = sum(r["ts"] >= nba_model.parse_time("2026-10-01T00:00:00Z") for r in window)
        teams[side] = dict(team=code, exact_window=window, later_pretipoff_window=later_window,
                           exact_conflicts=conflicts, later_conflicts=later_conflicts,
                           current_season_rows=current_count, prior_season_rows=len(window)-current_count,
                           old_season_fraction=(len(window)-current_count)/len(window),
                           mean_points=statistics.mean(r["points"] for r in window),
                           mean_opponent_points=statistics.mean(r["opponent_points"] for r in window),
                           mean_raw_net_points=statistics.mean(r["points"]-r["opponent_points"] for r in window),
                           possessions_present=sum(r.get("possessions") is not None for r in window),
                           opponent_counts=dict(Counter(r["opponent"] for r in window)),
                           window_start_utc=window[0]["played_at"], window_end_utc=window[-1]["played_at"],
                           latest_game_to_forecast_hours=round((f["created_at"]-window[-1]["ts"])/3600, 4),
                           latest_game_to_tipoff_days=round((f["tipoff_ts"]-window[-1]["ts"])/86400, 4),
                           missing_from_saved_window=added)
    market = p["market"]
    if market.get("home_spread") is None:
        line_f = CON.execute("SELECT payload FROM nba_forecasts WHERE game_id=? "
                             "AND json_extract(payload,'$.kind')='spread-line' "
                             "ORDER BY created_at LIMIT 1", (f["game_id"],)).fetchone()
        market = json.loads(line_f["payload"])["market"] if line_f else market
    line = market.get("home_spread")
    actual_margin = f["home_score"]-f["away_score"]
    old_side = spread_side(p["projection"]["margin"], line) if line is not None else None
    later_side = spread_side(later_replay["margin"], line) if line is not None else None
    records.append(dict(
        game_id=f["game_id"], forecast_id=f["forecast_id"], home=f["home_team"], away=f["away_team"],
        tipoff_utc=f["tipoff_utc"], created_at=f["created_at"], created_at_utc=iso(f["created_at"]),
        created_hours_before_tipoff=round((f["tipoff_ts"]-f["created_at"])/3600, 4),
        saved_model_version=f["model_version"], saved_parameter_fingerprint=p["model"]["fingerprint"],
        payload_sha256_matches=hashlib.sha256(f["payload"].encode()).hexdigest() == f["payload_sha256"],
        source_artifact_ids=json.loads(f["source_artifact_ids"]), exact_sources=sources,
        sources_received_by_forecast=all(s["received_at"]<=f["created_at"] for s in sources),
        exact_reconstructed_history=exact, exact_source_replay=replay,
        replay_differences={k: [p["projection"].get(k), replay[k]] for k in numeric_fields
                            if p["projection"].get(k) != replay[k]},
        saved_projection=p["projection"], teams=teams,
        later_pretipoff_sources=later_sources, later_pretipoff_replay=later_replay,
        margin_change_from_refreshed_inputs=round(later_replay["margin"]-p["projection"]["margin"], 4),
        home_spread=line, actual_home_margin=actual_margin, saved_spread_side=old_side,
        later_pretipoff_spread_side=later_side,
        saved_spread_result=spread_result(old_side, actual_margin, line) if line is not None else None,
        later_pretipoff_spread_result=spread_result(later_side, actual_margin, line) if line is not None else None,
        saved_absolute_margin_error=round(abs(p["projection"]["margin"]-actual_margin), 4),
        later_pretipoff_absolute_margin_error=round(abs(later_replay["margin"]-actual_margin), 4),
    ))

summary = dict(
    audit_date="2026-10-28", database_open_mode="ro + query_only", target_outcomes_used_for_fitting=False,
    note="Exact source replay uses current pure game baseline; saved model metadata remains v1.1.0. "
         "Later replay selects only archived same-URL schedules received before target tipoff and "
         "applies unchanged four-hour feature lag. It is a diagnostic counterfactual, not a replacement forecast.",
    games=len(records), exact_numeric_replay_matches=sum(not r["replay_differences"] for r in records),
    payload_hash_matches=sum(r["payload_sha256_matches"] for r in records),
    all_source_hashes_match=all(s["body_sha256_matches"] for r in records for s in r["exact_sources"]),
    all_sources_http_200=all(s["http_status"] == 200 for r in records for s in r["exact_sources"]),
    all_sources_received_by_forecast=all(r["sources_received_by_forecast"] for r in records),
    forecast_hours_before_tipoff_min=min(r["created_hours_before_tipoff"] for r in records),
    forecast_hours_before_tipoff_max=max(r["created_hours_before_tipoff"] for r in records),
    exact_window_rows=sum(len(t["exact_window"]) for r in records for t in r["teams"].values()),
    exact_current_season_rows=sum(t["current_season_rows"] for r in records for t in r["teams"].values()),
    exact_prior_season_rows=sum(t["prior_season_rows"] for r in records for t in r["teams"].values()),
    exact_possessions_present=sum(t["possessions_present"] for r in records for t in r["teams"].values()),
    exact_input_conflicts=sum(r["exact_source_replay"]["input_conflicts"] for r in records),
    current_source_age_at_forecast_hours_min=min(s["receipt_age_at_forecast_hours"] for r in records
                                                for s in r["exact_sources"] if s["season_query"] == "2027"),
    current_source_age_at_forecast_hours_max=max(s["receipt_age_at_forecast_hours"] for r in records
                                                for s in r["exact_sources"] if s["season_query"] == "2027"),
    current_source_age_at_tipoff_hours_min=min(s["receipt_age_at_tipoff_hours"] for r in records
                                              for s in r["exact_sources"] if s["season_query"] == "2027"),
    current_source_age_at_tipoff_hours_max=max(s["receipt_age_at_tipoff_hours"] for r in records
                                              for s in r["exact_sources"] if s["season_query"] == "2027"),
    missing_team_games=sum(len(t["missing_from_saved_window"]) for r in records for t in r["teams"].values()),
    affected_teams=sum(bool(t["missing_from_saved_window"]) for r in records for t in r["teams"].values()),
    affected_games=sum(any(t["missing_from_saved_window"] for t in r["teams"].values()) for r in records),
    missing_games_already_four_hours_old_at_forecast=sum(
        row["kickoff_relative_to_forecast_hours"] <= -4 for r in records
        for t in r["teams"].values() for row in t["missing_from_saved_window"]),
    missed_b2b=[dict(game_id=r["game_id"], side=s, team=r["teams"][s]["team"],
                    saved_days=r["saved_projection"]["rest_days"][s],
                    later_days=r["later_pretipoff_replay"]["rest_days"][s])
                for r in records for s in ("home", "away")
                if r["saved_projection"]["rest"][s] != "b2b"
                and r["later_pretipoff_replay"]["rest"][s] == "b2b"],
    maximum_absolute_margin_change=max(abs(r["margin_change_from_refreshed_inputs"]) for r in records),
    saved_margin_mae=statistics.mean(r["saved_absolute_margin_error"] for r in records),
    later_pretipoff_margin_mae=statistics.mean(r["later_pretipoff_absolute_margin_error"] for r in records),
    refreshed_margin_error_improved=sum(r["later_pretipoff_absolute_margin_error"] < r["saved_absolute_margin_error"]
                                       for r in records),
    refreshed_margin_error_worsened=sum(r["later_pretipoff_absolute_margin_error"] > r["saved_absolute_margin_error"]
                                       for r in records),
    saved_spread_results=dict(Counter(r["saved_spread_result"] for r in records)),
    later_pretipoff_spread_results=dict(Counter(r["later_pretipoff_spread_result"] for r in records)),
    spread_side_changes=[r["game_id"] for r in records
                         if r["saved_spread_side"] != r["later_pretipoff_spread_side"]],
)
(OUT/"inputs_replay.json").write_text(json.dumps(dict(summary=summary, games=records), indent=2)+"\n")
print(json.dumps(summary, indent=2))

