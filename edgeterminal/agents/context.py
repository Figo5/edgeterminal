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
import uuid
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
    # Longest/most specific first: "receiving yards" must not be read as "receptions".
    ("player_reception_yards", r"\b(receiving|reception)\s*yards?\b|\brec\s*yds?\b"),
    ("player_rush_yards", r"\brush(?:ing)?\s*yards?\b"),
    ("player_pass_yards", r"\bpass(?:ing)?\s*yards?\b"),
    ("player_receptions", r"\breceptions?\b|\bcatches\b"),
    ("player_anytime_td", r"\b(touchdown|td|anytime|score a td|scorer)s?\b"),
    ("moneyline", r"\bmoney\s?lines?\b|\bml\b|\bwho wins\b|\bto win\b"),
    ("spread", r"\bspreads?\b|\bagainst the spread\b|\bats\b|\bhandicap\b"),
    ("total", r"\btotals?\b|\bover\/under\b|\bo\/u\b"),
)
# Prop markets, and the provider category each belongs to. A request for
# "receiving yards" is a request for the receiving category -- never the whole
# board, which is how a market switch turned into an unrelated moneyline list.
PROP_MARKETS = {
    "player_anytime_td": "td",
    "player_reception_yards": "receiving",
    "player_receptions": "receiving",
    "player_rush_yards": "rushing",
    "player_pass_yards": "passing",
}
GAME_MARKETS = {"moneyline", "spread", "total"}


def market_category(market: Optional[str]) -> Optional[str]:
    """Provider prop category for a market, or None for game markets."""
    return PROP_MARKETS.get(market or "")
# An explicit request overrides stale UI/history context; these say "a different game".
_EXPLICIT_DAY = re.compile(
    r"\b(tonight|today|tomorrow|this weekend|next week|"
    r"mon|tues|wednes|thurs|fri|satur|sun)\w*\b|\btnf\b|\bsnf\b|\bmnf\b", re.I)
_THIS_GAME = re.compile(r"\b(this|that|the current|the selected|the same)\s+"
                        r"(game|matchup|one|fixture)\b", re.I)
# "that game"/"the same game" points backwards at the conversation. "this game"
# is the one genuinely ambiguous form: it can mean the game on screen.
_THAT_GAME = re.compile(r"\b(that|the same)\s+(game|matchup|one|fixture)\b", re.I)


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


def _stale_metadata(events: List[Dict], now_local: dt.datetime) -> Optional[str]:
    """Stored metadata that contradicts itself, regardless of the clock.

    A row whose kickoff has passed while its status still reads "scheduled" says
    the schedule has not been updated through that game, so the retrieval
    timestamp alone is not enough to call it current.
    """
    for ev in events:
        kickoff = ev.get("kickoff_local")
        status = str(ev.get("status") or "").upper()
        if kickoff is not None and kickoff < now_local and "FINAL" not in status:
            return (f"{ev['name']} kicked off at "
                    f"{kickoff.strftime('%a %d %b %H:%M %Z')} but is still stored as "
                    f"'{ev.get('status')}'")
    return None


def _all_stored(now_local: dt.datetime) -> List[Dict]:
    """Every stored NFL row with a parsed local kickoff, played or not."""
    out = []
    for row in db.q("SELECT id, name, home_team, away_team, start_time, status "
                    "FROM events WHERE sport='NFL' ORDER BY start_time ASC"):
        row = dict(row)
        if not settings.demo_mode and str(row["id"]).startswith("demo-"):
            continue
        kickoff = local_kickoff(row["start_time"])
        if kickoff is None:
            continue
        row["kickoff_local"] = kickoff
        out.append(row)
    return out


def _needs_refresh(ctx: Dict, res: Dict, meta: Dict, now_local: dt.datetime) -> Optional[str]:
    """Why the schedule should be re-fetched once before answering, or None.

    Three triggers, all cheap to evaluate and all about the schedule being
    untrustworthy rather than about wanting more data: it is older than the
    configured limit; the requested day or team is not in it; or a stored row
    contradicts its own metadata.
    """
    if meta["stale"]:
        age = meta["age_s"]
        return ("schedule has never been retrieved" if age is None
                else f"schedule is {int(age)}s old (limit {int(meta['limit_s'])}s)")
    if res["state"] == "UNAVAILABLE" and (ctx.get("day_constraint") or ctx.get("teams")):
        return "the requested game is not in the stored schedule"
    conflict = _stale_metadata(_all_stored(now_local), now_local)
    if conflict:
        return f"stored schedule contradicts itself: {conflict}"
    return None


