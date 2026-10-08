"""ESPN-only near-kickoff quote capture for prospective market comparisons."""
from __future__ import annotations

import json
import time

from . import db, nfl_identity, nfl_market
from .integrity import parse_time
from .providers.espn import EspnOdds

WINDOW_S = 15 * 60
INACTIVE = {"STATUS_POSTPONED","STATUS_CANCELED","STATUS_CANCELLED","STATUS_FINAL"}


def _mapped_live_game(event):
    if nfl_identity.canonical_id(event) != event["id"]:
        return False
    try:
        meta=json.loads(event.get("meta") or "{}")
    except (TypeError,ValueError):
        return False
    if meta.get("is_demo"):
        return False
    return db.q1("SELECT 1 FROM nfl_provider_games WHERE provider='espn' AND game_id=? LIMIT 1",
                 (event["id"],)) is not None


def capture_if_due(now=None, provider=None):
    stamp=time.time() if now is None else now
    targets={}
    for event in db.q("SELECT * FROM events WHERE sport='NFL'"):
        kickoff=parse_time(event.get("start_time"))
        if (_mapped_live_game(event) and kickoff is not None and
                0 < kickoff-stamp <= WINDOW_S and
                str(event.get("status") or "").upper() not in INACTIVE):
            targets[event["id"]]=event
    return _capture(targets,provider,stamp)


def capture_week(season,week,provider=None,now=None):
    """Freeze-time ESPN observation for every unstarted game in a week."""
    stamp=time.time() if now is None else now
    prefix=f"nfl-{int(season)}-reg-{int(week):02d}-"
    targets={}
    for event in db.q("SELECT * FROM events WHERE sport='NFL' AND id LIKE ?",(prefix+"%",)):
        kickoff=parse_time(event.get("start_time"))
        if (_mapped_live_game(event) and kickoff is not None and stamp<kickoff and
                str(event.get("status") or "").upper() not in INACTIVE):
            targets[event["id"]]=event
    return _capture(targets,provider,stamp)


def capture_current(provider=None, now=None):
    """Observe ESPN prices for every unstarted game in the stored current week."""
    stamp = time.time() if now is None else now
    current_ids = (db.kv_get("nfl_current_schedule") or {}).get("game_ids") or []
    targets = {}
    for game_id in current_ids:
        event = db.q1("SELECT * FROM events WHERE id=? AND sport='NFL'", (game_id,))
        kickoff = parse_time(event.get("start_time")) if event else None
        if (event and _mapped_live_game(event) and kickoff is not None and
                stamp < kickoff and str(event.get("status") or "").upper() not in INACTIVE):
            targets[game_id] = event
    return _capture(targets, provider, stamp)


def _record_attempts(targets, stamp, outcomes, error_type=None):
    with db.tx() as c:
        c.executemany(
            "INSERT INTO nfl_capture_attempts(game_id,kickoff_utc,attempted_at,provider,"
            "outcome,rows_stored,error_type) VALUES(?,?,?,?,?,?,?)",
            [(game_id,parse_time(event["start_time"]),stamp,"espn-odds",
              outcomes.get(game_id,0) and "success" or ("error" if error_type else "no_quote"),
              outcomes.get(game_id,0),error_type) for game_id,event in targets.items()])


def _capture(targets,provider,stamp):
    result={"provider":"espn-odds","target_games":len(targets),"rows_stored":0}
    if not targets:
        return result
    # Explicit ESPN adapter: this job cannot consume an Odds API request.
    try:
        if provider is None:
            parts = next(iter(targets)).split("-")
            provider = EspnOdds(season=int(parts[1]), week=int(parts[3]))
        raw=provider.fetch_odds("NFL")
        if not isinstance(raw,list) or any(not isinstance(row,dict) for row in raw):
            raise ValueError("malformed ESPN odds response")
        by_game={game_id:[] for game_id in targets}
        for row in raw:
            mapped=db.mapped_nfl_game("espn",str(row.get("event_id") or ""))
            if mapped in targets:
                by_game[mapped].append(dict(row,event_id=mapped))
        accepted=[]
        for game_id,rows in by_game.items():
            accepted.extend(nfl_market.valid_game_quotes(rows,targets[game_id]))
        if accepted:
            result["rows_stored"]=db.store_odds(accepted,source="espn-odds",is_demo=False)
    except Exception as exc:
        _record_attempts(targets,stamp,{},type(exc).__name__)
        raise
    outcomes={game_id:sum(row["event_id"]==game_id for row in accepted) for game_id in targets}
    _record_attempts(targets,stamp,outcomes)
    result["games_with_quotes"]=len({r["event_id"] for r in accepted})
    result["games_without_quotes"]=sorted(set(targets)-{r["event_id"] for r in accepted})
    return result


def main():
    result=capture_if_due()
    print(json.dumps(result))
    if result["target_games"] and result["games_without_quotes"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
