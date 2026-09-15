"""Turn web research into bounded, provenance-carrying structured features.

The research layer never produces a probability, a price or a unit size. It
produces *modifiers* with a source attached, and deterministic code decides how
far they are allowed to move a projection:

* every multiplier is clamped to [MIN_MULTIPLIER, MAX_MULTIPLIER];
* a weak source (LOW/UNVERIFIED) can never move a feature more than
  WEAK_SOURCE_LIMIT;
* disagreeing confirming sources raise uncertainty instead of quietly picking
  the friendlier claim.
"""
from __future__ import annotations

import re
import time
from typing import Dict, Iterable, List, Optional

from .sources import RANK, HIGH, PRIMARY, classify, freshness, topics

MIN_MULTIPLIER = 0.70
MAX_MULTIPLIER = 1.25
WEAK_SOURCE_LIMIT = 0.06
STRENGTH_FACTOR = {PRIMARY: 1.0, HIGH: 1.0, "MEDIUM": 0.6, "LOW": 0.35, "UNVERIFIED": 0.25}
MAX_PENALTY_PER_CONFLICT = 0.15
MAX_TOTAL_PENALTY = 0.4
WEB_ODDS_MAX_AGE_S = 1800.0

# Claims about a team-mate or the whole unit still matter when researching a
# specific player; claims about another individual's role do not.
TEAM_LEVEL_RULES = {"teammate_ruled_out", "committee_unchanged"}

# Each rule maps a plain-language claim to a bounded change on a named feature.
# `direction` is only used for conflict detection and reporting.
CLAIM_RULES: List[Dict] = [
    dict(id="lead_back_promotion", direction="+",
         pattern=r"(lead|starting|feature|bell[- ]cow|workhorse)\s+(back|rb|runner)"
                 r"|promoted to (the )?starting|takes over as (the )?(lead|starting)",
         effects={"workload_multiplier": 0.15, "opportunity_share_multiplier": 0.12}),
    dict(id="snap_limit", direction="-",
         pattern=r"snap count|limited (workload|snaps|role)|won'?t (play|see) a full|"
                 r"workload (limit|cap)|pitch count|managed workload",
         effects={"workload_multiplier": -0.18, "availability_multiplier": -0.10}),
    dict(id="committee_unchanged", direction="0",
         pattern=r"committee (is )?(unchanged|remains|intact)|same (role|split)|"
                 r"no change (to|in) (his|the) (role|usage)",
         effects={"workload_multiplier": 0.0}),
    dict(id="teammate_ruled_out", direction="+",
         pattern=r"(wr1|wr2|rb1|te1|top (receiver|weapon)|number one (receiver|option))"
                 r"[^.]{0,40}(ruled out|out for|will miss|inactive|placed on ir)",
         effects={"target_share_multiplier": 0.15, "opportunity_share_multiplier": 0.08}),
    dict(id="expected_to_play", direction="+",
         pattern=r"expected to (play|suit up)|full (practice|participant)|"
                 r"no (injury )?(designation|game status)|will play",
         effects={"availability_multiplier": 0.05}),
    dict(id="injury_doubt", direction="-",
         pattern=r"questionable|game[- ]time decision|doubtful|limited (practice|participant)|"
                 r"did not practice|uncertain (to play|status)",
         effects={"availability_multiplier": -0.15}),
    dict(id="role_increase", direction="+",
         pattern=r"increased (role|snaps|usage|workload)|bigger role|"
                 r"(more|extra) (targets|carries|touches)",
         effects={"workload_multiplier": 0.10}),
    dict(id="role_decrease", direction="-",
         pattern=r"reduced (role|snaps|usage|workload)|smaller role|"
                 r"(fewer|less) (targets|carries|touches)|benched",
         effects={"workload_multiplier": -0.10}),
]


def _matching_rules(text: str) -> List[Dict]:
    found = []
    for rule in CLAIM_RULES:
        if re.search(rule["pattern"], text, re.I):
            found.append(rule)
    return found


