"""Prospective research grades are append-only and never rewrite a freeze."""
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal.nfl_forward_grading import register_freeze, sync_all, sync_freeze, report


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn=None
    monkeypatch.setattr(settings,"database_url",f"sqlite:///{tmp_path/'grades.db'}")
    db.init()
    now=time.time()
    kickoff=now+3600
    start=datetime.fromtimestamp(kickoff,timezone.utc).isoformat()
    payload=dict(status="RESEARCH_ONLY",designation="PASS",season=2026,week=3,
                 family="linear",frozen_at=now-60,training_games=1903,
                 code_sha256="model-code-sha",games=[dict(
                     game_id="2026_03_ATL_GB",home="Green Bay Packers",
                     away="Atlanta Falcons",kickoff_et=start,
                     model=dict(margin=7.0,total=47.0,home_win_prob=.7),
                     features=dict(elo_margin=2.0),
                     frozen_quote=dict(home_handicap=-3.5,total=44.5,
                                       home_spread_price=-110,over_price=-110,
                                       home_moneyline_price=-150,away_moneyline_price=130,
                                       spread_book="draftkings",total_book="draftkings",
                                       moneyline_book="draftkings",
                                       spread_captured_at=now-120,
                                       total_captured_at=now-120,
                                       moneyline_captured_at=now-120))])
    path=tmp_path/"forward_2026_week03.json"
    path.write_text(json.dumps(payload))
    db.link_nfl_provider_game("espn","espn-1","nfl-2026-reg-03-atl-gb")
    yield dict(path=path,now=now,kickoff=kickoff,start=start,
               original_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    if db._conn is not None:
        db._conn.close();db._conn=None


def _result(frozen,home=27,away=20):
    return dict(id="espn-1",status="STATUS_FINAL",start_time=frozen["start"],
                home_team="Green Bay Packers",away_team="Atlanta Falcons",
                meta=dict(season=2026,season_type=2,week=3,home_score=str(home),
                          away_score=str(away),source_url="https://espn.example/final",
                          retrieved_at=frozen["kickoff"]+5*3600))


class Provider:
    def __init__(self,game):self.game=game
    def list_events_for_week(self,season,season_type,week):
        assert (season,season_type,week)==(2026,2,3)
        return [self.game]


def test_final_grade_and_correction_append_without_mutating_freeze(frozen):
    first=sync_freeze(frozen["path"],provider=Provider(_result(frozen)),
                      now=frozen["kickoff"]+5*3600)
    assert first["graded"]==1
    grade=db.q1("SELECT * FROM nfl_research_grades")
    row=json.loads(grade["payload"])
    assert row["predicted_home_points"]==27.0
    assert row["predicted_away_points"]==20.0
    assert row["actual_margin"]==7
    assert row["actual_total"]==47
    assert row["ats_result"]=="win"
    assert row["total_result"]=="win"
    assert row["straight_up_result"]=="win"
    assert row["absolute_margin_error"]==0
    assert row["absolute_total_error"]==0
    assert row["brier_contribution"]==pytest.approx(.09)
    assert row["frozen_moneyline"]["home_price"]==-150
    assert row["result_source"]=="https://espn.example/final"
    assert sync_freeze(frozen["path"],provider=Provider(_result(frozen)),
                       now=frozen["kickoff"]+6*3600)["graded"]==0
    second=sync_freeze(frozen["path"],provider=Provider(_result(frozen,28,20)),
                       now=frozen["kickoff"]+7*3600)
    assert second["corrected"]==1
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_grades")["n"]==2
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_grade_audit")["n"]==1
    summary=report(season=2026)["prospective_2026"]["linear"]
    assert summary["games_graded"]==1
    assert summary["margin_mae"]["n"]==1
    assert summary["ats_record"]["n_decisions"]==1
    assert summary["ats_record"]["win_rate_ci95"]==[1.0,1.0]
    assert summary["edge_buckets"]["spread"]["3-6"]["games_graded"]==1
    assert summary["edge_buckets"]["total"]["1-3"]["games_graded"]==1
    assert hashlib.sha256(frozen["path"].read_bytes()).hexdigest()==frozen["original_hash"]
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_research_grades")


def test_registry_rejects_changed_freeze_and_report_keeps_forward_separate(frozen):
    register_freeze(frozen["path"],now=frozen["now"])
    output=report(season=2026)
    assert output["prospective_2026"]["linear"]["games_graded"]==0
    assert output["retrospective_development"]["combined_with_forward"] is False
    frozen["path"].write_text(frozen["path"].read_text()+" ")
    with pytest.raises(ValueError,match="hash"):
        register_freeze(frozen["path"],now=frozen["now"]+1)


def test_registration_refuses_price_observed_after_freeze(frozen):
    payload=json.loads(frozen["path"].read_text())
    payload["games"][0]["frozen_quote"]["spread_captured_at"]=payload["frozen_at"]+10
    frozen["path"].write_text(json.dumps(payload))
    with pytest.raises(ValueError,match="prefreeze"):
        register_freeze(frozen["path"],now=frozen["now"])


def test_mismatch_or_nonfinal_result_does_not_grade(frozen):
    bad=_result(frozen)
    bad["home_team"]="Dallas Cowboys"
    assert sync_freeze(frozen["path"],provider=Provider(bad),
                       now=frozen["kickoff"]+5*3600)["graded"]==0
    bad=_result(frozen)
    bad["status"]="STATUS_IN_PROGRESS"
    assert sync_freeze(frozen["path"],provider=Provider(bad),
                       now=frozen["kickoff"]+5*3600)["graded"]==0
    bad=_result(frozen)
    bad["id"]="espn-unmapped"
    assert sync_freeze(frozen["path"],provider=Provider(bad),
                       now=frozen["kickoff"]+5*3600)["graded"]==0
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_grades")["n"]==0


def test_week3_negated_spread_field_uses_exact_source_quote(frozen):
    payload=json.loads(frozen["path"].read_text())
    quote=payload["games"][0]["frozen_quote"]
    quote.update(source="existing_immutable_live_shadow",home_handicap=3.5,
                 spread_captured_at=frozen["now"]-120,
                 total_captured_at=frozen["now"]-120)
    for key in ("spread_book","total_book","moneyline_book","home_spread_price",
                "over_price","home_moneyline_price","away_moneyline_price"):
        quote.pop(key,None)
    frozen["path"].write_text(json.dumps(payload))
    snapshot_id="source-snapshot"
    batch=[dict(id=i,market=market,selection=selection,line=line,price=price,
                book="draftkings",captured_at=frozen["now"]-120,source="espn-odds")
           for i,market,selection,line,price in (
               (1,"spread","Green Bay Packers",-3.5,-110),
               (2,"total","Over",44.5,-110),
               (3,"moneyline","Green Bay Packers",None,-150),
               (4,"moneyline","Atlanta Falcons",None,130))]
    db.save_nfl_snapshot(snapshot_id,"nfl-2026-reg-03-atl-gb",{"market":batch},"v1","f1")
    db.ex("INSERT INTO nfl_shadow_entries(game_id,prediction_id,snapshot_id,model_version,"
          "feature_version,season,season_type,week,home_team,away_team,kickoff_utc,frozen_at,"
          "designation,projected_home_points,projected_away_points,projected_margin,"
          "projected_total,home_win_probability) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          ("nfl-2026-reg-03-atl-gb","pred",snapshot_id,"v1","f1",2026,2,3,
           "Green Bay Packers","Atlanta Falcons",frozen["kickoff"],frozen["now"]-60,
           "PASS",27,20,7,47,.7))
    sync_freeze(frozen["path"],provider=Provider(_result(frozen,21,20)),
                now=frozen["kickoff"]+5*3600)
    grade=json.loads(db.q1("SELECT payload FROM nfl_research_grades")["payload"])
    assert grade["frozen_spread"]==-3.5
    assert grade["frozen_spread_field_in_original_file"]==3.5
    assert grade["ats_result"]=="loss"
    assert grade["frozen_moneyline"]["home_price"]==-150
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_market_attestations")["n"]==1
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("UPDATE nfl_game_snapshots SET payload='{}' WHERE id=?",(snapshot_id,))


