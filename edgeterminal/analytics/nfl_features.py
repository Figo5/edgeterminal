"""Deterministic NFL feature transforms.

These helpers keep correlated football inputs grouped and bounded. They do not
fetch data, assign recommendation states, or weaken production gates.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable

from ..pricing import ev_per_unit


def recency_weighted_rate(values: Iterable[float], half_life_games: float = 4.0) -> float:
    """Exponentially weight observations, with the newest value last."""
    xs = [float(v) for v in values]
    if not xs:
        raise ValueError("at least one observation is required")
    if half_life_games <= 0:
        raise ValueError("half_life_games must be positive")
    decay = math.log(2.0) / half_life_games
    weights = [math.exp(-decay * (len(xs) - 1 - i)) for i in range(len(xs))]
    return sum(v * w for v, w in zip(xs, weights)) / sum(weights)


def effective_sample_weight(current_games: int, prior_games: int = 17,
                            prior_equivalent_games: float = 6.0) -> Dict[str, float]:
    """Return deterministic current/prior mixing and small-sample uncertainty.

    Prior season contributes at most ``prior_equivalent_games`` and naturally
    decays as current observations accumulate.
    """
    current = max(0.0, float(current_games))
    prior = min(max(0.0, float(prior_games)), max(0.0, float(prior_equivalent_games)))
    denom = current + prior
    current_weight = current / denom if denom else 0.0
    prior_weight = prior / denom if denom else 0.0
    reliability = current / (current + max(1.0, prior_equivalent_games))
    uncertainty = 1.0 - math.sqrt(max(0.0, min(1.0, reliability)))
    return {"current_weight": round(current_weight, 6),
            "prior_weight": round(prior_weight, 6),
            "reliability": round(reliability, 6),
            "uncertainty": round(uncertainty, 6)}


def opponent_adjusted_rate(raw_rate: float, opponent_strength: float,
                           adjustment: float = 0.5, bound: float = 0.15) -> float:
    """Apply a bounded schedule adjustment; positive means tougher opponents."""
    delta = max(-bound, min(bound, float(opponent_strength))) * max(0.0, min(1.0, adjustment))
    return float(raw_rate) + delta


def blend_market_model(model_prob: float, market_prob: float,
                       model_reliability: float, market_depth: int) -> Dict[str, float]:
    """Blend independent model with a no-vig market prior, without copying it."""
    model = max(0.01, min(0.99, float(model_prob)))
    market = max(0.01, min(0.99, float(market_prob)))
    reliability = max(0.0, min(1.0, float(model_reliability)))
    depth = max(0, int(market_depth))
    market_strength = min(0.75, 0.30 + 0.10 * depth)
    model_weight = max(0.20, min(0.75, reliability * (1.0 - 0.35 * market_strength)))
    final = model_weight * model + (1.0 - model_weight) * market
    return {"final_prob": round(final, 6), "raw_model_prob": model,
            "market_prob": market, "model_weight": round(model_weight, 6)}


def ranking_score(confidence: float, edge_pct: float, ev_pct: float,
                  data_quality: float, market_depth: int, uncertainty: float) -> float:
    """Rank already-qualified picks; this never assigns recommendation state."""
    coverage = min(1.0, max(0, market_depth) / 4.0)
    quality = max(0.0, min(1.0, data_quality))
    risk = max(0.0, min(1.0, uncertainty))
    score = (0.35 * max(0.0, min(100.0, confidence)) +
             0.22 * max(-10.0, min(15.0, edge_pct)) * 4.0 +
             0.13 * max(-20.0, min(30.0, ev_pct)) * 2.0 +
             15.0 * quality + 10.0 * coverage - 18.0 * risk)
    return round(score, 3)


def select_best_model_quote(candidates: Iterable[Dict], model_reliability: float,
                            market_depth: int) -> Dict:
    """Choose a quote using the same posterior probability later reported."""
    scored = []
    for candidate in candidates:
        raw = float(candidate["raw_model_prob"])
        market = candidate.get("market_prob")
        blend = (blend_market_model(raw, market, model_reliability, market_depth)
                 if market is not None and market_depth >= 2 else None)
        final = blend["final_prob"] if blend else raw
        scored.append(dict(candidate, blend=blend, final_prob=final,
                           final_ev=ev_per_unit(final, candidate["quote"]["price"])))
    if not scored:
        raise ValueError("at least one quote candidate is required")
    return max(scored, key=lambda row: row["final_ev"])