def _validate_mapping(res: Dict, now_local: dt.datetime) -> List[str]:
    """After a refresh, confirm the resolved event still says what it said.

    A refresh can move a kickoff or rename a side. Re-reading the row is how a
    resolution that is now wrong gets caught instead of being carried forward.
    """
    problems: List[str] = []
    if res.get("state") != "OK" or not res.get("event_id"):
        return problems
    row = db.q1("SELECT id, name, home_team, away_team, start_time, status "
                "FROM events WHERE id=?", (res["event_id"],))
    if row is None:
        problems.append("the resolved event is no longer in the schedule")
        return problems
    row = dict(row)
    ev = res.get("event") or {}
    for field in ("home_team", "away_team"):
        if str(row.get(field) or "") != str(ev.get(field) or ""):
            problems.append(f"{field} changed after refresh: "
                            f"{ev.get(field)!r} -> {row.get(field)!r}")
    kickoff = local_kickoff(row["start_time"])
    if kickoff is None:
        problems.append("the refreshed event has no parsable kickoff")
    elif ev.get("kickoff_local") is not None and kickoff != ev["kickoff_local"]:
        problems.append(f"kickoff moved after refresh: "
                        f"{ev['kickoff_local'].strftime('%a %d %b %H:%M')} -> "
                        f"{kickoff.strftime('%a %d %b %H:%M')}")
    return problems


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

    # Order of authority below an explicit request:
    #   1. a selection the user just changed (a real instruction)
    #   2. the event this conversation already settled on
    #   3. a selection that has merely been re-attached to every payload
    # "that game" points backwards and never outranks the conversation.
    order: List[tuple] = []
    if ctx.get("selection_changed") and not ctx.get("refers_to_conversation"):
        order.append(("selected_event_id", "changed-ui-selection"))
    order.append(("history_event_id", "conversation-context"))
    order.append(("selected_event_id", "selected-ui-event"))
    for key, source in order:
        candidate = ctx.get(key)
        if candidate:
            hit = next((e for e in events if e["id"] == candidate), None)
            if hit:
                reasons.append(f"no explicit day/team; using {source}")
                return dict(state="OK", event_id=hit["id"], event=hit,
                            source=source, reasons=reasons)
            if key == "history_event_id":
                # The conversation's game is gone from the schedule. Say so rather
                # than quietly sliding to whatever is selected.
                reasons.append("the game this conversation was about is no longer "
                               "in the schedule")
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
          now: Optional[float] = None, allow_refresh: bool = True,
          prior: Optional[Dict] = None) -> Dict:
    """The compact structured context for one turn.

    `prior` is the conversation state this same conversation was given at the end
    of its last turn. It is the only carrier of "what we were talking about":
    scraping ids out of answer text never worked, because an answer names the
    matchup, not its id.
    """
    now_local = local_now(now)
    teams_known = [r["home_team"] for r in db.q(
        "SELECT DISTINCT home_team FROM events WHERE sport='NFL'")] + \
        [r["away_team"] for r in db.q("SELECT DISTINCT away_team FROM events WHERE sport='NFL'")]
    teams_known = [t for t in dict.fromkeys(teams_known) if t]

    explicit = bool(_EXPLICIT_DAY.search(user_message))
    this_game = bool(_THIS_GAME.search(user_message))
    that_game = bool(_THAT_GAME.search(user_message))
    teams = _teams_in(user_message, teams_known)
    prior = _clean_prior(prior)

    # A selection the user actually changed is an instruction. The same value
    # re-attached to every payload by the frontend is not: the browser sends
    # `event_id` on every turn whether or not anyone touched it, and treating
    # that as intent is what made "that game" jump to the dashboard's game.
    selection_changed = bool(
        selected_event_id
        and prior is not None
        and prior.get("selected_event_id") != selected_event_id)
    # With no prior turn there is nothing to be sticky *about*.
    if prior is None and selected_event_id:
        selection_changed = True
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
        history_event_id=(prior or {}).get("event_id") or _history_event(history),
        explicit_day=explicit, refers_to_selected=this_game,
        refers_to_conversation=that_game,
        selection_changed=selection_changed,
        prior=prior,
        conversation_id=(prior or {}).get("conversation_id") or uuid.uuid4().hex[:16],
        turn=int((prior or {}).get("turn") or 0) + 1,
    )
    # A market named in this message wins; otherwise the conversation keeps the
    # one it was already on. Asking to *see prices* changes the action, not the
    # market -- that reset is what produced a moneyline board mid-conversation.
    if ctx["market"] is None and prior and prior.get("market"):
        ctx["market"] = prior["market"]
        ctx["market_source"] = "conversation"
    else:
        ctx["market_source"] = "message" if ctx["market"] else None
    ctx["category"] = market_category(ctx["market"])
    if ctx.get("player") is None and prior and prior.get("player") and not teams:
        ctx["player"] = prior.get("player")
    # A bare conversational reference carries no day or team of its own, so any
    # stray weekday word in it must not be read as a new constraint.
    if (this_game or that_game) and not explicit and (selected_event_id or ctx["history_event_id"]):
        ctx["day_constraint"], ctx["teams"] = None, []
    ctx["resolution"] = resolve_event(ctx)
    ctx["schedule"] = _ensure_fresh_schedule(ctx, allow_refresh=allow_refresh)
    log.info("chat context task=%s market=%s day=%s teams=%s -> event=%s (%s) schedule=%s",
             ctx["task"], ctx["market"],
             (ctx["day_constraint"] or {}).get("label"), teams or "-",
             (ctx["resolution"].get("event_id") or "-")[:8], ctx["resolution"]["state"],
             "uncertain" if ctx["schedule"].get("uncertain") else "ok")
    return ctx


