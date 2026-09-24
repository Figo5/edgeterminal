"""Research-only NFL player-prop capture from SportsGameOdds v2.

Capture is an explicit command. API board reads only stored quotes and never
calls a provider or enters the game's recommendation/pick pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from . import db, nfl_identity
from .config import settings
from .providers.base import ProviderError, get_raw_meta
from .providers.normalize import normalize_book

PROVIDER = "sportsgameodds"
URL = "https://api.sportsgameodds.com/v2/events"
STAT_MARKETS = {
    "passing_yards": "passing_yards",
    "passing_touchdowns": "passing_touchdowns",
    "passing_interceptions": "interceptions",
    "rushing_yards": "rushing_yards",
    "rushing_attempts": "rushing_attempts",
    "receiving_receptions": "receptions",
    "receiving_yards": "receiving_yards",
}
INACTIVE = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_FINAL",
            "POSTPONED", "CANCELED", "CANCELLED", "FINAL"}
QUOTE_COLUMNS = (
    "batch_id", "canonical_game_id", "provider_event_id", "player_id",
    "provider_player_id", "player_name", "team", "opponent", "market",
    "line", "side", "price", "sportsbook", "retrieved_at", "kickoff_utc",
    "provider", "source", "provider_updated_at", "line_type", "offer_index",
)


def _warn(warnings: dict[str, int], reason: str) -> None:
    warnings[reason] = warnings.get(reason, 0) + 1


def _rate_headers(headers: dict) -> dict:
    """Keep quota evidence without archiving cookies or unrelated headers."""
    return {key.lower(): value for key, value in headers.items()
            if any(part in key.lower() for part in
                   ("ratelimit", "rate-limit", "quota", "remaining", "retry-after", "credits", "request-id"))}


def _utc(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    result = re.sub(r"\s+", " ", value).strip()
    return result if result and len(result) <= 120 else None


def _american(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if abs(parsed) >= 100 and abs(parsed) <= 100000 else None


def _line(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _time(value: Any) -> float | None:
    parsed = nfl_identity.kickoff_utc(value) if isinstance(value, str) else None
    return parsed.timestamp() if parsed else None


def _team(event: dict, side: str) -> str | None:
    teams = event.get("teams")
    item = teams.get(side) if isinstance(teams, dict) else None
    if not isinstance(item, dict):
        return None
    names = item.get("names") or {}
    if not isinstance(names, dict):
        return None
    return nfl_identity.canonical_team(names.get("long") or item.get("name"))


def _event_identity(event: dict) -> dict | None:
    if event.get("leagueID") != "NFL" or not event.get("eventID"):
        return None
    status = event.get("status") or {}
    if not isinstance(status, dict) or status.get("cancelled") is True or status.get("ended") is True:
        return None
    stamp = event.get("startTime") or status.get("startsAt")
    home, away = _team(event, "home"), _team(event, "away")
    if not home or not away or home == away or not nfl_identity.kickoff_utc(stamp):
        return None
    return {"home_team": home, "away_team": away, "start_time": stamp}


def _schedule(season: int, week: int) -> list[dict]:
    prefix = f"nfl-{season}-reg-{week:02d}-"
    games = db.q("SELECT * FROM events WHERE sport='NFL' AND id LIKE ?", (prefix + "%",))
    return [g for g in games if nfl_identity.canonical_id(g) == g["id"]
            and str(g.get("status") or "").upper() not in INACTIVE
            and not json.loads(g.get("meta") or "{}").get("is_demo")]


class SportsGameOddsProps:
    """Explicit, bounded fetch; header authentication keeps keys out of URLs."""

    def __init__(self, api_key: str | None = None, getter=None):
        self.api_key = api_key if api_key is not None else settings.sportsgameodds_api_key
        self.getter = getter

    def fetch(self, starts_after: float, starts_before: float,
              batch_id: str | None = None, include_alt_lines: bool = True) -> dict:
        if not self.api_key:
            raise ProviderError(PROVIDER, "SPORTSGAMEODDS_API_KEY is not configured")
        if starts_after >= starts_before:
            raise ValueError("invalid event time range")
        data: list[dict] = []
        notices: list[str] = []
        batch_id = batch_id or str(uuid.uuid4())
        cursor = None
        cursors = set()
        rate_metadata = []
        for page_number in range(1, 11):
            params = {"leagueID": "NFL", "startsAfter": _utc(starts_after),
                      "startsBefore": _utc(starts_before), "started": "false",
                      "cancelled": "false", "oddsAvailable": "true",
                      "includeAltLines": "true" if include_alt_lines else "false", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            db.ex("INSERT INTO nfl_prop_api_request_attempts VALUES(?,?,?,?)",
                  (batch_id, page_number, time.time(), json.dumps(params, sort_keys=True)))
            try:
                if self.getter is None:
                    status, body, headers = get_raw_meta(
                        URL, params=params, headers={"x-api-key": self.api_key},
                        provider=PROVIDER, timeout=30)
                    source_kind = "http_body"
                else:
                    supplied = self.getter(URL, params=params, headers={"x-api-key": self.api_key},
                                           provider=PROVIDER, retries=0)
                    if isinstance(supplied, tuple) and len(supplied) == 3:
                        status, body, headers = supplied
                        source_kind = "http_body"
                    else:
                        status = 200
                        body = json.dumps(supplied, separators=(",", ":")).encode()
                        headers = {}
                        source_kind = "fixture_serialized"
            except Exception as exc:
                db.ex("INSERT INTO nfl_prop_api_request_results VALUES(?,?,?,?,?,?,?)",
                      (batch_id, page_number, time.time(), None, None, "{}", type(exc).__name__))
                raise
            selected_headers = _rate_headers(headers)
            selected_headers["capture_body_kind"] = source_kind
            with db.tx() as c:
                c.execute("INSERT INTO nfl_prop_raw_responses VALUES(?,?,?,?,?,?,?,?)",
                          (batch_id, page_number, time.time(), status,
                           json.dumps(params, sort_keys=True), json.dumps(selected_headers, sort_keys=True),
                           hashlib.sha256(body).hexdigest(), body))
                c.execute("INSERT INTO nfl_prop_api_request_results VALUES(?,?,?,?,?,?,?)",
                          (batch_id, page_number, time.time(), status,
                           hashlib.sha256(body).hexdigest(),
                           json.dumps(selected_headers, sort_keys=True), None))
            rate_metadata.append(selected_headers)
            if status != 200:
                raise ProviderError(PROVIDER, f"HTTP {status} from api.sportsgameodds.com/v2/events")
            try:
                page = json.loads(body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("malformed SportsGameOdds JSON page") from exc
            if not isinstance(page, dict) or page.get("success") is not True or not isinstance(page.get("data"), list):
                raise ValueError("malformed SportsGameOdds event page")
            if any(not isinstance(event, dict) for event in page["data"]):
                raise ValueError("malformed SportsGameOdds event")
            data.extend(page["data"])
            if isinstance(page.get("notice"), str) and page["notice"]:
                notices.append(page["notice"])
            cursor = page.get("nextCursor")
            if not cursor:
                return {"success": True, "data": data,
                        "_request_count": page_number, "_rate_headers": rate_metadata,
                        "_notices": notices}
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("invalid SportsGameOdds pagination cursor")
            cursors.add(cursor)
        raise ValueError("SportsGameOdds event pagination exceeded 10 pages")


class ArchivedSportsGameOddsProps:
    """Replay already archived HTTP bodies without another provider request."""

    def fetch(self, starts_after: float, starts_before: float,
              batch_id: str | None = None) -> dict:
        if not batch_id:
            raise ValueError("archived capture requires its original batch ID")
        pages = db.q("SELECT * FROM nfl_prop_raw_responses WHERE batch_id=? ORDER BY page_number",
                     (batch_id,))
        if not pages or [p["page_number"] for p in pages] != list(range(1, len(pages) + 1)):
            raise ValueError("missing or noncontiguous archived response pages")
        events, notices, rate_headers = [], [], []
        for index, archived in enumerate(pages):
            params = json.loads(archived["request_params"])
            if (params.get("leagueID") != "NFL" or params.get("includeAltLines") != "true"
                    or params.get("startsAfter") != _utc(starts_after)
                    or params.get("startsBefore") != _utc(starts_before)):
                raise ValueError("archived request does not match current schedule cutoff")
            body = archived["body"]
            if hashlib.sha256(body).hexdigest() != archived["body_sha256"]:
                raise ValueError("archived response hash mismatch")
            if archived["http_status"] != 200:
                raise ValueError("archived response was not successful")
            page = json.loads(body)
            if page.get("success") is not True or not isinstance(page.get("data"), list):
                raise ValueError("malformed archived response")
            events.extend(page["data"])
            if isinstance(page.get("notice"), str) and page["notice"]:
                notices.append(page["notice"])
            rate_headers.append(json.loads(archived["response_headers"]))
            if bool(page.get("nextCursor")) != (index < len(pages) - 1):
                raise ValueError("archived pagination is incomplete")
        return {"success": True, "data": events, "_request_count": 0,
                "_archived_request_count": len(pages), "_rate_headers": rate_headers,
                "_notices": notices}


def _market(odd: dict) -> tuple[str, str] | None:
    if odd.get("periodID") != "game":
        return None
    stat, bet_type, side = odd.get("statID"), odd.get("betTypeID"), odd.get("sideID")
    if stat in STAT_MARKETS and bet_type == "ou" and side in {"over", "under"}:
        return STAT_MARKETS[stat], side
    if stat == "touchdowns" and bet_type == "yn" and side in {"yes", "no"}:
        return "anytime_touchdowns", side
    return None


def _quotes(event: dict, game: dict, batch_id: str, retrieved_at: float) -> tuple[list[dict], dict, dict]:
    """Normalize every available main and alternate offer; count omissions."""
    audit = {"warnings": {}, "offers_seen": 0, "alternate_offers_seen": 0,
             "offers_normalized": 0}
    warnings = audit["warnings"]
    players, odds = event.get("players"), event.get("odds")
    if not isinstance(players, dict) or not isinstance(odds, dict):
        _warn(warnings, "missing_players_or_odds_object")
        return [], {}, audit
    teams = event["teams"]
    team_ids = {str(teams[side].get("teamID")): _team(event, side)
                for side in ("home", "away")}
    if "None" in team_ids or len(team_ids) != 2:
        _warn(warnings, "missing_distinct_team_ids")
        return [], {}, audit
    kickoff = nfl_identity.kickoff_utc(game["start_time"]).timestamp()
    provider_kickoff = nfl_identity.kickoff_utc(
        event.get("startTime") or (event.get("status") or {}).get("startsAt"))
    if (retrieved_at >= kickoff or not provider_kickoff or
            retrieved_at >= provider_kickoff.timestamp() or
            (event.get("status") or {}).get("started") is True):
        _warn(warnings, "provider_event_started_or_kickoff_conflict")
        return [], {}, audit
    rows: list[dict] = []
    aliases: dict[str, tuple[str, str]] = {}
    for key, odd in odds.items():
        if not isinstance(odd, dict):
            _warn(warnings, "malformed_odd_object")
            continue
        market = _market(odd)
        if not market:
            if odd.get("statID") in set(STAT_MARKETS) | {"touchdowns"}:
                _warn(warnings, "unsupported_variant")
            continue
        player_ref = odd.get("playerID") or odd.get("statEntityID")
        if not isinstance(player_ref, str) or not player_ref or player_ref in {"all", "home", "away"}:
            _warn(warnings, "missing_player_id")
            continue
        expected = f"{odd['statID']}-{player_ref}-game-{odd['betTypeID']}-{odd['sideID']}"
        if key != expected or odd.get("oddID", key) != key or odd.get("statEntityID") != player_ref:
            _warn(warnings, "odd_id_mismatch")
            continue
        player = players.get(player_ref)
        if not isinstance(player, dict) or player.get("playerID", player_ref) != player_ref:
            _warn(warnings, "player_not_in_event")
            continue
        player_names = player.get("names") or {}
        name = _name(player.get("name") or
                     (player_names.get("long") if isinstance(player_names, dict) else None))
        team = team_ids.get(str(player.get("teamID")))
        if not name:
            _warn(warnings, "missing_player_name")
            continue
        if not team:
            _warn(warnings, "unresolved_player_team")
            continue
        existing = db.q1("SELECT player_id,player_name FROM nfl_prop_player_aliases "
                         "WHERE provider=? AND provider_player_id=?", (PROVIDER, player_ref))
        if existing and existing["player_name"].casefold() != name.casefold():
            raise ValueError(f"provider player ID changed name: {player_ref}")
        canonical_player = existing["player_id"] if existing else f"sgo:{player_ref}"
        aliases[player_ref] = (canonical_player, name)
        opponent = game["away_team"] if team == game["home_team"] else game["home_team"]
        offers = odd.get("byBookmaker")
        if not isinstance(offers, dict):
            _warn(warnings, "missing_by_bookmaker")
            continue
        for book_ref, offer in offers.items():
            sportsbook = normalize_book(book_ref)
            if sportsbook == "unknown":
                _warn(warnings, "invalid_sportsbook")
                continue
            if not isinstance(offer, dict):
                _warn(warnings, "malformed_book_offer")
                continue
            alternates = offer.get("altLines", [])
            if not isinstance(alternates, list):
                _warn(warnings, "malformed_alt_lines")
                alternates = []
            options = [("main", 0, offer)] + [("alternate", index, item)
                                              for index, item in enumerate(alternates, 1)]
            for line_type, offer_index, item in options:
                audit["offers_seen"] += 1
                if line_type == "alternate":
                    audit["alternate_offers_seen"] += 1
                if not isinstance(item, dict):
                    _warn(warnings, "malformed_book_offer")
                    continue
                if item.get("available") is not True:
                    _warn(warnings, "unavailable_offer")
                    continue
                price = _american(item.get("odds"))
                if price is None:
                    _warn(warnings, "invalid_price")
                    continue
                line = _line(item.get("overUnder")) if market[0] != "anytime_touchdowns" else None
                if market[0] != "anytime_touchdowns" and line is None:
                    _warn(warnings, "missing_or_invalid_line")
                    continue
                updated = _time(item.get("lastUpdatedAt") or offer.get("lastUpdatedAt"))
                if updated is not None and updated > retrieved_at + 300:
                    _warn(warnings, "future_quote_timestamp")
                    continue
                rows.append(dict(batch_id=batch_id, canonical_game_id=game["id"],
                                 provider_event_id=str(event["eventID"]),
                                 player_id=canonical_player, provider_player_id=player_ref,
                                 player_name=name, team=team, opponent=opponent,
                                 market=market[0], line=line, side=market[1], price=price,
                                 sportsbook=sportsbook, retrieved_at=retrieved_at,
                                 kickoff_utc=kickoff, provider=PROVIDER, source=URL,
                                 provider_updated_at=updated, line_type=line_type,
                                 offer_index=offer_index))
                audit["offers_normalized"] += 1
    return rows, aliases, audit


def capture_week(season: int, week: int, provider: SportsGameOddsProps | None = None,
                 retrieved_at: float | None = None, batch_id: str | None = None,
                 target_game_ids: set[str] | None = None) -> dict:
    stamp = time.time() if retrieved_at is None else retrieved_at
    schedule = _schedule(season, week)
    if not schedule:
        raise ValueError("no canonical, live NFL schedule for requested week")
    targets = ([g for g in schedule if g["id"] in target_game_ids]
               if target_game_ids is not None else schedule)
    if not targets or (target_game_ids is not None and {g["id"] for g in targets} != target_game_ids):
        raise ValueError("requested prop games are not in the canonical live schedule")
    kickoffs = [nfl_identity.kickoff_utc(g["start_time"]).timestamp() for g in targets]
    if stamp >= min(kickoffs):
        raise ValueError("target game already started; prop capture requires pre-kickoff games")
    batch_id = batch_id or str(uuid.uuid4())
    if db.q1("SELECT 1 FROM nfl_prop_capture_batches WHERE batch_id=?", (batch_id,)):
        raise ValueError("capture batch has already been sealed")
    padding = 30 * 60 if target_game_ids is not None else 4 * 3600
    client = provider or SportsGameOddsProps()
    if target_game_ids is not None and isinstance(client, SportsGameOddsProps):
        payload = client.fetch(min(kickoffs) - padding, max(kickoffs) + padding,
                               batch_id=batch_id, include_alt_lines=False)
    else:
        payload = client.fetch(min(kickoffs) - padding, max(kickoffs) + padding,
                               batch_id=batch_id)
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), list):
        raise ValueError("malformed SportsGameOdds response")
    raw = json.dumps({key: value for key, value in payload.items() if not key.startswith("_")},
                     sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    mappings: list[tuple[str, str]] = []
    aliases: dict[str, tuple[str, str]] = {}
    rows: list[dict] = []
    event_audits: list[dict] = []
    seen = set()
    mapped = set()
    for event in payload["data"]:
        if not isinstance(event, dict) or event.get("leagueID") != "NFL":
            raise ValueError("malformed or non-NFL event in NFL page")
        provider_id = event.get("eventID")
        if not isinstance(provider_id, str) or not provider_id or provider_id in seen:
            raise ValueError("missing or duplicate provider event ID")
        seen.add(provider_id)
        event_audit = {"provider_event_id": provider_id, "mapped": False,
                       "canonical_game_id": None, "players_in_feed": len(event.get("players") or {})
                       if isinstance(event.get("players"), dict) else None,
                       "players_with_quotes": 0, "quotes": 0, "books": [], "markets": [],
                       "freshest_quote_timestamp": None, "warnings": {}}
        event_audits.append(event_audit)
        identity = _event_identity(event)
        if not identity:
            _warn(event_audit["warnings"], "invalid_or_inactive_event_identity")
            continue
        game_id = db.mapped_nfl_game(PROVIDER, provider_id)
        candidate = nfl_identity.match_schedule_event(identity, schedule)
        if game_id and candidate and game_id != candidate:
            raise ValueError("SportsGameOdds event mapping conflicts with schedule")
        game = next((g for g in schedule if g["id"] == (game_id or candidate)), None)
        if not game or game["home_team"] != identity["home_team"] or game["away_team"] != identity["away_team"]:
            _warn(event_audit["warnings"], "no_unique_canonical_game_match")
            continue
        mapped.add(provider_id)
        event_audit["mapped"] = True
        event_audit["canonical_game_id"] = game["id"]
        mappings.append((provider_id, game["id"]))
        event_rows, event_aliases, normalization = _quotes(event, game, batch_id, stamp)
        event_audit.update(players_with_quotes=len({r["player_id"] for r in event_rows}),
                           quotes=len(event_rows), books=sorted({r["sportsbook"] for r in event_rows}),
                           markets=sorted({r["market"] for r in event_rows}),
                           freshest_quote_timestamp=max((r["provider_updated_at"] or r["retrieved_at"]
                                                         for r in event_rows), default=None),
                           warnings=normalization["warnings"],
                           offers_seen=normalization["offers_seen"],
                           alternate_offers_seen=normalization["alternate_offers_seen"])
        for key, value in event_aliases.items():
            if key in aliases and aliases[key] != value:
                raise ValueError("player ID changed within capture")
            aliases[key] = value
        rows.extend(event_rows)
    quote_keys = [(r["canonical_game_id"], r["player_id"], r["market"],
                   r["side"], r["sportsbook"], r["line_type"], r["offer_index"])
                  for r in rows]
    if len(quote_keys) != len(set(quote_keys)):
        raise ValueError("duplicate normalized sportsbook/player/market offer")
    # A single transaction makes mapping, raw response, and quotes one atomic snapshot.
    with db.tx() as c:
        for provider_id, game_id in mappings:
            existing = c.execute("SELECT game_id FROM nfl_provider_games WHERE provider=? AND provider_game_id=?",
                                 (PROVIDER, provider_id)).fetchone()
            if existing and existing["game_id"] != game_id:
                raise ValueError("provider event changed canonical game")
            c.execute("INSERT OR IGNORE INTO nfl_provider_games(provider,provider_game_id,game_id,linked_at) VALUES(?,?,?,?)",
                      (PROVIDER, provider_id, game_id, stamp))
        for provider_player_id, (player_id, name) in aliases.items():
            c.execute("INSERT OR IGNORE INTO nfl_prop_players(player_id,player_name,first_seen_at) VALUES(?,?,?)",
                      (player_id, name, stamp))
            c.execute("INSERT OR IGNORE INTO nfl_prop_player_aliases(provider,provider_player_id,player_id,player_name,linked_at) VALUES(?,?,?,?,?)",
                      (PROVIDER, provider_player_id, player_id, name, stamp))
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  (batch_id, PROVIDER, stamp, hashlib.sha256(raw.encode()).hexdigest(),
                   raw, len(seen), len(mapped), len(rows),
                   json.dumps({"season": season, "week": week,
                               "target_game_ids": sorted(target_game_ids) if target_game_ids is not None else None,
                               "rate_headers": payload.get("_rate_headers", []),
                               "provider_notices": payload.get("_notices", []),
                               "events": event_audits},
                              sort_keys=True)))
        c.executemany("INSERT INTO nfl_prop_snapshots(" + ",".join(QUOTE_COLUMNS) + ") VALUES(" +
                      ",".join("?" for _ in QUOTE_COLUMNS) + ")",
                      [tuple(row[col] for col in QUOTE_COLUMNS) for row in rows])
    return {"batch_id": batch_id, "provider": PROVIDER,
            "target_game_ids": sorted(target_game_ids) if target_game_ids is not None else None,
            "events_seen": len(seen),
            "events_mapped": len(mapped), "event_mapping_rate": len(mapped) / len(seen) if seen else None,
            "quotes_stored": len(rows), "markets": sorted({r["market"] for r in rows}),
            "bookmakers": sorted({r["sportsbook"] for r in rows}),
            "unique_players": len({r["player_id"] for r in rows}),
            "alternate_quotes": sum(r["line_type"] == "alternate" for r in rows),
            "normalization_warnings": {item["provider_event_id"]: item["warnings"]
                                       for item in event_audits if item["warnings"]},
            "live_requests": payload.get("_request_count", 0),
            "archived_requests": payload.get("_archived_request_count", 0),
            "rate_headers": payload.get("_rate_headers", []),
            "provider_notices": payload.get("_notices", [])}


def board(game_id: str, market: str | None = None) -> dict:
    game = db.q1("SELECT id FROM events WHERE id=? AND sport='NFL'", (game_id,))
    if not game:
        raise ValueError("unknown canonical NFL game")
    if market and market not in set(STAT_MARKETS.values()) | {"anytime_touchdowns"}:
        raise ValueError("unsupported player-prop market")
    latest_batch = None
    for batch in db.q("SELECT batch_id,audit_json FROM nfl_prop_capture_batches ORDER BY retrieved_at DESC"):
        audit = json.loads(batch["audit_json"] or "{}")
        if any(item.get("canonical_game_id") == game_id for item in audit.get("events", [])):
            latest_batch = batch["batch_id"]
            break
    sql = "SELECT * FROM nfl_prop_snapshots WHERE canonical_game_id=?"
    params: list[Any] = [game_id]
    if latest_batch:
        sql += " AND batch_id=?"
        params.append(latest_batch)
    elif db.q1("SELECT 1 FROM nfl_prop_capture_batches WHERE audit_json IS NOT NULL LIMIT 1"):
        # New captures with no quote for this game must not expose an older line.
        sql += " AND 1=0"
    if market:
        sql += " AND market=?"
        params.append(market)
    rows = db.q(sql + " ORDER BY player_name,market,sportsbook,side,line_type,offer_index", params)
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS", "game_id": game_id,
            "provider": PROVIDER, "batch_id": latest_batch,
            "quotes": rows, "quote_count": len(rows),
            "note": "Observed prices only. No projections, recommendations, or picks."}


def coverage_report(batch_id: str) -> dict:
    """Explain one immutable capture, including games with no normalized quotes."""
    batch = db.q1("SELECT * FROM nfl_prop_capture_batches WHERE batch_id=?", (batch_id,))
    if not batch:
        raise ValueError("unknown prop capture batch")
    audit = json.loads(batch["audit_json"] or "{}")
    events = list(audit.get("events", []))
    mapped_ids = {item.get("canonical_game_id") for item in events if item.get("mapped")}
    for game in _schedule(int(audit.get("season", 0)), int(audit.get("week", 0))):
        if game["id"] not in mapped_ids:
            events.append({"provider_event_id": None, "canonical_game_id": game["id"],
                           "mapped": False, "players_in_feed": None,
                           "players_with_quotes": 0, "quotes": 0, "books": [], "markets": [],
                           "freshest_quote_timestamp": None,
                           "warnings": {"not_returned_or_not_mapped": 1}})
    rows = db.q("SELECT * FROM nfl_prop_snapshots WHERE batch_id=?", (batch_id,))
    pages = db.q("SELECT page_number,fetched_at,http_status,body_sha256,response_headers "
                 "FROM nfl_prop_raw_responses WHERE batch_id=? ORDER BY page_number", (batch_id,))
    for page in pages:
        page["response_headers"] = json.loads(page["response_headers"])
    raw_books = set()
    notices = []
    raw_events = {}
    for archived in db.q("SELECT body FROM nfl_prop_raw_responses WHERE batch_id=? ORDER BY page_number",
                         (batch_id,)):
        body = json.loads(archived["body"])
        if isinstance(body.get("notice"), str) and body["notice"]:
            notices.append(body["notice"])
        for event in body.get("data", []):
            raw_events[event.get("eventID")] = event
            for odd in (event.get("odds") or {}).values():
                if isinstance(odd, dict) and isinstance(odd.get("byBookmaker"), dict):
                    raw_books.update(odd["byBookmaker"])
    if not raw_events:
        raw_events = {event.get("eventID"): event
                      for event in json.loads(batch["raw_payload"]).get("data", [])}
    warnings = Counter()
    for event in events:
        warnings.update(event.get("warnings") or {})
    price_groups = defaultdict(set)
    duplicate_groups = Counter()
    for row in rows:
        if row["market"] != "anytime_touchdowns":
            price_groups[(row["line_type"], row["canonical_game_id"], row["player_id"],
                          row["market"], row["sportsbook"], row["line"])].add(row["side"])
        duplicate_groups[(row["canonical_game_id"], row["player_id"], row["market"],
                          row["sportsbook"], row["side"], row["line"])] += 1
    samples = {}
    for market in sorted({row["market"] for row in rows}):
        row = min((r for r in rows if r["market"] == market),
                  key=lambda r: (r["line_type"] != "main", r["canonical_game_id"],
                                 r["player_name"], r["sportsbook"], r["id"]))
        game = db.q1("SELECT home_team,away_team,start_time FROM events WHERE id=?",
                     (row["canonical_game_id"],))
        alias = db.q1("SELECT player_id FROM nfl_prop_player_aliases WHERE provider=? AND provider_player_id=?",
                      (PROVIDER, row["provider_player_id"]))
        raw_event = raw_events.get(row["provider_event_id"]) or {}
        stat = next((key for key, value in STAT_MARKETS.items() if value == market), "touchdowns")
        bet_type = "yn" if market == "anytime_touchdowns" else "ou"
        odd_id = f"{stat}-{row['provider_player_id']}-game-{bet_type}-{row['side']}"
        raw_odd = (raw_event.get("odds") or {}).get(odd_id) or {}
        matching_books = [(book_id, offer) for book_id, offer in
                          (raw_odd.get("byBookmaker") or {}).items()
                          if normalize_book(book_id) == row["sportsbook"]]
        raw_book = matching_books[0][1] if len(matching_books) == 1 else {}
        alternatives = raw_book.get("altLines") or []
        raw_offer = (raw_book if row["line_type"] == "main" else
                     alternatives[row["offer_index"] - 1]
                     if isinstance(alternatives, list) and 0 < row["offer_index"] <= len(alternatives)
                     else {})
        raw_player = (raw_event.get("players") or {}).get(row["provider_player_id"]) or {}
        raw_player_team = next((side for side in ("home", "away")
                                if (raw_event.get("teams") or {}).get(side, {}).get("teamID")
                                == raw_player.get("teamID")), None)
        checks = {
            "canonical_game": bool(game and nfl_identity.canonical_id(dict(
                id=row["canonical_game_id"], home_team=game["home_team"],
                away_team=game["away_team"], start_time=game["start_time"],
                meta={"season": audit.get("season"), "season_type": 2,
                      "week": audit.get("week")})) == row["canonical_game_id"]),
            "provider_event_mapping": db.mapped_nfl_game(PROVIDER, row["provider_event_id"])
            == row["canonical_game_id"],
            "player_alias": bool(alias and alias["player_id"] == row["player_id"]),
            "player_name": bool(_name(row["player_name"])),
            "team_and_opponent": bool(game and {row["team"], row["opponent"]}
                                      == {game["home_team"], game["away_team"]}),
            "market_and_side": bool(_market({"statID": next((key for key, value in STAT_MARKETS.items()
                                                               if value == market), "touchdowns"),
                                              "periodID": "game",
                                              "betTypeID": "yn" if market == "anytime_touchdowns" else "ou",
                                              "sideID": row["side"]})),
            "line_and_price": _american(row["price"]) is not None and
            ((row["line"] is None) if market == "anytime_touchdowns" else _line(row["line"]) is not None),
            "sportsbook": normalize_book(row["sportsbook"]) == row["sportsbook"],
            "timestamps": row["retrieved_at"] < row["kickoff_utc"] and
            row["kickoff_utc"] == nfl_identity.kickoff_utc(game["start_time"]).timestamp(),
            "raw_provider_event": raw_event.get("eventID") == row["provider_event_id"],
            "raw_player": raw_player.get("playerID") == row["provider_player_id"]
            and _name(raw_player.get("name")) == row["player_name"]
            and raw_player_team is not None
            and _team(raw_event, raw_player_team) == row["team"],
            "raw_offer": bool(raw_offer.get("available") is True
                              and raw_odd.get("statID") == stat
                              and raw_odd.get("sideID") == row["side"]
                              and _american(raw_offer.get("odds")) == row["price"]
                              and (market == "anytime_touchdowns" or
                                   _line(raw_offer.get("overUnder")) == row["line"])
                              and _time(raw_offer.get("lastUpdatedAt") or raw_book.get("lastUpdatedAt"))
                              == row["provider_updated_at"]),
        }
        samples[market] = {**{key: row[key] for key in (
            "canonical_game_id", "provider_event_id", "player_id", "provider_player_id",
            "player_name", "team", "opponent", "market", "line", "side", "price",
            "sportsbook", "retrieved_at", "kickoff_utc", "line_type", "offer_index")},
            "checks": checks, "all_passed": all(checks.values())}
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS", "batch_id": batch_id,
            "provider": PROVIDER, "retrieved_at": batch["retrieved_at"],
            "events_seen": batch["events_seen"], "events_mapped": batch["events_mapped"],
            "mapping_rate": (batch["events_mapped"] / batch["events_seen"]
                             if batch["events_seen"] else None),
            "total_quotes": len(rows), "unique_players": len({r["player_id"] for r in rows}),
            "books": sorted({r["sportsbook"] for r in rows}),
            "raw_books_present": sorted(raw_books),
            "book_quote_counts": dict(sorted(Counter(r["sportsbook"] for r in rows).items())),
            "markets": sorted({r["market"] for r in rows}),
            "market_quote_counts": dict(sorted(Counter(r["market"] for r in rows).items())),
            "alternate_quotes": sum(r["line_type"] == "alternate" for r in rows),
            "duplicate_same_line_groups": sum(n > 1 for n in duplicate_groups.values()),
            "paired_main_line_groups": sum(sides == {"over", "under"} for key, sides in price_groups.items()
                                           if key[0] == "main"),
            "main_line_groups": sum(key[0] == "main" for key in price_groups),
            "paired_alternate_line_groups": sum(sides == {"over", "under"} for key, sides in price_groups.items()
                                                if key[0] == "alternate"),
            "alternate_line_groups": sum(key[0] == "alternate" for key in price_groups),
            "numeric_line_quotes": sum(r["line"] is not None for r in rows),
            "price_complete_quotes": sum(r["price"] is not None for r in rows),
            "provider_quote_timestamp_missing": sum(r["provider_updated_at"] is None for r in rows),
            "normalization_warning_counts": dict(sorted(warnings.items())),
            "provider_notices": notices, "normalization_samples": samples,
            "raw_pages": pages, "archived_http_responses": sum(
                page["response_headers"].get("capture_body_kind") == "http_body" for page in pages),
            "games": events}


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit NFL player-prop research capture")
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(capture_week(args.season, args.week), sort_keys=True))


if __name__ == "__main__":
    main()
