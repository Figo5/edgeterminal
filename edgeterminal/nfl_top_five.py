"""Five prospective prop selections per game, with a statistical W/L record.

Model family, training cutoff and residual sigma are inherited from the original
lab. New completed weeks update input features only. Selections never backfill
completed games and are locked at the first complete, fresh five-prop board.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from collections import Counter, defaultdict

from . import db, nfl_props, nfl_prop_capture_ops, nfl_prop_grading, nfl_prop_lab, nfl_prop_result_ingest
from .config import ROOT, settings
from .integrity import parse_time

POLICY = "top-five-confidence-v1"
BOOKS = 'draftkings,fanduel,caesars,espnbet,bovada'


def provider():
    # Model labels differ from feed stat IDs (receptions is receiving_receptions).
    # Keep requests bounded to modeled markets and the books already in use.
    stat_ids = {market: stat for stat, market in nfl_props.STAT_MARKETS.items()}
    ids = ",".join(f"{stat_ids[m]}-PLAYER_ID-game-ou-{side}"
                   for m in nfl_prop_lab.MARKETS for side in ("over", "under"))
    return nfl_props.SportsGameOddsProps(odd_ids=ids, bookmaker_ids=BOOKS)


def next_capture(event: dict, last: dict | None, now: float) -> float | None:
    kickoff = parse_time(event['start_time'])
    if (not kickoff or now >= kickoff or
            str(event.get('status') or '').upper() in nfl_prop_capture_ops.INACTIVE):
        return None
    frequency = nfl_prop_capture_ops.cadence(kickoff - now) or 6 * 3600
    if last and not last['success']:
        frequency = max(frequency, nfl_prop_capture_ops.FAILURE_BACKOFF)
    return max(kickoff - 7 * 86400, last['attempted_at'] + frequency if last else now)


def coverage(game_ids: list[str], now: float | None = None) -> list[dict]:
    """Explain missing selections from stored captures; never fetch or model."""
    stamp = time.time() if now is None else now
    attempts = nfl_prop_capture_ops._attempts(stamp)
    latest = nfl_prop_capture_ops._latest_per_game(attempts)
    blocked, reason = nfl_prop_capture_ops._constrained(attempts, stamp)
    locked = {r['game_id'] for r in db.q('SELECT game_id FROM nfl_top_five_selections')}
    batches, output = {}, []
    for game_id in game_ids:
        event = db.q1('SELECT start_time,status FROM events WHERE id=?', (game_id,))
        last = latest.get(game_id)
        due_at = next_capture(event, last, stamp) if event else None
        batch_id = last['batch_id'] if last else None
        if batch_id and batch_id not in batches:
            pairs, _ = nfl_prop_lab._pair_quotes(batch_id)
            freeze = db.q1('SELECT freeze_sha,payload FROM nfl_prop_research_freezes WHERE source_batch_id=?', (batch_id,))
            modeled = None
            if freeze:
                if hashlib.sha256(freeze['payload'].encode()).hexdigest() != freeze['freeze_sha']:
                    raise ValueError('coverage freeze integrity mismatch')
                modeled = json.loads(freeze['payload']).get('projections', [])
            batches[batch_id] = pairs, modeled
        pairs, modeled = batches.get(batch_id, ([], None))
        count = len({(r['player_id'], r['market']) for r in pairs if r['canonical_game_id'] == game_id})
        eligible = len({(r['player_id'], r['market']) for r in modeled if r['canonical_game_id'] == game_id}) if modeled is not None else None
        if game_id in locked:
            status, message = 'locked', 'Five selections locked before kickoff.'
        elif due_at is None:
            status, message = 'not_selected', 'No prospective selections recorded. Past games are never filled in later.'
        elif not settings.sportsgameodds_api_key:
            status, message = 'missing_source', 'Player prop source is not configured.'
        elif blocked:
            status, message = 'source_paused', 'Prop updates are paused by the source quota guard.'
        elif last and not last['success']:
            status, message = 'capture_failed', 'The last prop capture failed. A bounded retry is scheduled.'
        elif not last:
            status, message = 'awaiting_capture', 'Waiting for the first scheduled prop capture.'
        elif eligible is not None and eligible >= 5:
            status, message = 'selection_needed', 'Five supported markets are available; selection needs review.'
        elif count >= 5 and (eligible is None or eligible < 5):
            status, message = 'projection_gap', 'Lines were returned, but fewer than five have verified player identities and model inputs.'
        elif count:
            noun = 'market' if count == 1 else 'markets'
            status, message = 'partial_lines', f'{count} distinct supported main-line {noun} returned. Five are required.'
        else:
            status, message = 'waiting_lines', 'The latest feed response returned no complete supported prop lines for this game.'
        output.append({'game_id': game_id, 'status': status, 'message': message,
                       'main_line_markets': count, 'modeled_markets': eligible,
                       'last_checked_at': last['attempted_at'] if last else None,
                       'next_capture_at': due_at if not blocked and game_id not in locked else None,
                       'blocked_reason': reason if blocked and game_id not in locked else None,
                       'source': 'SportsGameOdds'})
    return output


def rank(projections: list[dict]) -> list[dict]:
    """One strongest side per player/market, across observed main-line books."""
    rows = sorted(projections, key=lambda r: (-max(r["probability_over"], r["probability_under"]),
                                             r["player_id"], r["market"], r["sportsbook"], r["quote_id"]))
    seen, selected = set(), []
    for row in rows:
        side = "over" if row["probability_over"] >= row["probability_under"] else "under"
        key = (row["player_id"], row["market"])
        if row["side"] != side or key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) == 5:
            break
    return selected


def lock(freeze_sha: str, now: float) -> dict:
    frozen = nfl_prop_grading._freeze(freeze_sha)
    grouped = defaultdict(list)
    for row in frozen["projections"]:
        if row["observed_at"] <= now < row["kickoff_utc"] and now - row["observed_at"] <= 86400:
            grouped[row["canonical_game_id"]].append(row)
    counts = Counter()
    for game, rows in grouped.items():
        if db.q1("SELECT 1 FROM nfl_top_five_selections WHERE game_id=?", (game,)):
            continue
        event = db.q1("SELECT start_time,status FROM events WHERE id=?", (game,))
        current_kickoff = parse_time(event["start_time"]) if event else None
        if (not current_kickoff or now >= current_kickoff or
                event["status"] in nfl_prop_capture_ops.INACTIVE):
            continue
        seal = db.q1("SELECT frozen_at FROM nfl_prop_research_freezes WHERE freeze_sha=?", (freeze_sha,))
        if not seal or now < seal["frozen_at"]:
            raise ValueError("selection predates its model freeze")
        top = rank(rows)
        if len(top) < 5:
            counts["awaiting_five_props"] += 1
            continue
        payload = json.dumps({"policy": POLICY, "game_id": game, "selected_at": now,
                              "freeze_sha": freeze_sha, "selections": top}, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        db.ex("INSERT OR IGNORE INTO nfl_top_five_selections VALUES(?,?,?,?,?)",
              (game, freeze_sha, now, payload, hashlib.sha256(payload.encode()).hexdigest()))
        counts["games_selected"] += 1
    return dict(counts)


def view(game_id: str | None = None) -> dict:
    records = db.q("SELECT * FROM nfl_top_five_selections" +
                   (" WHERE game_id=?" if game_id else "") + " ORDER BY selected_at DESC,game_id",
                   (game_id,) if game_id else ())
    games, counts, sources = [], Counter(), {}
    for record in records:
        if hashlib.sha256(record["payload"].encode()).hexdigest() != record["sha256"]:
            raise ValueError("top five selection integrity mismatch")
        payload = json.loads(record["payload"])
        if (payload.get("policy") != POLICY or payload.get("game_id") != record["game_id"] or
                payload.get("freeze_sha") != record["freeze_sha"] or
                payload.get("selected_at") != record["selected_at"] or
                len(payload.get("selections", [])) != 5):
            raise ValueError("invalid top five selection record")
        if record["freeze_sha"] not in sources:
            frozen = nfl_prop_grading._freeze(record["freeze_sha"])
            sources[record["freeze_sha"]] = (
                {r["quote_id"]: r for r in frozen["projections"]},
                {r["quote_id"]: r for r in nfl_prop_grading._latest(record["freeze_sha"])})
        frozen_rows, grades = sources[record["freeze_sha"]]
        rows = []
        for n, q in enumerate(payload["selections"], 1):
            if (frozen_rows.get(q["quote_id"]) != q or
                    q["canonical_game_id"] != record["game_id"] or
                    not q["observed_at"] <= record["selected_at"] < q["kickoff_utc"]):
                raise ValueError("top five selection source mismatch")
            grade = grades.get(q["quote_id"], {})
            result = grade.get("research_statistical_result", "pending")
            counts[result] += 1
            rows.append({**q, "rank": n, "player": q["player_name"],
                         "stronger_side": q["side"], "confidence": q["probability_" + q["side"]],
                         "quote_observed_at": q["observed_at"], "result": result,
                         "actual": grade.get("actual_stat"), "graded_at": grade.get("graded_at"),
                         "revision": grade.get("revision"), "selected_at": record["selected_at"]})
        games.append({"game_id": record["game_id"], "selected_at": record["selected_at"],
                      "freeze_sha": record["freeze_sha"], "rows": rows})
    decisions = counts["won"] + counts["lost"]
    return {"policy": POLICY, "games": games, "game_count": len(games), "record": dict(counts),
            "win_rate": counts["won"] / decisions if decisions else None,
            "selection_count": sum(counts.values()), "readiness": "RESEARCH_ONLY"}


def _inputs(season: int, week: int) -> tuple[dict, dict, dict]:
    if season != 2026:
        raise ValueError("prop model is validated for the 2026 research season")
    directory = ROOT / "data/nflverse/prop_live"
    directory.mkdir(parents=True, exist_ok=True)
    raw = nfl_prop_result_ingest._fetch(nfl_prop_result_ingest.STATS_URL.format(season=season))
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows or not {"player_id", "game_id", "week", "season", "season_type", *nfl_prop_lab.USAGE} <= rows[0].keys():
        raise ValueError("current player history lacks required columns")
    if max(int(r["week"]) for r in rows if r["season"] == str(season) and r["season_type"] == "REG") < week - 1:
        raise ValueError("current player history is missing the previous week's results")
    digest = hashlib.sha256(raw).hexdigest()
    (directory / f"{digest}.csv").write_bytes(raw)
    path = directory / f"stats_player_week_{season}.csv"
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)
    train, _, training_hashes = nfl_prop_lab.build_dataset()
    _, history, feature_hashes = nfl_prop_lab.build_dataset((season, week), directory)
    return train, history, {**{"training:" + k: v for k, v in training_hashes.items()},
                            **{"features:" + k: v for k, v in feature_hashes.items()}}


def capture_missing(now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    current = db.kv_get("nfl_current_schedule") or {}
    targets = []
    for game in current.get("game_ids", []):
        event = db.q1("SELECT start_time,status FROM events WHERE id=?", (game,))
        kickoff = parse_time(event["start_time"]) if event else None
        if kickoff and 0 < kickoff - stamp <= 7 * 86400 and event["status"] not in nfl_prop_capture_ops.INACTIVE:
            if not db.q1("SELECT 1 FROM nfl_top_five_selections WHERE game_id=?", (game,)):
                targets.append(game)
    if not targets:
        return {"status": "no_missing_selections"}
    if not settings.sportsgameodds_api_key:
        return {"status": "missing_prop_api_key", "games_without_five": targets}
    attempts = nfl_prop_capture_ops._attempts(stamp)
    blocked, reason = nfl_prop_capture_ops._constrained(attempts, stamp)
    if blocked:
        return {"status": reason, "games_without_five": targets}
    # Early-week captures are batched every six hours; missing boards retry
    # more often near kickoff using the existing bounded capture cadence.
    recent = nfl_prop_capture_ops._latest_per_game(attempts)
    def due(game):
        event = db.q1("SELECT start_time,status FROM events WHERE id=?", (game,))
        candidate = next_capture(event, recent.get(game), stamp)
        return candidate is not None and stamp >= candidate
    targets = [g for g in targets if due(g)]
    if not targets:
        return {"status": "awaiting_next_prop_capture"}
    season, week = int(current["season"]), int(current["week"])
    train, history, hashes = _inputs(season, week)
    validation_path = ROOT / "data/research/nfl_prop_lab_2026_week3.json"
    validation = json.loads(validation_path.read_text())["validation"]
    hashes["model_selection"] = hashlib.sha256(validation_path.read_bytes()).hexdigest()
    capture = nfl_prop_capture_ops.capture(season, week, targets, stamp, provider=provider())
    report = nfl_prop_lab.project_week3(train, history, validation, capture["batch_id"],
                                       season=season, week=week, stats_dir=ROOT / "data/nflverse/prop_live")
    # Keep complete pairs for grading and provenance; choose only five strongest
    # sides for the visible research record.
    if not report["projections"]:
        return {"status": "no_projectable_props", "games_without_five": targets,
                "capture": capture}
    sealed = nfl_prop_lab.seal_forward(report, hashes)
    selected = lock(sealed["freeze_sha"], time.time())
    return {"status": "captured", **selected, "capture": capture, "freeze": sealed}


def grade_due(now: float) -> dict:
    results, failures = [], []
    for row in db.q("SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at"):
        sha = row["freeze_sha"]
        try:
            frozen = nfl_prop_grading._freeze(sha)
            if any(q["kickoff_utc"] + nfl_prop_grading.POSTGAME_DELAY_S <= now for q in frozen["projections"]):
                results.append(nfl_prop_result_ingest.sync(sha, now=now))
        except Exception as exc:
            failures.append(f"{sha}: {type(exc).__name__}")
    if failures:
        raise ValueError("prop grading needs review for freeze(s): " + ", ".join(failures))
    return {"freezes_checked": len(results), "results": results}
