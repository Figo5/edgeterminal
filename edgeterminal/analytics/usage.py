"""Player usage from reproducible NFL play-by-play.

Opportunity first: carries, targets, red-zone and goal-line touches are counted
from actual plays. Anything play-by-play cannot support (snap share, route
participation, air-yard share) is returned as explicitly missing — never
estimated, never fabricated.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from ..providers.normalize import normalize_team

RED_ZONE_YARDLINE = 20.0
GOAL_LINE_YARDLINE = 5.0
MISSING_METRICS = ["snap_share", "route_participation"]
RECENT_GAMES = 4

# Columns read from the nflverse play-by-play cache for player usage.
USAGE_COLUMNS = (
    "game_id", "season", "week", "season_type", "posteam", "td_team", "touchdown",
    "rush_attempt", "rush_touchdown", "rusher_player_id", "rusher_player_name",
    "pass_attempt", "complete_pass", "pass_touchdown", "interception", "yards_gained",
    "air_yards", "yardline_100", "receiver_player_id", "receiver_player_name",
    "passer_player_id", "passer_player_name",
)


def _num(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


def _player(row: Dict, kind: str):
    return row.get(f"{kind}_player_id"), row.get(f"{kind}_player_name")


def _blank(name: Optional[str], team: Optional[str]) -> Dict:
    return dict(player_id=None, name=name, team=team, games=set(),
                carries=0.0, rush_yards=0.0, rush_tds=0.0,
                targets=0.0, receptions=0.0, rec_yards=0.0, rec_tds=0.0,
                air_yards=0.0, pass_attempts=0.0, completions=0.0, pass_yards=0.0,
                pass_tds=0.0, interceptions=0.0, red_zone_touches=0.0,
                goal_line_touches=0.0, red_zone_targets=0.0, red_zone_carries=0.0,
                td_team_tds=0.0)


def aggregate_player_usage(plays: Iterable[Dict], season: int,
                           team: Optional[str] = None,
                           upto_week: Optional[int] = None,
                           retrieved_at: Optional[float] = None) -> Dict[str, Dict]:
    """Aggregate offensive usage per player from regular-season plays."""
    rows: Dict[str, Dict] = {}
    team_tds = defaultdict(float)
    for play in plays:
        if _num(play.get("season")) not in (None, float(season)):
            continue
        if str(play.get("season_type") or "REG").upper() != "REG":
            continue
        week = _num(play.get("week"))
        if upto_week is not None and week is not None and week >= float(upto_week):
            continue
        offence = normalize_team(None, play.get("posteam"))
        if not offence:
            continue
        if team and offence != team:
            continue
        game = str(play.get("game_id") or "")
        yardline = _num(play.get("yardline_100"))
        in_red_zone = yardline is not None and yardline <= RED_ZONE_YARDLINE
        at_goal_line = yardline is not None and yardline <= GOAL_LINE_YARDLINE
        if _num(play.get("touchdown")) == 1 and play.get("td_team"):
            if normalize_team(None, play.get("td_team")) == offence:
                team_tds[offence] += 1.0

        if _num(play.get("rush_attempt")) == 1:
            pid, name = _player(play, "rusher")
            if name:
                key = pid or name
                entry = rows.setdefault(key, _blank(name, offence))
                entry["player_id"] = pid
                entry["games"].add(game)
                entry["carries"] += 1.0
                entry["rush_yards"] += _num(play.get("yards_gained")) or 0.0
                if _num(play.get("rush_touchdown")) == 1:
                    entry["rush_tds"] += 1.0
                if in_red_zone:
                    entry["red_zone_touches"] += 1.0
                    entry["red_zone_carries"] += 1.0
                if at_goal_line:
                    entry["goal_line_touches"] += 1.0

        if _num(play.get("pass_attempt")) == 1:
            pid, name = _player(play, "receiver")
            if name:
                key = "rec:" + (pid or name)
                entry = rows.setdefault(key, _blank(name, offence))
                entry["player_id"] = pid
                entry["games"].add(game)
                entry["targets"] += 1.0
                air = _num(play.get("air_yards"))
                if air is not None:
                    entry["air_yards"] += air
                if _num(play.get("complete_pass")) == 1:
                    entry["receptions"] += 1.0
                    entry["rec_yards"] += _num(play.get("yards_gained")) or 0.0
                    if _num(play.get("pass_touchdown")) == 1:
                        entry["rec_tds"] += 1.0
                if in_red_zone:
                    entry["red_zone_targets"] += 1.0
                    entry["red_zone_touches"] += 1.0
                if at_goal_line:
                    entry["goal_line_touches"] += 1.0

        pid, name = _player(play, "passer")
        if name and _num(play.get("pass_attempt")) == 1:
            key = "pass:" + (pid or name)
            entry = rows.setdefault(key, _blank(name, offence))
            entry["player_id"] = pid
            entry["games"].add(game)
            entry["pass_attempts"] += 1.0
            if _num(play.get("complete_pass")) == 1:
                entry["completions"] += 1.0
                entry["pass_yards"] += _num(play.get("yards_gained")) or 0.0
            if _num(play.get("pass_touchdown")) == 1:
                entry["pass_tds"] += 1.0
            if _num(play.get("interception")) == 1:
                entry["interceptions"] += 1.0

    stamp = float(retrieved_at if retrieved_at is not None else time.time())
    out: Dict[str, Dict] = {}
    for key, entry in rows.items():
        games = len(entry.pop("games"))
        if not games:
            continue
        touches = entry["carries"] + entry["targets"]
        entry.update(
            key=key, games=games,
            total_tds=entry["rush_tds"] + entry["rec_tds"],
            total_yards=entry["rush_yards"] + entry["rec_yards"] + entry["pass_yards"],
            touches=touches,
            snap_share=None,
            rush_yards=round(entry["rush_yards"], 2), rec_yards=round(entry["rec_yards"], 2),
            air_yards=round(entry["air_yards"], 2), pass_yards=round(entry["pass_yards"], 2),
            touches_per_game=round(touches / games, 3),
            carries_per_game=round(entry["carries"] / games, 3),
            targets_per_game=round(entry["targets"] / games, 3),
            td_per_game=round((entry["rush_tds"] + entry["rec_tds"]) / games, 3),
            pass_attempts_per_game=round(entry["pass_attempts"] / games, 3),
            team=entry["team"], season=int(season), source="nflverse-pbp",
            retrieved_at=stamp, missing=list(MISSING_METRICS))
        out[key] = entry
    return out


def find_player(rows: Dict[str, Dict], player: str) -> Optional[Dict]:
    """Resolve a player name against the abbreviated names nflverse stores.

    Play-by-play calls players "J.Conner" and "A.St. Brown", while the Odds API
    uses "James Conner" and "Amon-Ra St. Brown". Matching therefore runs on a
    normalised surname plus first initial, never on the raw string.
    """
    candidates = find_candidates(rows, player)
    return candidates[0] if candidates else None


def find_candidates(rows: Dict[str, Dict], player: str) -> List[Dict]:
    """Every usage row that could be this player, best match first.

    Integrity code needs the whole candidate set: a name that matches two people
    must be refused, not silently resolved to the busier one.
    """
    if not (player or "").strip():
        return []
    needle = player.strip().lower()
    by_key = [r for r in rows.values() if (r.get("key") or "").lower().endswith(needle)]
    if by_key:
        return sorted(by_key, key=lambda r: -r["touches"])
    wanted = _name_parts(player)
    if not wanted["last"]:
        return []
    scored: List[tuple] = []
    for row in rows.values():
        parts = _name_parts(row.get("name"))
        if not parts["last"]:
            continue
        last_ok = parts["last"] == wanted["last"]
        last2_ok = bool(parts["last2"]) and parts["last2"] == wanted["last2"]
        if not (last_ok or last2_ok):
            continue
        initial_ok = (_same_initial(parts["initial"], wanted["initial"])
                      or last2_ok and parts["last2"])
        if not initial_ok:
            continue
        strength = 3 if parts["normalised"] == wanted["normalised"] else (
            2 if (parts["first"] and parts["first"] == wanted["first"]) else 1)
        scored.append((strength, float(row.get("touches") or 0), row))
    if not scored:
        # Last resort: the query is a prefix of a stored name ("Kelce" -> "T.Kelce"
        # is already covered, so this only catches unusual fixtures).
        partial = [r for r in rows.values()
                   if (player or "").strip().lower() in (r.get("name") or "").lower()]
        return sorted(partial, key=lambda r: -r["touches"])
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in scored]


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _same_initial(a: str, b: str) -> bool:
    """A missing initial is a wildcard; two present initials must agree.

    Being permissive here is how 'Amon-Ra St. Brown' would otherwise match
    'C.Brown': a one-letter initial is still an initial.
    """
    if not a or not b:
        return True
    return a[0] == b[0]


def _name_parts(name: Optional[str]) -> Dict[str, str]:
    """'J.Conner', 'J.K. Dobbins' and 'James Conner' all resolve to 'conner'.

    The surname is the trailing token(s); the initial is the first letter of the
    first name token. Multi-word surnames ('st brown') are kept for the
    higher-confidence two-token comparison.
    """
    raw = re.sub(r"[^A-Za-z .'\-]", " ", str(name or ""))
    raw = raw.replace("-", " ").replace(".", " ").replace("'", "")
    tokens = [t for t in raw.lower().split() if t and t not in SUFFIXES]
    if not tokens:
        return dict(normalised="", first="", initial="", last="", last2="")
    if len(tokens) == 1:
        return dict(normalised=tokens[0], first="", initial="", last=tokens[0], last2="")
    first, surname_tokens = tokens[0], tokens[1:]
    return dict(normalised=" ".join(tokens), first=first, initial=first[:1],
                last=surname_tokens[-1], last2=" ".join(surname_tokens[-2:]))


def role_stability(rows: Dict[str, Dict], player_key: str, recent: int = RECENT_GAMES) -> Dict:
    """Is the player's touch volume trending, and how noisy is it?"""
    entry = rows.get(player_key)
    if not entry:
        return dict(stability="unknown", reason="no usage rows", trend=None)
    per_game = entry["touches"] / max(1, entry["games"])
    if entry["games"] < MIN_GAMES_FOR_TREND:
        return dict(stability="volatile", trend=None, touches_per_game=round(per_game, 2),
                    reason=f"only {entry['games']} game(s) of usage")
    return dict(stability="stable", trend=None, touches_per_game=round(per_game, 2),
                reason=f"{entry['games']} games at {per_game:.1f} touches/game")


MIN_GAMES_FOR_TREND = 4


def usage_report(plays: Iterable[Dict], season: int, player: str,
                 team: Optional[str] = None, upto_week: Optional[int] = None) -> Dict:
    rows = aggregate_player_usage(plays, season=season, team=team, upto_week=upto_week)
    entry = find_player(rows, player)
    if not entry:
        return dict(available=False, player=player, reason="no play-by-play usage rows for this player",
                    missing=list(MISSING_METRICS), source="nflverse-pbp")
    stability = role_stability(rows, entry["key"])
    red_zone_share = None
    team_rz = sum(r["red_zone_touches"] for r in rows.values() if r["team"] == entry["team"])
    if team_rz:
        red_zone_share = round(entry["red_zone_touches"] / team_rz, 4)
    entry = dict(entry)
    entry["missing"] = list(MISSING_METRICS)
    return dict(available=True, player=entry["name"], usage=entry,
                role_stability=stability["stability"], stability_reason=stability["reason"],
                red_zone_share=red_zone_share,
                missing=entry["missing"], source="nflverse-pbp")