def _ensure_fresh_schedule(ctx: Dict, allow_refresh: bool = True) -> Dict:
    """Refresh the schedule at most once, then re-resolve against it.

    Relative dates are only as good as the schedule they are resolved against,
    so a stale, incomplete or self-contradictory schedule is refreshed before
    the answer is built rather than after. Exactly one attempt: a provider that
    is down must not turn one question into a retry loop, and nothing here polls
    in the background.

    On failure the last-known retrieval time and the uncertainty are carried
    forward. A stale schedule is never relabelled as newly verified, and no
    other game is substituted for the one that could not be confirmed.
    """
    from .. import engine

    now_local = ctx["now_local"]
    meta = engine.schedule_meta("NFL", now=ctx.get("now_utc"))
    reason = _needs_refresh(ctx, ctx["resolution"], meta, now_local)
    out = dict(meta, refresh_attempted=False, refresh_ok=None, refresh_reason=reason,
               changed=[], mapping_problems=[], uncertain=False)
    if not reason:
        return out
    if not allow_refresh:
        out["uncertain"] = bool(meta["stale"])
        out["note"] = f"schedule not refreshed ({reason}); refresh disabled for this call"
        return out

    out["refresh_attempted"] = True
    try:
        result = engine.refresh_schedule("NFL")
    except Exception as e:  # noqa: BLE001 - a refresh failure must not break a turn
        log.error("schedule refresh raised: %s", type(e).__name__)
        result = dict(ok=False, error=f"{type(e).__name__}", changed=[])

    out["refresh_ok"] = bool(result.get("ok"))
    out["changed"] = result.get("changed") or []
    if not result.get("ok"):
        # Keep the last-known timestamp and say plainly that it is unverified.
        out["error"] = result.get("error")
        out["uncertain"] = True
        out["note"] = (f"schedule refresh failed ({result.get('error')}); answering from the "
                       f"schedule last retrieved "
                       + (f"{int(meta['age_s'])}s ago" if meta["age_s"] is not None
                          else "at an unknown time")
                       + ". Kickoffs and statuses are not verified current.")
        return out

    # Re-resolve against the refreshed schedule, then confirm the resolved row
    # still maps to what it did before the refresh.
    ctx["resolution"] = resolve_event(ctx)
    out.update({k: v for k, v in
                engine.schedule_meta("NFL", now=ctx.get("now_utc")).items()})
    problems = _validate_mapping(ctx["resolution"], now_local)
    out["mapping_problems"] = problems
    out["uncertain"] = bool(problems)
    if problems:
        out["note"] = ("the resolved game changed during refresh: " + "; ".join(problems))
    return out


