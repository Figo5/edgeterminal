"""Freeze an offline candidate before kickoff; it cannot issue official picks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import db, nfl_research as lab
from .backtest import _num, load_schedules
from .nfl_baselines import baselines_before
from .providers.normalize import normalize_team


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _frozen_quote(quote, saved, home, away, now):
    """Carry the book's home handicap verbatim; negative means home favorite."""
    if not quote:
        return dict(home_handicap=None,total=None,source="existing_immutable_live_shadow",
                    spread_captured_at=None,total_captured_at=None)
    captured=quote["home_spread_captured_at"]
    if any(value is not None and value>now for value in
           (captured,quote["total_captured_at"])):
        raise ValueError("market quote timestamp exceeds freeze time")
    batch=json.loads(saved["payload"]).get("market",[]) if saved else []
    spread=next((r for r in batch if r.get("market")=="spread" and
                 r.get("selection")==home and r.get("line")==quote["home_spread_line"] and
                 r.get("book")==quote["home_spread_book"] and
                 r.get("captured_at")==captured),None)
    total=next((r for r in batch if r.get("market")=="total" and
                r.get("selection")=="Over" and r.get("line")==quote["total_line"] and
                r.get("book")==quote["total_book"] and
                r.get("captured_at")==quote["total_captured_at"]),None)
    if (quote["home_spread_line"] is not None and spread is None or
            quote["total_line"] is not None and total is None):
        raise ValueError("shadow market line has no exact immutable source quote")
    moneyline={r["selection"]:r for r in batch if r.get("market")=="moneyline" and
               r.get("captured_at")==captured and
               r.get("book")==quote["home_spread_book"]}
    return dict(home_handicap=quote["home_spread_line"],total=quote["total_line"],
                source="existing_immutable_live_shadow",
                spread_book=quote["home_spread_book"],home_spread_price=quote["home_spread_price"],
                total_book=quote["total_book"],over_price=quote["over_price"],
                moneyline_book=quote["home_spread_book"],
                home_moneyline_price=moneyline.get(home,{}).get("price"),
                away_moneyline_price=moneyline.get(away,{}).get("price"),
                moneyline_captured_at=captured if moneyline else None,
                spread_captured_at=captured,total_captured_at=quote["total_captured_at"],
                source_snapshot_id=quote["snapshot_id"],
                spread_source_quote_id=spread.get("id") if spread else None,
                total_source_quote_id=total.get("id") if total else None,
                moneyline_home_source_quote_id=moneyline.get(home,{}).get("id"),
                moneyline_away_source_quote_id=moneyline.get(away,{}).get("id"),
                spread_provider=spread.get("source") if spread else None,
                total_provider=total.get("source") if total else None,
                moneyline_provider=moneyline.get(home,{}).get("source"))


def _write_freeze(out, payload):
    """Durably create a complete file without replacing an existing freeze."""
    out=Path(out)
    out.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=".forward-freeze-",dir=out.parent)
    try:
        with os.fdopen(fd,"w") as handle:
            json.dump(payload,handle,indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp,out)
    finally:
        os.unlink(temp)


