"""Weekly orchestration cannot overwrite an existing freeze or start late."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from edgeterminal import nfl_forward_weekly as weekly
from edgeterminal.nfl_forward_research import _frozen_quote,_write_freeze


def _game(kickoff):
    stamp=datetime.fromtimestamp(kickoff,ZoneInfo("America/New_York"))
    return dict(season="2026",week="3",game_type="REG",gameday=stamp.date().isoformat(),
                gametime=stamp.time().strftime("%H:%M"))


def test_existing_week3_is_never_regenerated(tmp_path,monkeypatch):
    now=1790170000.0
    out=tmp_path/"forward_2026_week03.json"
    out.write_text("original")
    monkeypatch.setattr(weekly,"load_schedules",lambda *args,**kw:[_game(now+3600)])
    monkeypatch.setattr(weekly,"freeze",lambda *args,**kw: (_ for _ in ()).throw(
        AssertionError("must not regenerate")))
    monkeypatch.setattr(weekly,"sync_all",lambda *args,**kw:{"sync":[]})
    result=weekly.run(cache=tmp_path,out_dir=tmp_path,now=now,refresh_sources=False)
    assert result["freeze"]=="already exists; unchanged"
    assert out.read_text()=="original"


def test_weekly_refreshes_only_the_live_schedule(tmp_path,monkeypatch):
    now=1790170000.0
    (tmp_path/"forward_2026_week03.json").write_text("original")
    monkeypatch.setattr(weekly,"load_schedules",lambda *args,**kw:[_game(now+3600)])
    calls=[]
    monkeypatch.setattr(weekly.engine,"refresh_schedule",
                        lambda sport:calls.append(sport) or {"ok":True})
    monkeypatch.setattr(weekly.engine,"refresh",lambda *args,**kw: (_ for _ in ()).throw(
        AssertionError("odds refresh is forbidden")))
    monkeypatch.setattr(weekly,"sync_all",lambda *args,**kw:{"sync":[]})
    weekly.run(cache=tmp_path,out_dir=tmp_path,now=now,refresh_sources=True)
    assert calls==["NFL"]


def test_weekly_runner_does_not_freeze_after_first_kickoff(tmp_path,monkeypatch):
    now=1790170000.0
    monkeypatch.setattr(weekly,"load_schedules",lambda *args,**kw:[_game(now-3600)])
    monkeypatch.setattr(weekly,"freeze",lambda *args,**kw: (_ for _ in ()).throw(
        AssertionError("late freeze")))
    monkeypatch.setattr(weekly,"sync_all",lambda *args,**kw:{"sync":[]})
    result=weekly.run(cache=tmp_path,out_dir=tmp_path,now=now,refresh_sources=False)
    assert "no week" in result["freeze"]


def test_future_freeze_preserves_book_spread_sign_and_same_batch_moneyline():
    now=1000.0
    quote=dict(home_spread_line=-3.5,total_line=44.5,
               home_spread_book="draftkings",total_book="draftkings",
               home_spread_price=-110,over_price=-105,
               home_spread_captured_at=now-60,total_captured_at=now-60,
               snapshot_id="source-snapshot")
    batch=[dict(market="moneyline",selection=team,book="draftkings",
                captured_at=now-60,price=price,id=i)
           for i,team,price in ((3,"Green Bay Packers",-150),(4,"Atlanta Falcons",130))]
    batch.extend([dict(id=1,market="spread",selection="Green Bay Packers",line=-3.5,
                       book="draftkings",captured_at=now-60,price=-110,source="espn-odds"),
                  dict(id=2,market="total",selection="Over",line=44.5,
                       book="draftkings",captured_at=now-60,price=-105,source="espn-odds")])
    result=_frozen_quote(quote,{"payload":json.dumps({"market":batch})},
                         "Green Bay Packers","Atlanta Falcons",now)
    assert result["home_handicap"]==-3.5
    assert result["home_moneyline_price"]==-150
    assert result["away_moneyline_price"]==130
    assert result["spread_source_quote_id"]==1


def test_atomic_freeze_file_is_complete_and_exclusive(tmp_path):
    path=tmp_path/"forward_2026_week04.json"
    _write_freeze(path,{"status":"RESEARCH_ONLY","designation":"PASS"})
    original=path.read_bytes()
    assert json.loads(original)["designation"]=="PASS"
    with pytest.raises(FileExistsError):
        _write_freeze(path,{"designation":"changed"})
    assert path.read_bytes()==original
    assert not list(tmp_path.glob(".forward-freeze-*"))
