"""ESPN public endpoints: schedules, team stats, injuries, news.

Free, no key, no scraping of rendered HTML -- these are the same JSON endpoints
espn.com calls. Rate limits are unknown, so responses are cached in-process and
every call is manual-refresh driven.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import SPORTS
from .base import (InjuryProvider, NewsProvider, OddsProvider, ProviderError, StatsProvider,
                   get_json)
from .normalize import normalize_book, normalize_team

log = logging.getLogger("edgeterminal.providers.espn")
SITE = "https://site.api.espn.com/apis/site/v2/sports"
CORE = "https://site.api.espn.com/apis/v2/sports"

_cache: Dict[str, tuple] = {}
TTL = 900.0
SCOREBOARD_URL = f"{SITE}/football/nfl/scoreboard"


def _cached(key: str, fn, ttl: float = TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _path(sport: str) -> str:
    return SPORTS[sport]["espn"]


def _scoreboard(sport: str, force: bool = False) -> Dict:
    """Current scoreboard; odds refresh bypasses the schedule cache."""
    def fetch():
        params = {"limit": 100}
        if sport == "CFB":
            params["groups"] = "80"
        return get_json(f"{SITE}/{_path(sport)}/scoreboard", params, provider="espn")
    if force:
        return fetch()
    return _cached(f"sb:{sport}", fetch, ttl=TTL)


class EspnEvents:
    name = "espn-events"
    is_demo = False

    def list_events(self, sport: str) -> List[Dict]:
        data = _scoreboard(sport)
        retrieved = time.time()
        out: List[Dict] = []
        for ev in (data or {}).get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            teams = comp.get("competitors", [])
            home = next((t for t in teams if t.get("homeAway") == "home"), {})
            away = next((t for t in teams if t.get("homeAway") == "away"), {})
            venue = (comp.get("venue") or {})
            addr = venue.get("address") or {}
            out.append(dict(
                id=f"espn-{ev.get('id')}", sport=sport, league=SPORTS[sport]["label"],
                name=ev.get("name") or ev.get("shortName"),
                home_team=normalize_team((home.get("team") or {}).get("displayName"),
                                         (home.get("team") or {}).get("abbreviation")),
                away_team=normalize_team((away.get("team") or {}).get("displayName"),
                                         (away.get("team") or {}).get("abbreviation")),
                start_time=ev.get("date"),
                venue=venue.get("fullName"),
                status=((ev.get("status") or {}).get("type") or {}).get("name", "scheduled"),
                meta=dict(espn_id=ev.get("id"), season=(data.get("season") or {}).get("year"),
                          season_type=(data.get("season") or {}).get("type"),
                          week=(data.get("week") or {}).get("number"),
                          source="ESPN scoreboard", source_url=f"{SITE}/{_path(sport)}/scoreboard",
                          retrieved_at=retrieved, indoor=bool(venue.get("indoor")),
                          neutral_site=bool(comp.get("neutralSite")),
                          city=addr.get("city"), state=addr.get("state"),
                          home_abbr=(home.get("team") or {}).get("abbreviation"),
                          away_abbr=(away.get("team") or {}).get("abbreviation"),
                          home_score=home.get("score"), away_score=away.get("score"))))
        return out


class EspnOdds(OddsProvider):
    """Free primary-market feed exposed in ESPN's current scoreboard JSON."""
    name = "espn-odds"
    is_demo = False

    def fetch_odds(self, sport: str) -> List[Dict]:
        if sport != "NFL":
            raise ProviderError(self.name, "only NFL is enabled")
        data = _scoreboard(sport, force=True)
        retrieved = time.time()
        source_url = f"{SITE}/{_path(sport)}/scoreboard"
        rows: List[Dict] = []
        for ev in data.get("events", []) or []:
            comp = (ev.get("competitions") or [{}])[0]
            teams = comp.get("competitors", []) or []
            home_raw = next((t for t in teams if t.get("homeAway") == "home"), {})
            away_raw = next((t for t in teams if t.get("homeAway") == "away"), {})
            home = normalize_team((home_raw.get("team") or {}).get("displayName"),
                                  (home_raw.get("team") or {}).get("abbreviation"))
            away = normalize_team((away_raw.get("team") or {}).get("displayName"),
                                  (away_raw.get("team") or {}).get("abbreviation"))
            if not home or not away:
                continue
            for offer in comp.get("odds", []) or []:
                provider = offer.get("provider") or {}
                book = normalize_book(provider.get("displayName") or provider.get("name"))
                common = dict(event_id=f"espn-{ev.get('id')}", sport=sport, book=book,
                              source_url=source_url, provider_updated_at=None)
                moneyline = offer.get("moneyline") or {}
                for side, selection in (("home", home), ("away", away)):
                    close = (moneyline.get(side) or {}).get("close") or {}
                    price = _price(close.get("odds"))
                    if price is not None:
                        rows.append(dict(common, market="moneyline", selection=selection,
                                         line=None, price=price))
                spread = offer.get("pointSpread") or {}
                for side, selection in (("home", home), ("away", away)):
                    close = (spread.get(side) or {}).get("close") or {}
                    price, line = _price(close.get("odds")), _line(close.get("line"))
                    if price is not None and line is not None:
                        rows.append(dict(common, market="spread", selection=selection,
                                         line=line, price=price))
                total = offer.get("total") or {}
                for side, selection in (("over", "Over"), ("under", "Under")):
                    close = (total.get(side) or {}).get("close") or {}
                    price, line = _price(close.get("odds")), _line(close.get("line"))
                    if price is not None and line is not None:
                        rows.append(dict(common, market="total", selection=selection,
                                         line=abs(line), price=price))
        if not rows:
            raise ProviderError(self.name, "scoreboard returned no current NFL prices")
        return rows


