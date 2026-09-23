"""Read-only, repeatable checks for a registered pregame research freeze."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import subprocess
from pathlib import Path

from . import db,nfl_identity
from .integrity import parse_time
from .providers.espn import EspnEvents
from .nfl_forward_grading import RESULT_WAIT_S


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(path,cache,live_provider=None):
    path,cache=Path(path),Path(cache)
    root=Path(__file__).resolve().parents[1]
    raw=path.read_bytes()
    freeze=json.loads(raw)
    digest=hashlib.sha256(raw).hexdigest()
    relative=path.resolve().relative_to(root)
    committed=subprocess.run(["git","show",f"HEAD:{relative}"],cwd=root,
                             capture_output=True,check=True).stdout
    registration=db.q1("SELECT * FROM nfl_research_freezes WHERE freeze_sha=?",(digest,))
    version=db.q1("SELECT v.*,e.status,l.freeze_sha FROM nfl_research_model_freeze_links l "
                  "JOIN nfl_research_model_versions v ON l.version_id=v.version_id "
                  "JOIN nfl_research_model_version_events e ON e.id=(SELECT MAX(id) "
                  "FROM nfl_research_model_version_events WHERE version_id=v.version_id) "
                  "WHERE l.freeze_sha=?",(digest,))
    source_hashes={name:dict(frozen=expected,current=_sha(cache/name) if
                    (cache/name).exists() else None) for name,expected in
                   freeze.get("source_sha256",{}).items()}
    pbp=cache/f"play_by_play_{freeze['season']}.csv.gz"
    target_plays=0
    with gzip.open(pbp,"rt") as handle:
        for row in csv.DictReader(handle):
            if row.get("season_type") in {"REG","2"} and row.get("week")==str(freeze["week"]):
                target_plays+=1
    current_targets=[]
    with gzip.open(cache/"games.csv.gz","rt") as handle:
        for row in csv.DictReader(handle):
            if (row.get("season")==str(freeze["season"]) and
                    row.get("week")==str(freeze["week"]) and
                    row.get("game_type")=="REG"):
                current_targets.append(row)
    provider_events=(live_provider or EspnEvents()).list_events_for_week(
        freeze["season"],2,freeze["week"])
    if not isinstance(provider_events,list) or any(not isinstance(r,dict) for r in provider_events):
        raise ValueError("invalid ESPN event list")
    by_id={r["id"]:r for r in provider_events}
    game_checks=[]
    for game in freeze["games"]:
        canonical=(f"nfl-{freeze['season']}-reg-{freeze['week']:02d}-"
                   f"{nfl_identity.team_code(game['away'])}-{nfl_identity.team_code(game['home'])}")
        aliases=db.q("SELECT provider_game_id FROM nfl_provider_games "
                     "WHERE provider='espn' AND game_id=?",(canonical,))
        matches=[by_id.get(a["provider_game_id"]) for a in aliases]
        matches=[m for m in matches if m is not None]
        identity_ok=len(matches)==1 and nfl_identity.canonical_id(matches[0])==canonical
        kickoff=parse_time(game["kickoff_et"])
        kickoff_ok=identity_ok and parse_time(matches[0]["start_time"])==kickoff
        rows=db.q("SELECT * FROM nfl_research_market_freezes WHERE freeze_sha=? AND game_id=?",
                  (digest,game["game_id"]))
        att=db.q1("SELECT * FROM nfl_research_market_attestations WHERE freeze_sha=? "
                  "AND game_id=?",(digest,game["game_id"]))
        saved=db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=?",
                    (att["snapshot_id"],)) if att else None
        snapshot_ok=bool(saved and hashlib.sha256(saved["payload"].encode()).hexdigest()==
                         att["snapshot_sha256"])
        market_batch=json.loads(saved["payload"]).get("market",[]) if snapshot_ok else []
        quote_by_id={r.get("id"):r for r in market_batch}
        prices_ok=bool(rows and all(r["source_snapshot_id"]==att["snapshot_id"] and
                      r["source_quote_id"] in quote_by_id and
                      quote_by_id[r["source_quote_id"]].get("market")==r["market"] and
                      quote_by_id[r["source_quote_id"]].get("selection")==r["selection"] and
                      quote_by_id[r["source_quote_id"]].get("book")==r["book"] and
                      quote_by_id[r["source_quote_id"]].get("line")==r["line"] and
                      quote_by_id[r["source_quote_id"]].get("price")==r["price"] and
                      quote_by_id[r["source_quote_id"]].get("captured_at")==r["fetched_at"] and
                      r["fetched_at"]<=freeze["frozen_at"] and r["fetched_at"]<kickoff
                      for r in rows)) if att else False
        spread=next((r for r in rows if r["market"]=="spread" and
                     r["selection"]==game["home"]),None)
        sign_ok=bool(spread and spread["line"]==-game["frozen_quote"]["home_handicap"])
        game_checks.append(dict(game_id=game["game_id"],canonical_id=canonical,
                                espn_event_id=matches[0]["id"] if matches else None,
                                identity_ok=identity_ok,kickoff_ok=kickoff_ok,
                                market_rows=len(rows),source_snapshot_id=att["snapshot_id"] if att else None,
                                source_snapshot_sha_ok=snapshot_ok,prices_traceable=prices_ok,
                                spread_sign_sidecar_ok=sign_ok))
    triggers={r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='trigger'")}
    protected={"nfl_research_freeze_no_update","nfl_research_freeze_no_delete",
               "nfl_research_market_no_update","nfl_research_market_no_delete",
               "nfl_research_market_no_late_insert",
               "nfl_research_attestation_no_update","nfl_research_attestation_no_delete",
               "nfl_research_attestation_no_late_insert"}
    source_drift=[name for name,row in source_hashes.items() if row["current"]!=row["frozen"]]
    feature_names=sorted({name for game in freeze["games"] for name in game.get("features",{})})
    expected_feature_hash=freeze.get("feature_hash") or hashlib.sha256(json.dumps(
        [freeze["code_sha256"],feature_names],separators=(",",":")).encode()).hexdigest()
    expected_model_hash=freeze.get("model_hash") or freeze["code_sha256"]
    code_at_commit=subprocess.run(
        ["git","show",f"{version['code_commit']}:edgeterminal/nfl_research.py"],
        cwd=root,capture_output=True,check=True).stdout if version else None
    return dict(freeze_sha256=digest,committed_sha256=hashlib.sha256(committed).hexdigest(),
                status=freeze["status"],designation=freeze["designation"],
                frozen_at=freeze["frozen_at"],training_games=freeze["training_games"],
                postgame_delay_seconds=RESULT_WAIT_S,
                committed_file_matches=raw==committed,game_count=len(game_checks),
                all_espn_identities_match=all(g["identity_ok"] for g in game_checks),
                all_kickoffs_match=all(g["kickoff_ok"] for g in game_checks),
                all_market_sidecars_traceable=all(g["prices_traceable"] and
                                                 g["source_snapshot_sha_ok"] and
                                                 g["spread_sign_sidecar_ok"] for g in game_checks),
                model_hash_recorded=bool(registration and
                                         registration["model_hash"]==expected_model_hash),
                feature_hash_recorded=bool(registration and
                                           registration["feature_hash"]==expected_feature_hash),
                code_sha256_matches=_sha(root/"edgeterminal/nfl_research.py")==freeze["code_sha256"],
                code_commit_sha256_matches=bool(code_at_commit and
                    hashlib.sha256(code_at_commit).hexdigest()==freeze["code_sha256"]),
                version_registry_linked=bool(version and version["status"]=="research" and
                                             version["model_hash"]==registration["model_hash"] and
                                             version["feature_hash"]==registration["feature_hash"]),
                week3_training_plays=target_plays,
                current_schedule_target_games=len(current_targets),
                current_schedule_target_scores_blank=all(
                    not r.get("home_score") and not r.get("away_score") for r in current_targets),
                all_frozen_before_kickoff=all(freeze["frozen_at"]<
                    parse_time(g["kickoff_et"]) for g in freeze["games"]),
                source_hashes=source_hashes,
                source_hash_drift=source_drift,immutable_db_triggers_present=protected<=triggers,
                grade_count=db.q1("SELECT COUNT(*) n FROM nfl_research_grades WHERE freeze_sha=?",
                                  (digest,))["n"],games=game_checks)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("freeze_path")
    parser.add_argument("--cache",default="data/nflverse/forward_2026")
    parser.add_argument("--output")
    args=parser.parse_args()
    result=audit(args.freeze_path,args.cache)
    rendered=json.dumps(result,indent=2,sort_keys=True)+"\n"
    if args.output:
        Path(args.output).write_text(rendered)
    else:
        print(rendered,end="")


if __name__=="__main__":
    main()
