"""Timestamped prospective QB, injury, and weather observations.

These records are research inputs only. They are never retroactively inserted
into the historical model or used to alter an existing weekly freeze.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from . import db
from .integrity import parse_time

KINDS = {"qb_start", "qb_change", "qb_availability", "offensive_injury",
         "defensive_injury", "weather"}


def record(item, now=None):
    stamp=time.time() if now is None else now
    if item.get("kind") not in KINDS or not item.get("game_id") or not item.get("subject"):
        raise ValueError("invalid context identity")
    if not item.get("source_url") or not isinstance(item.get("payload"),dict):
        raise ValueError("context needs source and structured payload")
    try:
        fetched,kickoff=float(item["fetched_at"]),float(item["kickoff_utc"])
        published=(float(item["source_published_at"])
                   if item.get("source_published_at") is not None else None)
        valid_for=(float(item["valid_for_utc"])
                   if item.get("valid_for_utc") is not None else None)
    except (TypeError,ValueError,KeyError) as exc:
        raise ValueError("invalid context timestamp") from exc
    if not all(math.isfinite(x) for x in (fetched,kickoff)) or fetched>stamp+1 or fetched>=kickoff:
        raise ValueError("context must be observed before kickoff")
    if published is not None and (not math.isfinite(published) or published>fetched):
        raise ValueError("source publication cannot postdate observation")
    if valid_for is not None and not math.isfinite(valid_for):
        raise ValueError("invalid weather valid-for time")
    if item["kind"]=="weather" and (valid_for is None or abs(valid_for-kickoff)>3*3600):
        raise ValueError("weather forecast must cover game time")
    level="published_and_observed" if published is not None else "observed_only"
    payload=json.dumps(item["payload"],sort_keys=True,separators=(",",":"))
    identity=json.dumps([item["game_id"],item["kind"],item["subject"],payload,
                         item["source_url"],published,fetched,valid_for,kickoff],
                        separators=(",",":"))
    digest=hashlib.sha256(identity.encode()).hexdigest()
    db.ex("INSERT OR IGNORE INTO nfl_research_context_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (digest,item["game_id"],item["kind"],item["subject"],payload,
           item["source_url"],published,fetched,valid_for,kickoff,level))
    return digest


def capture_snapshot(snapshot, now=None):
    """Capture available detailed-event context; ignore unproven publication times."""
    game=snapshot.get("game") or {}
    kickoff=parse_time(game.get("kickoff_utc"))
    game_id=game.get("id")
    stamp=time.time() if now is None else now
    if not game_id or kickoff is None or stamp>=kickoff:
        return []
    written=[]
    for injury in snapshot.get("injuries") or []:
        position=str(injury.get("position") or "").upper()
        if position=="QB":
            kind="qb_availability"
        elif position in {"WR","RB","TE","FB","OT","OG","C","OL","LT","RT","LG","RG"}:
            kind="offensive_injury"
        elif position in {"DL","DT","DE","EDGE","LB","CB","S","DB","FS","SS"}:
            kind="defensive_injury"
        else:
            continue
        fetched=parse_time(injury.get("retrieved_at"))
        if fetched is None or fetched>stamp+1 or fetched>=kickoff:
            continue
        item=dict(game_id=game_id,kind=kind,
                  subject=str(injury.get("player") or injury.get("name") or ""),
                  payload={"team":injury.get("team"),"position":position,
                           "status":injury.get("status"),"injury":injury.get("injury")},
                  source_url=injury.get("source_url"),fetched_at=fetched,kickoff_utc=kickoff)
        if item["subject"] and item["source_url"]:
            written.append(record(item,now=stamp))
    weather=snapshot.get("weather") or {}
    if weather and not weather.get("indoor"):
        fetched=parse_time(weather.get("retrieved_at"))
        valid_for=parse_time(weather.get("valid_for"))
        if fetched is not None and valid_for is not None and fetched<=stamp+1 and fetched<kickoff:
            written.append(record(dict(game_id=game_id,kind="weather",subject="game_time",
                                       payload={key:weather.get(key) for key in
                                                ("temp_f","wind_mph","wind_gust_mph",
                                                 "precip_chance","conditions")},
                                       source_url="https://open-meteo.com/",
                                       fetched_at=fetched,valid_for_utc=valid_for,
                                       kickoff_utc=kickoff),now=stamp))
    return written
