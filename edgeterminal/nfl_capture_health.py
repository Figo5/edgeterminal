"""Research-only accounting for near-kickoff ESPN capture opportunities."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, nfl_identity
from .integrity import parse_time
from .market_provenance import CLOSE_WINDOW_S


def _iso(stamp):
    return datetime.fromtimestamp(stamp,timezone.utc).isoformat() if stamp is not None else None


def report(freeze_path, now=None):
    stamp=time.time() if now is None else float(now)
    freeze=json.loads(Path(freeze_path).read_text())
    games=[]
    for game in freeze["games"]:
        canonical=(f"nfl-{freeze['season']}-reg-{freeze['week']:02d}-"
                   f"{nfl_identity.team_code(game['away'])}-{nfl_identity.team_code(game['home'])}")
        frozen_kickoff=parse_time(game["kickoff_et"])
        event=db.q1("SELECT start_time,status FROM events WHERE id=?",(canonical,))
        current_kickoff=parse_time(event["start_time"]) if event else None
        kickoff=current_kickoff or frozen_kickoff
        attempts=db.q("SELECT * FROM nfl_capture_attempts WHERE game_id=? AND "
                      "attempted_at>=? AND attempted_at<? ORDER BY attempted_at,id",
                      (canonical,kickoff-CLOSE_WINDOW_S,kickoff))
        observations=db.q("SELECT * FROM odds_snapshots WHERE event_id=? AND is_demo=0 "
                          "AND fetched_at<=? ORDER BY fetched_at DESC,id DESC",
                          (canonical,stamp))
        eligible=[r for r in observations if
                  r["quote_phase"]!="unclassified" and
                  kickoff-CLOSE_WINDOW_S <= r["fetched_at"] < kickoff and
                  r["kickoff_utc"]==kickoff and r["price"] is not None and
                  r["market"] in {"spread","total","moneyline"} and
                  (r["market"]=="moneyline" or r["line"] is not None)]
        latest=observations[0] if observations else None
        status=str(event["status"] or "").upper() if event else "UNMAPPED"
        inactive=status in {"STATUS_POSTPONED","STATUS_CANCELED","STATUS_CANCELLED"}
        valid=bool(eligible)
        missed=stamp>=kickoff and not valid
        games.append(dict(game_id=canonical,frozen_kickoff_utc=_iso(frozen_kickoff),
                          current_kickoff_utc=_iso(kickoff),
                          kickoff_changed=kickoff!=frozen_kickoff,
                          expected_window_start_utc=_iso(kickoff-CLOSE_WINDOW_S),
                          expected_window_end_utc=_iso(kickoff),
                          schedule_status=status,capture_attempts=len(attempts),
                          captures_succeeded=sum(a["outcome"]=="success" for a in attempts),
                          captures_missed=int(missed),valid_near_close_observation=valid,
                          near_close_markets=sorted({r["market"] for r in eligible}),
                          near_close_provider=eligible[0]["provider"] if eligible else None,
                          near_close_book=eligible[0]["book"] if eligible else None,
                          latest_observation_age_seconds=(round(stamp-latest["fetched_at"],1)
                                                          if latest else None),
                          latest_observation_at=_iso(latest["fetched_at"]) if latest else None,
                          latest_provider=latest["provider"] if latest else None,
                          latest_book=latest["book"] if latest else None,
                          attempted_outcomes=[a["outcome"] for a in attempts],
                          needs_review=inactive or kickoff!=frozen_kickoff))
    return dict(label="PROSPECTIVE_RESEARCH_CAPTURE_HEALTH",season=freeze["season"],
                week=freeze["week"],as_of_utc=_iso(stamp),expected_windows=len(games),
                capture_attempts=sum(g["capture_attempts"] for g in games),
                captures_succeeded=sum(g["captures_succeeded"] for g in games),
                captures_missed=sum(g["captures_missed"] for g in games),
                valid_near_close_games=sum(g["valid_near_close_observation"] for g in games),
                games=games)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("freeze_path")
    parser.add_argument("--output")
    args=parser.parse_args()
    result=report(args.freeze_path)
    rendered=json.dumps(result,indent=2,sort_keys=True)+"\n"
    if args.output:
        Path(args.output).write_text(rendered)
    else:
        print(rendered,end="")


if __name__=="__main__":
    main()
