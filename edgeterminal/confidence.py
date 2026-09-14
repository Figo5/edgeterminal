"""Deterministic confidence framework.

Confidence is NOT an LLM opinion. It is a weighted combination of measurable
component scores, each in [0,1], then capped by data quality and finally
adjusted by historical calibration.

Confidence answers "how much do I trust this estimate", edge answers "how much
is the price wrong". They are kept separate on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# Tunable coefficients. Sum need not be 1.0 -- normalised at scoring time.
WEIGHTS: Dict[str, float] = {
    "edge_strength": 0.20,
    "model_agreement": 0.16,
    "data_quality": 0.14,
    "sample_size": 0.10,
    "market_liquidity": 0.09,
    "market_stability": 0.08,
    "availability_certainty": 0.10,  # injury / lineup
    "projection_stability": 0.07,    # variance of the projection itself
    "context_certainty": 0.06,       # weather / news / travel unknowns
}

# Hard ceilings by data quality tier. A CFB prop with no snap data must never
# be able to print 96.
QUALITY_CAPS = {"excellent": 96.0, "good": 90.0, "limited": 79.0, "poor": 64.0}

QUALITY_SCORE = {"excellent": 1.0, "good": 0.8, "limited": 0.5, "poor": 0.25}

CONF_BUCKETS = [(50, 59), (60, 69), (70, 79), (80, 84), (85, 89), (90, 94), (95, 100)]


def bucket_of(conf: float) -> str:
    for lo, hi in CONF_BUCKETS:
        if lo <= conf <= hi:
            return f"{lo}-{hi}"
    return "<50" if conf < 50 else "95-100"


def quality_tier(score: float) -> str:
    if score >= 0.9:
        return "excellent"
    if score >= 0.72:
        return "good"
    if score >= 0.45:
        return "limited"
    return "poor"


@dataclass
class ConfidenceInputs:
    """All values in [0,1] unless noted."""
    edge_pct: float = 0.0                 # probability points, can be negative
    model_agreement: float = 0.5
    data_quality: float = 0.6
    sample_size_n: int = 0                # observations backing the projection
    market_type: str = "spread"
    line_move_magnitude: float = 0.0      # in points or cents-equivalent
    books_quoting: int = 1
    availability_certainty: float = 0.8   # 1.0 = lineup fully confirmed
    projection_cv: float = 0.35           # coefficient of variation of projection
    context_certainty: float = 0.8        # weather/news knowns
    penalties: List[Dict] = field(default_factory=list)  # [{"reason":..,"points":..}]
    prior_season_share: float = 0.0
    unresolved_major_injury: bool = False


LIQUIDITY = {
    "moneyline": 1.0, "spread": 1.0, "total": 0.95, "team_total": 0.7,
    "player_prop": 0.55, "outright": 0.45, "top5": 0.5, "top10": 0.55,
    "top20": 0.55, "matchup": 0.65, "parlay": 0.5,
}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def components(ci: ConfidenceInputs) -> Dict[str, float]:
    edge = ci.edge_pct
    # 0 at <=0 edge, 1.0 around +8pts. Big edges are suspicious, not better,
    # so the curve flattens and then backs off above ~12 points.
    if edge <= 0:
        edge_strength = 0.0
    elif edge <= 8:
        edge_strength = edge / 8.0
    elif edge <= 12:
        edge_strength = 1.0
    else:
        edge_strength = max(0.45, 1.0 - (edge - 12) / 20.0)

    n = max(0, ci.sample_size_n)
    sample = _clamp((n / 24.0) ** 0.5) if n else 0.15

    liq_base = LIQUIDITY.get(ci.market_type, 0.6)
    books = _clamp((ci.books_quoting - 1) / 5.0)
    liquidity = _clamp(0.6 * liq_base + 0.4 * books)

    # A market that has barely moved is stable; big moves mean information we
    # may not have. Scale is market dependent.
    scale = 3.0 if ci.market_type in ("spread", "total") else 25.0
    stability = _clamp(1.0 - ci.line_move_magnitude / scale)

    proj_stability = _clamp(1.0 - (ci.projection_cv - 0.15) / 0.55)

    return {
        "edge_strength": _clamp(edge_strength),
        "model_agreement": _clamp(ci.model_agreement),
        "data_quality": _clamp(ci.data_quality),
        "sample_size": sample,
        "market_liquidity": liquidity,
        "market_stability": stability,
        "availability_certainty": _clamp(ci.availability_certainty),
        "projection_stability": proj_stability,
        "context_certainty": _clamp(ci.context_certainty),
    }


@dataclass
class ConfidenceResult:
    score: float
    raw_score: float
    tier: str
    components: Dict[str, float]
    contributions: Dict[str, float]
    penalties: List[Dict]
    calibration_delta: float
    cap_applied: Optional[float]
    version: str


def score(ci: ConfidenceInputs, calibration_delta: float = 0.0,
          version: str = "et-conf-0.4.0") -> ConfidenceResult:
    comps = components(ci)
    total_w = sum(WEIGHTS.values())
    contributions = {k: WEIGHTS[k] * v / total_w for k, v in comps.items()}
    raw = 100.0 * sum(contributions.values())

    # Re-centre: a fully mediocre input set (all 0.5) should land near 55, not
    # 50, and a strong one near 92. Mild expansion around the midpoint.
    expanded = 50.0 + (raw - 50.0) * 1.35
    out = expanded

    for p in ci.penalties:
        out -= float(p.get("points", 0))

    # These are evidence ceilings, not soft weights. Strong edge cannot compensate
    # for missing independent market evidence or unresolved high-impact availability.
    evidence_caps: List[float] = []
    if ci.books_quoting <= 1:
        evidence_caps.append(79.0)
    if ci.model_agreement < 0.4:
        evidence_caps.append(79.0)
    if ci.unresolved_major_injury:
        evidence_caps.append(74.0)
    prior_share = _clamp(ci.prior_season_share)
    if prior_share:
        out -= 8.0 * prior_share
        if prior_share >= 0.5:
            evidence_caps.append(79.0)

    out += calibration_delta

    tier = quality_tier(ci.data_quality)
    cap = QUALITY_CAPS[tier]
    if evidence_caps:
        cap = min(cap, *evidence_caps)
    cap_applied = cap if out > cap else None
    out = min(out, cap)
    out = max(0.0, min(100.0, out))

    return ConfidenceResult(
        score=round(out, 1), raw_score=round(raw, 1), tier=tier,
        components=comps, contributions={k: round(v * 100, 2) for k, v in contributions.items()},
        penalties=list(ci.penalties), calibration_delta=round(calibration_delta, 2),
        cap_applied=cap_applied, version=version,
    )


def agreement_from_signals(signals: Sequence[float]) -> float:
    """Agreement in [0,1] from sub-model directional signals in [-1,1].

    All pointing the same way and strongly -> near 1. Split -> near 0.
    """
    vals = [max(-1.0, min(1.0, float(s))) for s in signals]
    if not vals:
        return 0.5
    if len(vals) == 1:
        return 0.5 + 0.35 * abs(vals[0])
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    spread_term = 1.0 - min(1.0, var / 0.5)
    return _clamp(0.25 + 0.45 * abs(mean) + 0.3 * spread_term)


def data_quality_score(parts: Dict[str, float], weights: Optional[Dict[str, float]] = None) -> float:
    """Weighted coverage/freshness values in [0,1]; booleans remain supported."""
    if not parts:
        return 0.3
    w = weights or {}
    num = sum(w.get(k, 1.0) * _clamp(float(v)) for k, v in parts.items())
    den = sum(w.get(k, 1.0) for k in parts)
    return num / den if den else 0.3
