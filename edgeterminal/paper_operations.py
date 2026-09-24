"""Read-only integrity and settlement audit for the prospective PAPER ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from urllib.parse import urlencode

from . import db, nfl_prop_result_ingest, paper_research
from .providers.espn import SCOREBOARD_URL


def _sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _availability_hash(row: dict) -> str:
    identity = json.dumps([row["game_id"], row["kind"], row["subject"], row["payload"],
                           row["source_url"], row["source_published_at"], row["fetched_at"],
                           row["valid_for_utc"], row["kickoff_utc"]], separators=(",", ":"))
    return _sha(identity)


def audit_positions(now: float | None = None) -> dict:
    """Verify stored bytes and joins without polling or changing any ledger row."""
    stamp = time.time() if now is None else now
    trigger_names = {row["name"] for row in db.q(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
        "('paper_position_no_update','paper_position_no_delete')")}
    policy = paper_research.active_policy()
    current_policy_sha = paper_research.policy_hash(policy)
    positions = []
    for position in db.q("SELECT * FROM paper_positions ORDER BY created_at,id"):
        issues = []
        expected_id = _sha(f"{position['policy_version']}:{position['freeze_sha']}:{position['quote_id']}")
        if position["id"] != expected_id:
            issues.append("position_id_mismatch")
        if len(trigger_names) != 2:
            issues.append("position_immutability_trigger_missing")
        registered = db.q1("SELECT * FROM paper_policies WHERE version=?", (position["policy_version"],))
        if not registered or registered["sha256"] != position["policy_hash"] or \
                _sha(registered["payload"]) != registered["sha256"]:
            issues.append("policy_provenance_mismatch")
        if position["policy_version"] == policy["version"] and position["policy_hash"] != current_policy_sha:
            issues.append("active_policy_hash_mismatch")
        freeze = db.q1("SELECT * FROM nfl_prop_research_freezes WHERE freeze_sha=?", (position["freeze_sha"],))
        frozen_quote = None
        if not freeze or _sha(freeze["payload"]) != position["freeze_sha"]:
            issues.append("freeze_sha_mismatch")
        pairing = db.q1("SELECT * FROM paper_prop_quote_pairings WHERE quote_id=?", (position["quote_id"],))
        grade_quote_id = position["quote_id"]
        metadata = None
        if pairing:
            if pairing["freeze_sha"] != position["freeze_sha"] or \
                    _sha(pairing["payload"]) != pairing["pair_sha256"]:
                issues.append("pairing_sha_mismatch")
            else:
                paired = json.loads(pairing["payload"])
                grade_quote_id = pairing["frozen_quote_id"]
                metadata = paired.get("metadata")
                if paired.get("frozen_quote_id") != grade_quote_id or \
                        paired.get("quote", {}).get("quote_id") != position["quote_id"]:
                    issues.append("pairing_identity_mismatch")
        if freeze and _sha(freeze["payload"]) == position["freeze_sha"]:
            frozen_payload = json.loads(freeze["payload"])
            matches = [q for q in frozen_payload.get("projections", []) if q.get("quote_id") == grade_quote_id]
            if len(matches) != 1:
                issues.append("frozen_grade_quote_missing")
            else:
                frozen_quote = matches[0]
                if any(frozen_quote.get(key) != position[field] for key, field in
                       (("canonical_game_id", "game_id"), ("player_id", "player_id"),
                        ("player_name", "player_name"), ("market", "market"),
                        ("projection", "model_projection"), ("model_version", "model_version"))):
                    issues.append("frozen_projection_identity_mismatch")
                if not frozen_quote.get("gsis_player_id"):
                    issues.append("official_player_id_missing")
            if metadata:
                signature = metadata.get("model_hash") or metadata.get("model_signature")
                if signature != position["model_hash"]:
                    issues.append("model_signature_mismatch")
        quote = db.q1("SELECT * FROM nfl_prop_snapshots WHERE id=?", (position["quote_id"],))
        if not quote or any(quote[key] != position[field] for key, field in
                            (("canonical_game_id", "game_id"), ("player_id", "player_id"),
                             ("market", "market"), ("side", "side"), ("sportsbook", "sportsbook"),
                             ("line", "line"), ("price", "odds"))) or \
                (quote and quote["provider_updated_at"] != position["quote_observed_at"]):
            issues.append("source_quote_mismatch")
        elif not quote["retrieved_at"] <= position["created_at"] < position["kickoff_utc"]:
            issues.append("position_timing_mismatch")
        if quote:
            batch = db.q1("SELECT * FROM nfl_prop_capture_batches WHERE batch_id=?", (quote["batch_id"],))
            raw = db.q("SELECT body,body_sha256 FROM nfl_prop_raw_responses WHERE batch_id=?",
                       (quote["batch_id"],))
            if not batch or _sha(batch["raw_payload"]) != batch["raw_sha256"] or \
                    not raw or any(_sha(r["body"]) != r["body_sha256"] for r in raw):
                issues.append("raw_provider_source_mismatch")
        if frozen_quote and quote and frozen_quote.get("uncertainty_sigma", 0) > 0:
            over = .5 * math.erfc((position["line"] - position["model_projection"]) /
                                  (frozen_quote["uncertainty_sigma"] * math.sqrt(2)))
            probability = over if position["side"] == "over" else 1 - over
            if abs(probability - position["model_probability"]) > 2e-6:
                issues.append("model_probability_mismatch")
        availability = db.q1("SELECT * FROM nfl_research_context_observations WHERE observation_sha=?",
                             (position["availability_observation_id"],))
        if not availability or _availability_hash(availability) != availability["observation_sha"] or \
                availability["fetched_at"] != position["availability_observed_at"] or \
                availability["source_url"] != position["availability_source_url"] or \
                availability["game_id"] != position["game_id"] or \
                availability["subject"] != position["player_name"] or \
                paper_research.classify_availability(json.loads(availability["payload"]).get("status")) != "available":
            issues.append("availability_evidence_mismatch")
        season, week = int(position["game_id"].split("-")[1]), int(position["game_id"].split("-")[3])
        score_url = SCOREBOARD_URL + "?" + urlencode({"dates": season, "seasontype": 2,
                                                       "week": week, "limit": 100})
        latest = db.q1("SELECT result FROM paper_settlement_revisions WHERE position_id=? "
                       "ORDER BY revision DESC LIMIT 1", (position["id"],))
        grade_count = db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions WHERE freeze_sha=? AND quote_id=?",
                            (position["freeze_sha"], grade_quote_id))["n"]
        positions.append({**{key: position[key] for key in
                            ("id", "player_name", "game_id", "market", "side", "sportsbook", "line", "odds",
                             "model_projection", "model_probability", "market_probability",
                             "probability_difference", "quote_observed_at", "created_at",
                             "time_to_kickoff_seconds", "availability_observed_at", "policy_version",
                             "policy_hash", "model_version", "model_hash", "freeze_sha")},
                          "grade_quote_id": grade_quote_id,
                          "integrity": "verified" if not issues else "failed",
                          "integrity_issues": issues,
                          "settlement_status": latest["result"] if latest else "pending",
                          "sourced_grade_revisions": grade_count,
                          "expected_final_game_source": score_url,
                          "expected_player_stat_source": nfl_prop_result_ingest.STATS_URL.format(season=season),
                          "participation_evidence_if_needed":
                              "reviewed NFL Game Book PDF and attestation for absent, zero, or inactive player"})
    return {"as_of": stamp, "read_only": True, "positions": positions,
            "verified": sum(p["integrity"] == "verified" for p in positions),
            "failed": sum(p["integrity"] == "failed" for p in positions)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only PAPER position audit")
    parser.parse_args()
    print(json.dumps(audit_positions(), sort_keys=True))


if __name__ == "__main__":
    main()
