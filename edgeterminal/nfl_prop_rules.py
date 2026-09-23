"""Versioned, research-only interpretations of published NFL prop house rules."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .nfl_prop_lab import ROOT

DEFAULT_RULES = ROOT / "data/research/nfl_prop_rules_2026-09-23.json"
SCENARIOS = ("inactive", "active_zero", "leaves_early", "postponed", "canceled",
             "suspended", "stat_correction", "push")


def load_rules(path: str | Path = DEFAULT_RULES) -> tuple[str, dict]:
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    books = data.get("books")
    if not isinstance(books, dict) or set(books) != {
            "fanduel", "draftkings", "betmgm", "caesars", "espnbet", "bovada"}:
        raise ValueError("settlement registry must cover all frozen sportsbook labels")
    for book, rule in books.items():
        if any(not isinstance(rule.get(key), str) or not rule[key] for key in SCENARIOS):
            raise ValueError(f"incomplete settlement rule for {book}")
        if not rule.get("source_urls") or not rule.get("jurisdiction"):
            raise ValueError(f"settlement rule lacks provenance for {book}")
    return digest, data


def interpret(book: str, research_result: str, player_status: str,
              participation: dict | None, rules: dict) -> tuple[str, str]:
    """Return conditional settlement interpretation and rule scenario; never payout."""
    rule = rules["books"].get(book)
    if rule is None:
        return "undetermined", "unregistered_book"
    if research_result == "void":
        scenario = "canceled" if player_status == "not_applicable" else "inactive"
        policy = rule[scenario]
        if scenario == "canceled" and policy == "void_conditional":
            return "void", scenario
        if scenario == "inactive" and policy in {"void_if_no_snap", "void_if_no_participation",
                                                   "void_if_not_active_roster"}:
            return "void", scenario
        return "undetermined", scenario
    if rule["participation_requirement"] == "unknown":
        return "undetermined", "participation_or_jurisdiction_unverified"
    if rule["participation_requirement"] == "snap":
        if not participation or participation.get("snap_played") is not True:
            return "undetermined", "snap_unverified"
    if rule["participation_requirement"] == "active_roster":
        if not participation or participation.get("active_roster") is not True:
            return "undetermined", "active_roster_unverified"
        if book == "bovada" and participation.get("snap_played") is False:
            return "undetermined", "bovada_product_type_unverified"
    if research_result == "push":
        return ("push", "push") if rule["push"] == "refund_conditional" else ("undetermined", "push")
    if research_result in {"won", "lost"}:
        return research_result, "standard_completed_prop"
    return "undetermined", "unsupported_statistical_result"
