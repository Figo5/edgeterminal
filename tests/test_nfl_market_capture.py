"""Near-kickoff ESPN capture is narrow and never calls Odds API."""
import json
import time
from datetime import datetime,timezone

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal.nfl_market_capture import capture_if_due,capture_week
from edgeterminal.nfl_capture_health import report as health_report
from edgeterminal.nfl_forward_research import _current_quote


@pytest.fixture
def fixture_db(tmp_path,monkeypatch):
    if db._conn is not None:
        db._conn.close();db._conn=None
    monkeypatch.setattr(settings,"database_url",f"sqlite:///{tmp_path/'capture.db'}")
    db.init()
    now=time.time()
    kickoff=now+10*60
    game="nfl-2026-reg-03-atl-gb"
    db.upsert_event(dict(id=game,sport="NFL",league="NFL",
                         home_team="Green Bay Packers",away_team="Atlanta Falcons",
                         start_time=datetime.fromtimestamp(kickoff,timezone.utc).isoformat(),
                         status="STATUS_SCHEDULED",meta=dict(season=2026,week=3,season_type=2)))
    db.link_nfl_provider_game("espn","espn-1",game)
    yield now,kickoff,game
    db._conn.close();db._conn=None


class Provider:
    def __init__(self):self.calls=0
    def fetch_odds(self,sport):
        self.calls+=1
        return [dict(event_id="espn-1",sport=sport,book="Draft Kings",market="spread",
                     selection="Green Bay Packers",line=-3.5,price=-110),
                dict(event_id="espn-1",sport=sport,book="Draft Kings",market="spread",
                     selection="Atlanta Falcons",line=3.5,price=-110)]


def test_capture_only_when_game_is_near_and_mapped(fixture_db):
    now,kickoff,game=fixture_db
    provider=Provider()
    assert capture_if_due(now=now-3600,provider=provider)["rows_stored"]==0
    assert provider.calls==0
    result=capture_if_due(now=now,provider=provider)
    assert result["rows_stored"]==2
    assert provider.calls==1
    rows=db.q("SELECT * FROM odds_snapshots WHERE event_id=?",(game,))
    assert len(rows)==2
    assert rows[0]["book"]=="draftkings"
    assert rows[0]["provider"]=="espn-odds"
    frozen=_current_quote(game,"Green Bay Packers","Atlanta Falcons",time.time())
    assert frozen["home_handicap"]==-3.5
    assert frozen["spread_source_quote_id"] in {r["id"] for r in rows}


def test_unmapped_offer_cannot_be_assigned_by_team_name(fixture_db):
    now,_,game=fixture_db
    class Unmapped(Provider):
        def fetch_odds(self,sport):
            return [dict(row,event_id="espn-unknown") for row in super().fetch_odds(sport)]
    result=capture_if_due(now=now,provider=Unmapped())
    assert result["rows_stored"]==0
    assert result["games_without_quotes"]==[game]


def test_weekly_capture_reads_only_espn_fixture(fixture_db):
    now,_,_=fixture_db
    result=capture_week(2026,3,provider=Provider(),now=now)
    assert result["rows_stored"]==2


def test_demo_event_near_kickoff_never_triggers_espn_fetch(fixture_db):
    now,_,_=fixture_db
    db.upsert_event(dict(id="demo-nfl-dolphins-at-jets",sport="NFL",league="NFL",
                         home_team="New York Jets",away_team="Miami Dolphins",
                         start_time=datetime.fromtimestamp(now+60,timezone.utc).isoformat(),
                         status="STATUS_SCHEDULED",meta={"is_demo":True}))
    provider=Provider()
    result=capture_if_due(now=now,provider=provider)
    assert result["target_games"]==1
    assert result["games_without_quotes"]==[]


@pytest.mark.parametrize("failure",[ConnectionError("machine offline"),TimeoutError("ESPN timeout")])
def test_capture_failure_is_logged_without_fabricating_quotes(fixture_db,tmp_path,failure):
    now,_,game=fixture_db
    class Broken:
        def fetch_odds(self,sport):
            raise failure
    with pytest.raises(type(failure)):
        capture_if_due(now=now,provider=Broken())
    attempt=db.q1("SELECT * FROM nfl_capture_attempts WHERE game_id=?",(game,))
    assert attempt["outcome"]=="error"
    assert attempt["error_type"]==type(failure).__name__
    assert db.q1("SELECT COUNT(*) n FROM odds_snapshots")["n"]==0
    freeze=tmp_path/"health.json"
    freeze.write_text(json.dumps(dict(season=2026,week=3,games=[dict(
        home="Green Bay Packers",away="Atlanta Falcons",
        kickoff_et=datetime.fromtimestamp(now+600,timezone.utc).isoformat())])))
    health=health_report(freeze,now=now+601)
    assert health["capture_attempts"]==1
    assert health["captures_missed"]==1
    assert health["valid_near_close_games"]==0


