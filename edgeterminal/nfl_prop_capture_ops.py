"""Quota-bounded, append-only SportsGameOdds pregame research capture."""
from __future__ import annotations

import json
import re
import time
import uuid

from . import db, nfl_props
from .config import settings

MONTH_SECONDS = 30 * 86400
MAX_LOCAL_REQUESTS_30D = 180
MAX_LOCAL_OBJECTS_30D = 1200
FAILURE_BACKOFF = 30 * 60
QUOTA_BACKOFF = 24 * 3600
INACTIVE = {"POSTPONED", "CANCELED", "CANCELLED", "FINAL", "STATUS_POSTPONED",
            "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_FINAL", "STATUS_IN_PROGRESS"}


def cadence(seconds_to_kickoff: float) -> int | None:
    if not 0 < seconds_to_kickoff <= 24 * 3600:
        return None
    if seconds_to_kickoff <= 15 * 60:
        return 5 * 60
    if seconds_to_kickoff <= 60 * 60:
        return 15 * 60
    if seconds_to_kickoff <= 6 * 3600:
        return 60 * 60
    return 6 * 3600


def _attempts(now: float) -> list[dict]:
    return db.q("SELECT * FROM nfl_prop_capture_attempts WHERE attempted_at>=? "
                "ORDER BY attempted_at DESC", (now - MONTH_SECONDS,))


def _latest_per_game(rows: list[dict]) -> dict[str, dict]:
    latest = {}
    for row in rows:
        for game_id in json.loads(row["target_game_ids"]):
            latest.setdefault(game_id, row)
    return latest


def _frozen_games(now: float) -> set[str]:
    games = set()
    for row in db.q("SELECT freeze_sha,payload FROM nfl_prop_research_freezes WHERE frozen_at<=?", (now,)):
        import hashlib
        if hashlib.sha256(row["payload"].encode()).hexdigest() != row["freeze_sha"]:
            continue
        games.update(q["canonical_game_id"] for q in json.loads(row["payload"]).get("projections", []))
    return games


def _constrained(rows: list[dict], now: float) -> tuple[bool, str | None]:
    requests = sum(r["requests_made"] for r in rows)
    objects = sum(max(r["events_returned"] or 0, len(json.loads(r["target_game_ids"]))) for r in rows)
    if requests >= MAX_LOCAL_REQUESTS_30D or objects >= MAX_LOCAL_OBJECTS_30D:
        return True, "local_30_day_quota_guard"
    for row in rows:
        if row["attempted_at"] < now - QUOTA_BACKOFF:
            break
        if any(code in (row["error"] or "") for code in ("401", "403", "429")) or \
                "quota" in (row["error"] or "").lower():
            return True, "provider_quota_cooldown"
        for headers in json.loads(row["quota_json"]):
            if not isinstance(headers, dict):
                continue
            for key, value in headers.items():
                if "remaining" in key and ("quota" in key or "objects" in key or "credits" in key):
                    try:
                        if float(value) <= 2:
                            return True, "provider_remaining_low"
                    except (TypeError, ValueError):
                        pass
    return False, None


def plan(events: list[dict], now: float, *, key_configured: bool | None = None) -> dict:
    """One batched request per week and due tick; no call before a frozen model exists."""
    if key_configured is None:
        key_configured = bool(settings.sportsgameodds_api_key)
    rows = _attempts(now)
    blocked, reason = _constrained(rows, now)
    frozen = _frozen_games(now)
    last_by_game = _latest_per_game(rows)
    due: dict[tuple[int, int], list[dict]] = {}
    next_at = None
    for event in events:
        game_id = event.get("id", "")
        parts = game_id.split("-")
        if (game_id not in frozen or len(parts) != 6 or parts[2] != "reg" or
                str(event.get("status") or "").upper() in INACTIVE):
            continue
        kickoff = event["kickoff_utc"]
        frequency = cadence(kickoff - now)
        if frequency is None:
            if kickoff > now + 24 * 3600:
                candidate = kickoff - 24 * 3600
                next_at = min(next_at, candidate) if next_at is not None else candidate
            continue
        last = last_by_game.get(game_id)
        wait = frequency
        if last and not last["success"]:
            wait = max(wait, FAILURE_BACKOFF)
        candidate = last["attempted_at"] + wait if last else now
        next_at = min(next_at, candidate) if next_at is not None else candidate
        if now >= candidate:
            due.setdefault((int(parts[1]), int(parts[3])), []).append(event)
    return {"due": {key: sorted(e["id"] for e in value) for key, value in due.items()}
            if key_configured and not blocked else {},
            "next_at": next_at, "key_configured": key_configured,
            "blocked_reason": reason if blocked else "missing_api_key" if not key_configured else None,
            "requests_30d": sum(r["requests_made"] for r in rows),
            "objects_30d_lower_bound": sum(max(r["events_returned"] or 0,
                                                len(json.loads(r["target_game_ids"]))) for r in rows)}


