"""Shrinkage of raw team rates toward a league prior.

Week 1 points-per-game is almost pure noise. Regressing observed rates toward a
league average with a pseudo-game weight is the difference between a model that
projects a 39-point Vikings offence and one that does not.
"""
from __future__ import annotations

from typing import Dict, Tuple

# (league average points per game, pseudo-games of prior weight)
PRIORS = {"NFL": (22.5, 6.0), "CFB": (28.5, 5.0), "NBA": (114.0, 8.0)}


def shrink_stats(stats: Dict[str, Dict], sport: str) -> Tuple[Dict[str, Dict], float]:
    """Returns (shrunk stats, mean effective sample weight in [0,1])."""
    prior, k = PRIORS.get(sport, (22.5, 6.0))
    out: Dict[str, Dict] = {}
    weights = []
    for name, row in stats.items():
        n = float(row.get("games") or 0)
        w = n / (n + k) if (n + k) > 0 else 0.0
        weights.append(w)
        pf = float(row.get("points_for") or prior)
        pa = float(row.get("points_against") or prior)
        out[name] = dict(row,
                         points_for=round(w * pf + (1 - w) * prior, 3),
                         points_against=round(w * pa + (1 - w) * prior, 3),
                         raw_points_for=pf, raw_points_against=pa,
                         shrink_weight=round(w, 3))
    return out, (sum(weights) / len(weights) if weights else 0.0)
