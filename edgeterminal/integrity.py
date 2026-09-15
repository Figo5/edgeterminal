"""Event and player integrity.

Proven failure modes this guards against:

* a stored provider id that no longer exists (completed games are removed from
  the feed) silently resolving to a *different* live game;
* two meetings between the same teams on different dates;
* provider payloads whose matchup/kickoff does not match the requested event;
* prop rows for players who cannot be shown to play in this event;
* cache entries shared between events.

The local event id and the provider event id are kept explicitly separate
everywhere: `event["id"]` is ours, `provider_id` is theirs, and only a validated
pair may be used to fetch or cache anything.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Dict, Iterable, List, Optional

CACHE_VERSION = 2

IDENTITY_OK = "OK"
IDENTITY_EXPIRED = "EXPIRED"
IDENTITY_MISMATCH = "MISMATCH"
IDENTITY_AMBIGUOUS = "AMBIGUOUS"
IDENTITY_UNAVAILABLE = "UNAVAILABLE"

PARTICIPANT = "participant"
MISMATCH = "mismatch"
UNRESOLVED = "unresolved"

DEFAULT_KICKOFF_TOLERANCE_S = 6 * 3600.0
GAME_WINDOW_S = 4 * 3600.0          # a game is over ~4h after kickoff
NFL_SPORT_KEYS = ("americanfootball_nfl", "NFL")
TEAM_UNIT_RE = re.compile(r"\b(d/st|defense|special teams)\b", re.I)


# --------------------------------------------------------------------- helpers
def parse_time(value) -> Optional[float]:
    """ISO-8601 (with Z or offset) or an epoch value -> epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    return None


def _team_key(name: Optional[str]) -> str:
    from .providers.normalize import normalize_team
    canonical = normalize_team(name) or (name or "")
    return re.sub(r"[^a-z ]", "", canonical.lower()).strip()


def _same_team(left: Optional[str], right: Optional[str]) -> bool:
    a, b = _team_key(left), _team_key(right)
    return bool(a) and a == b


def prop_cache_key(provider_id: Optional[str], event: Dict, markets: Iterable[str]) -> str:
    """Cache identity for prop payloads: version + both event ids + matchup + kickoff."""
    fingerprint = "|".join([
        f"v{CACHE_VERSION}",
        str(provider_id or "none"),
        str(event.get("id") or "none"),
        _team_key(event.get("away_team")), _team_key(event.get("home_team")),
        str(parse_time(event.get("start_time")) or 0),
        ",".join(sorted(markets or [])),
    ])
    return f"props:v{CACHE_VERSION}:" + hashlib.sha1(fingerprint.encode()).hexdigest()[:20]


# ---------------------------------------------------------------- event identity
def _mismatch_reasons(payload: Dict, event: Dict) -> List[str]:
    reasons: List[str] = []
    sport_key = str(payload.get("sport_key") or payload.get("sport") or "NFL")
    if sport_key not in NFL_SPORT_KEYS:
        reasons.append(f"provider event is not NFL ({sport_key})")
    if not _same_team(payload.get("home_team"), event.get("home_team")):
        reasons.append(f"home team mismatch (provider {payload.get('home_team')!r} vs "
                       f"stored {event.get('home_team')!r})")
    if not _same_team(payload.get("away_team"), event.get("away_team")):
        reasons.append(f"away team mismatch (provider {payload.get('away_team')!r} vs "
                       f"stored {event.get('away_team')!r})")
    return reasons


def event_is_expired(event: Dict, now: Optional[float] = None) -> bool:
    kickoff = parse_time(event.get("start_time"))
    stamp = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    if kickoff is None:
        return False
    return kickoff + GAME_WINDOW_S < stamp


def validate_provider_event(payload: Dict, event: Dict,
                            now: Optional[float] = None,
                            tolerance_s: float = DEFAULT_KICKOFF_TOLERANCE_S) -> Dict:
    """Compare a provider payload against the event we asked for."""
    stamp = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    provider_id = payload.get("id")
    verdict = dict(state=IDENTITY_OK, provider_id=provider_id, reasons=[],
                   requested=dict(event_id=event.get("id"), home=event.get("home_team"),
                                  away=event.get("away_team"),
                                  kickoff=event.get("start_time")),
                   returned=dict(provider_id=provider_id, home=payload.get("home_team"),
                                 away=payload.get("away_team"),
                                 kickoff=payload.get("commence_time")))
    if event_is_expired(event, stamp):
        verdict["state"] = IDENTITY_EXPIRED
        verdict["provider_id"] = None
        verdict["reasons"].append(
            f"stored event kicked off {event.get('start_time')} and is already finished")
        return verdict
    reasons = _mismatch_reasons(payload, event)
    kickoff = parse_time(payload.get("commence_time"))
    stored_kickoff = parse_time(event.get("start_time"))
    if kickoff is not None and stored_kickoff is not None and abs(kickoff - stored_kickoff) > tolerance_s:
        reasons.append(f"kickoff mismatch (provider {payload.get('commence_time')} vs "
                       f"stored {event.get('start_time')})")
    if kickoff is not None and kickoff + GAME_WINDOW_S < stamp:
        reasons.append("provider event has already finished")
    if reasons:
        verdict["state"] = IDENTITY_MISMATCH
        verdict["provider_id"] = None
        verdict["reasons"] = reasons
    return verdict