def freeze(cache, out, season, week, family):
    cache, out = Path(cache), Path(out)
    if out.exists():
        raise FileExistsError(f"immutable research freeze already exists: {out}")
    now=time.time()
    schedules=load_schedules(cache)
    targets=[g for g in schedules if _num(g.get("season"))==season
             and _num(g.get("week"))==week and g.get("game_type")=="REG"]
    if not 1 <= len(targets) <= 16:
        raise ValueError(f"invalid regular-season slate: {len(targets)} games")
    for g in targets:
        if _num(g.get("home_score")) is not None or _num(g.get("away_score")) is not None:
            raise ValueError("cannot freeze a game with a final score")
        kickoff=datetime.fromisoformat(g["gameday"]+"T"+g["gametime"]).replace(
            tzinfo=ZoneInfo("America/New_York")).timestamp()
        if now>=kickoff:
            raise ValueError("cannot freeze after kickoff")
    rows=lab.build_dataset(cache,seasons=range(2019,season+1))
    past=[r for r in rows if (r["season"],r["week"])<(season,week)]
    prior_current=[g for g in schedules if _num(g.get("season"))==season and
                   g.get("game_type")=="REG" and _num(g.get("week")) is not None and
                   _num(g.get("week"))<week]
    if any(_num(g.get("home_score")) is None or _num(g.get("away_score")) is None
           for g in prior_current):
        raise ValueError("prior regular-season week is not completely final")
    if {r["game_id"] for r in past if r["season"]==season} != {
            g["game_id"] for g in prior_current}:
        raise ValueError("prior-season games or play data missing from training cutoff")
    if any(not r["metrics"][r["home"]] or not r["metrics"][r["away"]]
           for r in past if r["season"]==season):
        raise ValueError("prior regular-season play data is incomplete")
    snapshot_teams={normalize_team(None,g["home_team"]) for g in targets} | {
        normalize_team(None,g["away_team"]) for g in targets}
    snapshots={team:lab._team_snapshot(past,team) for team in snapshot_teams}
    opponents={opp for row in snapshots.values() for opp in row["opponents"]}
    snapshots.update({team:lab._team_snapshot(past,team)
                      for team in opponents if team not in snapshots})
    fit={name:lab._models(past,lab.FEATURES,name) for name in
         (("linear","boosted") if family=="ensemble" else (family,))}
    quotes={(r["home_team"],r["away_team"]):r for r in db.q(
        "SELECT * FROM nfl_shadow_entries WHERE season=? AND week=?",(season,week))}
    predictions=[]
    for source in sorted(targets,key=lambda g:g["game_id"]):
        home=normalize_team(None,source["home_team"])
        away=normalize_team(None,source["away_team"])
        game=dict(home=home,away=away,neutral=str(source.get("location") or "").lower()=="neutral",
                  home_rest=_num(source.get("home_rest")),away_rest=_num(source.get("away_rest")))
        game["baselines"]=baselines_before(schedules,season,week,home,away,game["neutral"])
        features,_=lab._features_before(past,game,snapshots)
        x=np.asarray([[features[f] for f in lab.FEATURES]])
        estimates=[]
        for margin,total,win in fit.values():
            estimates.append(dict(margin=float(margin.predict(x)[0]),
                                  total=float(total.predict(x)[0]),
                                  home_win_prob=float(win.predict_proba(x)[0,1])))
        predicted={key:sum(e[key] for e in estimates)/len(estimates)
                   for key in ("margin","total","home_win_prob")}
        quote=quotes.get((home,away))
        saved=(db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=?",(quote["snapshot_id"],))
               if quote else None)
        kickoff=datetime.fromisoformat(source["gameday"]+"T"+source["gametime"]).replace(
            tzinfo=ZoneInfo("America/New_York")).isoformat()
        predictions.append(dict(game_id=source["game_id"],home=home,away=away,
                                kickoff_et=kickoff,model=predicted,features=features,
                                frozen_quote=_frozen_quote(quote,saved,home,away,now)))
    inputs={p.name:_sha(p) for p in [cache/"games.csv.gz"]+
            [cache/f"play_by_play_{s}.csv.gz" for s in range(2019,season+1)]}
    payload=dict(status="RESEARCH_ONLY",designation="PASS",season=season,week=week,
                 family=family,frozen_at=now,training_games=len(past),games=predictions,
                 source_sha256=inputs,code_sha256=_sha(Path(lab.__file__)),
                 model_version="linear-ridge100-logitC0.1" if family=="linear" else family,
                 model_hash=_sha(Path(lab.__file__)),
                 feature_version="nfl-research-features-1",
                 feature_hash=hashlib.sha256(json.dumps(lab.FEATURES).encode()).hexdigest(),
                 methodology="all model inputs through prior week; no target outcomes")
    _write_freeze(out,payload)
    return dict(path=str(out),games=len(predictions),training_games=len(past),
                quotes=sum(p["frozen_quote"]["home_handicap"] is not None for p in predictions))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cache-dir",default="data/nflverse/forward_2026")
    parser.add_argument("--out",default="reports/nfl_model_v2/forward_2026_week03.json")
    parser.add_argument("--season",type=int,default=2026)
    parser.add_argument("--week",type=int,default=3)
    parser.add_argument("--family",choices=("linear","boosted","ensemble"),required=True)
    args=parser.parse_args()
    print(json.dumps(freeze(args.cache_dir,args.out,args.season,args.week,args.family)))


if __name__=="__main__":main()
