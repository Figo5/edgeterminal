"""Provider resolution with an explicit fallback hierarchy.

    primary free source -> secondary free source -> cached -> demo -> unavailable

Demo fixtures are only used when DEMO_MODE is on or nothing else is configured,
and everything they return is tagged is_demo so the UI can label it. A provider
failure is recorded as a structured status, never silently replaced by invented
numbers.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import settings
from . import demo as D
from .base import ProviderError
from .espn import EspnEvents, EspnInjuries, EspnNews, EspnOdds, EspnStats
from .openmeteo import OpenMeteo
from .search import Brave, DuckDuckGo, NoSearch, Tavily

log = logging.getLogger("edgeterminal.registry")

STATUS: Dict[str, Dict] = {}


def record(kind: str, name: str, ok: bool, detail: str = "", is_demo: bool = False) -> None:
    prev = STATUS.get(kind, {})
    STATUS[kind] = dict(kind=kind, name=name, ok=ok, detail=detail, is_demo=is_demo,
                        last_attempt=time.time(),
                        last_success=time.time() if ok else prev.get("last_success"))


def statuses() -> List[Dict]:
    return sorted(STATUS.values(), key=lambda s: s["kind"])


def _chain(kind: str, candidates: List[Tuple[str, Callable[[], Any]]],
           call: Callable[[Any], Any]) -> Tuple[Any, str, bool]:
    """Try each candidate in order. Returns (result, provider_name, is_demo)."""
    errors = []
    for name, factory in candidates:
        try:
            provider = factory()
        except ProviderError as e:
            errors.append(f"{name}: {e.detail}")
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        try:
            result = call(provider)
            if result in (None, [], {}):
                errors.append(f"{name}: empty result")
                continue
            detail = ("fallback after: " + "; ".join(errors[-3:])) if errors else ""
            record(kind, name, True, detail=detail,
                   is_demo=getattr(provider, "is_demo", False))
            return result, name, getattr(provider, "is_demo", False)
        except ProviderError as e:
            log.warning("%s provider %s failed: %s", kind, name, e.detail)
            errors.append(f"{name}: {e.detail}")
        except Exception as e:  # noqa: BLE001
            log.warning("%s provider %s raised: %s", kind, name, e)
            errors.append(f"{name}: {type(e).__name__}: {e}")
    record(kind, candidates[-1][0] if candidates else "none", False, "; ".join(errors[-3:]))
    raise ProviderError(kind, "; ".join(errors) or "no providers configured")


# Which sports are currently being served demo odds. Mixing a demo price book
# with live team stats would manufacture edges that do not exist, so the whole
# data family for a sport moves together.
_DEMO_FAMILY: Dict[str, bool] = {}


def set_family(sport: str, is_demo: bool) -> None:
    _DEMO_FAMILY[sport] = bool(is_demo)


def family_is_demo(sport: Optional[str]) -> bool:
    return bool(sport and _DEMO_FAMILY.get(sport))


def _use_demo(sport: Optional[str] = None) -> bool:
    return settings.demo_mode or family_is_demo(sport)


# ---------------------------------------------------------------------- odds
def _odds_candidates(sport: Optional[str] = None) -> List[Tuple[str, Callable]]:
    if _use_demo():
        return [("demo-odds", D.DemoOdds)]
    out: List[Tuple[str, Callable]] = []
    if settings.odds_provider in ("auto", "theoddsapi") and settings.odds_api_key:
        from .theoddsapi import TheOddsApi
        out.append(("theoddsapi", TheOddsApi))
    if sport == "NFL" and settings.odds_provider in ("auto", "espn"):
        out.append(("espn-odds", EspnOdds))
    out.append(("demo-odds", D.DemoOdds))
    return out


def fetch_odds(sport: str) -> Tuple[List[Dict], str, bool]:
    return _chain("odds", _odds_candidates(sport), lambda p: p.fetch_odds(sport))


def list_events(sport: str, prefer: Optional[str] = None) -> Tuple[List[Dict], str, bool]:
    """Events must come from the same family as the odds, otherwise the event
    ids will not line up and every market ends up orphaned."""
    cands: List[Tuple[str, Callable]] = []
    if prefer and prefer.startswith("demo"):
        cands.append(("demo-odds", D.DemoOdds))
    elif not _use_demo():
        if settings.odds_provider in ("auto", "theoddsapi") and settings.odds_api_key:
            from .theoddsapi import TheOddsApi
            cands.append(("theoddsapi-events", TheOddsApi))
        cands.append(("espn-events", EspnEvents))
        cands.append(("demo-odds", D.DemoOdds))
    else:
        cands.append(("demo-odds", D.DemoOdds))
    return _chain("events", cands, lambda p: p.list_events(sport))


# ---------------------------------------------------------------------- stats
def team_stats(sport: str) -> Tuple[Dict[str, Dict], str, bool]:
    cands: List[Tuple[str, Callable]] = []
    if not _use_demo(sport) and settings.stats_provider in ("auto", "espn"):
        cands.append(("espn-stats", EspnStats))
    if _use_demo(sport):
        cands.append(("demo-stats", D.DemoStats))
    stats, source, is_demo = _chain("stats", cands, lambda p: p.team_stats(sport))
    if sport == "NFL" and not is_demo and settings.stats_provider in ("auto", "espn", "nflverse"):
        from .nflverse import load_cached_team_features, merge_feature_seasons
        season = datetime.now(timezone.utc).year
        advanced = load_cached_team_features(season, settings.nflverse_cache_dir)
        # Early in a season only some teams have plays; fill the gaps per team
        # from the prior season rather than dropping the efficiency inputs.
        prior = load_cached_team_features(season - 1, settings.nflverse_cache_dir)
        advanced = merge_feature_seasons(advanced, prior, season=season)
        if advanced:
            stats = {team: dict(row, **advanced.get(team, {})) for team, row in stats.items()}
            source = f"{source}+nflverse-pbp"
            record("stats", source, True, detail="ESPN context merged with locally cached nflverse PBP")
    return stats, source, is_demo


def player_pool(sport: str, team: str) -> Tuple[List[Dict], str, bool]:
    """Player usage baselines. Only demo fixtures ship with these today --
    a real usage feed (nflverse / NBA stats) plugs in here."""
    try:
        cands = [("demo-stats", D.DemoStats)] if _use_demo(sport) else []
        return _chain("players", cands,
                      lambda p: p.player_stats(sport, team))
    except ProviderError:
        return [], "none", False


# ------------------------------------------------------------------ injuries
def injuries(sport: str, team: Optional[str] = None) -> Tuple[List[Dict], str, bool]:
    cands: List[Tuple[str, Callable]] = []
    if not _use_demo(sport) and settings.injury_provider in ("auto", "espn"):
        cands.append(("espn-injuries", EspnInjuries))
    if _use_demo(sport):
        cands.append(("demo-injuries", D.DemoInjuries))
    try:
        return _chain("injuries", cands, lambda p: p.injuries(sport, team))
    except ProviderError as e:
        record("injuries", "none", False, str(e))
        return [], "unavailable", False


# ---------------------------------------------------------------------- news
def news(sport: str, query: Optional[str] = None) -> Tuple[List[Dict], str, bool]:
    cands: List[Tuple[str, Callable]] = []
    if not _use_demo(sport) and settings.news_provider in ("auto", "espn"):
        cands.append(("espn-news", EspnNews))
    if _use_demo(sport):
        cands.append(("demo-news", D.DemoNews))
    try:
        return _chain("news", cands, lambda p: p.news(sport, query))
    except ProviderError:
        return [], "unavailable", False


# -------------------------------------------------------------------- weather
def weather(lat: Optional[float], lon: Optional[float], when_iso: Optional[str] = None,
            indoor: bool = False, city: Optional[str] = None,
            state: Optional[str] = None, sport: Optional[str] = None) -> Tuple[Optional[Dict], str, bool]:
    if indoor:
        record("weather", "n/a-indoor", True)
        return dict(indoor=True, conditions="Indoor venue", source="venue"), "indoor", False
    if (lat is None or lon is None) and city and not _use_demo(sport):
        try:
            lat, lon = OpenMeteo().geocode(city, state)
        except ProviderError as e:
            record("weather", "open-meteo", False, e.detail)
    if lat is None or lon is None:
        record("weather", "none", False, "no venue coordinates or geocodable city")
        return None, "unavailable", False
    cands: List[Tuple[str, Callable]] = []
    if not _use_demo(sport) and settings.weather_provider in ("auto", "openmeteo"):
        cands.append(("open-meteo", OpenMeteo))
    if _use_demo(sport):
        cands.append(("demo-weather", D.DemoWeather))
    try:
        return _chain("weather", cands, lambda p: p.forecast(lat, lon, when_iso))
    except ProviderError:
        return None, "unavailable", False


# ----------------------------------------------------------------------- golf
def golf_tournament() -> Tuple[Optional[Dict], str, bool]:
    try:
        return _chain("golf", [("demo-golf", D.DemoGolf)], lambda p: p.tournament())
    except ProviderError:
        return None, "unavailable", False


def golf_field() -> Tuple[Dict[str, Dict], str, bool]:
    try:
        return _chain("golf-field", [("demo-golf", D.DemoGolf)], lambda p: p.field_stats())
    except ProviderError:
        return {}, "unavailable", False


# --------------------------------------------------------------------- search
def web_search(query: str, limit: int = 5) -> Tuple[List[Dict], str, bool]:
    if _use_demo():
        cands = [("demo-search", D.DemoSearch)]
    else:
        by_name = {"duckduckgo": DuckDuckGo, "tavily": Tavily, "brave": Brave, "none": NoSearch}
        primary = by_name.get(settings.search_provider, DuckDuckGo)
        cands = [(settings.search_provider, primary)]
        if primary is not DuckDuckGo and settings.search_provider != "none":
            cands.append(("duckduckgo", DuckDuckGo))
    try:
        return _chain("search", cands, lambda p: p.search(query, limit))
    except ProviderError as e:
        record("search", settings.search_provider, False, str(e))
        return [], "unavailable", False
