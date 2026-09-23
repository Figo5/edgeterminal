"""Fail-closed weekly linear research freeze and forward grade maintenance."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import engine
from .backtest import _num, load_schedules
from .nfl_forward_grading import sync_all
from .nfl_forward_research import freeze
from .providers.nflverse import _ensure_archive


def _kickoff(game):
    return datetime.fromisoformat(game["gameday"]+"T"+game["gametime"]).replace(
        tzinfo=ZoneInfo("America/New_York")).timestamp()


def run(season=2026, cache="data/nflverse/forward_2026",
        out_dir="reports/nfl_model_v2", now=None, refresh_sources=True):
    stamp=time.time() if now is None else now
    cache,out_dir=Path(cache),Path(out_dir)
    result={"season":season,"freeze":"no week within 36 hours of first kickoff"}
    # Refresh the schedule before choosing a cutoff; on network failure grading
    # still runs, but a new freeze requires a fresh schedule.
    schedule_fresh=not refresh_sources
    if refresh_sources:
        try:
            schedules=load_schedules(cache,refresh=True)
            schedule_fresh=True
        except Exception as exc:
            result["schedule_error"]=f"{type(exc).__name__}: {exc}"
            schedules=load_schedules(cache)
    else:
        schedules=load_schedules(cache)
    if refresh_sources:
        # ESPN event aliases are needed by the near-kickoff capture and final
        # grader. This endpoint reads schedule only, never Odds API.
        result["live_schedule"]=engine.refresh_schedule("NFL")
    candidates=[]
    for week in range(1,19):
        slate=[g for g in schedules if _num(g.get("season"))==season and
               _num(g.get("week"))==week and g.get("game_type")=="REG"]
        if slate:
            first=min(_kickoff(g) for g in slate)
            if stamp < first <= stamp+36*3600:
                candidates.append((first,week))
    if candidates:
        _,week=min(candidates)
        path=out_dir/f"forward_{season}_week{week:02d}.json"
        if path.exists():
            result["freeze"]="already exists; unchanged"
        elif not schedule_fresh:
            result["freeze"]="refused: fresh schedule unavailable"
        else:
            try:
                if refresh_sources:
                    _ensure_archive(season,cache,refresh=True)
                # freeze() checks every kickoff, all prior scores and play data,
                # then creates with O_EXCL. Any missing or late data is a refusal.
                result["freeze"]=freeze(cache,path,season,week,"linear")
            except Exception as exc:
                result["freeze"]=f"refused: {type(exc).__name__}: {exc}"
    result["grading"]=sync_all(season,out_dir,now=stamp)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--season",type=int,default=2026)
    parser.add_argument("--cache",default="data/nflverse/forward_2026")
    parser.add_argument("--out-dir",default="reports/nfl_model_v2")
    args=parser.parse_args()
    output=run(args.season,args.cache,args.out_dir)
    print(json.dumps(output))
    if (str(output["freeze"]).startswith("refused") or output.get("schedule_error") or
            output.get("live_schedule",{}).get("ok") is False or
            any("error" in item for item in output["grading"]["sync"])):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
