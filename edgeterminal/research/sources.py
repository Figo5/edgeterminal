"""Deterministic source-reliability and freshness classification.

The web is evidence of varying quality. This module turns a URL/title into a
reliability class and a freshness weight so downstream code can decide how much
a claim is allowed to move a projection. Nothing here reads the network.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

PRIMARY = "PRIMARY"
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
UNVERIFIED = "UNVERIFIED"

ORDER = (PRIMARY, HIGH, MEDIUM, LOW, UNVERIFIED)
RANK = {name: i for i, name in enumerate(ORDER)}

# Official league / club properties.
OFFICIAL_DOMAINS = ("nfl.com",)
OFFICIAL_SUFFIXES = ("nfl.com",)
# Official club sites are hosted on the club's own domain (chiefs.com, ...).
CLUB_DOMAINS = (
    "azcardinals.com", "atlantafalcons.com", "baltimoreravens.com", "buffalobills.com",
    "panthers.com", "chicagobears.com", "bengals.com", "clevelandbrowns.com",
    "dallascowboys.com", "denverbroncos.com", "detroitlions.com", "packers.com",
    "houstontexans.com", "colts.com", "jaguars.com", "chiefs.com", "raiders.com",
    "chargers.com", "therams.com", "miamidolphins.com", "vikings.com",
    "patriots.com", "neworleanssaints.com", "giants.com", "newyorkjets.com",
    "philadelphiaeagles.com", "steelers.com", "seahawks.com", "49ers.com",
    "buccaneers.com", "titansonline.com", "commanders.com",
)

HIGH_DOMAINS = ("espn.com", "theathletic.com", "apnews.com", "reuters.com",
                "sports.yahoo.com", "nfl.com")
MEDIUM_DOMAINS = ("cbssports.com", "nbcsports.com", "si.com", "rotowire.com",
                  "fantasypros.com", "pff.com", "profootballtalk.nbcsports.com",
                  "sportsline.com", "usatoday.com", "foxsports.com", "theringer.com",
                  "profootballnetwork.com", "draftsharks.com", "4for4.com",
                  "footballoutsiders.com", "sharpfootballanalysis.com", "vegasinsider.com")
COMMUNITY_DOMAINS = ("reddit.com", "x.com", "twitter.com", "facebook.com",
                     "instagram.com", "tiktok.com", "youtube.com", "t.me",
                     "discord.com", "forums.", "forum.")

INJURY_HINTS = ("injury", "injuries", "practice", "questionable", "doubtful",
                "out-for", "status-report")
ROLE_HINTS = ("depth-chart", "depth chart", "snap", "usage", "role", "workload",
              "backfield", "target-share", "red-zone", "goal-line")
ODDS_HINTS = ("odds", "betting", "lines", "spread", "prop")
WEATHER_HINTS = ("weather", "forecast", "wind", "rain", "temperature")


def _domain(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url if "//" in url else f"//{url}").netloc.lower()
    except ValueError:
        return ""
    return re.sub(r"^www\.", "", host)


def _matches(domain: str, domains: Tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) or d in domain for d in domains)


def topics(url: Optional[str], title: Optional[str] = None) -> list:
    text = f"{url or ''} {title or ''}".lower()
    found = []
    if any(h in text for h in INJURY_HINTS):
        found.append("injury")
    if any(h in text for h in ROLE_HINTS):
        found.append("role")
    if any(h in text for h in ODDS_HINTS):
        found.append("odds")
    if any(h in text for h in WEATHER_HINTS):
        found.append("weather")
    if not found:
        found.append("general")
    return found


def classify(url: Optional[str], title: Optional[str] = None) -> Dict:
    """Map a source to a reliability class, its kind, and the reason for it."""
    domain = _domain(url)
    if not domain:
        return dict(domain="", kind="unknown", reliability=UNVERIFIED, topics=topics(url, title),
                    reason="no resolvable source domain")

    if _matches(domain, CLUB_DOMAINS):
        return dict(domain=domain, kind="club", reliability=PRIMARY, topics=topics(url, title),
                    reason="official club source")
    if _matches(domain, OFFICIAL_DOMAINS):
        kind = "injury_report" if "injury" in (url or "").lower() else "league"
        return dict(domain=domain, kind=kind, reliability=PRIMARY, topics=topics(url, title),
                    reason="official league source")
    if _matches(domain, COMMUNITY_DOMAINS):
        return dict(domain=domain, kind="community", reliability=UNVERIFIED,
                    topics=topics(url, title),
                    reason="community/social post — supporting context only")
    if _matches(domain, HIGH_DOMAINS):
        return dict(domain=domain, kind="outlet", reliability=HIGH, topics=topics(url, title),
                    reason="established sports outlet")
    if _matches(domain, MEDIUM_DOMAINS):
        return dict(domain=domain, kind="data_site", reliability=MEDIUM, topics=topics(url, title),
                    reason="established data/analysis site")
    if any(token in domain for token in ("blog", "betting", "tips", "predict", "picks")):
        return dict(domain=domain, kind="blog", reliability=LOW, topics=topics(url, title),
                    reason="unverified betting/analysis blog")
    return dict(domain=domain, kind="unknown", reliability=LOW, topics=topics(url, title),
                reason="unrecognised domain")


def is_confirming(url: Optional[str]) -> bool:
    """Only PRIMARY/HIGH sources may confirm a factual claim on their own."""
    return RANK[classify(url)["reliability"]] <= RANK[HIGH]


# --------------------------------------------------------------------- freshness
_HALF_LIFE_S = {"injury": 12 * 3600.0, "role": 4 * 86400.0, "depth_chart": 10 * 86400.0,
                "odds": 1800.0, "weather": 6 * 3600.0, "general": 5 * 86400.0}


def freshness(published_at: Optional[float], now: float, topic: str = "general") -> float:
    """Exponential recency weight in [0,1]; unknown timestamps are treated as old."""
    half_life = _HALF_LIFE_S.get(topic, _HALF_LIFE_S["general"])
    if published_at is None:
        return round(min(0.5, 2 ** (-1.0)), 4)
    age = max(0.0, float(now) - float(published_at))
    return round(2 ** (-age / half_life), 4)


def relevance_label(published_at: Optional[float], now: float, topic: str = "general") -> str:
    weight = freshness(published_at, now, topic=topic)
    if weight >= 0.75:
        return "current"
    if weight >= 0.35:
        return "recent"
    if weight >= 0.15:
        return "background"
    return "stale"