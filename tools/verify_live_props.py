"""Live prop verification with an integrity trace.

    python tools/verify_live_props.py                 # one upcoming event, one market
    python tools/verify_live_props.py --market player_anytime_td
    python tools/verify_live_props.py --credits       # show Odds API quota only

Deliberately narrow: one event, one market, sanitized output. It never prints a
key, and it reports quota before and after so the cost is visible.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edgeterminal import db, integrity as I, props          # noqa: E402
from edgeterminal.analytics import usage as U               # noqa: E402
from edgeterminal.config import settings                    # noqa: E402
from edgeterminal.providers import registry as R            # noqa: E402


def quota() -> dict:
    return R.prop_quota()


def upcoming_event() -> dict:
    """A genuinely upcoming NFL event from the provider feed (not the local cache)."""
    from edgeterminal.providers.theoddsapi import TheOddsApi
    api = TheOddsApi()
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    events = [e for e in api.list_events("NFL")
              if (I.parse_time(e.get("start_time")) or 0) > now + 3600]
    events.sort(key=lambda e: I.parse_time(e["start_time"]) or 0)
    return events[0] if events else {}


def trace(event: dict, market: str) -> dict:
    began = quota()
    board = props.analyze_props(event["id"], category=None, markets=[market])
    ended = quota()
    identity = board.get("identity") or {}
    players = board.get("player_identity") or {}
    return dict(
        state=board.get("state"),
        requested=dict(matchup=f"{event.get('away_team')} @ {event.get('home_team')}",
                       kickoff=event.get("start_time"), local_event_id=event.get("id")),
        resolved=dict(identity_state=identity.get("state"),
                      provider_event_id=(identity.get("provider_id") or "")[:12],
                      market=market),
        rows_returned=len(board.get("recommendations") or []) +
                      len(board.get("skipped") or []),
        identity_counts=dict(participants=len(players.get("participants") or []),
                             mismatches=len(players.get("mismatches") or []),
                             unresolved=len(players.get("unresolved") or [])),
        excluded_sample=[dict(player=v["player"], state=v["state"], reason=v.get("reason"))
                         for v in (players.get("mismatches") or [])[:3] +
                                  (players.get("unresolved") or [])[:3]],
        recommendations=[dict(player=r["player"], market=r["market"], state=r["state"],
                              price=r["price"], book=r["book"], final_prob=r["final_prob"],
                              usage_basis=r.get("usage_basis"))
                         for r in (board.get("recommendations") or [])[:5]],
        warnings=(board.get("warnings") or [])[:3],
        quota=dict(before=began, after=ended),
    )


def expired_demo() -> dict:
    """Show the refusal path for a finished event that is still in the local DB."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    rows = [dict(r) for r in db.q("SELECT * FROM events WHERE sport='NFL'")]
    finished = [r for r in rows if I.event_is_expired(r, now)
                and not str(r["id"]).startswith("demo-")]
    if not finished:
        return dict(skipped="no finished live event in the local database")
    event = finished[0]
    board = props.analyze_props(event["id"], category="td")
    return dict(matchup=f"{event['away_team']} @ {event['home_team']}",
                kickoff=event.get("start_time"), state=board.get("state"),
                identity=(board.get("identity") or {}).get("state"),
                recommendations=len(board.get("recommendations") or []),
                note=board.get("note"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="player_anytime_td")
    parser.add_argument("--credits", action="store_true")
    args = parser.parse_args()
    if args.credits:
        print(json.dumps(quota(), indent=2))
        return 0
    if not settings.odds_api_key:
        print(json.dumps(dict(error="ODDS_API_KEY not configured — live verification is "
                                    "unavailable rather than demo-substituted")))
        return 1
    event = upcoming_event()
    if not event:
        print(json.dumps(dict(error="no upcoming NFL event in the provider feed")))
        return 1
    stored = db.q1("SELECT * FROM events WHERE id=?", (event["id"],))
    if stored:
        print(json.dumps(trace(dict(stored), args.market), indent=2, default=str))
    else:
        print(json.dumps(dict(note="upcoming provider event is not in the local database; "
                                   "run a refresh first",
                              provider_event_id=event["id"][:12],
                              matchup=f"{event['away_team']} @ {event['home_team']}",
                              kickoff=event.get("start_time")), indent=2))
    print(json.dumps(dict(expired_event_refusal=expired_demo()), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())