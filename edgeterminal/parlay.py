"""Parlay construction with correlation-adjusted probability.

Legs are never multiplied as if independent. Same-game legs get an explicit
correlation treatment, and the optimiser is allowed to return a two-leg parlay
-- or nothing at all -- rather than padding legs to inflate the price.
"""
from __future__ import annotations

import itertools
import math
from typing import Dict, List, Optional, Sequence, Tuple

from .config import settings
from .pricing import (american_to_decimal, american_to_prob, decimal_to_american,
                      ev_per_unit, recommend_units)

MAX_LEGS = 5

# Rule-based correlation coefficients between leg archetypes in the SAME game.
# Positive = the legs win together more often than chance.
SAME_GAME_RULES: List[Tuple[str, str, float, str]] = [
    ("qb_pass_over", "wr_rec_over_same_team", 0.42, "QB passing volume and his receiver's yardage share the same dropback sample"),
    ("qb_pass_over", "team_total_over_same", 0.30, "Passing volume tracks the team's scoring"),
    ("rb_rush_over", "team_spread_favorite_same", 0.34, "Leading teams run more; the rushing over and the cover reinforce each other"),
    ("qb_pass_over", "team_spread_favorite_same", -0.18, "Big favourites stop throwing once ahead"),
    ("wr_rec_over", "game_total_over", 0.26, "Receiving yardage and game total share the pace/efficiency driver"),
    ("rb_rush_over", "game_total_under", 0.16, "Run-heavy scripts shorten games"),
    ("team_ml", "team_spread_same", 0.66, "Same side, overlapping outcome space — near-duplicate risk"),
    ("over", "under_same_game", -0.95, "Directly opposing outcomes"),
]

CROSS_GAME_RHO = 0.03   # weak common factor (officiating, scoring environment, weather regimes)


def _archetype(leg: Dict) -> str:
    m, sel = leg.get("market"), (leg.get("selection") or "")
    side = (leg.get("side") or "").lower()
    if m == "player_prop":
        low = sel.lower()
        if "passing" in low:
            return "qb_pass_over" if side == "over" else "qb_pass_under"
        if "rushing" in low:
            return "rb_rush_over" if side == "over" else "rb_rush_under"
        if "receiv" in low or "reception" in low:
            return "wr_rec_over" if side == "over" else "wr_rec_under"
        return "prop_" + side
    if m == "total":
        return "game_total_over" if sel.lower() == "over" else "game_total_under"
    if m == "moneyline":
        return "team_ml"
    if m == "spread":
        return "team_spread"
    return m or "other"


def pair_correlation(a: Dict, b: Dict) -> Tuple[float, str]:
    """Correlation between two legs, with a human-readable reason."""
    if a["event_id"] != b["event_id"]:
        return CROSS_GAME_RHO, "Different games — treated as near-independent with a small common factor"
    aa, ba = _archetype(a), _archetype(b)
    same_team = _team_of(a) and _team_of(a) == _team_of(b)
    if a.get("market") == b.get("market") == "player_prop":
        pa, pb = _player_of(a), _player_of(b)
        if pa and pa == pb:
            same_side = (a.get("side") or "").lower() == (b.get("side") or "").lower()
            return ((0.5, f"Both legs are {pa} props driven by the same minutes/usage — "
                          f"strongly correlated, not two independent bets")
                    if same_side else
                    (-0.3, f"Opposing sides of two {pa} props sharing one usage driver"))

    if {aa, ba} == {"game_total_over", "game_total_under"}:
        return -0.95, "Directly opposing sides of the same total"
    if aa == "team_ml" and ba == "team_spread" or ba == "team_ml" and aa == "team_spread":
        if same_team or _sel(a) == _sel(b):
            return 0.66, "Moneyline and spread on the same side overlap heavily — near-duplicate risk"
        return -0.6, "Opposite sides of the same game"
    if {aa, ba} <= {"qb_pass_over", "wr_rec_over"} and aa != ba:
        return (0.42, "QB passing volume and receiver yardage come from the same dropbacks") if same_team \
            else (-0.12, "Opposing offences compete for the same game time")
    if {aa, ba} == {"qb_pass_over", "game_total_over"}:
        return 0.28, "Passing volume and total scoring share the same pace/efficiency driver"
    if {aa, ba} == {"wr_rec_over", "game_total_over"}:
        return 0.26, "Receiving yardage rises with the scoring environment"
    if {aa, ba} == {"rb_rush_over", "game_total_under"}:
        return 0.16, "Run-heavy scripts shorten games and suppress the total"
    if {aa, ba} == {"qb_pass_over", "game_total_under"}:
        return -0.22, "Passing volume and an under pull against each other"
    if aa == ba and same_team:
        return 0.25, "Two legs on the same team's production"
    return 0.08, "Same game — mild shared game-state dependence"


