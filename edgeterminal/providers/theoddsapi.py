"""The Odds API adapter (free tier: 500 requests/month).

Only used when ODDS_API_KEY is set. Requests are deliberately manual-refresh
only -- there is no polling loop anywhere in this app.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ..config import SPORTS, settings
from .base import OddsProvider, ProviderError, get_json

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


class TheOddsApi(OddsProvider):
    name = "theoddsapi"
    is_demo = False

    def __init__(self, api_key: Optional[str] = None):
        self.key = api_key or settings.odds_api_key
        if not self.key:
            raise ProviderError(self.name, "ODDS_API_KEY not configured")
        self.remaining: Optional[str] = None

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
