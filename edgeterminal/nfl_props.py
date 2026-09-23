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
from datetime import datetime, timezone
from typing import Any

from . import db, nfl_identity
from .config import settings
from .providers.base import ProviderError, get_json
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
    "provider", "source", "provider_updated_at",
)


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
    item = (event.get("teams") or {}).get(side) or {}
    names = item.get("names") or {}
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
        self.getter = getter or get_json

    def fetch(self, starts_after: float, starts_before: float) -> dict:
        if not self.api_key:
            raise ProviderError(PROVIDER, "SPORTSGAMEODDS_API_KEY is not configured")
        if starts_after >= starts_before:
            raise ValueError("invalid event time range")
        data: list[dict] = []
        cursor = None
        cursors = set()
        for _ in range(10):
            params = {"leagueID": "NFL", "startsAfter": _utc(starts_after),
                      "startsBefore": _utc(starts_before), "started": "false",
                      "cancelled": "false", "oddsAvailable": "true", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.getter(URL, params=params, headers={"x-api-key": self.api_key},
                               provider=PROVIDER, retries=0)
            if not isinstance(page, dict) or page.get("success") is not True or not isinstance(page.get("data"), list):
                raise ValueError("malformed SportsGameOdds event page")
            if any(not isinstance(event, dict) for event in page["data"]):
                raise ValueError("malformed SportsGameOdds event")
            data.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return {"success": True, "data": data}
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("invalid SportsGameOdds pagination cursor")
            cursors.add(cursor)
        raise ValueError("SportsGameOdds event pagination exceeded 10 pages")


def _market(odd: dict) -> tuple[str, str] | None:
    if odd.get("periodID") != "game":
        return None
    stat, bet_type, side = odd.get("statID"), odd.get("betTypeID"), odd.get("sideID")
    if stat in STAT_MARKETS and bet_type == "ou" and side in {"over", "under"}:
        return STAT_MARKETS[stat], side
    if stat == "touchdowns" and bet_type == "yn" and side in {"yes", "no"}:
        return "anytime_touchdowns", side
    return None


def _quotes(event: dict, game: dict, batch_id: str, retrieved_at: float) -> tuple[list[dict], dict]:
    """Return only unambiguous player/book offers and their stable aliases."""
    players, odds = event.get("players"), event.get("odds")
    if not isinstance(players, dict) or not isinstance(odds, dict):
        raise ValueError("mapped SportsGameOdds event lacks players or odds object")
    teams = event["teams"]
    team_ids = {str(teams[side].get("teamID")): _team(event, side)
                for side in ("home", "away")}
    if "None" in team_ids or len(team_ids) != 2:
        raise ValueError("mapped SportsGameOdds event lacks distinct team IDs")
    kickoff = nfl_identity.kickoff_utc(game["start_time"]).timestamp()
    provider_kickoff = nfl_identity.kickoff_utc(
        event.get("startTime") or (event.get("status") or {}).get("startsAt"))
    if (retrieved_at >= kickoff or not provider_kickoff or
            retrieved_at >= provider_kickoff.timestamp() or
            (event.get("status") or {}).get("started") is True):
        raise ValueError("cannot capture post-kickoff player props")
    rows: list[dict] = []
    aliases: dict[str, tuple[str, str]] = {}
    for key, odd in odds.items():
        if not isinstance(odd, dict):
            raise ValueError("malformed SportsGameOdds odds object")
        market = _market(odd)
        if not market:
            continue
        player_ref = odd.get("playerID") or odd.get("statEntityID")
        if not isinstance(player_ref, str) or not player_ref or player_ref in {"all", "home", "away"}:
            continue
        expected = f"{odd['statID']}-{player_ref}-game-{odd['betTypeID']}-{odd['sideID']}"
        if key != expected or odd.get("oddID", key) != key or odd.get("statEntityID") != player_ref:
            raise ValueError("SportsGameOdds oddID disagrees with structured market fields")
        player = players.get(player_ref)
        if not isinstance(player, dict) or player.get("playerID", player_ref) != player_ref:
            continue
        name = _name(player.get("name") or (player.get("names") or {}).get("long"))
        team = team_ids.get(str(player.get("teamID")))
        if not name or not team:
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
            continue
        for book_ref, offer in offers.items():
            if not isinstance(offer, dict) or offer.get("available") is not True:
                continue
            price = _american(offer.get("odds"))
            line = _line(offer.get("overUnder")) if market[0] != "anytime_touchdowns" else None
            if price is None or (market[0] != "anytime_touchdowns" and line is None):
                continue
            updated = _time(offer.get("lastUpdatedAt"))
            if updated is not None and updated > retrieved_at + 300:
                continue
            rows.append(dict(batch_id=batch_id, canonical_game_id=game["id"],
                             provider_event_id=str(event["eventID"]),
                             player_id=canonical_player, provider_player_id=player_ref,
                             player_name=name, team=team, opponent=opponent,
                             market=market[0], line=line, side=market[1], price=price,
                             sportsbook=normalize_book(book_ref), retrieved_at=retrieved_at,
                             kickoff_utc=kickoff, provider=PROVIDER, source=URL,
                             provider_updated_at=updated))
    return rows, aliases


def capture_week(season: int, week: int, provider: SportsGameOddsProps | None = None,
                 retrieved_at: float | None = None) -> dict:
    stamp = time.time() if retrieved_at is None else retrieved_at
    schedule = _schedule(season, week)
    if not schedule:
        raise ValueError("no canonical, live NFL schedule for requested week")
    kickoffs = [nfl_identity.kickoff_utc(g["start_time"]).timestamp() for g in schedule]
    if stamp >= min(kickoffs):
        raise ValueError("week has already started; explicit prop capture requires pre-week kickoff")
    payload = (provider or SportsGameOddsProps()).fetch(min(kickoffs) - 4 * 3600,
                                                        max(kickoffs) + 4 * 3600)
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), list):
        raise ValueError("malformed SportsGameOdds response")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    batch_id = str(uuid.uuid4())
    mappings: list[tuple[str, str]] = []
    aliases: dict[str, tuple[str, str]] = {}
    rows: list[dict] = []
    seen = set()
    mapped = set()
    for event in payload["data"]:
        if not isinstance(event, dict) or event.get("leagueID") != "NFL":
            raise ValueError("malformed or non-NFL event in NFL page")
        provider_id = event.get("eventID")
        if not isinstance(provider_id, str) or not provider_id or provider_id in seen:
            raise ValueError("missing or duplicate provider event ID")
        seen.add(provider_id)
        identity = _event_identity(event)
        if not identity:
            continue
        game_id = db.mapped_nfl_game(PROVIDER, provider_id)
        candidate = nfl_identity.match_schedule_event(identity, schedule)
        if game_id and candidate and game_id != candidate:
            raise ValueError("SportsGameOdds event mapping conflicts with schedule")
        game = next((g for g in schedule if g["id"] == (game_id or candidate)), None)
        if not game or game["home_team"] != identity["home_team"] or game["away_team"] != identity["away_team"]:
            continue
        mapped.add(provider_id)
        mappings.append((provider_id, game["id"]))
        event_rows, event_aliases = _quotes(event, game, batch_id, stamp)
        for key, value in event_aliases.items():
            if key in aliases and aliases[key] != value:
                raise ValueError("player ID changed within capture")
            aliases[key] = value
        rows.extend(event_rows)
    quote_keys = [(r["canonical_game_id"], r["player_id"], r["market"],
                   r["side"], r["sportsbook"]) for r in rows]
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
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?)",
                  (batch_id, PROVIDER, stamp, hashlib.sha256(raw.encode()).hexdigest(),
                   raw, len(seen), len(mapped), len(rows)))
        c.executemany("INSERT INTO nfl_prop_snapshots(" + ",".join(QUOTE_COLUMNS) + ") VALUES(" +
                      ",".join("?" for _ in QUOTE_COLUMNS) + ")",
                      [tuple(row[col] for col in QUOTE_COLUMNS) for row in rows])
    return {"batch_id": batch_id, "provider": PROVIDER, "events_seen": len(seen),
            "events_mapped": len(mapped), "event_mapping_rate": len(mapped) / len(seen) if seen else None,
            "quotes_stored": len(rows), "markets": sorted({r["market"] for r in rows}),
            "bookmakers": sorted({r["sportsbook"] for r in rows})}


def board(game_id: str, market: str | None = None) -> dict:
    game = db.q1("SELECT id FROM events WHERE id=? AND sport='NFL'", (game_id,))
    if not game:
        raise ValueError("unknown canonical NFL game")
    if market and market not in set(STAT_MARKETS.values()) | {"anytime_touchdowns"}:
        raise ValueError("unsupported player-prop market")
    rows = db.q("SELECT * FROM nfl_prop_snapshots WHERE canonical_game_id=? "
                "ORDER BY retrieved_at DESC,id DESC", (game_id,))
    latest = {}
    for row in rows:
        if market and row["market"] != market:
            continue
        key = (row["player_id"], row["market"], row["side"], row["sportsbook"])
        latest.setdefault(key, row)
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS", "game_id": game_id,
            "provider": PROVIDER, "quotes": list(latest.values()), "quote_count": len(latest),
            "note": "Observed prices only. No projections, recommendations, or picks."}


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit NFL player-prop research capture")
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(capture_week(args.season, args.week), sort_keys=True))


if __name__ == "__main__":
    main()