def _team_of(leg: Dict) -> Optional[str]:
    return leg.get("team") or leg.get("selection", "").split(" Over ")[0].split(" Under ")[0]


def _player_of(leg: Dict) -> Optional[str]:
    sel = leg.get("selection", "")
    for tok in (" Over ", " Under "):
        if tok in sel:
            return sel.split(tok, 1)[0].strip()
    return None


def _sel(leg: Dict) -> str:
    return leg.get("selection", "")


def combined_probability(legs: Sequence[Dict]) -> Tuple[float, float, List[str]]:
    """Returns (independent product, correlation-adjusted probability, notes).

    Gaussian-copula style adjustment: each pair's correlation shifts the joint
    probability away from the independent product, scaled by how much room
    there is to move. Approximate by design -- the point is to stop pretending
    correlated legs multiply cleanly, not to claim a precise joint density.
    """
    probs = [float(l["model_prob"]) for l in legs]
    indep = math.prod(probs)
    notes: List[str] = []
    adj = indep
    for a, b in itertools.combinations(range(len(legs)), 2):
        rho, why = pair_correlation(legs[a], legs[b])
        pa, pb = probs[a], probs[b]
        # covariance of two Bernoullis at correlation rho
        cov = rho * math.sqrt(pa * (1 - pa) * pb * (1 - pb))
        joint_pair = pa * pb + cov
        lift = joint_pair / (pa * pb) if pa * pb > 0 else 1.0
        adj *= lift
        if abs(rho) >= 0.15:
            notes.append(f"{_short(legs[a])} x {_short(legs[b])}: rho {rho:+.2f} — {why}")
    adj = max(min(adj, min(probs)), 1e-6)
    return indep, adj, notes


def _short(leg: Dict) -> str:
    line = f" {leg['line']:g}" if leg.get("line") is not None else ""
    return f"{leg['selection']}{line}"


def parlay_price(legs: Sequence[Dict]) -> int:
    dec = math.prod(american_to_decimal(l["price"]) for l in legs)
    return decimal_to_american(dec)


def evaluate(legs: Sequence[Dict]) -> Dict:
    """Price a leg combination.

    The combined price is the product of the individual leg prices. That is
    correct for cross-game parlays. For same-game legs a book quotes its own
    correlated SGP number instead, always worse than the multiplied one, so the
    correlation lift is removed from the reported edge rather than banked as
    free value.
    """
    price = parlay_price(legs)
    indep, adj, notes = combined_probability(legs)
    implied = american_to_prob(price)
    same_game = len({l["event_id"] for l in legs}) < len(legs)
    lift = (adj / indep) if indep > 0 else 1.0
    implied_effective = min(0.999, implied * lift) if (same_game and lift > 1) else implied
    ev = ev_per_unit(adj, price) if not same_game else (adj / max(implied_effective, 1e-9) - 1.0)
    conf = min(l["confidence"] for l in legs) - 2.0 * (len(legs) - 1)
    conf = max(0.0, conf)
    edge = (adj - implied_effective) * 100
    return dict(legs=list(legs), combined_price=price, independent_prob=round(indep, 4),
                correlation_adjusted_prob=round(adj, 4), implied_prob=round(implied, 4),
                implied_prob_effective=round(implied_effective, 4),
                correlation_lift=round(lift, 3), same_game=same_game,
                edge_pct=round(edge, 2), ev_per_unit=round(ev, 4),
                confidence=round(conf, 1),
                units=recommend_units(conf, edge, "parlay", 0.8, 0.8),
                price_note=("Same-game legs: the quoted figure multiplies the individual prices. "
                            "Your book will offer a lower same-game price, so the correlation "
                            "lift has been removed from the edge shown."
                            if same_game else
                            "Cross-game legs: the multiplied price is what a book will actually "
                            "offer."),
                correlation_notes=notes)


