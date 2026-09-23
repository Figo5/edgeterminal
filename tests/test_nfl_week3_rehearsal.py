"""Fixture-only full-slate rehearsal; no live 2026 results are fetched."""
import copy
import hashlib
import json
from pathlib import Path

from edgeterminal import db,nfl_identity
from edgeterminal.config import settings
from edgeterminal.integrity import parse_time
from edgeterminal.nfl_forward_grading import report,sync_freeze


REAL_FREEZE=Path(__file__).resolve().parents[1]/"reports/nfl_model_v2/forward_2026_week03.json"


def test_full_week3_synthetic_slate_and_score_correction(tmp_path,monkeypatch):
    original_hash=hashlib.sha256(REAL_FREEZE.read_bytes()).hexdigest()
    if db._conn is not None:
        db._conn.close();db._conn=None
    monkeypatch.setattr(settings,"database_url",f"sqlite:///{tmp_path/'rehearsal.db'}")
    db.init()
    freeze=copy.deepcopy(json.loads(REAL_FREEZE.read_text()))
    assert len(freeze["games"])==16
    results=[]
    for index,game in enumerate(freeze["games"]):
        game["frozen_quote"]={"source":"synthetic_fixture", "home_handicap":-3.5,
                              "total":45.5,"home_spread_price":-110,"over_price":-110,
                              "home_moneyline_price":-150,"away_moneyline_price":130,
                              "spread_book":"fixturebook","total_book":"fixturebook",
                              "moneyline_book":"fixturebook",
                              "spread_captured_at":freeze["frozen_at"]-60,
                              "total_captured_at":freeze["frozen_at"]-60,
                              "moneyline_captured_at":freeze["frozen_at"]-60}
        canonical=(f"nfl-2026-reg-03-{nfl_identity.team_code(game['away'])}-"
                   f"{nfl_identity.team_code(game['home'])}")
        provider_id=f"fixture-espn-{index}"
        db.link_nfl_provider_game("espn",provider_id,canonical)
        kickoff=parse_time(game["kickoff_et"])
        results.append(dict(id=provider_id,status="STATUS_FINAL",start_time=game["kickoff_et"],
                            home_team=game["home"],away_team=game["away"],
                            meta=dict(season=2026,season_type=2,week=3,
                                      home_score=str(20+index%10),away_score=str(17+index%8),
                                      retrieved_at=kickoff+4.5*3600,
                                      source_url="fixture://synthetic-final")))
    fixture=tmp_path/"forward_2026_week03.json"
    fixture.write_text(json.dumps(freeze))
    fixture_hash=hashlib.sha256(fixture.read_bytes()).hexdigest()
    class FixtureProvider:
        def list_events_for_week(self,*args):return results
    now=max(parse_time(g["kickoff_et"]) for g in freeze["games"])+5*3600
    first=sync_freeze(fixture,provider=FixtureProvider(),now=now)
    assert first["graded"]==16 and first["unmatched"]==0
    grades=[json.loads(r["payload"]) for r in db.q(
        "SELECT payload FROM nfl_research_grades ORDER BY game_id")]
    assert len(grades)==16
    for grade in grades:
        assert isinstance(grade["final_home_score"],int)
        assert grade["absolute_margin_error"]==abs(
            grade["predicted_margin"]-grade["actual_margin"])
        assert grade["absolute_total_error"]==abs(
            grade["predicted_total"]-grade["actual_total"])
        assert grade["ats_result"] in {"win","loss","push","pass"}
        assert grade["total_result"] in {"win","loss","push","pass"}
        assert grade["straight_up_result"] in {"win","loss","push","pass"}
        if grade["actual_margin"]!=0:
            assert grade["brier_contribution"] is not None
        assert grade["frozen_moneyline"]["home_price"]==-150
    assert sync_freeze(fixture,provider=FixtureProvider(),now=now)["graded"]==0
    results[0]["meta"]["home_score"]="31"
    results[0]["meta"]["retrieved_at"]=now+60
    corrected=sync_freeze(fixture,provider=FixtureProvider(),now=now+60)
    assert corrected["corrected"]==1
    audit=db.q1("SELECT * FROM nfl_research_grade_audit")
    assert audit["prior_revision"]==1 and audit["new_revision"]==2
    original=db.q1("SELECT * FROM nfl_research_grades WHERE game_id=? AND revision=1",
                   (grades[0]["game_id"],))
    assert original is not None and original["home_score"]!=31
    assert report(2026)["prospective_2026"]["linear"]["games_graded"]==16
    assert hashlib.sha256(fixture.read_bytes()).hexdigest()==fixture_hash
    assert hashlib.sha256(REAL_FREEZE.read_bytes()).hexdigest()==original_hash
    db._conn.close();db._conn=None