def extract(items: Iterable[Dict], player: Optional[str] = None,
            now: Optional[float] = None) -> Dict:
    """Convert research items into bounded features plus agreement/conflict data."""
    stamp = time.time() if now is None else float(now)
    contributions: Dict[str, List[Dict]] = {}
    claims: List[Dict] = []
    for item in items or []:
        title = item.get("title") or ""
        snippet = item.get("snippet") or ""
        text = f"{title}. {snippet}"
        matched = _matching_rules(text)
        if player and player.lower() not in text.lower():
            # An individual role claim about somebody else must not move this
            # player's projection.
            matched = [rule for rule in matched if rule["id"] in TEAM_LEVEL_RULES]
        if not matched:
            continue
        verdict = classify(item.get("url"), title)
        reliability = item.get("reliability") or verdict["reliability"]
        strength = STRENGTH_FACTOR.get(reliability, 0.25)
        topic = (item.get("topics") or topics(item.get("url"), title))[0]
        topic = "depth_chart" if topic == "role" and "depth" in text.lower() else topic
        weight = freshness(item.get("published_at"), stamp, topic=topic)
        for rule in matched:
            for feature, delta in rule["effects"].items():
                scaled = delta * strength
                if RANK[reliability] >= RANK["LOW"] and abs(scaled) > WEAK_SOURCE_LIMIT:
                    scaled = WEAK_SOURCE_LIMIT if scaled > 0 else -WEAK_SOURCE_LIMIT
                # A claim nobody can date is worth less, but never worth nothing.
                scaled *= 0.75 + 0.25 * weight
                contributions.setdefault(feature, []).append(dict(
                    rule=rule["id"], direction=rule["direction"], delta=scaled,
                    reliability=reliability, url=item.get("url"), domain=verdict["domain"],
                    published_at=item.get("published_at"), retrieved_at=item.get("retrieved_at"),
                    topic=topic, freshness=weight, claim=title or snippet[:160]))
                claims.append(dict(rule=rule["id"], feature=feature, reliability=reliability,
                                   url=item.get("url"), claim=title or snippet[:160]))

    features: Dict[str, Dict] = {}
    conflicts: List[Dict] = []
    for feature, rows in contributions.items():
        # Reliability-weighted mean prevents stacking three articles about the
        # same beat report into one oversized adjustment.
        total_weight = sum(STRENGTH_FACTOR.get(r["reliability"], 0.25) for r in rows)
        blended = sum(r["delta"] * STRENGTH_FACTOR.get(r["reliability"], 0.25)
                      for r in rows) / total_weight if total_weight else 0.0
        value = min(MAX_MULTIPLIER, max(MIN_MULTIPLIER, 1.0 + blended))
        best = max(rows, key=lambda r: -RANK[r["reliability"]])
        features[feature] = dict(
            value=round(value, 4), delta=round(value - 1.0, 4),
            provenance=dict(source=best.get("domain"), url=best.get("url"),
                            published_at=best.get("published_at"),
                            retrieved_at=best.get("retrieved_at"),
                            reliability=best["reliability"], claim=best["claim"],
                            topic=best.get("topic")),
            claims=[dict(rule=r["rule"], reliability=r["reliability"], url=r["url"],
                         claim=r["claim"]) for r in rows])
        confirming = [r for r in rows if RANK[r["reliability"]] <= RANK[HIGH]]
        positives = [r for r in confirming if r["delta"] > 0.001]
        negatives = [r for r in confirming if r["delta"] < -0.001]
        if positives and negatives:
            conflicts.append(dict(feature=feature,
                                  for_claim=[dict(claim=r["claim"], url=r["url"],
                                                  reliability=r["reliability"])
                                             for r in positives],
                                  against_claim=[dict(claim=r["claim"], url=r["url"],
                                                      reliability=r["reliability"])
                                                 for r in negatives]))

    # Availability is conservative: disagreement must not raise it above neutral.
    if "availability_multiplier" in features:
        features["availability_multiplier"]["value"] = round(
            min(1.0, features["availability_multiplier"]["value"]), 4)

    penalty = min(MAX_TOTAL_PENALTY, MAX_PENALTY_PER_CONFLICT * len(conflicts))
    if conflicts and "availability_multiplier" in features:
        features["availability_multiplier"]["value"] = round(
            1.0 - MAX_PENALTY_PER_CONFLICT, 4)
    return dict(features=features, conflicts=conflicts, claims=claims,
                uncertainty_penalty=round(penalty, 4),
                source_agreement=round(1.0 - penalty, 4),
                multipliers={k: v["value"] for k, v in features.items()})


def web_fallback_quote(payload: Dict) -> Dict:
    """A price parsed out of a web page. Never mixed with structured odds.

    Eligible for an Official Pick only when the page carried a verifiable,
    recent timestamp and the parse was validated against the page text.
    """
    url = payload.get("url")
    verdict = classify(url, payload.get("source_label"))
    now = float(payload.get("now") or time.time())
    retrieved = float(payload.get("retrieved_at") or now)
    page_ts = payload.get("page_timestamp")
    age = (now - float(page_ts)) if page_ts is not None else None
    parsed = bool(payload.get("parsed"))
    reasons = []
    if page_ts is None:
        reasons.append("page timestamp unavailable — freshness cannot be verified")
    elif age is not None and age > WEB_ODDS_MAX_AGE_S:
        reasons.append(f"page timestamp is {age / 60:.0f} minutes old")
    if not parsed:
        reasons.append("price was not validated against the page text")
    if RANK[verdict["reliability"]] > RANK[HIGH]:
        reasons.append(f"source reliability is {verdict['reliability']}")
    return dict(player=payload.get("player"), market=payload.get("market"),
                price=payload.get("price"), line=payload.get("line"),
                source_type="WEB_FALLBACK", structured=False,
                url=url, source_label=payload.get("source_label"),
                reliability=verdict["reliability"], domain=verdict["domain"],
                page_timestamp=page_ts, retrieved_at=retrieved,
                age_s=round(age, 1) if age is not None else None,
                eligible_for_official=not reasons, rejection_reasons=reasons)