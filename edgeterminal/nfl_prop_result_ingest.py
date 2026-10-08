"""Archive postgame prop sources, then invoke append-only research grading.

The default score source is ESPN's week scoreboard and the default player-stat
source is the nflverse weekly player-stat release. Neither is a sportsbook
settlement feed. Missing participation requires a reviewed NFL Game Book
attestation; no missing row is silently converted to zero or inactivity.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlencode

from . import db, nfl_identity, nfl_prop_grading, nfl_prop_lab
from .integrity import parse_time
from .providers.base import client
from .providers.espn import SCOREBOARD_URL

STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
FORWARD_REPORT_PATH = nfl_prop_lab.ROOT / "data/research/nfl_prop_forward_report.json"
REQUIRED_STATS = {"game_id", "player_id", "season", "week", "season_type", "team",
                  "opponent_team", *nfl_prop_lab.LABEL.values()}


def _fetch(url: str) -> bytes:
    response = client().get(url, timeout=25)
    response.raise_for_status()
    return response.content


def _source_file(digest: str) -> str:
    return f"db:nfl_prop_grade_source_artifacts:{digest}"


def _archive(freeze_sha: str, game_id: str, kind: str, raw: bytes,
             url: str, retrieved_at: float, metadata: dict) -> dict:
    digest = hashlib.sha256(raw).hexdigest()
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)",
                  (digest, url, retrieved_at, raw))
        c.execute("INSERT OR IGNORE INTO nfl_prop_result_ingestions VALUES(?,?,?,?,?,?,?,?)",
                  (freeze_sha, game_id, kind, digest, url, retrieved_at, retrieved_at,
                   json.dumps(metadata, sort_keys=True)))
    saved = db.q1("SELECT * FROM nfl_prop_result_ingestions WHERE freeze_sha=? AND game_id=? "
                  "AND source_kind=? AND source_sha256=?",
                  (freeze_sha, game_id, kind, digest))
    return {"source": saved["source_url"], "file": _source_file(digest),
            "sha256": digest, "retrieved_at": saved["retrieved_at"]}


def _latest_source(freeze_sha: str, game_id: str, kind: str) -> dict | None:
    row = db.q1("SELECT * FROM nfl_prop_result_ingestions WHERE freeze_sha=? AND game_id=? "
                "AND source_kind=? ORDER BY observed_at DESC, rowid DESC LIMIT 1",
                (freeze_sha, game_id, kind))
    if not row:
        return None
    return {"source": row["source_url"], "file": _source_file(row["source_sha256"]),
            "sha256": row["source_sha256"], "retrieved_at": row["retrieved_at"]}


def _raw(source: dict) -> bytes:
    row = db.q1("SELECT payload FROM nfl_prop_grade_source_artifacts WHERE sha256=?",
                (source["sha256"],))
    if not row or hashlib.sha256(bytes(row["payload"])).hexdigest() != source["sha256"]:
        raise ValueError("archived result source hash mismatch")
    return bytes(row["payload"])


def _games(freeze_sha: str) -> dict[str, list[dict]]:
    frozen = nfl_prop_grading._freeze(freeze_sha)
    games = defaultdict(list)
    for quote in frozen["projections"]:
        games[quote["canonical_game_id"]].append(quote)
    return dict(games)


def _expected_kickoff(game_id: str, quotes: list[dict]) -> float:
    frozen = min(q["kickoff_utc"] for q in quotes)
    event = db.q1("SELECT start_time FROM events WHERE id=?", (game_id,))
    current = parse_time(event["start_time"]) if event else None
    return min(frozen, current) if current is not None else frozen


def _schedule_ids(games: dict[str, list[dict]]) -> dict[str, str]:
    schedule = nfl_prop_lab._schedule()
    result = {}
    for game_id, row in schedule.items():
        if row["game_type"] != "REG":
            continue
        canonical = (f"nfl-{row['season']}-reg-{int(row['week']):02d}-"
                     f"{nfl_prop_lab._team(row['away_team'])}-"
                     f"{nfl_prop_lab._team(row['home_team'])}")
        if canonical in games:
            if canonical in result:
                raise ValueError("duplicate nflverse game mapping")
            result[canonical] = game_id
    return result


def _scoreboard(raw: bytes, games: dict[str, list[dict]]) -> dict[str, dict]:
    try:
        data = json.loads(raw)
        sample = next(iter(games))
        parts = sample.split("-")
        if (int(data["season"]["year"]) != int(parts[1]) or
                int(data["season"]["type"]) != 2 or
                int(data["week"]["number"]) != int(parts[3])):
            raise ValueError("scoreboard season/week mismatch")
        events = data["events"]
        if not isinstance(events, list):
            raise ValueError("scoreboard events malformed")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("scoreboard response malformed") from exc
    mapped = {}
    for event in events:
        if not isinstance(event, dict) or not event.get("id"):
            raise ValueError("scoreboard event malformed")
        canonical = db.mapped_nfl_game("espn", f"espn-{event['id']}")
        if canonical not in games:
            continue
        if canonical in mapped:
            raise ValueError("duplicate mapped ESPN event")
        competitors = (event.get("competitions") or [{}])[0].get("competitors") or []
        home = [r for r in competitors if r.get("homeAway") == "home"]
        away = [r for r in competitors if r.get("homeAway") == "away"]
        if len(home) != 1 or len(away) != 1:
            raise ValueError("scoreboard lacks unique home/away")
        home_code = nfl_identity.team_code((home[0].get("team") or {}).get("displayName"))
        away_code = nfl_identity.team_code((away[0].get("team") or {}).get("displayName"))
        if canonical != f"{sample.split('-')[0]}-{parts[1]}-reg-{parts[3]}-{away_code}-{home_code}":
            raise ValueError("mapped ESPN game identity conflicts with teams")
        kickoff = parse_time(event.get("date"))
        if kickoff is None:
            raise ValueError("scoreboard event lacks kickoff")
        status = (event.get("status") or {}).get("type") or {}
        state = str(status.get("name") or "").upper()
        complete = status.get("completed") is True
        score = None
        if state == "STATUS_FINAL":
            if not complete:
                raise ValueError("ESPN final flag conflicts with completion")
            try:
                h, a = home[0]["score"], away[0]["score"]
                if any(isinstance(x, bool) for x in (h, a)):
                    raise ValueError("boolean score")
                score = {"home": int(h), "away": int(a)}
                if any(x < 0 or str(x) != str(y) for x, y in ((score["home"], h), (score["away"], a))):
                    raise ValueError("invalid final score")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("ESPN final lacks valid score") from exc
        elif complete:
            raise ValueError("unrecognized completed ESPN status")
        normalized_status = {"STATUS_POSTPONED": "postponed", "STATUS_CANCELED": "canceled",
                             "STATUS_SUSPENDED": "suspended", "STATUS_IN_PROGRESS": "in_progress",
                             "STATUS_SCHEDULED": "not_started"}.get(state, state.lower())
        mapped[canonical] = {"status": "final" if score is not None else normalized_status,
                             "score": score, "kickoff": kickoff,
                             "provider_event_id": f"espn-{event['id']}"}
    return mapped


def _stats(raw: bytes, nflverse_id: str, canonical: str) -> dict[str, dict]:
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        if not reader.fieldnames or not REQUIRED_STATS <= set(reader.fieldnames):
            raise ValueError("player-stat source lacks required columns")
        rows = [r for r in reader if r.get("game_id") == nflverse_id]
    except UnicodeDecodeError as exc:
        raise ValueError("player-stat source is not UTF-8 CSV") from exc
    if len(rows) < 50:
        raise ValueError("player-stat game coverage incomplete")
    parts = canonical.split("-")
    teams = {parts[-2].upper(), parts[-1].upper()}
    teams = {nfl_identity.team_code(t) for t in teams}
    counts = Counter()
    found = {}
    for row in rows:
        if (row["season"] != parts[1] or int(row["week"]) != int(parts[3]) or
                row["season_type"] != "REG"):
            raise ValueError("player-stat season/week identity mismatch")
        team, opponent = nfl_identity.team_code(row["team"]), nfl_identity.team_code(row["opponent_team"])
        if team not in teams or opponent not in teams or team == opponent:
            raise ValueError("player-stat team identity mismatch")
        gsis = row["player_id"]
        if not gsis:
            # nflverse can include an anonymous team-penalty aggregate row.
            # Ignore it only when it carries no player name or modeled stat.
            if (row.get("player_name") or row.get("player_display_name") or
                    any(row.get(label) not in (None, "", "0", "0.0")
                        for label in nfl_prop_lab.LABEL.values())):
                raise ValueError("unidentified player-stat row has modeled data")
            continue
        if gsis in found:
            raise ValueError("missing or duplicate GSIS player ID")
        for market, label in nfl_prop_lab.LABEL.items():
            if row[label] not in (None, ""):
                nfl_prop_grading._number(row[label], nonnegative=market in {"receptions", "rushing_attempts"})
        found[gsis] = row
        counts[team] += 1
    if len(found) < 50 or len(counts) != 2 or min(counts.values()) < 20:
        raise ValueError("player-stat team coverage incomplete")
    return found


def _participation(raw: bytes, game: str, frozen_ids: set[str]) -> tuple[dict, dict]:
    try:
        data = json.loads(raw)
        if data["canonical_game_id"] != game or not data.get("reviewer"):
            raise ValueError("participation attestation identity or reviewer missing")
        if isinstance(data.get("reviewed_at"), bool) or not isinstance(data.get("reviewed_at"), (int, float)):
            raise ValueError("participation attestation needs review timestamp")
        source_url = data["source_url"]
        if not isinstance(source_url, str) or not (
                source_url.startswith("https://www.nfl.com/") or
                source_url.startswith("https://static.clubs.nfl.com/")):
            raise ValueError("participation needs NFL Game Book URL")
        source_path = Path(data["source_file"])
        source_bytes = source_path.read_bytes()
        if not source_bytes.startswith(b"%PDF-"):
            raise ValueError("participation source is not a Game Book PDF")
        if hashlib.sha256(source_bytes).hexdigest() != data["source_sha256"]:
            raise ValueError("Game Book source SHA-256 mismatch")
        players = {}
        for player in data["players"]:
            gsis = player["gsis_player_id"]
            if gsis not in frozen_ids or gsis in players:
                raise ValueError("participation player identity ambiguous")
            active, snap = player["active_roster"], player["snap_played"]
            if not isinstance(active, bool) or not isinstance(snap, bool) or snap and not active:
                raise ValueError("inconsistent participation status")
            if not isinstance(player.get("certified_no_stats"), bool):
                raise ValueError("participation needs no-stat attestation")
            players[gsis] = player
    except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
        raise ValueError("malformed participation attestation") from exc
    return data, {"players": players, "source_bytes": source_bytes,
                  "reviewer": data["reviewer"], "reviewed_at": data["reviewed_at"]}


def _player_evidence(quotes: list[dict], stats: dict[str, dict], stat_source: dict,
                     participation: dict | None, participation_source: dict | None) -> tuple[list[dict], int]:
    needed = defaultdict(set)
    for quote in quotes:
        needed[quote["gsis_player_id"]].add(quote["market"])
    players, pending = [], 0
    attested = (participation or {}).get("players") or {}
    for gsis, markets in needed.items():
        row = stats.get(gsis)
        reviewed = attested.get(gsis)
        values = {}
        if row:
            for market in markets:
                value = row.get(nfl_prop_lab.LABEL[market])
                if value not in (None, ""):
                    values[market] = nfl_prop_grading._number(value,
                        nonnegative=market in {"receptions", "rushing_attempts"})
        if reviewed and reviewed["certified_no_stats"] and not row:
            values = {market: 0.0 for market in markets}
        active_by_stats = bool(row and nfl_prop_grading._documented_participation(row))
        if reviewed:
            status = "active" if reviewed["active_roster"] else "inactive"
            if active_by_stats and not reviewed["snap_played"]:
                raise ValueError("Game Book attestation conflicts with recorded player action")
            source = {**participation_source, "active_roster": reviewed["active_roster"],
                      "snap_played": reviewed["snap_played"], "reviewer": participation["reviewer"],
                      "reviewed_at": participation["reviewed_at"],
                      "manifest_sha256": participation["manifest_sha256"]}
        elif active_by_stats:
            status = "active"
            source = {**stat_source, "active_roster": True, "snap_played": True,
                      "inference": "nonzero recorded game action"}
        else:
            status, source = "unknown", None
        if status == "unknown" or (status == "active" and any(m not in values for m in markets)):
            pending += 1
        item = {"gsis_player_id": gsis, "status": status,
                "stats": values if status == "active" else {}}
        if source:
            item["participation_evidence"] = source
        players.append(item)
    return players, pending


def status_view(freeze_sha: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    games = _games(freeze_sha)
    details = []
    for game_id, quotes in sorted(games.items()):
        scoreboard = _latest_source(freeze_sha, game_id, "scoreboard")
        stat_source = _latest_source(freeze_sha, game_id, "player_stats")
        state = None
        if scoreboard:
            row = db.q1("SELECT metadata_json FROM nfl_prop_result_ingestions WHERE "
                        "freeze_sha=? AND game_id=? AND source_kind='scoreboard' AND source_sha256=?",
                        (freeze_sha, game_id, scoreboard["sha256"]))
            state = json.loads(row["metadata_json"])
        kickoff = state["kickoff"] if state else _expected_kickoff(game_id, quotes)
        graded = db.q1("SELECT COUNT(DISTINCT quote_id) n FROM nfl_prop_grade_revisions WHERE "
                      "freeze_sha=? AND quote_id IN (" + ",".join("?" for _ in quotes) + ")",
                      (freeze_sha, *(q["quote_id"] for q in quotes)))["n"]
        history = db.q("SELECT quote_id,revision,payload FROM nfl_prop_grade_revisions WHERE "
                       "freeze_sha=? AND quote_id IN (" +
                       ",".join("?" for _ in quotes) + ") ORDER BY quote_id,revision",
                       (freeze_sha, *(q["quote_id"] for q in quotes)))
        revisions = 0
        correction_evidence = set()
        prior = {}
        for entry in history:
            value = json.loads(entry["payload"])
            before = prior.get(entry["quote_id"])
            if before and (before.get("actual_stat"), before.get("final_score")) != (
                    value.get("actual_stat"), value.get("final_score")):
                revisions += 1
                correction_evidence.add(value.get("evidence_sha"))
            prior[entry["quote_id"]] = value
        pending = None
        if stat_source and state and state.get("status") == "final":
            try:
                nflverse_id = _schedule_ids({game_id: quotes})[game_id]
                stats = _stats(_raw(stat_source), nflverse_id, game_id)
                manifest_source = _latest_source(freeze_sha, game_id, "participation_manifest")
                manifest = json.loads(_raw(manifest_source)) if manifest_source else None
                reviewed = {p["gsis_player_id"]: p for p in manifest["players"]} if manifest else {}
                needed = defaultdict(set)
                for quote in quotes:
                    needed[quote["gsis_player_id"]].add(quote["market"])
                pending = 0
                for gsis, markets in needed.items():
                    row = stats.get(gsis)
                    review = reviewed.get(gsis)
                    if not review and not (row and nfl_prop_grading._documented_participation(row)):
                        pending += 1
                    elif review and review["active_roster"] and not row and not review["certified_no_stats"]:
                        pending += 1
                    elif row and (review is None or review["active_roster"]) and any(
                            row.get(nfl_prop_lab.LABEL[m]) in (None, "") for m in markets):
                        pending += 1
            except (ValueError, KeyError):
                pending = None
        if graded == len(quotes):
            label = "graded"
        elif state and state.get("status") in {"postponed", "canceled", "suspended"}:
            label = state["status"]
        elif not state or state.get("status") != "final":
            label = "not_started" if now < kickoff else "in_progress"
        elif now < kickoff + nfl_prop_grading.POSTGAME_DELAY_S:
            label = "final_wait"
        elif not stat_source:
            label = "source_review" if _latest_source(freeze_sha, game_id, 'player_stats_incomplete') else "awaiting_player_stats"
        else:
            label = "ready_for_grading" if pending == 0 else "participation_pending"
        details.append({"game_id": game_id, "status": label, "frozen_quotes": len(quotes),
                        "graded_quotes": graded, "players_pending_participation": pending,
                        "source_corrections_detected": len(correction_evidence),
                        "corrected_quote_revisions": revisions,
                        "latest_scoreboard_sha256": scoreboard["sha256"] if scoreboard else None,
                        "latest_player_stat_sha256": stat_source["sha256"] if stat_source else None,
                        "actual_kickoff_utc": kickoff})
    counts = Counter(r["status"] for r in details)
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS",
            "freeze_sha": freeze_sha, "as_of": now,
            "counts": dict(counts), "games": details,
            "players_pending_participation": sum(r["players_pending_participation"] or 0 for r in details),
            "source_corrections_detected": sum(r["source_corrections_detected"] for r in details)}


def sync(freeze_sha: str, *, scoreboard_bytes: bytes | None = None,
         stats_bytes: bytes | None = None, participation_bytes: bytes | None = None,
         scoreboard_source: str | None = None, stats_source: str | None = None,
         now: float | None = None) -> dict:
    now = time.time() if now is None else now
    games = _games(freeze_sha)
    first = next(iter(games))
    season, week = int(first.split("-")[1]), int(first.split("-")[3])
    if not all(q["canonical_game_id"].split("-")[1:4] == first.split("-")[1:4]
               for rows in games.values() for q in rows):
        raise ValueError("one sync requires a single frozen season/week")
    due = {game: quotes for game, quotes in games.items()
           if _expected_kickoff(game, quotes) <= now}
    if not due:
        return {"grade_counts": [], "status": status_view(freeze_sha, now),
                "network_requests": 0}
    requests = 0
    official_score_url = SCOREBOARD_URL + "?" + urlencode({"dates": season, "seasontype": 2,
                                                             "week": week, "limit": 100})
    score_url = scoreboard_source or official_score_url
    if scoreboard_bytes is not None and not scoreboard_source:
        raise ValueError("supplied scoreboard bytes require an explicit source URL")
    if scoreboard_bytes is None and scoreboard_source:
        raise ValueError("scoreboard source override requires supplied bytes")
    if scoreboard_bytes is None:
        scoreboard_bytes = _fetch(official_score_url)
        requests += 1
    events = _scoreboard(scoreboard_bytes, games)
    missing = set(due) - set(events)
    if missing:
        raise ValueError(f"scoreboard missing mapped frozen game(s): {sorted(missing)}")
    for game, state in events.items():
        if game in due:
            _archive(freeze_sha, game, "scoreboard", scoreboard_bytes, score_url,
                     now, state)
    final_due = {game: state for game, state in events.items()
                 if game in due and state["status"] == "final" and
                 now >= state["kickoff"] + nfl_prop_grading.POSTGAME_DELAY_S}
    if not final_due:
        return {"grade_counts": [], "status": status_view(freeze_sha, now),
                "network_requests": requests}
    official_stats_url = STATS_URL.format(season=season)
    stats_url = stats_source or official_stats_url
    if stats_bytes is not None and not stats_source:
        raise ValueError("supplied player-stat bytes require an explicit source URL")
    if stats_bytes is None and stats_source:
        raise ValueError("player-stat source override requires supplied bytes")
    if stats_bytes is None:
        stats_bytes = _fetch(official_stats_url)
        requests += 1
    schedule = _schedule_ids(games)
    participation_data = None
    if participation_bytes is not None:
        try:
            participation_data = json.loads(participation_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError("participation attestation is not JSON") from exc
        if not isinstance(participation_data, dict) or participation_data.get("canonical_game_id") not in games:
            raise ValueError("participation attestation is not for a frozen game")
    grade_counts = []
    for game, state in final_due.items():
        if game not in schedule:
            raise ValueError("frozen game has no unique nflverse schedule identity")
        try:
            stats = _stats(stats_bytes, schedule[game], game)
        except ValueError as exc:
            # Archive the exact received bytes, but keep the game pending.
            _archive(freeze_sha, game, "player_stats_incomplete", stats_bytes,
                     stats_url, now, {"status": "incomplete", "reason": str(exc)})
            continue
        stat_source = _archive(freeze_sha, game, "player_stats", stats_bytes,
                               stats_url, now, {"nflverse_game_id": schedule[game],
                                                "player_rows": len(stats)})
        part, part_source = None, None
        if participation_data is not None and participation_data.get("canonical_game_id") == game:
            validated, part = _participation(participation_bytes, game,
                                             {q["gsis_player_id"] for q in games[game]})
            if not state["kickoff"] <= validated["reviewed_at"] <= now:
                raise ValueError("participation attestation timestamp is outside completed game")
            raw_book = part["source_bytes"]
            part_source = _archive(freeze_sha, game, "participation_gamebook", raw_book,
                                   validated["source_url"], now, {"reviewer": validated["reviewer"]})
            manifest_source = _archive(freeze_sha, game, "participation_manifest", participation_bytes,
                                       "local:reviewed-gamebook-attestation", now,
                                       {"gamebook_sha256": part_source["sha256"],
                                        "reviewer": validated["reviewer"]})
            part["manifest_sha256"] = manifest_source["sha256"]
        players, pending = _player_evidence(games[game], stats, stat_source,
                                            part, part_source)
        score_source = _latest_source(freeze_sha, game, "scoreboard")
        retrieved = max(score_source["retrieved_at"], stat_source["retrieved_at"],
                        part_source["retrieved_at"] if part_source else 0)
        evidence = {"game_id": game, "game_status": "final",
                    "final_score": state["score"],
                    "actual_kickoff_utc": state["kickoff"],
                    "final_game_source": score_source,
                    "player_stat_source": stat_source,
                    "result_retrieved_at": retrieved, "players": players}
        outcome = nfl_prop_grading.grade_freeze(freeze_sha, [evidence], now=now)
        outcome["game_id"] = game
        outcome["players_pending_participation"] = pending
        grade_counts.append(outcome)
    if any(item["new_revisions"] for item in grade_counts):
        FORWARD_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        FORWARD_REPORT_PATH.write_text(json.dumps(nfl_prop_grading.forward_report(), indent=2,
                                                  sort_keys=True) + "\n")
    return {"grade_counts": grade_counts, "status": status_view(freeze_sha, now),
            "network_requests": requests}


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only NFL prop result ingestion")
    parser.add_argument("action", choices=("sync", "status"))
    parser.add_argument("--freeze-sha")
    parser.add_argument("--scoreboard-file", type=Path)
    parser.add_argument("--scoreboard-source-url")
    parser.add_argument("--stats-file", type=Path)
    parser.add_argument("--stats-source-url")
    parser.add_argument("--participation-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    freeze_sha = args.freeze_sha or db.q1(
        "SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at DESC LIMIT 1")["freeze_sha"]
    if args.action == "status":
        result = status_view(freeze_sha)
    else:
        result = sync(freeze_sha,
                      scoreboard_bytes=args.scoreboard_file.read_bytes() if args.scoreboard_file else None,
                      stats_bytes=args.stats_file.read_bytes() if args.stats_file else None,
                      participation_bytes=args.participation_file.read_bytes() if args.participation_file else None,
                      scoreboard_source=args.scoreboard_source_url,
                      stats_source=args.stats_source_url)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