def match_provider_event(candidates: Iterable[Dict], event: Dict,
                         now: Optional[float] = None,
                         tolerance_s: float = DEFAULT_KICKOFF_TOLERANCE_S) -> Dict:
    """Find the provider event that is *this* game, or refuse explicitly.

    Matching is exact on both canonical team names, then nearest kickoff within
    tolerance. Two plausible candidates are refused as ambiguous rather than
    guessed, and a finished stored event is never substituted with the next
    meeting between the same teams.
    """
    stamp = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    if event_is_expired(event, stamp):
        return dict(state=IDENTITY_EXPIRED, provider_id=None, candidate=None, alternatives=[],
                    reasons=[f"stored event kicked off {event.get('start_time')} and is finished"])
    stored_kickoff = parse_time(event.get("start_time"))
    plausible: List[Dict] = []
    for candidate in candidates or []:
        if _mismatch_reasons(candidate, event):
            continue
        kickoff = parse_time(candidate.get("commence_time"))
        if kickoff is not None and kickoff + GAME_WINDOW_S < stamp:
            continue
        if kickoff is not None and stored_kickoff is not None and \
                abs(kickoff - stored_kickoff) > tolerance_s:
            continue
        plausible.append(dict(candidate, _kickoff=kickoff or 0.0))
    if not plausible:
        return dict(state=IDENTITY_UNAVAILABLE, provider_id=None, candidate=None,
                    alternatives=[], reasons=["no provider event matches this matchup and kickoff"])
    if len(plausible) > 1:
        plausible.sort(key=lambda c: abs(c["_kickoff"] - (stored_kickoff or c["_kickoff"])))
        return dict(state=IDENTITY_AMBIGUOUS, provider_id=None, candidate=None,
                    alternatives=[c.get("id") for c in plausible],
                    reasons=[f"{len(plausible)} provider events match this matchup within the "
                             "kickoff tolerance"])
    chosen = plausible[0]
    return dict(state=IDENTITY_OK, provider_id=chosen.get("id"), candidate=chosen,
                alternatives=[], reasons=[])


# ---------------------------------------------------------------- player identity
def _team_unit(event: Dict, name: str) -> bool:
    return bool(TEAM_UNIT_RE.search(name or ""))


def classify_player(name: str, candidates: Iterable[Dict], event: Dict) -> Dict:
    """Decide whether a prop-row player can be shown to be in this event.

    Candidates are usage rows (or any record with `name` and `team`). Identity
    rests on event-date team evidence, never on last name alone or on a
    remembered team.
    """
    label = (name or "").strip()
    if not label:
        return dict(player=label, state=UNRESOLVED, team=None, kind="player",
                    reason="empty player name", alternatives=0)
    if _team_unit(event, label):
        return dict(player=label, state=PARTICIPANT, team=None, kind="team_unit",
                    reason="team unit market belongs to this event", alternatives=0)
    rows = [r for r in (candidates or [])]
    if not rows:
        return dict(player=label, state=UNRESOLVED, team=None, kind="player",
                    reason="no usage or roster record to confirm this player", alternatives=0)
    # One person can appear in many rows (two seasons of usage, several games).
    # Ambiguity is about distinct *people*, so candidates are grouped by identity.
    def _identity(row: Dict) -> str:
        """Stable id when available, otherwise name+team (never name alone)."""
        stable = row.get("player_id") or row.get("key")
        if stable:
            return str(stable).lower()
        return f"{str(row.get('name') or '').lower()}|{str(row.get('team') or '').lower()}"

    by_identity: Dict[str, List[Dict]] = {}
    for row in rows:
        by_identity.setdefault(_identity(row), []).append(row)
    event_teams = (event.get("home_team"), event.get("away_team"))
    on_team_ids = sorted({i for i, group in by_identity.items()
                          if any(g.get("team") in event_teams for g in group)})
    elsewhere_ids = [i for i in by_identity if i not in on_team_ids]

    def _best_touches(identity: str) -> float:
        return max(float(g.get("touches") or 0) for g in by_identity[identity])

    if not on_team_ids:
        if len(by_identity) == 1:
            only_id = next(iter(by_identity))
            only = by_identity[only_id][0]
            return dict(player=label, state=MISMATCH, team=only.get("team"), kind="player",
                        reason=f"resolved to {only.get('team')}, which is not in this event",
                        alternatives=1)
        return dict(player=label, state=UNRESOLVED, team=None, kind="player",
                    reason="several players share this name and none is on either roster",
                    alternatives=len(by_identity))
    if len(on_team_ids) > 1:
        return dict(player=label, state=UNRESOLVED, team=None, kind="player",
                    reason="several distinct same-name players are on this event's rosters",
                    alternatives=len(on_team_ids))
    confirmed_id = on_team_ids[0]
    confirmed = max(by_identity[confirmed_id], key=lambda g: float(g.get("touches") or 0))
    if elsewhere_ids and max(_best_touches(i) for i in elsewhere_ids) > \
            float(confirmed.get("touches") or 0):
        return dict(player=label, state=UNRESOLVED, team=None, kind="player",
                    reason="a same-name player elsewhere has a much larger role",
                    alternatives=len(by_identity))
    return dict(player=label, state=PARTICIPANT, team=confirmed.get("team"), kind="player",
                reason="event-date team evidence matches this matchup",
                alternatives=len(by_identity))


def is_model_eligible(verdict: Dict) -> bool:
    """Only confirmed participants may be modelled."""
    return verdict.get("state") == PARTICIPANT


def filter_model_eligible(verdicts: Iterable[Dict]) -> Dict[str, List[Dict]]:
    """Split player verdicts into eligible and excluded, for reporting."""
    eligible, excluded = [], []
    for verdict in verdicts:
        (eligible if is_model_eligible(verdict) else excluded).append(verdict)
    return dict(eligible=eligible, excluded=excluded)