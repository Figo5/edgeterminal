"""Sealed, content-verified inputs and fitted estimators for future NFL freezes.

Bundles contain joblib/pickle files. Load those files only from trusted local
storage after verifying the manifest; pickle is not a safe interchange format.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import pickle
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import joblib

from . import db, nfl_identity
from .config import ROOT, settings
from .integrity import parse_time

SCHEMA_VERSION = 1
REQUIRED = {"predictions.json", "market.json", "estimators.joblib",
            "training_rows.pkl", "hyperparameters.json", "feature_config.json",
            "environment.json"}


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda:handle.read(1024*1024),b""):
            digest.update(block)
    return digest.hexdigest()


def bundle_root():
    return Path(settings.nflverse_cache_dir).parent/"forward_bundles"


def code_commit():
    """Require a committed code tree; an unrelated local data edit is allowed."""
    paths=["edgeterminal", "requirements.txt", "requirements-model.txt"]
    dirty=subprocess.run(["git","status","--porcelain","--",*paths],cwd=ROOT,
                         capture_output=True,text=True,check=True).stdout
    if dirty.strip():
        raise ValueError("model code or requirements are uncommitted; refusing freeze")
    return subprocess.run(["git","rev-parse","HEAD"],cwd=ROOT,capture_output=True,
                          text=True,check=True).stdout.strip()


def _write_bytes(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _json_bytes(value):
    return (json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+"\n").encode()


def _copy_bytes(source, destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    with Path(source).open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src,dst,1024*1024)
        dst.flush()
        os.fsync(dst.fileno())


def _fsync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _jsonable(value):
    if value is None or isinstance(value,(str,int,float,bool)):
        return value
    if isinstance(value,(list,tuple)):
        return [_jsonable(x) for x in value]
    if isinstance(value,dict):
        return {str(k):_jsonable(v) for k,v in value.items()}
    return {"class":f"{type(value).__module__}.{type(value).__qualname__}",
            "repr":repr(value)}


def _market_artifact(payload):
    records=[]
    for game in payload["games"]:
        quote=game.get("frozen_quote") or {}
        canonical=(f"nfl-{payload['season']}-reg-{payload['week']:02d}-"
                   f"{nfl_identity.team_code(game['away'])}-{nfl_identity.team_code(game['home'])}")
        source_rows=[]
        specs=(("spread_source_quote_id","home_handicap","home_spread_price","spread",
                "spread_captured_at",game["home"]),
               ("total_source_quote_id","total","over_price","total","total_captured_at","Over"),
               ("moneyline_home_source_quote_id","home_moneyline_price","home_moneyline_price",
                "moneyline","moneyline_captured_at",game["home"]),
               ("moneyline_away_source_quote_id","away_moneyline_price","away_moneyline_price",
                "moneyline","moneyline_captured_at",game["away"]))
        if quote.get("source")=="observed_current_market":
            for id_key,value_key,price_key,market,time_key,selection in specs:
                source_id=quote.get(id_key)
                if quote.get(value_key) is not None and source_id is None:
                    raise ValueError(f"market quote lacks {id_key}")
                if source_id is None:
                    continue
                row=db.q1("SELECT * FROM odds_snapshots WHERE id=?",(source_id,))
                actual=(row["price"] if market=="moneyline" else row["line"]) if row else None
                if (row is None or row["event_id"]!=canonical or row["market"]!=market or
                        row["selection"]!=selection or actual!=quote.get(value_key) or
                        row["price"]!=quote.get(price_key) or
                        row["fetched_at"]!=quote.get(time_key) or
                        row["fetched_at"]>payload["frozen_at"]):
                    raise ValueError("frozen market quote has no matching source row")
                source_rows.append(row)
        snapshot=None
        if quote.get("source_snapshot_id") is not None:
            saved=db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=?",
                        (quote["source_snapshot_id"],))
            if saved is None:
                raise ValueError("frozen market source snapshot is missing")
            snapshot=dict(id=quote["source_snapshot_id"],payload=saved["payload"])
        records.append(dict(game_id=game["game_id"],canonical_id=canonical,
                            frozen_quote=quote,source_rows=source_rows,
                            source_snapshot=snapshot))
    return dict(schema_version=1,as_of=payload["frozen_at"],games=records)


def _environment():
    packages=sorted((dict(name=d.metadata.get("Name") or "unknown",version=d.version)
                     for d in importlib.metadata.distributions()),
                    key=lambda item:item["name"].lower())
    return dict(python=sys.version,implementation=platform.python_implementation(),
                executable=sys.executable,platform=platform.platform(),packages=packages)


class BundleStage:
    """Copy input bytes first, train from the copies, then publish atomically."""

    def __init__(self, cache, out, seasons, root=None):
        self.cache=Path(cache)
        self.out=Path(out)
        self.root=Path(root) if root is not None else bundle_root()
        self.seasons=list(seasons)
        self.path=None
        self.input_hashes={}
        self.published=False

    def __enter__(self):
        self.root.mkdir(parents=True,exist_ok=True)
        self.path=Path(tempfile.mkdtemp(prefix=".staging-",dir=self.root))
        try:
            names=["games.csv.gz"]+[f"play_by_play_{season}.csv.gz" for season in self.seasons]
            for name in names:
                destination=self.path/"inputs"/name
                _copy_bytes(self.cache/name,destination)
                self.input_hashes[name]=sha256(destination)
            return self
        except Exception:
            self.__exit__(*sys.exc_info())
            raise

    @property
    def inputs(self):
        return self.path/"inputs"

    def seal(self, payload, past, fit, feature_names, groups, commit):
        if self.published or self.out.exists():
            raise FileExistsError("research freeze already exists")
        if not re.fullmatch(r"[0-9a-f]{40}",commit):
            raise ValueError("code commit must be a full Git SHA")
        first=min(parse_time(g["kickoff_et"]) for g in payload["games"])
        if time.time()>=first or payload["frozen_at"]>=first:
            raise ValueError("cannot seal a reproducibility bundle after kickoff")
        if payload["source_sha256"]!=self.input_hashes or payload["training_games"]!=len(past):
            raise ValueError("bundle inputs or training count do not match freeze")
        expected_feature_hash=hashlib.sha256(json.dumps(tuple(feature_names)).encode()).hexdigest()
        if payload.get("feature_hash")!=expected_feature_hash:
            raise ValueError("feature schema hash does not match fitted model inputs")
        if payload.get("code_sha256")!=sha256(ROOT/"edgeterminal/nfl_research.py"):
            raise ValueError("model code hash does not match bundled code")
        bundle_id=uuid.uuid4().hex
        payload["reproducibility_bundle_id"]=bundle_id
        payload["code_commit"]=commit
        payload["training_cutoff"]=payload["frozen_at"]
        with (self.path/"training_rows.pkl").open("xb") as handle:
            pickle.dump(past,handle,protocol=5)
            handle.flush();os.fsync(handle.fileno())
        model_path=self.path/"estimators.joblib"
        files=joblib.dump(fit,model_path,compress=3)
        if len(files)!=1 or not model_path.is_file():
            raise ValueError("fitted estimators did not serialize to one artifact")
        with model_path.open("rb") as handle:os.fsync(handle.fileno())
        payload["model_hash"]=sha256(model_path)
        hyper={name:{target:_jsonable(estimator.get_params(deep=True)) for target,estimator
                     in zip(("margin","total","win_probability"),models)}
               for name,models in fit.items()}
        _write_bytes(self.path/"hyperparameters.json",_json_bytes(hyper))
        _write_bytes(self.path/"feature_config.json",_json_bytes(dict(
            schema_version=payload["feature_version"],features=list(feature_names),
            groups={k:list(v) for k,v in groups.items()},numeric_dtype="float64")))
        _write_bytes(self.path/"environment.json",_json_bytes(_environment()))
        _write_bytes(self.path/"market.json",_json_bytes(_market_artifact(payload)))
        for source in sorted((ROOT/"edgeterminal").rglob("*.py")):
            relative=source.relative_to(ROOT)
            _copy_bytes(source,self.path/"code"/relative)
        for source in sorted(ROOT.glob("requirements*.txt")):
            _copy_bytes(source,self.path/"code"/source.name)
        prediction_bytes=(json.dumps(payload,indent=2)+"\n").encode()
        _write_bytes(self.path/"predictions.json",prediction_bytes)
        artifacts={str(path.relative_to(self.path)):dict(sha256=sha256(path),bytes=path.stat().st_size)
                   for path in self.path.rglob("*") if path.is_file()}
        sealed_at=time.time()
        if sealed_at>=first:
            raise ValueError("bundle sealing crossed kickoff")
        manifest=dict(schema_version=SCHEMA_VERSION,bundle_id=bundle_id,
                      freeze_sha256=hashlib.sha256(prediction_bytes).hexdigest(),
                      season=payload["season"],week=payload["week"],family=payload["family"],
                      sealed_at=sealed_at,first_kickoff=first,
                      code_commit=commit,training_cutoff=payload["training_cutoff"],
                      training_games=len(past),feature_version=payload["feature_version"],
                      feature_hash=payload["feature_hash"],model_hash=payload["model_hash"],
                      source_sha256=self.input_hashes,artifacts=artifacts)
        manifest_bytes=_json_bytes(manifest)
        _write_bytes(self.path/"manifest.json",manifest_bytes)
        manifest_sha=hashlib.sha256(manifest_bytes).hexdigest()
        _write_bytes(self.path/"seal.json",_json_bytes(dict(
            bundle_id=bundle_id,manifest_sha256=manifest_sha,
            freeze_sha256=manifest["freeze_sha256"],sealed_at=sealed_at)))
        _fsync_dir(self.path)
        final=self.root/bundle_id
        if final.exists():
            raise FileExistsError("bundle ID already exists")
        os.rename(self.path,final)
        self.path=None
        for file in final.rglob("*"):
            if file.is_file(): file.chmod(0o444)
        for directory in sorted((p for p in final.rglob("*") if p.is_dir()),
                                key=lambda p:len(p.parts),reverse=True):
            directory.chmod(0o555)
        final.chmod(0o555)
        _fsync_dir(self.root)
        if time.time()>=first:
            raise ValueError("bundle finished after kickoff; freeze not published")
        self.out.parent.mkdir(parents=True,exist_ok=True)
        fd,temp=tempfile.mkstemp(prefix=".forward-freeze-",dir=self.out.parent)
        try:
            with os.fdopen(fd,"wb") as destination, (final/"predictions.json").open("rb") as source:
                shutil.copyfileobj(source,destination,1024*1024)
                destination.flush();os.fsync(destination.fileno())
            if sha256(temp)!=manifest["freeze_sha256"] or time.time()>=first:
                raise ValueError("freeze publication failed verification before kickoff")
            os.link(temp,self.out)
        finally:
            os.unlink(temp)
        _fsync_dir(self.out.parent)
        self.published=True
        return dict(path=str(self.out),bundle_id=bundle_id,manifest_sha256=manifest_sha,
                    freeze_sha256=manifest["freeze_sha256"])

    def __exit__(self,*_):
        if self.path is not None and self.path.exists():
            shutil.rmtree(self.path)


def verify_bundle(freeze_path, root=None, deep=True):
    freeze_path=Path(freeze_path)
    raw=freeze_path.read_bytes()
    payload=json.loads(raw)
    bundle_id=payload.get("reproducibility_bundle_id")
    if not isinstance(bundle_id,str) or not re.fullmatch(r"[0-9a-f]{32}",bundle_id):
        raise ValueError("future freeze lacks a valid reproducibility bundle ID")
    directory=(Path(root) if root is not None else bundle_root())/bundle_id
    try:
        manifest_bytes=(directory/"manifest.json").read_bytes()
        manifest=json.loads(manifest_bytes)
        seal=json.loads((directory/"seal.json").read_bytes())
    except (OSError,json.JSONDecodeError) as exc:
        raise ValueError("reproducibility bundle is missing or malformed") from exc
    freeze_sha=hashlib.sha256(raw).hexdigest()
    manifest_sha=hashlib.sha256(manifest_bytes).hexdigest()
    if (seal.get("manifest_sha256")!=manifest_sha or
            seal.get("freeze_sha256")!=freeze_sha or
            manifest.get("freeze_sha256")!=freeze_sha or
            seal.get("bundle_id")!=bundle_id or manifest.get("bundle_id")!=bundle_id or
            manifest.get("schema_version")!=SCHEMA_VERSION or
            manifest.get("sealed_at",float("inf"))>=manifest.get("first_kickoff",0) or
            manifest.get("training_cutoff")!=payload.get("training_cutoff") or
            manifest.get("training_games")!=payload.get("training_games") or
            manifest.get("source_sha256")!=payload.get("source_sha256") or
            manifest.get("code_commit")!=payload.get("code_commit") or
            manifest.get("model_hash")!=payload.get("model_hash") or
            manifest.get("feature_hash")!=payload.get("feature_hash")):
        raise ValueError("reproducibility bundle seal disagrees with freeze")
    artifacts=manifest.get("artifacts") or {}
    if not REQUIRED<=artifacts.keys() or not any(k.startswith("code/") for k in artifacts):
        raise ValueError("reproducibility bundle lacks required artifacts")
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in artifacts):
        raise ValueError("reproducibility bundle has unsafe artifact path")
    if {k.removeprefix("inputs/") for k in artifacts if k.startswith("inputs/")}!=set(payload["source_sha256"]):
        raise ValueError("reproducibility bundle input inventory differs")
    if not (directory/"predictions.json").is_file() or sha256(directory/"predictions.json")!=freeze_sha:
        raise ValueError("bundle prediction artifact is missing or changed")
    for name,expected in artifacts.items():
        file=directory/name
        if not file.is_file() or file.stat().st_size!=expected.get("bytes"):
            raise ValueError(f"reproducibility artifact is missing or truncated: {name}")
        if deep and sha256(file)!=expected.get("sha256"):
            raise ValueError(f"reproducibility artifact failed hash check: {name}")
    if deep:
        if any(artifacts[f"inputs/{name}"]["sha256"]!=expected for name,expected in
               payload["source_sha256"].items()):
            raise ValueError("input SHA-256 disagrees with frozen record")
        if (artifacts["predictions.json"]["sha256"]!=freeze_sha or
                artifacts["estimators.joblib"]["sha256"]!=payload["model_hash"]):
            raise ValueError("prediction or fitted model hash disagrees")
    return dict(bundle_id=bundle_id,bundle_path=str(directory),manifest_sha256=manifest_sha,
                freeze_sha256=freeze_sha,verified_artifacts=len(artifacts),deep=deep)


def main():
    parser=argparse.ArgumentParser(description="Verify a sealed NFL forward bundle")
    parser.add_argument("freeze_path")
    args=parser.parse_args()
    print(json.dumps(verify_bundle(args.freeze_path,deep=True),sort_keys=True))


if __name__=="__main__":
    main()
