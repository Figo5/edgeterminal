"""Append-only version identities for prospective NFL research freezes."""
from __future__ import annotations

import hashlib
import json
import time

from . import db


def register(freeze_sha: str, code_commit: str, first_eligible_season: int,
             first_eligible_week: int, training_cutoff: float | None = None,
             version: str | None = None) -> str:
    freeze = db.q1("SELECT * FROM nfl_research_freezes WHERE freeze_sha=?", (freeze_sha,))
    if freeze is None:
        raise ValueError("register the immutable freeze before its model version")
    cutoff = freeze["frozen_at"] if training_cutoff is None else float(training_cutoff)
    if cutoff > freeze["frozen_at"]:
        raise ValueError("training cutoff is after the freeze")
    fields = (freeze["family"], version or freeze["model_version"], code_commit, cutoff,
              freeze["feature_hash"], freeze["model_hash"],
              int(first_eligible_season), int(first_eligible_week))
    version_id = hashlib.sha256(json.dumps(fields,separators=(",", ":")).encode()).hexdigest()
    linked = db.q1("SELECT version_id FROM nfl_research_model_freeze_links WHERE freeze_sha=?",
                   (freeze_sha,))
    if linked and linked["version_id"] != version_id:
        raise ValueError("freeze is linked to another immutable model version")
    existing = db.q1("SELECT version_id FROM nfl_research_model_versions "
                     "WHERE model_name=? AND version=?",fields[:2])
    if existing and existing["version_id"] != version_id:
        raise ValueError("model name/version already identifies another configuration")
    stamp = time.time()
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO nfl_research_model_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (version_id,*fields,stamp))
        if not c.execute("SELECT 1 FROM nfl_research_model_version_events "
                         "WHERE version_id=? LIMIT 1",(version_id,)).fetchone():
            c.execute("INSERT INTO nfl_research_model_version_events"
                      "(version_id,status,last_eligible_season,last_eligible_week,recorded_at) "
                      "VALUES(?,?,?,?,?)",(version_id,"research",None,None,stamp))
        c.execute("INSERT OR IGNORE INTO nfl_research_model_freeze_links VALUES(?,?,?)",
                  (freeze_sha,version_id,stamp))
    return version_id


def retire(version_id: str, last_eligible_season: int, last_eligible_week: int) -> None:
    current = next((row for row in versions() if row["version_id"]==version_id),None)
    if current is None or current["status"]!="research":
        raise ValueError("only an active research version may be retired")
    if ((last_eligible_season,last_eligible_week) <
            (current["first_eligible_season"],current["first_eligible_week"])):
        raise ValueError("last eligible week precedes first eligible week")
    db.ex("INSERT INTO nfl_research_model_version_events"
          "(version_id,status,last_eligible_season,last_eligible_week,recorded_at) "
          "VALUES(?,?,?,?,?)",
          (version_id,"retired",int(last_eligible_season),int(last_eligible_week),time.time()))


def versions():
    return db.q("SELECT v.*, e.status,e.last_eligible_season,e.last_eligible_week,"
                "l.freeze_sha FROM nfl_research_model_versions v "
                "JOIN nfl_research_model_version_events e ON e.id=("
                "SELECT MAX(id) FROM nfl_research_model_version_events WHERE version_id=v.version_id) "
                "LEFT JOIN nfl_research_model_freeze_links l ON l.version_id=v.version_id "
                "ORDER BY v.first_eligible_season,v.first_eligible_week,v.registered_at")
