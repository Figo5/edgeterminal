"""Structured request context for one chat turn.

The chat workflow used to carry only a free-text note ("the user is viewing X,
resolve ambiguous references to this event"). Nothing interpreted *when* a user
was asking about, nothing validated the event id the model chose, and there was
no schedule tool, so "the Thursday night game" was answered with whichever event
the UI happened to have selected.

This module builds one compact, validated context per turn:

    request understanding  ->  deterministic event resolution  ->  tool boundary

Language understanding stays flexible (the model may still call tools and phrase
things freely), but every date, id, price and constraint is validated here, in
Python, against the stored schedule. Kickoffs are stored in UTC; NFL scheduling
language is Eastern, and a Thursday-night kickoff is already the next UTC date,
so every comparison happens in `settings.timezone`.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .. import db
from ..config import settings
from ..integrity import parse_time

log = logging.getLogger("edgeterminal.context")

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}
# "TNF"/"SNF"/"MNF" are ordinary NFL shorthand, not special-case phrase patches:
# each maps onto the same weekday machinery below.
SHORTHAND = {"tnf": 3, "thursday night football": 3, "snf": 6, "sunday night football": 6,
             "mnf": 0, "monday night football": 0}
EVENING_HOUR = 17          # "night"/"evening" games kick off at or after this local hour

TASKS = (
    ("schedule", r"\b(when|what time|schedule|who plays|who's playing|whos playing|kick\s?off)\b"),
    ("likelihood", r"\b(likely|likelihood|chance|odds of|who might|who's going to|whos going to|"
                   r"who will|expect(?:ed)? to score|probability)\b"),
    ("best_bet", r"\b(best bet|official pick|strongest|top play|should i bet|what should i play)\b"),
    ("prices", r"\b(price|odds|line|quote|number)s?\b"),
    ("research", r"\b(injur|role|snap|usage|news|report|status|workload|depth chart)\w*\b"),
)
MARKETS = (
    ("player_anytime_td", r"\b(touchdown|td|anytime|score a td|scorer)s?\b"),
    ("player_reception_yards", r"\b(receiving|reception)\s*yards?\b"),
    ("player_receptions", r"\breceptions?\b"),
    ("player_rush_yards", r"\brush(?:ing)?\s*yards?\b"),
    ("player_pass_yards", r"\bpass(?:ing)?\s*yards?\b"),
)
# An explicit request overrides stale UI/history context; these say "a different game".
_EXPLICIT_DAY = re.compile(
    r"\b(tonight|today|tomorrow|this weekend|next week|"
    r"mon|tues|wednes|thurs|fri|satur|sun)\w*\b|\btnf\b|\bsnf\b|\bmnf\b", re.I)
_THIS_GAME = re.compile(r"\b(this|that|the current|the selected)\s+(game|matchup|one)\b", re.I)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 - a bad TIMEZONE must not break chat
        log.warning("unknown TIMEZONE %r; falling back to America/New_York", settings.timezone)
        return ZoneInfo("America/New_York")


def local_now(now: Optional[float] = None) -> dt.datetime:
    stamp = dt.datetime.now(dt.timezone.utc) if now is None else \
        dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return stamp.astimezone(_tz())


def local_kickoff(start_time) -> Optional[dt.datetime]:
    """Stored kickoff (UTC) as local time, or None if it cannot be parsed."""
    stamp = parse_time(start_time)
    if stamp is None:
        return None
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).astimezone(_tz())


# ------------------------------------------------------------------ understanding
def _teams_in(text: str, known: List[str]) -> List[str]:
    """Match stored team names on whole words, longest first (never a substring)."""
    found, low = [], text.lower()
    for name in sorted(known, key=len, reverse=True):
        nickname = name.split()[-1].lower()
        if re.search(rf"\b{re.escape(name.lower())}\b", low) or \
           re.search(rf"\b{re.escape(nickname)}\b", low):
            if name not in found:
                found.append(name)
    return found


def _day_constraint(text: str, now_local: dt.datetime) -> Optional[Dict]:
    """A requested calendar day, resolved in local time. None = no constraint."""
    low = text.lower()
    night = bool(re.search(r"\b(night|evening|primetime|prime time)\b", low))
    for token, weekday in SHORTHAND.items():
        if re.search(rf"\b{re.escape(token)}\b", low):
            return dict(kind="weekday", weekday=weekday, night=True, label=token.upper())
    if re.search(r"\btonight\b", low):
        return dict(kind="date", date=now_local.date(), night=True, label="tonight")
    if re.search(r"\btoday\b", low):
        return dict(kind="date", date=now_local.date(), night=night, label="today")
    if re.search(r"\btomorrow\b", low):
        return dict(kind="date", date=now_local.date() + dt.timedelta(days=1),
                    night=night, label="tomorrow")
    if re.search(r"\bthis weekend\b", low):
        return dict(kind="range", weekdays={5, 6}, night=night, label="this weekend")
    if re.search(r"\bnext week\b", low):
        return dict(kind="week_offset", offset=1, night=night, label="next week")
    for name, weekday in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            return dict(kind="weekday", weekday=weekday, night=night, label=name)
    return None


def _task(text: str) -> str:
    for name, pattern in TASKS:
        if re.search(pattern, text, re.I):
            return name
    return "research"


def _market(text: str) -> Optional[str]:
    for name, pattern in MARKETS:
        if re.search(pattern, text, re.I):
            return name
    return None


# ------------------------------------------------------------------ resolution
def _candidates(now_local: Optional[dt.datetime] = None) -> List[Dict]:
    """Schedulable NFL events: real, still upcoming, one row per matchup.

    A status of "scheduled" is not proof a game is upcoming -- stale rows keep it
    after kickoff -- so the kickoff time decides. The same fixture also arrives from
    two feeds ("X @ Y" and "X at Y"), which would otherwise be offered as two
    different choices.
    """
    now_local = now_local or local_now()
    rows = db.q("SELECT id, name, home_team, away_team, start_time, status "
                "FROM events WHERE sport='NFL' ORDER BY start_time ASC")
    out, seen = [], {}
    for row in rows:
        row = dict(row)
        if not settings.demo_mode and str(row["id"]).startswith("demo-"):
            continue
        if "FINAL" in str(row.get("status") or "").upper():
            continue
        kickoff = local_kickoff(row["start_time"])
        if kickoff is None or kickoff < now_local:
            continue
        row["kickoff_local"] = kickoff
        key = (row["home_team"], row["away_team"], kickoff.strftime("%Y-%m-%d %H:%M"))
        if key in seen:
            # Prefer the row that carries prices: the odds feed id, not the ESPN one.
            if str(row["id"]).startswith("espn-"):
                continue
            out[seen[key]] = row
            continue
        seen[key] = len(out)
        out.append(row)
    return out


def _matches_day(event: Dict, constraint: Dict, now_local: dt.datetime) -> bool:
    kickoff = event["kickoff_local"]
    if constraint["kind"] == "date":
        if kickoff.date() != constraint["date"]:
            return False
    elif constraint["kind"] == "weekday":
        if kickoff.weekday() != constraint["weekday"]:
            return False
        if (kickoff - now_local).days > 7:      # the *next* such weekday, not any
            return False
    elif constraint["kind"] == "range":
        if kickoff.weekday() not in constraint["weekdays"]:
            return False
        if (kickoff - now_local).days > 7:
            return False
    elif constraint["kind"] == "week_offset":
        start = now_local + dt.timedelta(days=7 * constraint["offset"] - now_local.weekday())
        if not (start.date() <= kickoff.date() <= start.date() + dt.timedelta(days=6)):
            return False
    if constraint.get("night") and kickoff.hour < EVENING_HOUR:
        return False
    return True


def resolve_event(ctx: Dict) -> Dict:
    """Pick the event the user means, or say why it cannot be picked.

    Order of authority: an explicit day/team request, then the selected UI event,
    then the event carried over from the conversation. An event is never chosen for
    being first in the database, first with props, previously selected against an
    explicit request, already played, or a demo fixture.
    """
    now_local = ctx["now_local"]
    events = _candidates(now_local)
    constraint, teams = ctx.get("day_constraint"), ctx.get("teams") or []
    reasons: List[str] = []

    pool = events
    if teams:
        named = [e for e in pool if any(t in (e["home_team"], e["away_team"]) for t in teams)]
        if named:
            pool, _ = named, reasons.append(f"filtered to team(s): {', '.join(teams)}")
        else:
            return dict(state="UNAVAILABLE", event_id=None,
                        reason=f"no upcoming NFL game found for {', '.join(teams)}",
                        reasons=reasons)
    if constraint:
        day_pool = [e for e in pool if _matches_day(e, constraint, now_local)]
        reasons.append(f"{constraint['label']} -> {len(day_pool)} game(s) in "
                       f"{settings.timezone}")
        if not day_pool:
            return dict(state="UNAVAILABLE", event_id=None,
                        reason=f"no NFL game is scheduled {constraint['label']}",
                        reasons=reasons, alternatives=[
                            dict(id=e["id"], name=e["name"],
                                 kickoff_local=e["kickoff_local"].isoformat()) for e in pool[:3]])
        if len(day_pool) > 1 and not teams:
            # Several games share the slot and nothing else narrows it: ask rather
            # than silently taking one.
            return dict(state="AMBIGUOUS", event_id=None,
                        reason=f"{len(day_pool)} NFL games are scheduled {constraint['label']}",
                        reasons=reasons, alternatives=[
                            dict(id=e["id"], name=e["name"],
                                 kickoff_local=e["kickoff_local"].isoformat()) for e in day_pool])
        chosen = day_pool[0]
        return dict(state="OK", event_id=chosen["id"], event=chosen,
                    source="explicit-request", reasons=reasons)

    if teams:
        chosen = pool[0]
        return dict(state="OK", event_id=chosen["id"], event=chosen,
                    source="explicit-team", reasons=reasons)

    for key, source in (("selected_event_id", "selected-ui-event"),
                        ("history_event_id", "conversation-context")):
        candidate = ctx.get(key)
        if candidate:
            hit = next((e for e in events if e["id"] == candidate), None)
            if hit:
                reasons.append(f"no explicit day/team; using {source}")
                return dict(state="OK", event_id=hit["id"], event=hit,
                            source=source, reasons=reasons)
    if events:
        chosen = events[0]
        reasons.append("no explicit request and no selected game; using the next kickoff")
        return dict(state="OK", event_id=chosen["id"], event=chosen,
                    source="next-scheduled", reasons=reasons)
    return dict(state="UNAVAILABLE", event_id=None,
                reason="no upcoming NFL games are stored; refresh the schedule",
                reasons=reasons)


def build(user_message: str, history: Optional[List[Dict]] = None,
          selected_event_id: Optional[str] = None,
          now: Optional[float] = None) -> Dict:
    """The compact structured context for one turn."""
    now_local = local_now(now)
    teams_known = [r["home_team"] for r in db.q(
        "SELECT DISTINCT home_team FROM events WHERE sport='NFL'")] + \
        [r["away_team"] for r in db.q("SELECT DISTINCT away_team FROM events WHERE sport='NFL'")]
    teams_known = [t for t in dict.fromkeys(teams_known) if t]

    explicit = bool(_EXPLICIT_DAY.search(user_message))
    this_game = bool(_THIS_GAME.search(user_message))
    teams = _teams_in(user_message, teams_known)
    ctx = dict(
        message=user_message,
        task=_task(user_message),
        market=_market(user_message),
        teams=teams,
        day_constraint=_day_constraint(user_message, now_local),
        now_utc=(now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()),
        now_local=now_local,
        timezone=settings.timezone,
        selected_event_id=selected_event_id,
        history_event_id=_history_event(history),
        explicit_day=explicit, refers_to_selected=this_game,
    )
    # "This game" is the one case where the selected event outranks a stray weekday
    # word; an explicit day otherwise outranks stale UI and history context.
    if this_game and not explicit and selected_event_id:
        ctx["day_constraint"], ctx["teams"] = None, []
    ctx["resolution"] = resolve_event(ctx)
    log.info("chat context task=%s market=%s day=%s teams=%s -> event=%s (%s)",
             ctx["task"], ctx["market"],
             (ctx["day_constraint"] or {}).get("label"), teams or "-",
             (ctx["resolution"].get("event_id") or "-")[:8], ctx["resolution"]["state"])
    return ctx


def _history_event(history: Optional[List[Dict]]) -> Optional[str]:
    """The most recent event id the conversation actually settled on."""
    for turn in reversed(history or []):
        found = re.findall(r"\b([0-9a-f]{32}|espn-\d+|demo-[\w-]+)\b", str(turn.get("content") or ""))
        if found:
            return found[-1]
    return None


def summary_note(ctx: Dict) -> str:
    """The system note the model sees. Facts and precedence, not prose."""
    res = ctx["resolution"]
    lines = [f"Current time: {ctx['now_local'].strftime('%A %d %b %Y %H:%M')} "
             f"({ctx['timezone']}). Interpret all day references in this timezone.",
             f"Interpreted request: task={ctx['task']}"
             + (f", market={ctx['market']}" if ctx.get("market") else "")
             + (f", day={(ctx['day_constraint'] or {}).get('label')}" if ctx.get("day_constraint") else "")
             + (f", teams={', '.join(ctx['teams'])}" if ctx.get("teams") else "")]
    if res["state"] == "OK":
        ev = res["event"]
        lines.append(f"RESOLVED EVENT (validated, use this id and no other): {res['event_id']} "
                     f"- {ev['name']}, {ev['kickoff_local'].strftime('%a %d %b %H:%M %Z')} "
                     f"[resolved by {res['source']}]")
        lines.append("Event-scoped tools are locked to this id; do not pass a different one.")
    elif res["state"] == "AMBIGUOUS":
        opts = "; ".join(f"{a['name']}" for a in res.get("alternatives", [])[:4])
        lines.append(f"EVENT AMBIGUOUS: {res['reason']}. Ask one short question naming these "
                     f"options, and call no event-scoped tool: {opts}")
    else:
        lines.append(f"EVENT UNAVAILABLE: {res['reason']}. Say so plainly and offer the next "
                     f"scheduled games. Do not analyse a different game as if it were the one asked about.")
    return "\n".join(lines)
