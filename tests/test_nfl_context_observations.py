"""Prospective context retains retrieval provenance and rejects backfills."""
import sqlite3
import time

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal.nfl_context_observations import capture_snapshot,record


def test_context_is_append_only_and_requires_pregame_observation(tmp_path,monkeypatch):
    if db._conn is not None:
        db._conn.close();db._conn=None
    monkeypatch.setattr(settings,"database_url",f"sqlite:///{tmp_path/'context.db'}")
    db.init()
    now=time.time()
    item=dict(game_id="nfl-2026-reg-03-atl-gb",kind="qb_start",
              subject="Green Bay Packers",payload={"player":"Example QB","status":"announced"},
              source_url="https://team.example/announcement",source_published_at=now-60,
              fetched_at=now,kickoff_utc=now+3600)
    key=record(item,now=now)
    assert key==record(item,now=now)
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_context_observations")["n"]==1
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_research_context_observations")
    with pytest.raises(ValueError,match="before kickoff"):
        record(dict(item,fetched_at=now+3700),now=now+3700)
    with pytest.raises(ValueError,match="publication"):
        record(dict(item,source_published_at=now+1),now=now)
    snapshot=dict(game={"id":item["game_id"],"kickoff_utc":now+3600},
                  injuries=[dict(player="Example QB",team="Green Bay Packers",position="QB",
                                 status="Questionable",retrieved_at=now,
                                 source_url="https://espn.example/injuries")],
                  weather=dict(indoor=False,wind_mph=20,valid_for=now+3600,
                               retrieved_at=now))
    assert len(capture_snapshot(snapshot,now=now))==2
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_context_observations")["n"]==3
    assert capture_snapshot(snapshot,now=now+3600)==[]
    db._conn.close();db._conn=None