def test_failed_result_provider_keeps_grade_pending_and_still_writes_report(frozen):
    class FailingProvider:
        def list_events_for_week(self,*args):
            raise RuntimeError("scoreboard unavailable")
    output=sync_all(2026,frozen["path"].parent,provider=FailingProvider(),
                    now=frozen["kickoff"]+5*3600)
    assert "scoreboard unavailable" in output["sync"][0]["error"]
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_grades")["n"]==0
    assert (frozen["path"].parent/"forward_performance_2026.json").exists()


def test_closing_market_comparison_needs_timed_same_book_quotes(frozen):
    sync_freeze(frozen["path"],provider=Provider(_result(frozen)),
                now=frozen["kickoff"]+5*3600)
    assert report(2026)["prospective_2026"]["linear"]["versus_closing_market"]["margin"]["n"]==0
    canonical="nfl-2026-reg-03-atl-gb"
    rows=[("spread","Green Bay Packers",-3.5,-110),
          ("total","Over",44.5,-110),
          ("moneyline","Green Bay Packers",None,-150),
          ("moneyline","Atlanta Falcons",None,130)]
    for market,selection,line,price in rows:
        db.ex("INSERT INTO odds_snapshots(event_id,sport,book,market,selection,line,price,"
              "captured_at,source,is_demo,provider,fetched_at,kickoff_utc,quote_phase) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (canonical,"NFL","draftkings",market,selection,line,price,
               frozen["kickoff"]-300,"fixture",0,"fixture",frozen["kickoff"]-300,
               frozen["kickoff"],"current"))
    market=report(2026)["prospective_2026"]["linear"]["versus_closing_market"]
    assert market["margin"]["n"]==1
    assert market["total"]["n"]==1
    assert market["brier"]["n"]==1
    assert market["margin"]["candidate_minus_close_mae"]==-3.5
