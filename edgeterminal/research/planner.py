"""Decide what actually needs researching before paying for a search.

A prop question has a finite set of inputs. Structured data answers most of them;
only the genuine gaps become web queries, and each query must name the gap it is
closing. Redundant searching is a cost bug, not a feature.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

MIN_USAGE_GAMES = 4

# gap -> (priority, topic, query template)
GAP_TEMPLATES = {
    "odds": ("high", "odds", "{team} player prop odds"),
    "injuries": ("high", "injury", "{team} injury report practice status"),
    "usage": ("medium", "role", "{player} usage snaps carries targets latest"),
    "role": ("high", "role", "{player} expected role workload {team}"),
    "depth_chart": ("medium", "depth_chart", "{team} depth chart {position}"),
    "implied_total": ("medium", "odds", "{team} team total implied points"),
    "matchup": ("medium", "general", "{team} opponent defensive matchup {position}"),
    "weather": ("low", "weather", "{team} stadium weather forecast kickoff"),
}


def _fill(template: str, *, team: Optional[str], player: Optional[str],
          request: Dict, position: Optional[str]) -> str:
    text = template
    text = text.replace("{team}", team or request.get("event_name") or "NFL team")
    text = text.replace("{player}", player or request.get("player") or "player")
    text = text.replace("{position}", position or request.get("position") or "skill positions")
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_key(query: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", query.lower())


def plan_prop_research(request: Dict, known: Dict,
                       players: Optional[Iterable[str]] = None,
                       team: Optional[str] = None,
                       position: Optional[str] = None) -> List[Dict]:
    """Return the ordered, de-duplicated research steps for a prop analysis."""
    known = dict(known or {})
    steps: List[Dict] = []

    def add(gap: str, why: str, player: Optional[str] = None) -> None:
        priority, topic, template = GAP_TEMPLATES[gap]
        steps.append(dict(gap=gap, priority=priority, topic=topic,
                          query=_fill(template, team=team, player=player,
                                      request=request, position=position),
                          why=why, researched_for=player))

    if not known.get("has_odds"):
        add("odds", "no structured prop prices loaded yet")
    if not known.get("has_injuries"):
        add("injuries", "no current injury/status feed for this game")

    usage_games = int(known.get("usage_games") or 0)
    role_stable = bool(known.get("role_stable"))
    usage_players = list(players or [])[:3]
    if not known.get("has_usage"):
        add("usage", "no structured usage rows for these players")
    if usage_games < MIN_USAGE_GAMES or not role_stable:
        if usage_players:
            for name in usage_players:
                add("role", "usage sample is too small or the role is unstable", player=name)
        else:
            add("role", "usage sample is too small or the role is unstable")
    if not known.get("has_role") and not usage_players:
        add("role", "no role/depth-chart confirmation from a trusted source")
    if not known.get("has_implied_total"):
        add("implied_total", "team scoring environment is unknown")
    if not known.get("has_matchup", False) and (
            not known.get("has_role") or not known.get("has_injuries")):
        add("matchup", "opponent context is unconfirmed")
    if not known.get("has_weather"):
        add("weather", "outdoor conditions not yet retrieved")

    seen = set()
    unique: List[Dict] = []
    for step in sorted(steps, key=lambda s: ("high", "medium", "low").index(s["priority"])):
        key = _dedupe_key(step["query"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(step)
    return unique