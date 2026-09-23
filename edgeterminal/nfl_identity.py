"""Point-in-time NFL schedule identity, independent of provider event IDs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional

from .providers.normalize import NFL_TEAMS, normalize_team

_CODES = {name: code for code, name in NFL_TEAMS.items() if len(code) <= 3 and code not in {"jac", "was", "la"}}
_CODES.update({"Jacksonville Jaguars": "jax", "Washington Commanders": "wsh",
               "Los Angeles Rams": "lar"})
# Reverse map: the closed set of canonical full names a provider row may
# resolve to. Unknown raw strings are a refusal, never an identity -- storing
# "Fairfax Flamingos" would defeat event matching between providers.
_CODES_REV = {code: name for name, code in _CODES.items()}


def team_code(value: Optional[str]) -> Optional[str]:
    """Return a stable code only for a known NFL team; never fuzzy-match."""
    return _CODES.get(normalize_team(value))


def canonical_team(value: Optional[str]) -> Optional[str]:
    """Full canonical team name, or None for anything that is not a known NFL team."""
    code = team_code(value)
    return _CODES_REV.get(code) if code else None


def kickoff_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def canonical_id(event: Dict) -> Optional[str]:
    """Season/week and teams survive a kickoff reschedule.

    Missing season/week is a refusal, not an inferred game identity. A provider
    without those fields must be matched against the authoritative schedule.
    """
    meta = event.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return None
    season, week = meta.get("season"), meta.get("week")
    season_type = str(meta.get("season_type") or "").lower()
    home, away = team_code(event.get("home_team")), team_code(event.get("away_team"))
    if not (home and away and home != away and kickoff_utc(event.get("start_time"))):
        return None
    try:
        season, week = int(season), int(week)
    except (TypeError, ValueError):
        return None
    if season < 2000 or week < 1 or week > 22 or season_type not in {"1", "2", "3", "pre", "reg", "post"}:
        return None
    season_type = {"1": "pre", "2": "reg", "3": "post"}.get(season_type, season_type)
    return f"nfl-{season}-{season_type}-{week:02d}-{away}-{home}"


def match_schedule_event(provider_event: Dict, schedule: Iterable[Dict],
                         tolerance_hours: float = 4.0) -> Optional[str]:
    """Match exact teams/home-away and a unique nearby kickoff.

    Provider IDs already mapped in the database should take precedence on a
    later reschedule; this function is for a newly seen external ID.
    """
    home, away = team_code(provider_event.get("home_team")), team_code(provider_event.get("away_team"))
    kickoff = kickoff_utc(provider_event.get("start_time"))
    if not home or not away or not kickoff:
        return None
    found = []
    for game in schedule:
        if team_code(game.get("home_team")) != home or team_code(game.get("away_team")) != away:
            continue
        stamp = kickoff_utc(game.get("start_time"))
        if stamp and abs((stamp - kickoff).total_seconds()) <= tolerance_hours * 3600:
            found.append(game["id"])
    return found[0] if len(set(found)) == 1 else None
