"""Cadenced, fail-closed research operations. Product PASS is untouched."""
from __future__ import annotations

import argparse
import fcntl
import json
import time
import uuid
from pathlib import Path

from . import db, engine, nfl_forward_grading, nfl_forward_weekly
from . import nfl_top_five
from . import nfl_market_capture, nfl_prop_capture_ops, nfl_prop_result_ingest, paper_research, research_scheduler
from .config import ROOT, settings
from .integrity import parse_time
from .nfl_context_observations import capture_snapshot
from .providers import registry

LOCK = settings.db_path.with_name(settings.db_path.name + "-research.lock")
SCHEDULE_INTERVAL = 6 * 3600
CONTEXT_INTERVAL = 3 * 3600
RESULT_INTERVAL = 5 * 60
GAME_ODDS_INTERVAL = 15 * 60
GAME_ODDS_NEAR_INTERVAL = 5 * 60


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
    return RESULT_INTERVAL


def _schedule_interval(now: float, events: list[dict]) -> int:
    nearest = min((e["kickoff_utc"] - now for e in events if e["kickoff_utc"] >= now),
                  default=float("inf"))
    if nearest <= 2 * 3600:
        return 30 * 60
    if nearest <= 36 * 3600:
        return 2 * 3600
    return SCHEDULE_INTERVAL


def _game_odds_interval(now: float, events: list[dict]) -> int:
    nearest = min((e["kickoff_utc"] - now for e in events if e["kickoff_utc"] > now),
                  default=float("inf"))
    return GAME_ODDS_NEAR_INTERVAL if nearest <= 60 * 60 else GAME_ODDS_INTERVAL


def _last_attempt(name: str) -> float | None:
    row = db.q1("SELECT attempted_at FROM research_refresh_steps WHERE name=? "
                "ORDER BY attempted_at DESC LIMIT 1", (name,))
    return row["attempted_at"] if row else None


def _due(name: str, now: float, cadence: float) -> bool:
    prior = _last_attempt(name)
    if prior is not None and name in {'schedule', 'availability'}:
        step = db.q1('SELECT success FROM research_refresh_steps WHERE name=? '
                      'ORDER BY attempted_at DESC LIMIT 1', (name,))
        if not step['success']:
            cadence = min(cadence, RESULT_INTERVAL)
    return prior is None or now >= prior + cadence


def result_interval(step: dict | None) -> int:
    """Check result availability every five minutes, including failed reads."""
    return RESULT_INTERVAL


def _result_due(name: str, now: float) -> bool:
    step = db.q1('SELECT success,detail,attempted_at FROM research_refresh_steps '
                  'WHERE name=? ORDER BY attempted_at DESC LIMIT 1', (name,))
    return step is None or now >= step['attempted_at'] + result_interval(step)


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


def _availability_interval(now: float, events: list[dict]) -> int:
    nearest = min((e["kickoff_utc"] - now for e in events if e["kickoff_utc"] > now),
                  default=float("inf"))
    if nearest <= 15 * 60:
        return 5 * 60
    if nearest <= 60 * 60:
        return 15 * 60
    if nearest <= 6 * 3600:
        return 60 * 60
    return 6 * 3600


def _availability(events: list[dict], now: float) -> dict:
    due = [e for e in events if 0 < e["kickoff_utc"] - now <= 7 * 86400]
    if not due:
        return {"games": 0, "observations": 0}
    injuries, source, is_demo = registry.injuries("NFL")
    if is_demo or source == "unavailable":
        raise ValueError("timestamped live ESPN availability source unavailable")
    count = 0
    for game in due:
        teams = {game["home_team"], game["away_team"]}
        snapshot = {"game": {"id": game["id"], "kickoff_utc": game["start_time"]},
                    "injuries": [i for i in injuries if i.get("team") in teams],
                    "weather": {}}
        count += len(capture_snapshot(snapshot, now=now))
    return {"games": len(due), "observations": count, "source": source,
            "absence_from_injury_feed": "unresolved, never assumed available"}


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


