"""Future freezes need a complete, sealed data/model bundle before registration."""
import gzip
import hashlib
import json
import os
import time
from datetime import datetime,timezone

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression,Ridge

from edgeterminal import db
from edgeterminal.config import ROOT,settings
from edgeterminal.nfl_forward_grading import register_freeze
from edgeterminal.nfl_model_registry import register as register_model,versions
from edgeterminal.nfl_repro_bundle import BundleStage,sha256,verify_bundle


@pytest.fixture
def future_workspace(tmp_path,monkeypatch):
    if db._conn is not None:
        db._conn.close();db._conn=None
    monkeypatch.setattr(settings,"database_url",f"sqlite:///{tmp_path/'research.db'}")
    monkeypatch.setattr(settings,"nflverse_cache_dir",tmp_path/"pbp")
    db.init()
    cache=tmp_path/"mutable_cache"
    cache.mkdir()
    for name in ("games.csv.gz","play_by_play_2026.csv.gz"):
        with gzip.open(cache/name,"wb") as handle:
            handle.write(f"fixture bytes for {name}".encode())
    yield tmp_path,cache
    db._conn.close();db._conn=None


def _models():
    x=np.asarray([[0.],[1.],[2.],[3.]])
    return {"linear":(Ridge(alpha=100).fit(x,[2,4,6,8]),
                      Ridge(alpha=100).fit(x,[40,42,44,46]),
                      LogisticRegression(C=.1).fit(x,[0,0,1,1]))}


def _payload(kickoff,now):
    return dict(status="RESEARCH_ONLY",designation="PASS",season=2026,week=4,
                family="linear",frozen_at=now-60,training_games=1,
                code_sha256=sha256(ROOT/"edgeterminal/nfl_research.py"),
                feature_version="fixture-features-1",
                feature_hash=hashlib.sha256(json.dumps(("x",)).encode()).hexdigest(),
                model_version="linear-fixture",games=[dict(
                    game_id="2026_04_ATL_GB",home="Green Bay Packers",
                    away="Atlanta Falcons",kickoff_et=datetime.fromtimestamp(
                        kickoff,timezone.utc).isoformat(),
                    model=dict(margin=3.,total=45.,home_win_prob=.6),
                    features={"x":1.},frozen_quote={})])


def _seal(tmp_path,cache,kickoff=None):
    now=time.time()
    kickoff=now+3600 if kickoff is None else kickoff
    out=tmp_path/"forward_2026_week04.json"
    with BundleStage(cache,out,[2026]) as stage:
        original=stage.inputs.joinpath("games.csv.gz").read_bytes()
        (cache/"games.csv.gz").write_bytes(b"source changed after snapshot")
        assert stage.inputs.joinpath("games.csv.gz").read_bytes()==original
        payload=_payload(kickoff,now)
        payload["source_sha256"]=stage.input_hashes.copy()
        seal=stage.seal(payload,[dict(season=2026,week=3)],_models(),
                        ("x",),{"fixture":("x",)},"a"*40)
    return out,seal,original


def test_complete_future_bundle_seals_and_registers(future_workspace):
    tmp_path,cache=future_workspace
    out,seal,original=_seal(tmp_path,cache)
    verification=verify_bundle(out,deep=True)
    assert verification["bundle_id"]==seal["bundle_id"]
    assert verification["verified_artifacts"]>=9
    directory=tmp_path/"forward_bundles"/seal["bundle_id"]
    assert (directory/"inputs/games.csv.gz").read_bytes()==original
    assert os.stat(out).st_ino!=os.stat(directory/"predictions.json").st_ino
    assert sha256(directory/"estimators.joblib")==json.loads(out.read_text())["model_hash"]
    assert len(__import__("joblib").load(directory/"estimators.joblib"))==1
    digest=register_freeze(out)
    assert digest==seal["freeze_sha256"]
    att=db.q1("SELECT * FROM nfl_research_bundle_attestations WHERE freeze_sha=?",(digest,))
    assert att["manifest_sha256"]==seal["manifest_sha256"]
    assert register_freeze(out)==digest
    version=register_model(digest,"a"*40,2026,4,version="linear-fixture-2026w04")
    assert versions()[0]["version_id"]==version
    assert versions()[0]["status"]=="research"


def test_changed_input_artifact_refuses_future_registration(future_workspace):
    tmp_path,cache=future_workspace
    out,seal,_=_seal(tmp_path,cache)
    source=tmp_path/"forward_bundles"/seal["bundle_id"]/"inputs/games.csv.gz"
    size=source.stat().st_size
    source.chmod(0o644)
    source.write_bytes(b"x"*size)
    with pytest.raises(ValueError,match="hash check"):
        register_freeze(out)
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_freezes")["n"]==0
    assert db.q1("SELECT COUNT(*) n FROM nfl_research_bundle_attestations")["n"]==0


