"""Fixture-only prospective prop grading; no provider requests or picks."""
import hashlib
import csv
import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from edgeterminal import db, nfl_prop_lab
from edgeterminal.config import settings
from edgeterminal.nfl_prop_grading import grade_freeze, forward_report, local_evidence

GAME = "nfl-2026-reg-03-atl-gb"


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'grades.db'}")
    db.init()
    projections = []
    for quote_id, player, gsis, market, line, side in [
        (1, "A", "gsis-a", "receiving_yards", 10.0, "over"),
        (2, "A", "gsis-a", "receiving_yards", 10.0, "under"),
        (3, "B", "gsis-b", "receptions", 3.5, "over"),
        (4, "B", "gsis-b", "receptions", 3.5, "under"),
        (5, "C", "gsis-c", "receptions", 0.5, "over"),
        (6, "C", "gsis-c", "receptions", 0.5, "under"),
        (7, "D", "gsis-d", "receiving_yards", 8.5, "over"),
        (8, "D", "gsis-d", "receiving_yards", 8.5, "under")]:
        projections.append(dict(quote_id=quote_id, canonical_game_id=GAME, player_id=f"sgo:{player}",
                                gsis_player_id=gsis, player_name=player, market=market,
                                model_version="prop-lab-v1:ridge", projection=12.0 if market=="receiving_yards" else 2.0,
                                uncertainty_sigma=4.0, sportsbook="fanduel", line=line, side=side,
                                price=-110, observed_at=10.0, kickoff_utc=100.0,
                                probability_over=0.6, probability_under=0.4))
    body=json.dumps({"metadata":{"model_version":"prop-lab-v1"},"projections":projections},sort_keys=True)
    freeze_sha=hashlib.sha256(body.encode()).hexdigest()
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  ("batch", "sportsgameodds", 10, "source-hash", "{}", 1, 1, 8, "{}"))
        c.execute("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)",(freeze_sha,"batch",20,body))
    yield freeze_sha
    if db._conn is not None:
        db._conn.close(); db._conn=None