def _price(value) -> Optional[float]:
    try:
        price = float(str(value).replace("+", ""))
        return price if price <= -100 or price >= 100 else None
    except (TypeError, ValueError):
        return None


def _line(value) -> Optional[float]:
    try:
        cleaned = str(value).lower().replace("o", "").replace("u", "").replace("+", "")
        return float(cleaned)
    except (TypeError, ValueError):
        return None


class EspnStats(StatsProvider):
    name = "espn-stats"

    def team_stats(self, sport: str) -> Dict[str, Dict]:
        def fetch():
            return get_json(f"{CORE}/{_path(sport)}/standings",
                            {"level": 3 if sport == "CFB" else 1}, provider="espn-standings")

        data = _cached(f"stand:{sport}", fetch, ttl=6 * 3600)
        out = _parse_team_standings(data)
        usable = {k: v for k, v in out.items() if v["games"] > 0 and
                  (v["points_for"] or v["points_against"])}
        if sport == "NFL" and len(usable) < 32:
            # A just-starting week can omit teams that have not played. Fill only
            # the gaps from the prior completed season and mark that provenance.
            year = datetime.now(timezone.utc).year
            prior = _cached(f"stand:{sport}:{year - 1}", lambda: get_json(
                f"{CORE}/{_path(sport)}/standings", {"level": 1, "season": year - 1},
                provider="espn-standings"), ttl=24 * 3600)
            for name, row in _parse_team_standings(prior).items():
                if name not in usable and row.get("games", 0) > 0:
                    row["source"] = f"espn-standings-{year - 1}"
                    row["extra"]["prior_season_fallback"] = True
                    usable[name] = row
        if len(usable) < 4:
            raise ProviderError("espn-stats",
                                f"only {len(usable)} teams with played games — season not underway")
        return usable


def _parse_team_standings(data) -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        for child in _walk_standings(data):
            for entry in child.get("entries", []) or []:
                team = entry.get("team") or {}
                name = normalize_team(team.get("displayName") or team.get("name"),
                                      team.get("abbreviation"))
                if not name:
                    continue
                stats = {s.get("name"): s for s in entry.get("stats", []) or []}
                gp = _num(stats.get("gamesPlayed"))
                if not gp:
                    gp = sum(_num(stats.get(k)) or 0 for k in ("wins", "losses", "ties"))
                avg_f, avg_a = _num(stats.get("avgPointsFor")), _num(stats.get("avgPointsAgainst"))
                if avg_f is not None and avg_a is not None:
                    ppg_f, ppg_a = avg_f, avg_a
                else:
                    pf, pa = _num(stats.get("pointsFor")), _num(stats.get("pointsAgainst"))
                    if pf is None or pa is None or not gp:
                        continue
                    ppg_f, ppg_a = pf / gp, pa / gp
                out[name] = dict(
                    team=name, games=int(gp or 0),
                    points_for=round(ppg_f, 2), points_against=round(ppg_a, 2),
                    source="espn-standings", retrieved_at=time.time(),
                    extra=dict(record=(stats.get("overall") or {}).get("displayValue")))
        return out