def _omitted_odds(notices: list[str]) -> int | None:
    values = [int(value) for note in notices for value in
              re.findall(r"(\d+)\s+bookmaker\s+odds", note, flags=re.IGNORECASE)]
    return sum(values) if values else None


def capture(season: int, week: int, game_ids: list[str], now: float,
            provider=None) -> dict:
    """Persist the attempt even on offline/timeout/429; never retry inside this call."""
    batch_id = str(uuid.uuid4())
    attempt_id = uuid.uuid4().hex
    result = None
    error = None
    try:
        result = nfl_props.capture_week(season, week, provider=provider,
                                        retrieved_at=now, batch_id=batch_id,
                                        target_game_ids=set(game_ids))
        batch = db.q1("SELECT audit_json FROM nfl_prop_capture_batches WHERE batch_id=?", (batch_id,))
        mapped = {item["canonical_game_id"] for item in json.loads(batch["audit_json"])["events"]
                  if item["mapped"]}
        if not set(game_ids) <= mapped:
            raise ValueError("target event missing or unmapped in SportsGameOdds response")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    requests = db.q("SELECT * FROM nfl_prop_api_request_results WHERE batch_id=? ORDER BY page_number",
                    (batch_id,))
    attempts = db.q1("SELECT COUNT(*) n FROM nfl_prop_api_request_attempts WHERE batch_id=?", (batch_id,))
    quota = [json.loads(r["quota_json"]) for r in requests]
    notices = result.get("provider_notices", []) if result else []
    responses = db.q1("SELECT COUNT(*) n FROM nfl_prop_raw_responses WHERE batch_id=?", (batch_id,))["n"]
    db.ex("INSERT INTO nfl_prop_capture_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (attempt_id, batch_id, season, week, json.dumps(sorted(game_ids)), now, time.time(),
           int(error is None), attempts["n"], responses,
           result.get("events_seen") if result else None,
           result.get("quotes_stored") if result else None,
           json.dumps(quota, sort_keys=True), json.dumps(notices, sort_keys=True),
           _omitted_odds(notices), error))
    if error:
        raise RuntimeError(error)
    return {**result, "capture_attempt_id": attempt_id,
            "requests_made": attempts["n"], "responses_archived": responses,
            "omitted_bookmaker_odds": _omitted_odds(notices)}


def health(now: float | None = None, events: list[dict] | None = None) -> dict:
    stamp = time.time() if now is None else now
    rows = _attempts(stamp)
    latest = rows[0] if rows else None
    last_success = next((r for r in rows if r["success"]), None)
    schedule = plan(events or [], stamp)
    return {"configured": schedule["key_configured"],
            "blocked_reason": schedule["blocked_reason"],
            "last_attempt_at": latest["attempted_at"] if latest else None,
            "last_success_at": last_success["completed_at"] if last_success else None,
            "last_error": latest["error"] if latest and not latest["success"] else None,
            "next_expected_at": schedule["next_at"],
            "requests_30d": schedule["requests_30d"],
            "objects_30d_lower_bound": schedule["objects_30d_lower_bound"],
            "last_response_quota": json.loads(latest["quota_json"]) if latest else [],
            "last_provider_notices": json.loads(latest["provider_notices"]) if latest else [],
            "last_omitted_bookmaker_odds": latest["omitted_odds_count"] if latest else None}