def _latest_prop_freeze() -> str | None:
    row = db.q1("SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at DESC LIMIT 1")
    return row["freeze_sha"] if row else None


def _pair_prop_batch(batch_id: str, now: float) -> dict:
    batch = db.q1('SELECT retrieved_at FROM nfl_prop_capture_batches WHERE batch_id=?', (batch_id,))
    # A live receipt can follow worker start. Only advance to a receipt the
    # processing clock has reached; pair_batch still rejects future or missing batches.
    if batch and batch['retrieved_at'] <= max(now, time.time()):
        now = max(now, batch['retrieved_at'])
    return paper_research.pair_batch(batch_id, now=now)


def _failed_prop_pair_batches(now: float) -> list[tuple[str, str, str]]:
    retries = []
    for failed in db.q("SELECT * FROM research_refresh_steps WHERE success=0 AND name LIKE 'prop_pair_%' "
                       "AND id IN (SELECT MAX(id) FROM research_refresh_steps GROUP BY name)"):
        name = failed['name']
        parts = name.split('_')
        if (len(parts) != 4 or parts[:2] != ['prop', 'pair'] or
                len(parts[2]) != 4 or len(parts[3]) != 2 or
                not parts[2].isdigit() or not parts[3].isdigit() or not _result_due(name, now)):
            continue
        try:
            detail = json.loads(failed['detail'])
            if not isinstance(detail, dict):
                continue
            capture_run_id = detail.get('capture_run_id', failed['run_id'])
            if not isinstance(capture_run_id, str) or not capture_run_id:
                continue
            capture = db.q1('SELECT detail FROM research_refresh_steps WHERE run_id=? AND name=? '
                            'AND success=1 ORDER BY id DESC LIMIT 1',
                            (capture_run_id, f'prop_near_game_{parts[2]}_{parts[3]}'))
            source = json.loads(capture['detail']) if capture else {}
            if not isinstance(source, dict) or source.get('ok') is not True:
                continue
            batch_id = source['result']['batch_id']
            if not isinstance(batch_id, str) or not batch_id or detail.get('batch_id', batch_id) != batch_id:
                continue
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        retries.append((name, batch_id, capture_run_id))
    return retries


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
        raise ValueError(f"game grading failed for {len(failures)} freeze(s): " +
                         '; '.join(row['error'] for row in failures))
    return result


def status(now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    events = _events(stamp)
    latest = db.q1("SELECT * FROM research_refresh_runs ORDER BY finished_at DESC LIMIT 1")
    success = db.q1("SELECT finished_at FROM research_refresh_runs WHERE success=1 "
                    "ORDER BY finished_at DESC LIMIT 1")
    steps = db.q("SELECT name,success,detail,attempted_at FROM research_refresh_steps "
                 "WHERE id IN (SELECT MAX(id) FROM research_refresh_steps GROUP BY name) ORDER BY name")
    scheduler = research_scheduler.status()
    tick = db.q1("SELECT started_at,finished_at,success FROM research_refresh_ticks "
                 "ORDER BY started_at DESC LIMIT 1")
    prop_health = nfl_prop_capture_ops.health(stamp, events)
    tick_gap = stamp - tick["started_at"] if tick else None
    warnings = [w for w in prop_health["warnings"] if w != 'final_pregame_capture_window_missed']
    historical_warnings = []
    if prop_health.get('missed_final_window_games'):
        historical_warnings.append({
            'message': 'These completed games missed a prop capture in the final 15 minutes before kickoff.',
            'game_ids': prop_health['missed_final_window_games']})
    if prop_health["upcoming_capture_windows"] and tick_gap is None:
        warnings.append("worker_tick_history_not_yet_available")
    elif prop_health["upcoming_capture_windows"] and tick_gap > 12 * 60:
        warnings.append("worker_tick_gap; Mac sleep, offline state, or stopped worker is possible")
    if not scheduler["loaded"]:
        warnings.append("research_launchagent_not_loaded")
    from .product_insights import unresolved_failures
    unresolved = unresolved_failures(steps, stamp)
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS",
            "last_successful_research_refresh": success["finished_at"] if success else None,
            "last_attempt": latest["finished_at"] if latest else None,
            "next_scheduled_refresh": latest["next_due_at"] if latest else stamp,
            "planned_interval_seconds": interval(stamp, events),
            "data_freshness": {"last_prop_quote_at": (db.q1(
                "SELECT MAX(retrieved_at) AS t FROM nfl_prop_snapshots") or {}).get("t"),
                "last_game_quote_at": (db.q1(
                    "SELECT MAX(fetched_at) AS t FROM odds_snapshots WHERE sport='NFL' AND is_demo=0") or {}).get("t"),
                "last_availability_at": (db.q1(
                    "SELECT MAX(fetched_at) AS t FROM nfl_research_context_observations "
                    "WHERE kind IN ('offensive_injury','qb_availability')") or {}).get("t")},
            "sportsgameodds": prop_health,
            "provider_health": steps,
            "errors": [{"step": row["name"], "detail": row["detail"],
                        "attempted_at": row["attempted_at"]} for row in unresolved],
            "scheduler": scheduler,
            "worker_tick": tick,
            "worker_tick_gap_seconds": tick_gap,
            "operations_warnings": warnings,
            "historical_warnings": historical_warnings}


