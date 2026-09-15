"""The Odds API adapter (free tier: 500 requests/month).

Only used when ODDS_API_KEY is set. Requests are deliberately manual-refresh
only -- there is no polling loop anywhere in this app.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from ..config import SPORTS, settings
from ..research import cache as rcache
from .normalize import normalize_team
from .base import OddsProvider, ProviderError, get_json, get_json_meta

log = logging.getLogger("edgeterminal.providers.theoddsapi")
BASE = "https://api.the-odds-api.com/v4"

GOLF_KEYS = ["golf_pga_championship_winner", "golf_masters_tournament_winner",
             "golf_the_open_championship_winner", "golf_us_open_winner"]

PROP_MARKETS = {
    "NFL": "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions",
    "CFB": "",
    "NBA": "player_points,player_rebounds,player_assists,player_threes",
    "PGA": "",
}

MARKET_MAP = {"h2h": "moneyline", "spreads": "spread", "totals": "total",
              "outrights": "outright"}
PROP_MAP = {"player_pass_yds": "passing_yards", "player_rush_yds": "rushing_yards",
            "player_reception_yds": "receiving_yards", "player_receptions": "receptions",
            "player_points": "points", "player_rebounds": "rebounds",
            "player_assists": "assists", "player_threes": "threes"}

# Canonical prop market names used by the prop models.
PROP_MARKET_NAMES = {
    "player_anytime_td": "player_anytime_td", "player_1st_td": "player_1st_td",
    "player_pass_yds": "player_pass_yards", "player_pass_tds": "player_pass_tds",
    "player_pass_attempts": "player_pass_attempts",
    "player_pass_completions": "player_pass_completions",
    "player_rush_yds": "player_rush_yards", "player_rush_attempts": "player_rush_attempts",
    "player_receptions": "player_receptions",
    "player_reception_yds": "player_reception_yards",
}

# Priority prop markets. Fetched on demand (never during a dashboard refresh)
# because every event/market pair spends Odds API quota.
PRIORITY_PROP_MARKETS = [
    "player_anytime_td", "player_1st_td", "player_pass_yds", "player_pass_tds",
    "player_pass_attempts", "player_pass_completions", "player_rush_yds",
    "player_rush_attempts", "player_receptions", "player_reception_yds",
]
PROP_CATEGORIES = {
    "td": ["player_anytime_td", "player_1st_td"],
    "passing": ["player_pass_yds", "player_pass_tds", "player_pass_attempts",
                "player_pass_completions"],
    "rushing": ["player_rush_yds", "player_rush_attempts"],
    "receiving": ["player_receptions", "player_reception_yds"],
}
PROP_TTL_S = 300.0


def markets_for_category(category: Optional[str]) -> List[str]:
    if not category or category in ("all", "best"):
        return list(PRIORITY_PROP_MARKETS)
    return list(PROP_CATEGORIES.get(category, PRIORITY_PROP_MARKETS))


class TheOddsApi(OddsProvider):
    name = "theoddsapi"
    is_demo = False

    def __init__(self, api_key: Optional[str] = None):
        self.key = api_key or settings.odds_api_key
        if not self.key:
            raise ProviderError(self.name, "ODDS_API_KEY not configured")
        self.remaining: Optional[str] = None
        self.quota: Dict = {}
        self.last_fetch: Dict = {}

    def _record_quota(self, headers: Dict) -> None:
        """Track Odds API quota headers so the debug view can show real usage."""
        for key in ("x-requests-remaining", "x-requests-used", "x-requests-last"):
            if key in headers:
                self.quota[key.replace("x-requests-", "")] = headers[key]
        self.quota["checked_at"] = time.time()

    # ------------------------------------------------------------------ utils
    def _sport_keys(self, sport: str) -> List[str]:
        if sport == "PGA":
            return GOLF_KEYS
        return [SPORTS[sport]["key"]]

    def _get(self, path: str, params: Dict) -> object:
        params = dict(params, apiKey=self.key)
        return get_json(f"{BASE}{path}", params, provider=self.name)

    # ------------------------------------------------------------------ api
    def list_events(self, sport: str) -> List[Dict]:
        out: List[Dict] = []
        for key in self._sport_keys(sport):
            try:
                data = self._get(f"/sports/{key}/events", {})
            except ProviderError as e:
                log.warning("events fetch failed for %s: %s", key, e)
                continue
            for ev in data or []:
                out.append(dict(
                    id=ev["id"], sport=sport, league=key, start_time=ev.get("commence_time"),
                    home_team=ev.get("home_team"), away_team=ev.get("away_team"),
                    name=(f"{ev.get('away_team')} @ {ev.get('home_team')}"
                          if ev.get("home_team") else ev.get("sport_title", key)),
                    venue=None, meta={"odds_api_sport": key}))
        return out

    def resolve_event_id(self, event: Dict, cache: Optional[rcache.TTLCache] = None,
                         force: bool = False) -> Dict:
        """Resolve this stored event to a *validated* Odds API event.

        Returns an identity verdict (see `integrity.match_provider_event`), never
        a bare id: an id that no longer exists in the feed must not be reused, and
        a same-teams rematch must not stand in for the game we asked about.
        """
        from .. import integrity as I
        store = cache or rcache.shared_cache()
        key = f"oddsapi-identity-v{I.CACHE_VERSION}:{event.get('id')}"
        if not force:
            hit = store.get(key)
            if hit is not None:
                return hit
        try:
            candidates = self.list_events("NFL")
        except ProviderError as e:
            verdict = dict(state=I.IDENTITY_UNAVAILABLE, provider_id=None, alternatives=[],
                           reasons=[f"provider event list unavailable: {e.detail}"])
            store.set(key, verdict, ttl=60.0, topic="odds")
            return verdict
        verdict = I.match_provider_event(candidates, event)
        # An OK verdict can be cached for a while; refusals are cached briefly so
        # the app re-checks soon instead of freezing a wrong answer.
        store.set(key, verdict, ttl=1800.0 if verdict["state"] == I.IDENTITY_OK else 60.0,
                  topic="odds")
        return verdict

    def fetch_odds(self, sport: str, with_props: bool = True) -> List[Dict]:
        rows: List[Dict] = []
        markets = "outrights" if sport == "PGA" else "h2h,spreads,totals"
        for key in self._sport_keys(sport):
            try:
                data = self._get(f"/sports/{key}/odds", {
                    "regions": settings.odds_regions, "markets": markets,
                    "oddsFormat": "american", "dateFormat": "iso"})
            except ProviderError as e:
                log.warning("odds fetch failed for %s: %s", key, e)
                continue
            for ev in data or []:
                rows += self._flatten(ev, sport)
            if with_props and PROP_MARKETS.get(sport):
                for ev in (data or [])[:6]:   # props are 1 request per event; stay inside free tier
                    try:
                        detail = self._get(f"/sports/{key}/events/{ev['id']}/odds", {
                            "regions": settings.odds_regions,
                            "markets": PROP_MARKETS[sport],
                            "oddsFormat": "american"})
                        rows += self._flatten(detail, sport)
                    except ProviderError as e:
                        log.info("props unavailable for %s: %s", ev.get("id"), e)
        return rows

    # ---------------------------------------------------------------- props
    def fetch_event_props(self, event_id: str, markets: Optional[List[str]] = None,
                          category: Optional[str] = None,
                          cache: Optional[rcache.TTLCache] = None,
                          force: bool = False, event: Optional[Dict] = None) -> Dict:
        """Event-level player props, on demand and briefly cached.

        Exactly one request per call: every requested market rides together, so a
        free-tier key stays usable while the user clicks through a slate.
        """
        wanted = [m for m in (markets or markets_for_category(category))
                  if m in PROP_MARKET_NAMES or m in PROP_MAP]
        store = cache or rcache.shared_cache()
        from .. import integrity as I
        event = event or {}
        key = I.prop_cache_key(event_id, event, wanted)
        if not force:
            hit = store.get(key)
            if hit is not None:
                return dict(hit, cache="hit")
        try:
            data, headers = get_json_meta(
                f"{BASE}/sports/{SPORTS['NFL']['key']}/events/{event_id}/odds",
                {"regions": settings.odds_regions, "markets": ",".join(wanted),
                 "oddsFormat": "american", "dateFormat": "iso", "apiKey": self.key},
                provider=self.name)
        except ProviderError as e:
            log.warning("prop fetch failed for %s: %s", event_id, e)
            return dict(event_id=event_id, markets=wanted, rows=[], source=self.name,
                        error=e.detail, cache="miss", quota=dict(self.quota))
        self._record_quota(headers)
        rows = self._flatten_props(data or {}, event_id)
        # The payload must prove it is the game we asked for; otherwise nothing is
        # cached and the caller receives an explicit refusal.
        verdict = I.validate_provider_event(data or {}, event, tolerance_s=6 * 3600.0) \
            if event else dict(state=I.IDENTITY_OK, provider_id=event_id, reasons=[])
        if verdict["state"] != I.IDENTITY_OK:
            log.warning("prop payload rejected for %s: %s", event_id, verdict["reasons"])
            return dict(event_id=event_id, markets=wanted, rows=[], source=self.name,
                        retrieved_at=time.time(), quota=dict(self.quota), error=None,
                        cache="miss", identity=verdict)
        payload = dict(event_id=event_id, markets=wanted, rows=rows, source=self.name,
                       retrieved_at=time.time(), quota=dict(self.quota), error=None,
                       identity=verdict, cache_version=I.CACHE_VERSION)
        store.set(key, payload, ttl=PROP_TTL_S, topic="odds")
        self.last_fetch = dict(payload, cache="miss")
        return dict(payload, cache="miss")

    def _flatten_props(self, event: Dict, event_id: str) -> List[Dict]:
        rows: List[Dict] = []
        for bm in event.get("bookmakers", []) or []:
            book = (bm.get("key") or "").lower()
            for mk in bm.get("markets", []) or []:
                raw = mk.get("key", "")
                market = PROP_MARKET_NAMES.get(raw) or PROP_MAP.get(raw)
                if not market:
                    continue
                for oc in mk.get("outcomes", []) or []:
                    if raw in ("player_anytime_td", "player_1st_td"):
                        # Yes/No markets: `name` is the side, `description` is the
                        # player. Falling back to `name` for the player invents a
                        # scorer literally called "Yes", and dropping the side
                        # merges a Yes and a No quote into one price series.
                        player = (oc.get("description") or "").strip()
                        side = (oc.get("name") or "").strip().lower() or None
                        if side not in ("yes", "no"):
                            side = None
                    else:
                        player = (oc.get("description") or "").strip()
                        side = (oc.get("name") or "").strip().lower() or None
                    if not player:
                        continue
                    rows.append(dict(event_id=event_id, sport="NFL", book=book,
                                     market=market, api_market=raw, selection=player,
                                     player=player, side=side, line=oc.get("point"),
                                     price=oc.get("price"), is_demo=0))
        return rows

    def _flatten(self, ev: Dict, sport: str) -> List[Dict]:
        rows: List[Dict] = []
        if not ev:
            return rows
        for bm in ev.get("bookmakers", []) or []:
            book = (bm.get("key") or "").lower()
            for mk in bm.get("markets", []) or []:
                mkey = mk.get("key", "")
                if mkey in MARKET_MAP:
                    market = MARKET_MAP[mkey]
                    for oc in mk.get("outcomes", []) or []:
                        rows.append(dict(event_id=ev["id"], sport=sport, book=book,
                                         market=market, selection=oc.get("name"),
                                         line=oc.get("point"), price=oc.get("price")))
                elif mkey in PROP_MAP:
                    prop = PROP_MAP[mkey]
                    for oc in mk.get("outcomes", []) or []:
                        who = oc.get("description") or oc.get("name")
                        side = (oc.get("name") or "Over").title()
                        rows.append(dict(event_id=ev["id"], sport=sport, book=book,
                                         market="player_prop",
                                         selection=f"{who} {side} {prop}",
                                         line=oc.get("point"), price=oc.get("price")))
        return rows
