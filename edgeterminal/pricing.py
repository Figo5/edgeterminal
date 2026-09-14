"""Deterministic odds / probability / sizing math.

Nothing in here ever calls an LLM. Every number the terminal shows as a
probability, edge, fair price or unit size is produced by this module.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

UNIT_LADDER = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]


# ---------------------------------------------------------------- odds <-> probability
def american_to_prob(odds: float) -> float:
    """Implied probability of an American price (includes the book's vig)."""
    o = float(odds)
    if o == 0:
        raise ValueError("american odds cannot be 0")
    return (-o) / (-o + 100.0) if o < 0 else 100.0 / (o + 100.0)


def prob_to_american(p: float) -> int:
    """Fair American price for a probability. Rounded to the nearest integer."""
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    if p >= 0.5:
        return int(round(-100.0 * p / (1.0 - p)))
    return int(round(100.0 * (1.0 - p) / p))


def american_to_decimal(odds: float) -> float:
    o = float(odds)
    return 1.0 + (100.0 / -o if o < 0 else o / 100.0)


def decimal_to_american(dec: float) -> int:
    d = float(dec)
    if d <= 1.0:
        raise ValueError("decimal odds must exceed 1.0")
    return int(round((d - 1.0) * 100.0)) if d >= 2.0 else int(round(-100.0 / (d - 1.0)))


def payout_multiple(odds: float) -> float:
    """Profit per 1 unit staked."""
    return american_to_decimal(odds) - 1.0


# ---------------------------------------------------------------- vig removal
def devig(implied: Sequence[float]) -> List[float]:
    """Multiplicative (proportional) vig removal across a market's outcomes."""
    total = float(sum(implied))
    if total <= 0:
        raise ValueError("implied probabilities must sum > 0")
    return [x / total for x in implied]


def devig_american(odds: Sequence[float]) -> List[float]:
    return devig([american_to_prob(o) for o in odds])


def hold_pct(odds: Sequence[float]) -> float:
    """Book hold (overround) as a percentage."""
    return (sum(american_to_prob(o) for o in odds) - 1.0) * 100.0


# ---------------------------------------------------------------- value metrics
def ev_per_unit(p: float, odds: float) -> float:
    """Expected profit per 1 unit risked."""
    return p * payout_multiple(odds) - (1.0 - p)


def edge_pct(model_p: float, odds: float) -> float:
    """Edge in probability points: model probability minus the price's implied."""
    return (model_p - american_to_prob(odds)) * 100.0


def roi_pct(p: float, odds: float) -> float:
    return ev_per_unit(p, odds) * 100.0


def kelly_fraction(p: float, odds: float) -> float:
    b = payout_multiple(odds)
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return max(0.0, f)


def breakeven_price(model_p: float, min_edge_pct: float = 2.0) -> int:
    """Worst American price at which this stays playable at the edge floor.

    'Playable down to' on a pick card.
    """
    target_implied = model_p - (min_edge_pct / 100.0)
    if target_implied <= 0.01:
        return 100000
    return prob_to_american(min(target_implied, 0.99))


# ---------------------------------------------------------------- best price selection
def pick_best_price(
    quotes: Iterable[Dict],
    preferred: Sequence[str] = ("fanduel", "bet365"),
) -> Optional[Dict]:
    """Best available American price. Preferred books win exact ties.

    quotes: [{"book": "fanduel", "price": -110, "line": -3.5}, ...]
    """
    qs = [q for q in quotes if q.get("price") is not None]
    if not qs:
        return None
    pref = [p.lower() for p in preferred]

    def key(q: Dict) -> Tuple[float, int]:
        book = str(q.get("book", "")).lower()
        rank = pref.index(book) if book in pref else len(pref)
        return (-american_to_decimal(q["price"]), rank)

    return sorted(qs, key=key)[0]


# ---------------------------------------------------------------- distributions
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_over(mean: float, sd: float, line: float) -> float:
    """P(X > line) for a normal projection. Guards degenerate sd."""
    if sd <= 1e-9:
        return 1.0 if mean > line else 0.0
    return 1.0 - norm_cdf((line - mean) / sd)


def prob_over_count(mean: float, line: float, sd: Optional[float] = None) -> float:
    """P(count > line) for a discrete count prop (TDs, receptions, made threes).

    A normal approximation badly misprices low-mean counts, which is exactly
    where books hang the juiciest-looking fake edges. Poisson when the variance
    is at or below the mean, negative binomial when it is overdispersed.
    """
    mean = max(1e-6, float(mean))
    k = math.floor(line)  # need >= k+1 successes to go over a .5 line
    var = (sd ** 2) if sd else mean
    if var <= mean * 1.05:
        # Poisson survival: 1 - P(X <= k)
        cum, term = 0.0, math.exp(-mean)
        for i in range(0, k + 1):
            if i:
                term *= mean / i
            cum += term
        return max(0.0, min(1.0, 1.0 - cum))
    # negative binomial with matched mean/variance
    r = mean * mean / (var - mean)
    p = r / (r + mean)
    cum, term = 0.0, p ** r
    for i in range(0, k + 1):
        if i:
            term *= (r + i - 1) / i * (1 - p)
        cum += term
    return max(0.0, min(1.0, 1.0 - cum))


def push_adjusted_cover(margin: float, sd: float, line: float) -> float:
    """P(team covers `line`) where line is the handicap applied to the team.

    Integer lines can push; we exclude the push mass rather than counting it
    as a win, which is how a book grades it.
    """
    need = -line
    p_win = 1.0 - norm_cdf((need - margin) / sd)
    if abs(line - round(line)) < 1e-9:
        # discrete push mass around the key number
        p_push = norm_cdf((need + 0.5 - margin) / sd) - norm_cdf((need - 0.5 - margin) / sd)
        p_win = 1.0 - norm_cdf((need + 0.5 - margin) / sd)
        denom = max(1e-9, 1.0 - p_push)
        p_loss = norm_cdf((need - 0.5 - margin) / sd)
        return p_win / max(1e-9, p_win + p_loss) if (p_win + p_loss) > 0 else 0.5
    return p_win


# ---------------------------------------------------------------- unit sizing
def recommend_units(
    confidence: float,
    edge: float,
    market_type: str,
    data_quality: float,
    model_agreement: float,
) -> float:
    """Abstract unit size on the fixed ladder. Never above 5u, usually well below.

    Deliberately conservative: confidence gates the ceiling, edge and Kelly-ish
    scaling set the size inside it, props and thin data shrink it.
    """
    if confidence < 65 or edge <= 0:
        return 0.0
    base = 0.25 + (confidence - 65.0) / 35.0 * 1.75  # 0.25 .. 2.0
    base *= min(2.0, 0.6 + edge / 6.0)               # edge scaling
    base *= 0.55 + 0.45 * max(0.0, min(1.0, data_quality))
    base *= 0.6 + 0.4 * max(0.0, min(1.0, model_agreement))
    variance_mult = {
        "moneyline": 1.0, "spread": 1.0, "total": 0.95,
        "team_total": 0.8, "player_prop": 0.6, "outright": 0.4,
        "top5": 0.5, "top10": 0.55, "top20": 0.6, "matchup": 0.75, "parlay": 0.4,
    }.get(market_type, 0.7)
    base *= variance_mult
    base = min(base, 5.0)
    # snap down to the ladder
    ladder = [u for u in UNIT_LADDER if u <= base + 1e-9]
    return ladder[-1] if ladder else 0.0


# ---------------------------------------------------------------- movement
def line_move(open_line: Optional[float], current_line: Optional[float]) -> Dict:
    if open_line is None or current_line is None:
        return {"delta": None, "direction": "unknown", "magnitude": 0.0}
    delta = round(current_line - open_line, 2)
    return {
        "delta": delta,
        "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat"),
        "magnitude": abs(delta),
    }


def clv_pct(bet_odds: float, closing_odds: float) -> float:
    """Closing line value as a percentage of price improvement (no-vig-free, raw)."""
    return (american_to_decimal(bet_odds) / american_to_decimal(closing_odds) - 1.0) * 100.0
