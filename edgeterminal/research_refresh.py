"""Cadenced, fail-closed research operations. Product PASS is untouched."""
from __future__ import annotations

import argparse
import fcntl
import json
import time
import uuid
from pathlib import Path

from . import db, engine, nfl_forward_grading, nfl_forward_weekly
from . import nfl_market_capture, nfl_prop_result_ingest, nfl_props, paper_research
from .config import ROOT, settings
from .integrity import parse_time
from .nfl_context_observations import capture_snapshot
from .providers import registry

LOCK = settings.db_path.with_name(settings.db_path.name + "-research.lock")
GAME_DELAY = 4 * 3600
SCHEDULE_INTERVAL = 6 * 3600
CONTEXT_INTERVAL = 3 * 3600
RESULT_INTERVAL = 6 * 3600


def _events(now: float) -> list[dict]:
    events = []
    for row in db.q("SELECT * FROM events WHERE sport='NFL'"):
        kickoff = parse_time(row.get("start_time"))
        if kickoff is None:
            continue
        row["kickoff_utc"] = kickoff
        try:
            row["meta"] = json.loads(row.get("meta") or "{}")
        except (TypeError, ValueError):
            row["meta"] = {}
        if row["id"].startswith("nfl-") and not row["meta"].get("is_demo"):
            events.append(row)
    return events


def interval(now: float, events: list[dict]) -> int:
    """Launchd ticks every five minutes; expensive steps run less often."""
    nearest = min((e["kickoff_utc"] - now for e in events if -6 * 3600 <= e["kickoff_utc"] - now),
                  default=float("inf"))
    if -6 * 3600 <= nearest <= 2 * 3600:
        return 5 * 60
    if nearest <= 36 * 3600:
        return 15 * 60
    return 6 * 3600


def _schedule_interval(now: float, events: list[dict]) -> int:
    nearest = min((e["kickoff_utc"] - now for e in events if e["kickoff_utc"] >= now),
                  default=float("inf"))
    if nearest <= 2 * 3600:
        return 30 * 60
    if nearest <= 36 * 3600:
        return 2 * 3600
    return SCHEDULE_INTERVAL


def _last_attempt(name: str) -> float | None:
    row = db.q1("SELECT attempted_at FROM research_refresh_steps WHERE name=? "
                "ORDER BY attempted_at DESC LIMIT 1", (name,))
    return row["attempted_at"] if row else None


def _due(name: str, now: float, cadence: float) -> bool:
    prior = _last_attempt(name)
    return prior is None or now >= prior + cadence


def _context(events: list[dict], now: float) -> dict:
    due = [e for e in events if 0 < e["kickoff_utc"] - now <= 6 * 3600]
    if not due:
        return {"games": 0, "observations": 0}
    injuries, injury_source, is_demo = registry.injuries("NFL")
    if is_demo:
        injuries = []
    count = 0
    for game in due:
        teams = {game["home_team"], game["away_team"]}
        meta = game["meta"]
        weather, weather_source, weather_demo = registry.weather(
            None, None, game["start_time"], indoor=bool(meta.get("indoor")),
            city=meta.get("city"), state=meta.get("state"), sport="NFL")
        if weather_demo:
            weather = None
        # capture_snapshot enforces observation times and kickoff validity.
        snapshot = {"game": {"id": game["id"], "kickoff_utc": game["start_time"]},
                    "injuries": [i for i in injuries if i.get("team") in teams],
                    "weather": weather or {}}
        count += len(capture_snapshot(snapshot, now=now))
    return {"games": len(due), "observations": count,
            "injury_provider": injury_source, "weather_provider": weather_source,
            "qb_start_source": "unavailable; no timestamp-safe announced starter feed"}


def _week_due(events: list[dict], now: float) -> tuple[int, int] | None:
    weeks: dict[tuple[int, int], float] = {}
    for event in events:
        parts = event["id"].split("-")
        if len(parts) < 6 or parts[2] != "reg":
            continue
        season, week = int(parts[1]), int(parts[3])
        weeks[(season, week)] = min(weeks.get((season, week), float("inf")), event["kickoff_utc"])
    eligible = [(kickoff, season, week) for (season, week), kickoff in weeks.items()
                if 0 < kickoff - now <= 36 * 3600]
    if not eligible:
        return None
    _, season, week = min(eligible)
    return season, week


def _prop_capture_due(season: int, week: int, events: list[dict], now: float) -> bool:
    first = min(e["kickoff_utc"] for e in events if e["id"].startswith(f"nfl-{season}-reg-{week:02d}-"))
    prefix = f"nfl-{season}-reg-{week:02d}-%"
    existing = db.q1("SELECT 1 FROM nfl_prop_snapshots WHERE canonical_game_id LIKE ? LIMIT 1", (prefix,))
    return (0 < first - now <= 24 * 3600 and not existing and
            bool(settings.sportsgameodds_api_key) and
            _last_attempt(f"prop_capture_{season}_{week:02d}") is None)


def _latest_prop_freeze() -> str | None:
    row = db.q1("SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at DESC LIMIT 1")
    return row["freeze_sha"] if row else None


def _weekly(season: int, now: float) -> dict:
    result = nfl_forward_weekly.run(season=season, now=now)
    if (str(result.get("freeze", "")).startswith("refused") or result.get("schedule_error") or
            result.get("market_error") or result.get("live_schedule", {}).get("ok") is False):
        raise ValueError(f"weekly research freeze refused: {result.get('freeze')}")
    return result


def _market(now: float) -> dict:
    result = nfl_market_capture.capture_if_due(now=now)
    if result.get("games_without_quotes"):
        raise ValueError(f"target games without quotes: {result['games_without_quotes']}")
    return result


