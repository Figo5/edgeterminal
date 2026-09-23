"""Fixture-only checks for point-in-time NFL quotes and multi-book consensus."""
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal.market_provenance import closing_observation, consensus, normalize_quote
from edgeterminal.providers.theoddsapi import TheOddsApi


@pytest.fixture
def market_db(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'market.db'}")
    db.init()
    kickoff=time.time()+86400
    db.upsert_event(dict(id="nfl-2026-reg-03-atl-gb",sport="NFL",league="NFL",
                         home_team="Green Bay Packers",away_team="Atlanta Falcons",
                         start_time=datetime.fromtimestamp(kickoff,timezone.utc).isoformat(),
                         status="scheduled",meta=dict(season=2026,season_type=2,week=3)))
    yield kickoff
    db._conn.close()
    db._conn=None


def test_ingested_quote_has_explicit_provenance_and_is_append_only(market_db):
    game="nfl-2026-reg-03-atl-gb"
    db.store_odds([dict(event_id=game,sport="NFL",book="Draft Kings",
                        market="spreads",selection="Green Bay Packers",
                        line=-3.5,price=-110)],source="espn-odds")
    row=db.q1("SELECT * FROM odds_snapshots WHERE event_id=?",(game,))
    assert row["book"]=="draftkings"
    assert row["market"]=="spread"
    assert row["provider"]=="espn-odds"
    assert row["fetched_at"]==row["captured_at"]
    assert abs(row["kickoff_utc"]-market_db)<1
    assert row["quote_phase"]=="current"
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("UPDATE odds_snapshots SET price=-105 WHERE id=?",(row["id"],))


def test_consensus_uses_one_fresh_quote_per_book_and_normalizes_lines():
    now=100000.0
    event=dict(home_team="Green Bay Packers",away_team="Atlanta Falcons")
    raw=[dict(book="FanDuel Sportsbook",market="spreads",selection="Green Bay Packers",
              line=-3.0,price=-110,fetched_at=now-20),
         dict(book="Bet 365",market="spread",selection="Atlanta Falcons",
              line=3.5,price=-110,fetched_at=now-20),
         dict(book="Draft Kings",market="spread",selection="Green Bay Packers",
              line=-4.0,price=-110,fetched_at=now-20),
         dict(book="DraftKings",market="spread",selection="Green Bay Packers",
              line=-9.0,price=-110,fetched_at=now-1000)]
    rows=[normalize_quote(row,event,provider="fixture",kickoff=now+3600) for row in raw]
    result=consensus(rows,event,as_of=now,max_age_s=300)
    assert result["spread"]["n_books"]==3
    assert result["spread"]["home_handicap"]==-3.5
    assert set(result["spread"]["books"])=={"fanduel","bet365","draftkings"}


def test_closing_label_requires_a_near_kickoff_pregame_timestamp():
    kickoff=100000.0
    old=dict(book="draftkings",market="total",selection="Over",line=44.5,
             price=-110,provider="espn-odds",fetched_at=kickoff-7200,
             kickoff_utc=kickoff,quote_phase="current")
    assert closing_observation([old],kickoff,"draftkings","total","Over") is None
    near=dict(old,fetched_at=kickoff-480,line=45.5)
    chosen=closing_observation([old,near],kickoff,"draftkings","total","Over")
    assert chosen["line"]==45.5
    assert chosen["quote_phase"]=="closing"
    assert chosen["fetched_at"]<kickoff
    after=dict(near,fetched_at=kickoff+60,line=46.5)
    assert closing_observation([old,near,after],kickoff,"draftkings","total","Over")["line"]==45.5
    unclassified=dict(near,quote_phase="unclassified",line=48.5)
    assert closing_observation([unclassified],kickoff,"draftkings","total","Over") is None


def test_odds_api_multibook_fixture_and_contradictory_book_refusal():
    event=dict(home_team="Green Bay Packers",away_team="Atlanta Falcons")
    offers=[]
    for book,spread in (("fanduel",-3.0),("bet365",-3.5),
                        ("draftkings",-4.0),("caesars",-5.0)):
        away_line=-spread if book!="caesars" else 4.0
        offers.append(dict(key=book,markets=[dict(key="spreads",outcomes=[
            dict(name=event["home_team"],point=spread,price=-110),
            dict(name=event["away_team"],point=away_line,price=-110)])]))
    raw=dict(id="api-event",bookmakers=offers)
    adapter=TheOddsApi(api_key="fixture-key")
    rows=[normalize_quote(dict(row,fetched_at=1000),event,"theoddsapi",kickoff=2000)
          for row in adapter._flatten(raw,"NFL")]
    result=consensus(rows,event,as_of=1001,max_age_s=10)
    assert result["spread"]["n_books"]==3
    assert result["spread"]["home_handicap"]==-3.5
    assert "caesars" not in result["spread"]["books"]
