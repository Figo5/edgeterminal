"""ESPN-only near-kickoff quote capture for prospective market comparisons."""
from __future__ import annotations

import json
import time

from . import db, nfl_market
from .integrity import parse_time
from .providers.espn import EspnOdds

WINDOW_S = 15 * 60


def capture_if_due(now=None, provider=None):
    stamp=time.time() if now is None else now
    targets={}
    for event in db.q("SELECT * FROM events WHERE sport='NFL'"):
        kickoff=parse_time(event.get("start_time"))
        if (kickoff is not None and 0 < kickoff-stamp <= WINDOW_S and
                str(event.get("status") or "").upper() not in
                {"STATUS_POSTPONED","STATUS_CANCELED","STATUS_CANCELLED","STATUS_FINAL"}):
            targets[event["id"]]=event
    result={"provider":"espn-odds","target_games":len(targets),"rows_stored":0}
    if not targets:
        return result
    # Explicit ESPN adapter: this job cannot consume an Odds API request.
    raw=(provider or EspnOdds()).fetch_odds("NFL")
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