def build_best(candidates: List[Dict], max_legs: int = 3, min_conf: float = 70.0,
               same_game_ok: bool = True, sports: Optional[List[str]] = None,
               required_event: Optional[str] = None) -> Optional[Dict]:
    """Search leg combinations and return the single best parlay, or None.

    Optimises correlation-adjusted EV with an explicit penalty for extra legs,
    so a 2-leg parlay wins unless a third genuinely improves the bet.
    """
    pool = [c for c in candidates
            if c.get("confidence", 0) >= min_conf and c.get("edge_pct", 0) > 0
            and c.get("state") == "OFFICIAL PICK" and not c.get("stale")
            and c.get("data_quality") in ("good", "excellent")]
    if sports:
        pool = [c for c in pool if c["sport"] in sports]
    if required_event:
        pool = [c for c in pool if c["event_id"] == required_event]
    pool.sort(key=lambda c: (-c["confidence"], -c["edge_pct"]))
    pool = pool[:14]
    if len(pool) < 2:
        return None

    max_legs = max(2, min(MAX_LEGS, max_legs))
    best = None
    best_score = -1e9
    for k in range(2, max_legs + 1):
        for combo in itertools.combinations(pool, k):
            if len({(c["event_id"], c["market"], c["selection"]) for c in combo}) < k:
                continue
            if not same_game_ok and len({c["event_id"] for c in combo}) < k:
                continue
            # never pair directly contradictory legs
            rhos = [pair_correlation(a, b)[0] for a, b in itertools.combinations(combo, 2)]
            if any(r <= -0.5 for r in rhos):
                continue
            if any(r >= 0.6 for r in rhos):   # near-duplicate risk, not a real parlay
                continue
            res = evaluate(combo)
            if res["correlation_adjusted_prob"] < 0.08:
                continue
            # EV per unit, penalised for each additional leg's variance
            sc = res["ev_per_unit"] - 0.05 * (k - 2) + 0.004 * (res["confidence"] - 70)
            if sc > best_score:
                best_score, best = sc, res
    if not best or best["edge_pct"] <= 0:
        return None

    legs = best["legs"]
    best["rationale"] = [
        f"{_short(l)} @ {l['book']} {l['price']:+.0f} — model {l['model_prob']*100:.1f}% vs "
        f"implied {american_to_prob(l['price'])*100:.1f}% ({l['edge_pct']:+.1f} pts), "
        f"confidence {l['confidence']:.0f}" for l in legs]
    same_game = len({l["event_id"] for l in legs}) < len(legs)
    best["rationale"].append(
        f"Correlation-adjusted probability {best['correlation_adjusted_prob']*100:.1f}% vs "
        f"{best['independent_prob']*100:.1f}% if the legs were independent"
        + (" — the book almost certainly prices some of this same-game correlation into the "
           "payout, so treat the stated edge as an upper bound." if same_game else "."))
    best["risks"] = [
        "Every leg must land: a parlay's variance is far higher than its components.",
        "Correlation estimates are rule-based approximations, not a fitted joint model.",
    ] + [r for l in legs for r in (l.get("risks") or [])[:1]]
    best["state"] = ("OFFICIAL PICK" if best["confidence"] >= settings.official_pick_confidence
                     and best["edge_pct"] >= settings.min_edge_pct else "LEAN")
    return best