def _evidence(value=10.0, status="final", players=None):
    source={"source":"fixture-official","file":str(Path(__file__)),
            "sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "retrieved_at":20000.0}
    inactive={**source,"active_roster":False,"snap_played":False}
    active={**source,"active_roster":True,"snap_played":True}
    return dict(game_id=GAME, game_status=status, actual_kickoff_utc=100.0,
                final_score={"home":21,"away":17} if status=="final" else None,
                final_game_source=source,player_stat_source=source,result_retrieved_at=20000.0,
                players=players if players is not None else [
                    {"gsis_player_id":"gsis-a","status":"active","stats":{"receiving_yards":value}},
                    {"gsis_player_id":"gsis-b","status":"inactive","stats":{},"participation_evidence":inactive},
                    {"gsis_player_id":"gsis-c","status":"active","stats":{"receptions":0.0},"participation_evidence":active}])


def test_final_push_zero_inactive_missing_and_correction(frozen):
    first=grade_freeze(frozen,[_evidence()],now=20000.0)
    assert first["new_revisions"]==6 and first["pending_missing_stat"]==2
    latest=db.q("SELECT payload FROM nfl_prop_grade_revisions WHERE revision=1 ORDER BY quote_id")
    rows=[json.loads(x["payload"]) for x in latest]
    assert [x["result"] for x in rows]==["push","push","void","void","lost","won"]
    assert rows[0]["actual_stat"]==10.0 and rows[0]["absolute_error"]==2.0
    assert rows[0]["signed_error"]==2.0 and rows[0]["frozen_price"]==-110
    assert rows[4]["actual_stat"]==0.0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_source_artifacts")["n"]==1
    assert grade_freeze(frozen,[_evidence()],now=21000.0)["new_revisions"]==0
    corrected=grade_freeze(frozen,[_evidence(13.0)],now=22000.0)
    assert corrected["new_revisions"]==2
    original=json.loads(db.q1("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=1 AND revision=1")["payload"])
    revision=json.loads(db.q1("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=1 AND revision=2")["payload"])
    assert original["result"]=="push" and revision["result"]=="won"
    assert revision["sportsbook_settlement_interpretation"]=="undetermined"
    assert revision["settlement_scenario"]=="stat_correction_resettlement_unverified"
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_prop_grade_revisions")
    assert hashlib.sha256(db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?",(frozen,))["payload"].encode()).hexdigest()==frozen
    report=forward_report(frozen)
    assert report["prospective_2026"]["quote_sides_graded"]==6
    assert report["prospective_2026"]["distinct_player_markets_graded"]==3
    assert forward_report()["prospective_2026"]["weeks"]["3"]["graded_quote_sides"]==6
    comparison=next(x for x in report["prospective_2026"]["market_comparisons"]
                    if x["player_name"]=="A")
    assert comparison["projection_line_difference"]==2.0
    assert comparison["implied_probability_over"]==pytest.approx(110/210)
    assert comparison["over_result"]=="won"


def test_wait_delay_postponement_cancellation_and_no_fabricated_zero(frozen):
    early=_evidence()
    early["result_retrieved_at"]=100.0
    early["final_game_source"]["retrieved_at"]=100.0
    early["player_stat_source"]["retrieved_at"]=100.0
    for player in early["players"]:
        if "participation_evidence" in player:
            player["participation_evidence"]["retrieved_at"]=100.0
    assert grade_freeze(frozen,[early],now=100.0)["new_revisions"]==0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"]==0
    assert grade_freeze(frozen,[_evidence(status="postponed")],now=20000.0)["new_revisions"]==0
    moved=_evidence(status="postponed")
    moved["actual_kickoff_utc"]=30000.0
    assert grade_freeze(frozen,[moved],now=20000.0)["new_revisions"]==0
    assert grade_freeze(frozen,[_evidence(status="suspended")],now=20000.0)["new_revisions"]==0
    canceled=grade_freeze(frozen,[_evidence(status="canceled")],now=20000.0)
    assert canceled["new_revisions"]==8
    assert {json.loads(x["payload"])["result"] for x in db.q("SELECT payload FROM nfl_prop_grade_revisions")}=={"void"}


def test_missing_or_changed_provenance_fails_closed(frozen):
    evidence=_evidence()
    evidence["player_stat_source"]["sha256"]="0"*64
    with pytest.raises(ValueError,match="hash mismatch"):
        grade_freeze(frozen,[evidence],now=20000)
    evidence=_evidence()
    evidence["players"][2].pop("participation_evidence")
    with pytest.raises(ValueError,match="participation evidence"):
        grade_freeze(frozen,[evidence],now=20000)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"]==0


def test_book_interpretation_and_registry_revision(frozen,tmp_path):
    assert grade_freeze(frozen,[_evidence()],now=20000)["new_revisions"]==6
    grade=json.loads(db.q1("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=5")["payload"])
    assert grade["research_statistical_result"]=="lost"
    assert grade["sportsbook_settlement_interpretation"]=="lost"
    assert grade["final_game_source"]["sha256"]
    from edgeterminal.nfl_prop_rules import DEFAULT_RULES
    registry=json.loads(DEFAULT_RULES.read_text())
    registry["version"]="fixture-rules-revision"
    altered=tmp_path/"revised-rules.json"
    altered.write_text(json.dumps(registry))
    assert grade_freeze(frozen,[_evidence()],now=21000,rules_path=altered)["new_revisions"]==6
    original=json.loads(db.q1("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=5 AND revision=1")["payload"])
    revised=json.loads(db.q1("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=5 AND revision=2")["payload"])
    assert original["rules_sha256"]!=revised["rules_sha256"]
    assert original["research_statistical_result"]==revised["research_statistical_result"]


def test_rule_coverage_and_product_ambiguity():
    from edgeterminal import nfl_prop_rules
    _, rules=nfl_prop_rules.load_rules()
    assert set(rules["books"])=={"fanduel","draftkings","betmgm","caesars","espnbet","bovada"}
    assert all(set(nfl_prop_rules.SCENARIOS) <= set(rule) for rule in rules["books"].values())
    assert nfl_prop_rules.interpret("fanduel","lost","active",
                                     {"active_roster":True,"snap_played":False},rules)[0]=="undetermined"
    assert nfl_prop_rules.interpret("bovada","lost","active",
                                     {"active_roster":True,"snap_played":False},rules)[0]=="undetermined"
    assert nfl_prop_rules.interpret("espnbet","lost","active",
                                     {"active_roster":True,"snap_played":True},rules)[0]=="undetermined"


def test_final_score_correction_appends_revision(frozen):
    assert grade_freeze(frozen,[_evidence()],now=20000)["new_revisions"]==6
    updated=_evidence()
    updated["final_score"]={"home":24,"away":17}
    assert grade_freeze(frozen,[updated],now=21000)["new_revisions"]==6
    history=db.q("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=1 ORDER BY revision")
    assert [json.loads(row["payload"])["final_score"]["home"] for row in history]==[21,24]


def test_local_final_grade_and_cached_stats_leave_absent_players_pending(frozen, tmp_path, monkeypatch):
    schedule=tmp_path/"games.csv.gz"
    with gzip.open(schedule,"wt",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["game_id","season","game_type","week","away_team","home_team"])
        w.writeheader();w.writerow(dict(game_id="2026_03_ATL_GB",season=2026,game_type="REG",week=3,
                                         away_team="ATL",home_team="GB"))
    monkeypatch.setattr(nfl_prop_lab,"SCHEDULE",schedule)
    stats=tmp_path/"stats_player_week_2026.csv"
    with stats.open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["game_id","player_id","receiving_yards","receptions","carries"])
        w.writeheader()
        w.writerow(dict(game_id="2026_03_ATL_GB",player_id="gsis-a",receiving_yards=10,receptions=""))
        w.writerow(dict(game_id="2026_03_ATL_GB",player_id="gsis-c",receiving_yards="",receptions=0,carries=1))
        w.writerow(dict(game_id="2026_03_ATL_GB",player_id="gsis-d",receiving_yards=0,receptions=0,carries=0))
    metadata=stats.with_suffix(".metadata.json")
    metadata.write_text(json.dumps({
        "source_url":"https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv",
        "retrieved_at":20000,"sha256":hashlib.sha256(stats.read_bytes()).hexdigest()}))
    with db.tx() as c:
        c.execute("INSERT INTO events(id,sport,league,start_time) VALUES(?,?,?,?)",
                  (GAME,"NFL","NFL","1970-01-01T00:01:40Z"))
        c.execute("INSERT INTO nfl_research_grades VALUES(?,?,?,?,?,?,?,?,?)",
                  ("game-freeze","2026_03_ATL_GB",1,21,17,"fixture-final",20000,20000,"{}"))
    saved=metadata.read_text()
    metadata.write_text(saved.replace(hashlib.sha256(stats.read_bytes()).hexdigest(),"wrong-sha"))
    with pytest.raises(ValueError,match="unverified"):
        local_evidence(frozen,stats)
    metadata.write_text(saved)
    evidence=local_evidence(frozen,stats)
    assert len(evidence)==1 and len(evidence[0]["players"])==3
    assert next(x for x in evidence[0]["players"] if x["gsis_player_id"]=="gsis-d")["status"]=="unknown"
    graded=grade_freeze(frozen,evidence,now=30000)
    assert graded["new_revisions"]==4 and graded["pending_missing_stat"]==4
    assert all(json.loads(row["payload"])["actual_stat"]==0.0
               for row in db.q("SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id IN (5,6)"))


def test_cumulative_report_keeps_weekly_freezes_separate(frozen):
    assert grade_freeze(frozen,[_evidence()],now=20000)["new_revisions"]==6
    original=json.loads(db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?",(frozen,))["payload"])
    for quote in original["projections"]:
        quote["quote_id"]+=100
        quote["canonical_game_id"]="nfl-2026-reg-04-atl-gb"
        quote["kickoff_utc"]=700000
    body=json.dumps(original,sort_keys=True)
    second=hashlib.sha256(body.encode()).hexdigest()
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  ("batch-next","sportsgameodds",600000,"source-next","{}",1,1,8,"{}"))
        c.execute("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)",(second,"batch-next",600000,body))
    combined=forward_report()["prospective_2026"]
    assert combined["freeze_shas"]==[frozen,second]
    assert combined["frozen_quote_sides"]==16 and combined["quote_sides_graded"]==6
    assert combined["weeks"]=={"3":{"frozen_quote_sides":8,"graded_quote_sides":6},
                               "4":{"frozen_quote_sides":8,"graded_quote_sides":0}}