def test_sleeping_machine_is_a_missed_window_without_an_attempt(fixture_db,tmp_path):
    now,kickoff,game=fixture_db
    freeze=tmp_path/"health.json"
    freeze.write_text(json.dumps(dict(season=2026,week=3,games=[dict(
        home="Green Bay Packers",away="Atlanta Falcons",
        kickoff_et=datetime.fromtimestamp(kickoff,timezone.utc).isoformat())])))
    health=health_report(freeze,now=kickoff+1)
    assert health["capture_attempts"]==0
    assert health["captures_missed"]==1
    assert health["games"][0]["latest_observation_age_seconds"] is None


def test_capture_health_counts_valid_near_close_observation(fixture_db,tmp_path):
    now,kickoff,_=fixture_db
    capture_if_due(now=now,provider=Provider())
    freeze=tmp_path/"health.json"
    freeze.write_text(json.dumps(dict(season=2026,week=3,games=[dict(
        home="Green Bay Packers",away="Atlanta Falcons",
        kickoff_et=datetime.fromtimestamp(kickoff,timezone.utc).isoformat())])))
    health=health_report(freeze,now=kickoff-1)
    assert health["capture_attempts"]==1
    assert health["captures_succeeded"]==1
    assert health["valid_near_close_games"]==1
    assert health["games"][0]["near_close_markets"]==["spread"]
    assert health["games"][0]["near_close_provider"]=="espn-odds"
    assert health["games"][0]["latest_observation_age_seconds"]>0


def test_malformed_response_is_fail_closed_and_attempt_audited(fixture_db):
    now,_,_=fixture_db
    class Broken:
        def fetch_odds(self,sport):return [None]
    with pytest.raises(ValueError,match="malformed"):
        capture_if_due(now=now,provider=Broken())
    assert db.q1("SELECT outcome FROM nfl_capture_attempts")["outcome"]=="error"
    assert db.q1("SELECT COUNT(*) n FROM odds_snapshots")["n"]==0


@pytest.mark.parametrize("status",["STATUS_POSTPONED","STATUS_CANCELED","STATUS_CANCELLED"])
def test_inactive_game_never_requests_quote(fixture_db,status):
    now,_,game=fixture_db
    db.ex("UPDATE events SET status=? WHERE id=?",(status,game))
    provider=Provider()
    assert capture_if_due(now=now,provider=provider)["target_games"]==0
    assert provider.calls==0


def test_moved_kickoff_uses_new_capture_window(fixture_db):
    now,_,game=fixture_db
    moved=now+3600
    db.ex("UPDATE events SET start_time=? WHERE id=?",
          (datetime.fromtimestamp(moved,timezone.utc).isoformat(),game))
    provider=Provider()
    assert capture_if_due(now=now,provider=provider)["target_games"]==0
    assert capture_if_due(now=moved-600,provider=provider)["rows_stored"]==2
    assert provider.calls==1


def test_repeated_retrievals_append_and_source_quote_id_does_not_rewrite(fixture_db):
    now,_,game=fixture_db
    capture_if_due(now=now,provider=Provider())
    original=_current_quote(game,"Green Bay Packers","Atlanta Falcons",time.time())
    first_ids=[r["id"] for r in db.q("SELECT * FROM odds_snapshots ORDER BY id")]
    capture_if_due(now=now+1,provider=Provider())
    rows=db.q("SELECT * FROM odds_snapshots ORDER BY id")
    assert len(rows)==4
    assert all(r["id"] in [x["id"] for x in rows] for r in db.q(
        "SELECT * FROM odds_snapshots WHERE id IN (?,?)",first_ids))
    assert original["spread_source_quote_id"] in first_ids
    assert db.q1("SELECT COUNT(*) n FROM nfl_capture_attempts")["n"]==2
