"""Canonical identifiers shared by all external providers."""
from __future__ import annotations

import re
from typing import Optional

NFL_TEAMS = {
    "ari": "Arizona Cardinals", "arizona cardinals": "Arizona Cardinals",
    "atl": "Atlanta Falcons", "atlanta falcons": "Atlanta Falcons",
    "bal": "Baltimore Ravens", "baltimore ravens": "Baltimore Ravens",
    "buf": "Buffalo Bills", "buffalo bills": "Buffalo Bills",
    "car": "Carolina Panthers", "carolina panthers": "Carolina Panthers",
    "chi": "Chicago Bears", "chicago bears": "Chicago Bears",
    "cin": "Cincinnati Bengals", "cincinnati bengals": "Cincinnati Bengals",
    "cle": "Cleveland Browns", "cleveland browns": "Cleveland Browns",
    "dal": "Dallas Cowboys", "dallas cowboys": "Dallas Cowboys",
    "den": "Denver Broncos", "denver broncos": "Denver Broncos",
    "det": "Detroit Lions", "detroit lions": "Detroit Lions",
    "gb": "Green Bay Packers", "green bay packers": "Green Bay Packers",
    "hou": "Houston Texans", "houston texans": "Houston Texans",
    "ind": "Indianapolis Colts", "indianapolis colts": "Indianapolis Colts",
    "jax": "Jacksonville Jaguars", "jacksonville jaguars": "Jacksonville Jaguars",
    "jac": "Jacksonville Jaguars",
    "kc": "Kansas City Chiefs", "kc chiefs": "Kansas City Chiefs", "kansas city chiefs": "Kansas City Chiefs",
    "lv": "Las Vegas Raiders", "las vegas raiders": "Las Vegas Raiders",
    "lac": "Los Angeles Chargers", "la chargers": "Los Angeles Chargers", "los angeles chargers": "Los Angeles Chargers",
    "lar": "Los Angeles Rams", "la rams": "Los Angeles Rams", "los angeles rams": "Los Angeles Rams",
    "mia": "Miami Dolphins", "miami dolphins": "Miami Dolphins",
    "min": "Minnesota Vikings", "minnesota vikings": "Minnesota Vikings",
    "ne": "New England Patriots", "new england patriots": "New England Patriots",
    "no": "New Orleans Saints", "new orleans saints": "New Orleans Saints",
    "nyg": "New York Giants", "new york giants": "New York Giants",
    "nyj": "New York Jets", "new york jets": "New York Jets",
    "phi": "Philadelphia Eagles", "philadelphia eagles": "Philadelphia Eagles",
    "pit": "Pittsburgh Steelers", "pittsburgh steelers": "Pittsburgh Steelers",
    "sea": "Seattle Seahawks", "seattle seahawks": "Seattle Seahawks",
    "sf": "San Francisco 49ers", "san francisco 49ers": "San Francisco 49ers",
    "tb": "Tampa Bay Buccaneers", "tampa bay buccaneers": "Tampa Bay Buccaneers",
    "ten": "Tennessee Titans", "tennessee titans": "Tennessee Titans",
    "wsh": "Washington Commanders", "was": "Washington Commanders", "washington commanders": "Washington Commanders",
}

BOOKS = {
    "fanduel": "fanduel", "fanduel sportsbook": "fanduel", "fan duel": "fanduel",
    "bet365": "bet365", "bet 365": "bet365", "bet365 us": "bet365",
    "draftkings": "draftkings", "draft kings": "draftkings", "draftkings sportsbook": "draftkings",
    "caesars": "caesars", "caesars sportsbook": "caesars", "william hill": "caesars",
    "betmgm": "betmgm", "bet mgm": "betmgm", "betmgm sportsbook": "betmgm",
    "espn bet": "espnbet", "espnbet": "espnbet",
}


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", value.lower())).strip()


def normalize_team(name: Optional[str], abbreviation: Optional[str] = None) -> Optional[str]:
    if not name and not abbreviation:
        return None
    for candidate in (name, abbreviation):
        if candidate and _key(candidate) in NFL_TEAMS:
            return NFL_TEAMS[_key(candidate)]
    return str(name or abbreviation).strip()


def normalize_book(name: Optional[str]) -> str:
    key = _key(str(name or "unknown"))
    return BOOKS.get(key, key.replace(" ", "") or "unknown")