def run(now: float | None = None, *, record_tick: bool = True,
        force_prop_quotes: bool = False) -> dict:
    stamp = time.time() if now is None else now
    db.init()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "steps": {}}
        return _run_locked(stamp, record_tick=record_tick,
                           force_prop_quotes=force_prop_quotes)


def _run_locked(now: float, *, record_tick: bool = True,
                force_prop_quotes: bool = False) -> dict:
    run_id = uuid.uuid4().hex
    steps: dict[str, dict] = {}
    def attempt(name: str, action, *, context: dict | None = None) -> None:
        try:
            result = action()
            if isinstance(result, dict) and result.get("ok") is False:
                raise ValueError("provider reported unavailable")
            steps[name] = {"ok": True, "result": result}
        except Exception as exc:
            steps[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if context:
            steps[name] = {**context, **steps[name]}
        db.ex("INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) "
              "VALUES(?,?,?,?,?)", (run_id, name, now, int(steps[name]["ok"]),
                                     json.dumps(steps[name], sort_keys=True, default=str)))

    events = _events(now)
    if _due("schedule", now, _schedule_interval(now, events)):
        attempt("schedule", lambda: engine.refresh_schedule("NFL"))
        events = _events(now)
        if steps["schedule"]["ok"]:
            attempt("product_projections", engine.ensure_weekly_projections)
    if any(0 < e['kickoff_utc']-now <= 7*86400 for e in events) and _due('availability', now, _availability_interval(now, events)):
        attempt('availability', lambda: _availability(events, now))
    missing_capture_interval = min((nfl_prop_capture_ops.cadence(e["kickoff_utc"] - now)
                                    or 3600 for e in events if e["kickoff_utc"] > now), default=3600)
    if _due("top_five_capture", now, missing_capture_interval) or force_prop_quotes:
        attempt("top_five_capture", lambda: nfl_top_five.capture_missing(now, manual=force_prop_quotes))
    if _result_due("top_five_grading", now):
        attempt("top_five_grading", lambda: nfl_top_five.grade_due(now))
    # Previously saved comparison forecasts still receive sourced final grades.
    # New captures use only the active production research model.
    from . import nfl_prop_challenger
    if ((nfl_prop_challenger.DIRECTORY / 'active.json').exists() and
            _result_due('challenger_grading', now)):
        attempt('challenger_grading', lambda: nfl_prop_challenger.grade_due(now=now))
    if (any(0 < e["kickoff_utc"] - now <= 24 * 3600 for e in events) and
            _due("current_game_odds", now, _game_odds_interval(now, events))):
        attempt("current_game_odds", lambda: nfl_market_capture.capture_current(now=now))
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
    if any(name in steps and steps[name]['ok'] for name in ('current_game_odds',)) or any(
            name.startswith('current_game_market_') and step['ok'] for name, step in steps.items()):
        attempt('forecast_lines', engine.save_missing_forecast_lines)
    capture_succeeded = False
    for (season, number), game_ids in nfl_prop_capture_ops.plan(
            events, now, manual=force_prop_quotes)["due"].items():
        name = f"prop_near_game_{season}_{number:02d}"
        attempt(name, lambda s=season, w=number, ids=game_ids:
                nfl_prop_capture_ops.capture(s, w, ids, now))
        if steps[name]["ok"]:
            capture_succeeded = True
            batch_id = steps[name]["result"]["batch_id"]
            attempt(f"prop_pair_{season}_{number:02d}",
                    lambda bid=batch_id: _pair_prop_batch(bid, now),
                    context={'batch_id': batch_id, 'capture_run_id': run_id})
    for name, batch_id, capture_run_id in _failed_prop_pair_batches(now):
        if name not in steps:
            attempt(name, lambda bid=batch_id: _pair_prop_batch(bid, now),
                    context={'batch_id': batch_id, 'capture_run_id': capture_run_id})
    if any(0 < e["kickoff_utc"] - now <= nfl_market_capture.WINDOW_S for e in events):
        attempt("near_kickoff_market", lambda: _market(now))
    if any(0 < e["kickoff_utc"] - now <= 6 * 3600 for e in events) and _due("context", now, CONTEXT_INTERVAL):
        attempt("context", lambda: _context(events, now))
    availability_due = (any(0 < e["kickoff_utc"] - now <= 24 * 3600 for e in events) and
                        (capture_succeeded or _due("availability", now,
                                                   _availability_interval(now, events))))
    if availability_due and 'availability' not in steps:
        attempt("availability", lambda: _availability(events, now))
    if any(e["kickoff_utc"] <= now for e in events) and _result_due("game_grading", now):
        attempt("game_grading", lambda: _game_grading(now))
    if paper_research.needs_settlement():
        attempt("paper_settlement", paper_research.settle)
    policy = paper_research.active_policy()
    registered = db.q1("SELECT 1 FROM paper_policies WHERE version=?", (policy["version"],))
    if (not availability_due or steps["availability"]["ok"]) and (not registered or (policy["enabled"] and (
            capture_succeeded or paper_research.current_opportunities(now) or
            any(0 < e["kickoff_utc"] - now <= policy["quote_freshness_seconds"] for e in events)))):
        attempt("paper_policy", lambda: paper_research.evaluate(policy, now=now))
    if settings.nba_research_enabled and _due('nba_research', now, 300):
        from . import nba_ops
        attempt('nba_research', lambda: nba_ops.run(now))
    if _due('model_learning', now, 300):
        from . import model_learning
        attempt('model_learning', lambda: model_learning.run(now))
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
    if record_tick:
        # Manual checks must not make a sleeping LaunchAgent appear healthy.
        db.ex("INSERT INTO research_refresh_ticks VALUES(?,?,?,?)",
              (run_id, now, complete, int(successful)))
    # Saved-research notifications consume stored observations only. They cannot
    # change freezes, selections or grading and never send email automatically.
    from . import members
    if db.q1('SELECT 1 FROM member_saved_props LIMIT 1'):
        try:
            members.sync_alerts()
        except Exception:
            db.kv_set('member_alerts_health', {'ok': False, 'checked_at': time.time()})
        else:
            db.kv_set('member_alerts_health', {'ok': True, 'checked_at': time.time()})
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="EdgeTerminal background research refresh")
    parser.add_argument("action", nargs="?", choices=("run", "status"), default="run")
    args = parser.parse_args()
    result = status() if args.action == "status" else run()
    if args.action == 'run':
        from .service_ops import maintain_local
        maintain_local()
    print(json.dumps(result, sort_keys=True, default=str))
    if args.action == "run" and result.get("success") is False:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