def _walk_standings(node, acc=None):
    acc = acc if acc is not None else []
    if isinstance(node, dict):
        if "standings" in node and isinstance(node["standings"], dict):
            acc.append(node["standings"])
        for v in node.values():
            _walk_standings(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk_standings(v, acc)
    return acc


def _num(stat) -> Optional[float]:
    if not stat:
        return None
    v = stat.get("value")
    if v is None:
        try:
            v = float(str(stat.get("displayValue", "")).replace(",", ""))
        except (TypeError, ValueError):
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class EspnInjuries(InjuryProvider):
    name = "espn-injuries"

    def injuries(self, sport: str, team: Optional[str] = None) -> List[Dict]:
        if sport == "PGA":
            return []

        def fetch():
            return get_json(f"{SITE}/{_path(sport)}/injuries", provider="espn-injuries")

        data = _cached(f"inj:{sport}", fetch, ttl=1800)
        out: List[Dict] = []
        retrieved = _cache.get(f"inj:{sport}", (time.time(), None))[0]
        for grp in (data or {}).get("injuries", []) or []:
            team_obj = grp.get("team") or {}
            tname = normalize_team(grp.get("displayName") or team_obj.get("displayName"),
                                   team_obj.get("abbreviation"))
            for it in grp.get("injuries", []) or []:
                ath = it.get("athlete") or {}
                out.append(dict(
                    team=tname, player=ath.get("displayName") or it.get("longComment", "")[:40],
                    position=((ath.get("position") or {}).get("abbreviation")),
                    status=_injury_status(it.get("status")),
                    injury=(it.get("type") or {}).get("description") or it.get("shortComment"),
                    practice_status=it.get("practiceStatus"), game_status=it.get("status"),
                    detail=(it.get("longComment") or it.get("shortComment") or "")[:240],
                    source="espn-injuries", retrieved_at=retrieved,
                    source_url=f"{SITE}/{_path(sport)}/injuries"))
        return [r for r in out if not team or r["team"] == team]


class EspnNews(NewsProvider):
    name = "espn-news"

    def news(self, sport: str, query: Optional[str] = None) -> List[Dict]:
        def fetch():
            return get_json(f"{SITE}/{_path(sport)}/news", {"limit": 30}, provider="espn-news")

        data = _cached(f"news:{sport}", fetch, ttl=1800)
        items: List[Dict] = []
        retrieved = time.time()
        for a in (data or {}).get("articles", []) or []:
            links = ((a.get("links") or {}).get("web") or {})
            items.append(dict(title=a.get("headline"), url=links.get("href"),
                              publisher="ESPN", published=a.get("published"),
                              summary=a.get("description"), trust="reported",
                              retrieved_at=retrieved))
        if query:
            ql = [w for w in query.lower().split() if len(w) > 3]
            scored = [(sum(w in (i["title"] or "").lower() or w in (i.get("summary") or "").lower()
                           for w in ql), i) for i in items]
            scored.sort(key=lambda x: -x[0])
            hits = [i for s, i in scored if s > 0]
            return hits or items[:8]
        return items[:12]


def _injury_status(value) -> str:
    raw = str(value or "Unknown").strip()
    key = raw.lower().replace("status_", "").replace("injury_", "")
    labels = {"out": "Out", "doubtful": "Doubtful", "questionable": "Questionable",
              "probable": "Probable", "injured reserve": "IR", "ir": "IR",
              "physically unable to perform": "PUP", "pup": "PUP",
              "suspended": "Suspended", "active": "Active"}
    return labels.get(key, raw.title() if raw else "Unknown")
