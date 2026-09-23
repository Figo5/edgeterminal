"""Near-kickoff ESPN capture is narrow and never calls Odds API."""
import time
from datetime import datetime,timezone

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal.nfl_market_capture import capture_if_due,capture_week
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