def _game_grading(now: float) -> dict:
    result = nfl_forward_grading.sync_all(now=now)
    failures = [row for row in result.get("sync", []) if "error" in row]
    if failures:
        raise ValueError(f"game grading failed for {len(failures)} freeze(s)")
    return result


def status(now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    events = _events(stamp)
    latest = db.q1("SELECT * FROM research_refresh_runs ORDER BY finished_at DESC LIMIT 1")
    success = db.q1("SELECT finished_at FROM research_refresh_runs WHERE success=1 "
                    "ORDER BY finished_at DESC LIMIT 1")
    steps = db.q("SELECT name,success,detail,attempted_at FROM research_refresh_steps "
                 "WHERE id IN (SELECT MAX(id) FROM research_refresh_steps GROUP BY name) ORDER BY name")
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS",
            "last_successful_research_refresh": success["finished_at"] if success else None,
            "last_attempt": latest["finished_at"] if latest else None,
            "next_scheduled_refresh": latest["next_due_at"] if latest else stamp,
            "planned_interval_seconds": interval(stamp, events),
            "data_freshness": {"last_prop_quote_at": (db.q1(
                "SELECT MAX(retrieved_at) AS t FROM nfl_prop_snapshots") or {}).get("t"),
                "last_game_quote_at": (db.q1(
                    "SELECT MAX(fetched_at) AS t FROM odds_snapshots WHERE sport='NFL' AND is_demo=0") or {}).get("t")},
            "provider_health": steps,
            "errors": [{"step": row["name"], "detail": row["detail"],
                        "attempted_at": row["attempted_at"]} for row in steps if not row["success"]],
            "scheduler": "launchd LaunchAgent; service status via python -m edgeterminal.research_scheduler status"}


def run(now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    db.init()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "steps": {}}
        return _run_locked(stamp)


def _run_locked(now: float) -> dict:
    run_id = uuid.uuid4().hex
    steps: dict[str, dict] = {}
    def attempt(name: str, action) -> None:
        try:
            result = action()
            if isinstance(result, dict) and result.get("ok") is False:
                raise ValueError("provider reported unavailable")
            steps[name] = {"ok": True, "result": result}
        except Exception as exc:
            steps[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        db.ex("INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) "
              "VALUES(?,?,?,?,?)", (run_id, name, now, int(steps[name]["ok"]),
                                     json.dumps(steps[name], sort_keys=True, default=str)))

    events = _events(now)
    if _due("schedule", now, _schedule_interval(now, events)):
        attempt("schedule", lambda: engine.refresh_schedule("NFL"))
        events = _events(now)
    week = _week_due(events, now)
    if week:
        season, number = week
        weekly_name = f"weekly_freeze_{season}_{number:02d}"
        freeze_file = ROOT / f"reports/nfl_model_v2/forward_{season}_week{number:02d}.json"
        if not freeze_file.exists() and _due(weekly_name, now, 3600):
            attempt(weekly_name,
                    lambda: _weekly(season, now))
        market_name = f"current_game_market_{season}_{number:02d}"
        freeze_captured_market = (steps.get(weekly_name, {}).get("ok") and
                                  steps[weekly_name]["result"].get("market_capture"))
        if not freeze_captured_market and _due(market_name, now, 6 * 3600):
            attempt(market_name, lambda: nfl_market_capture.capture_week(season, number, now=now))
        if _prop_capture_due(season, number, events, now):
            attempt(f"prop_capture_{season}_{number:02d}",
                    lambda: nfl_props.capture_week(season, number, retrieved_at=now))
    if any(0 < e["kickoff_utc"] - now <= nfl_market_capture.WINDOW_S for e in events):
        attempt("near_kickoff_market", lambda: _market(now))
    if any(0 < e["kickoff_utc"] - now <= 6 * 3600 for e in events) and _due("context", now, CONTEXT_INTERVAL):
        attempt("context", lambda: _context(events, now))
    if any(e["kickoff_utc"] <= now - GAME_DELAY for e in events) and _due("game_grading", now, RESULT_INTERVAL):
        attempt("game_grading", lambda: _game_grading(now))
    freeze_sha = _latest_prop_freeze()
    if freeze_sha and any(e["kickoff_utc"] <= now for e in events) and _due("prop_grading", now, RESULT_INTERVAL):
        attempt("prop_grading", lambda: nfl_prop_result_ingest.sync(freeze_sha, now=now))
    if paper_research.needs_settlement():
        attempt("paper_settlement", paper_research.settle)
    policy = paper_research.example_policy()
    registered = db.q1("SELECT 1 FROM paper_policies WHERE version=?", (policy["version"],))
    if not registered or (policy["enabled"] and (
            paper_research.current_opportunities(now) or
            any(0 < e["kickoff_utc"] - now <= policy["quote_freshness_seconds"] for e in events))):
        attempt("paper_policy", lambda: paper_research.evaluate(policy, now=now))
    cadence = interval(now, events)
    complete = time.time()
    successful = all(item["ok"] for item in steps.values())
    output = {"readiness": "RESEARCH_ONLY", "designation": "PASS", "run_id": run_id,
              "started_at": now, "finished_at": complete, "success": successful,
              "next_due_at": now + cadence, "steps": steps}
    if steps:
        db.ex("INSERT INTO research_refresh_runs VALUES(?,?,?,?,?,?)",
              (run_id, now, complete, int(successful), now + cadence,
               json.dumps(output, sort_keys=True, default=str)))
    else:
        output["status"] = "no_work_due"
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="EdgeTerminal background research refresh")
    parser.add_argument("action", nargs="?", choices=("run", "status"), default="run")
    args = parser.parse_args()
    result = status() if args.action == "status" else run()
    print(json.dumps(result, sort_keys=True, default=str))
    if args.action == "run" and result.get("success") is False:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