def _clean_prior(prior: Optional[Dict]) -> Optional[Dict]:
    """Validate the conversation state the client echoed back.

    It arrives over the wire like `history` and `event_id`, so it is checked, not
    trusted: the event must still exist and the market must be one we know.
    """
    if not isinstance(prior, dict):
        return None
    out: Dict = {}
    eid = prior.get("event_id")
    if isinstance(eid, str) and eid:
        row = db.q1("SELECT id, name FROM events WHERE id=?", (eid,))
        if row:
            out["event_id"] = row["id"]
            out["event_name"] = row["name"]
    market = prior.get("market")
    if isinstance(market, str) and (market in PROP_MARKETS or market in GAME_MARKETS):
        out["market"] = market
    for key in ("conversation_id", "player", "task", "selected_event_id"):
        value = prior.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:80]
    try:
        out["turn"] = max(0, int(prior.get("turn") or 0))
    except (TypeError, ValueError):
        out["turn"] = 0
    return out or None


def conversation_state(ctx: Dict, validated: bool = True) -> Dict:
    """The compact state this conversation carries into its next turn.

    Only written when the turn actually validated. An answer that was rejected
    for naming the wrong game must not leave that game behind as the
    conversation's authoritative event.
    """
    res = ctx.get("resolution") or {}
    prior = ctx.get("prior") or {}
    keep_event = res.get("event_id") if (validated and res.get("state") == "OK") \
        else prior.get("event_id")
    keep_name = (res.get("event") or {}).get("name") if (validated and res.get("state") == "OK") \
        else prior.get("event_name")
    return dict(
        conversation_id=ctx.get("conversation_id"),
        turn=ctx.get("turn"),
        event_id=keep_event,
        event_name=keep_name,
        market=ctx.get("market") if validated else prior.get("market"),
        category=market_category(ctx.get("market") if validated else prior.get("market")),
        player=ctx.get("player") if validated else prior.get("player"),
        task=ctx.get("task"),
        # What the dashboard had selected on THIS turn, so the next turn can tell
        # a changed selection from the same value re-sent.
        selected_event_id=ctx.get("selected_event_id"),
        source=res.get("source") if validated else prior.get("source"),
        validated=bool(validated),
    )


def _history_event(history: Optional[List[Dict]]) -> Optional[str]:
    """The most recent event id the conversation actually settled on."""
    for turn in reversed(history or []):
        found = re.findall(r"\b([0-9a-f]{32}|espn-\d+|demo-[\w-]+)\b", str(turn.get("content") or ""))
        if found:
            return found[-1]
    return None


def schedule_note(ctx: Dict) -> str:
    """One line about how current the schedule behind this answer is.

    Rendered from the stored retrieval time, never from the fact that a refresh
    was attempted: an attempt that failed leaves the schedule exactly as stale
    as it was.
    """
    sched = ctx.get("schedule") or {}
    age = sched.get("age_s")
    when = ("never retrieved" if age is None
            else f"retrieved {int(age)}s ago")
    if sched.get("uncertain"):
        return (f"SCHEDULE UNVERIFIED ({when}): {sched.get('note') or 'freshness unconfirmed'}. "
                "State this uncertainty in the answer, do not present the schedule as "
                "confirmed current, do not substitute another game, and issue NO Official "
                "Pick or staked recommendation while the event cannot be verified.")
    if sched.get("refresh_attempted") and sched.get("refresh_ok"):
        return f"Schedule refreshed for this question ({when})."
    return f"Schedule {when}; within the {int(sched.get('limit_s') or 0)}s freshness limit."


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
    lines.append(schedule_note(ctx))
    return "\n".join(lines)