def test_frozen_market_artifact_preserves_exact_source_quote(future_workspace):
    tmp_path,cache=future_workspace
    now=time.time()
    captured=now-120
    kickoff=now+3600
    db.ex("INSERT INTO odds_snapshots(event_id,sport,book,market,selection,line,price,"
          "captured_at,source,is_demo,provider,fetched_at,kickoff_utc,quote_phase) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          ("nfl-2026-reg-04-atl-gb","NFL","draftkings","spread",
           "Green Bay Packers",-3.5,-110,captured,"fixture",0,"fixture",
           captured,kickoff,"current"))
    quote_id=db.q1("SELECT id FROM odds_snapshots")["id"]
    out=tmp_path/"forward_2026_week04.json"
    with BundleStage(cache,out,[2026]) as stage:
        payload=_payload(kickoff,now)
        payload["source_sha256"]=stage.input_hashes.copy()
        payload["games"][0]["frozen_quote"]=dict(
            source="observed_current_market",home_handicap=-3.5,
            home_spread_price=-110,spread_book="draftkings",
            spread_captured_at=captured,spread_source_quote_id=quote_id)
        seal=stage.seal(payload,[dict(season=2026,week=3)],_models(),
                        ("x",),{},"a"*40)
    market=json.loads((tmp_path/"forward_bundles"/seal["bundle_id"]/"market.json").read_text())
    assert market["games"][0]["source_rows"][0]["id"]==quote_id
    assert market["games"][0]["source_rows"][0]["price"]==-110
    assert verify_bundle(out,deep=True)["bundle_id"]==seal["bundle_id"]


def test_bundle_cannot_publish_after_kickoff(future_workspace):
    tmp_path,cache=future_workspace
    out=tmp_path/"forward_2026_week04.json"
    now=time.time()
    with BundleStage(cache,out,[2026]) as stage:
        payload=_payload(now-1,now-100)
        payload["source_sha256"]=stage.input_hashes.copy()
        with pytest.raises(ValueError,match="after kickoff"):
            stage.seal(payload,[dict(season=2026,week=3)],_models(),
                       ("x",),{},"a"*40)
    assert not out.exists()
    assert not list((tmp_path/"forward_bundles").iterdir())


def test_failed_estimator_serialization_cannot_publish(future_workspace,monkeypatch):
    tmp_path,cache=future_workspace
    out=tmp_path/"forward_2026_week04.json"
    with BundleStage(cache,out,[2026]) as stage:
        payload=_payload(time.time()+3600,time.time())
        payload["source_sha256"]=stage.input_hashes.copy()
        monkeypatch.setattr("edgeterminal.nfl_repro_bundle.joblib.dump",
                            lambda *args,**kw: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError,match="disk full"):
            stage.seal(payload,[dict(season=2026,week=3)],_models(),
                       ("x",),{},"a"*40)
    assert not out.exists()
    assert not list((tmp_path/"forward_bundles").iterdir())


def test_weekly_freeze_trains_from_snapshot_and_publishes_only_sealed_bundle(
        future_workspace,monkeypatch):
    from edgeterminal import nfl_forward_research as research
    tmp_path,cache=future_workspace
    for season in range(2019,2026):
        with gzip.open(cache/f"play_by_play_{season}.csv.gz","wb") as handle:
            handle.write(f"season {season}".encode())
    now=time.time()
    def schedule(cache_path):
        assert str(cache_path).startswith(str(tmp_path/"forward_bundles"))
        assert (cache_path/"games.csv.gz").exists()
        return [dict(season="2026",week="3",game_type="REG",
                     game_id="2026_03_ATL_GB",home_team="Green Bay Packers",
                     away_team="Atlanta Falcons",home_score="24",away_score="20"),
                dict(season="2026",week="4",game_type="REG",
                     game_id="2026_04_ATL_GB",home_team="Green Bay Packers",
                     away_team="Atlanta Falcons",home_score="",away_score="",
                     gameday=datetime.fromtimestamp(now+3600,timezone.utc).date().isoformat(),
                     gametime=datetime.fromtimestamp(now+3600,timezone.utc).strftime("%H:%M"))]
    prior=dict(season=2026,week=3,game_id="2026_03_ATL_GB",
               home="Green Bay Packers",away="Atlanta Falcons",
               metrics={"Green Bay Packers":{"epa":1},"Atlanta Falcons":{"epa":1}})
    monkeypatch.setattr(research,"code_commit",lambda:"a"*40)
    monkeypatch.setattr(research,"load_schedules",schedule)
    monkeypatch.setattr(research.lab,"build_dataset",lambda cache,seasons:[prior])
    monkeypatch.setattr(research.lab,"_team_snapshot",lambda past,team:{"opponents":[]})
    monkeypatch.setattr(research.lab,"_features_before",
                        lambda past,game,snapshots:({name:0.0 for name in research.lab.FEATURES},None))
    x=np.zeros((4,len(research.lab.FEATURES)))
    x[:,0]=np.arange(4)
    models={"linear":(Ridge().fit(x,[1,2,3,4]),Ridge().fit(x,[41,42,43,44]),
                      LogisticRegression().fit(x,[0,0,1,1]))}
    monkeypatch.setattr(research.lab,"_models",lambda *args:models["linear"])
    monkeypatch.setattr(research,"baselines_before",lambda *args:{})
    monkeypatch.setattr(research,"_current_quote",lambda *args:None)
    out=tmp_path/"forward_2026_week04.json"
    result=research.freeze(cache,out,2026,4,"linear")
    assert result["games"]==1 and result["training_games"]==1
    assert result["bundle_id"]==json.loads(out.read_text())["reproducibility_bundle_id"]
    assert verify_bundle(out,deep=True)["manifest_sha256"]==result["bundle_manifest_sha256"]
    assert register_freeze(out)==sha256(out)
