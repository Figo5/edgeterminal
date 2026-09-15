"""Deterministic touchdown and prop probability models.

Everything here is arithmetic on structured inputs. Web research contributes
*bounded multipliers* (see `research.features`); it never contributes a
probability directly.

TD probability = Poisson race over expected opportunities:

    expected_tds = team_tds x opportunity_share x bounded multipliers
    p(at least one) = 1 - exp(-expected_tds)

First-TD is the same race evaluated over the whole game, so the first-scorer
probabilities of every player sum to at most one.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

from ..pricing import american_to_decimal, american_to_prob, devig, prob_to_american
from .nfl_features import blend_market_model

TD_POINTS = 6.95          # touchdown + expected extra point / two-point mix
OPPONENT_FACTOR_MIN = 0.80
OPPONENT_FACTOR_MAX = 1.20
MAX_TD_PROB = 0.75
MIN_TD_PROB = 0.005
MULTIPLIER_KEYS = ("workload_multiplier", "availability_multiplier",
                   "target_share_multiplier", "opportunity_share_multiplier")


def team_td_environment(implied_total: float, opponent_td_allow: float = 1.0,
                        league_total: float = 22.5, points_per_td: float = TD_POINTS) -> Dict:
    """Projected team touchdowns from the scoring environment and the matchup.

    `opponent_td_allow` is this opponent's touchdowns-allowed index (1.0 = league
    average); it is bounded so a two-game sample cannot swing the projection.
    """
    factor = min(OPPONENT_FACTOR_MAX, max(OPPONENT_FACTOR_MIN, float(opponent_td_allow)))
    raw = float(implied_total) / float(points_per_td)
    adjusted = raw * (0.5 + 0.5 * factor)
    projected = min(6.0, max(1.0, adjusted))
    notes = [f"Implied team total {implied_total:.1f} at {points_per_td:.2f} points per TD",
             f"Opponent touchdown allowance x{factor:.2f} (league average = 1.00)"]
    if abs(factor - 1.0) > 0.001:
        notes.append("Matchup adjustment applied to the scoring environment")
    return dict(projected_team_tds=round(projected, 3), raw_tds=round(raw, 3),
                opponent_factor=round(factor, 3), points_per_td=points_per_td,
                league_total=league_total, notes=notes)


def _scaled_multipliers(multipliers: Optional[Dict]) -> Dict:
    scaled, adjustments = {}, []
    for key in MULTIPLIER_KEYS:
        value = (multipliers or {}).get(key)
        if value is None:
            continue
        value = min(1.25, max(0.70, float(value)))
        scaled[key] = value
        if abs(value - 1.0) > 0.001:
            adjustments.append(f"{key.replace('_', ' ')} x{value:.2f}")
    return dict(values=scaled, adjustments=adjustments)


def anytime_td_probability(team_tds: float, opportunity_share: float,
                           multipliers: Optional[Dict] = None) -> Dict:
    """Probability a player scores at least one touchdown."""
    factors = _scaled_multipliers(multipliers)
    share = max(0.0, min(0.85, float(opportunity_share)))
    combined = 1.0
    for value in factors["values"].values():
        combined *= value
    expected = max(0.0, float(team_tds) * share * combined)
    prob = min(MAX_TD_PROB, max(MIN_TD_PROB, 1.0 - math.exp(-expected)))
    return dict(prob=round(prob, 4), expected_tds=round(expected, 4),
                opportunity_share=round(share, 4), team_tds=round(float(team_tds), 3),
                multipliers=factors["values"], adjustments=factors["adjustments"],
                method="poisson-race")


def first_td_probabilities(players: Iterable[Dict]) -> Dict[str, float]:
    """First-scorer probability from a race among every expected scorer."""
    rows = [dict(p) for p in players if float(p.get("expected_tds") or 0) > 0]
    if not rows:
        return {}
    total = sum(float(row["expected_tds"]) for row in rows)
    if total <= 0:
        return {}
    race_share = 1.0 - math.exp(-total)
    return {row["name"]: round(float(row["expected_tds"]) / total * race_share, 4)
            for row in rows}


# --------------------------------------------------------------------- market
def no_vig_td_market(quotes: Dict[str, List[float]], min_outcomes: int = 3,
                     full_coverage_books: int = 2) -> Dict:
    """Normalise a juiced multi-outcome TD market into fair probabilities.

    Method: convert each book's prices to implied probability, take the per-player
    median across books, then remove the overround proportionally (the standard
    multiplicative no-vig method). Consensus across books beats one book's juice.
    """
    probs: Dict[str, float] = {}
    books = 0
    for player, prices in (quotes or {}).items():
        usable = [american_to_prob(float(p)) for p in (prices or [])]
        if not usable:
            continue
        books = max(books, len(usable))
        usable.sort()
        mid = len(usable) // 2
        median = usable[mid] if len(usable) % 2 else (usable[mid - 1] + usable[mid]) / 2
        probs[player] = median
    if len(probs) < min_outcomes:
        return dict(probs={}, overround=None, books=books, coverage="insufficient",
                    usable=False, method="proportional",
                    reason=f"a normalised market needs at least {min_outcomes} priced outcomes, "
                           f"got {len(probs)}")
    raw_sum = sum(probs.values())
    if raw_sum <= 0:
        return dict(probs={}, overround=None, books=books, coverage="insufficient",
                    usable=False, method="proportional", reason="prices contained no usable outcomes")
    coverage = "full" if books >= full_coverage_books else "thin"
    names = list(probs)
    fair = devig([probs[name] for name in names])
    return dict(probs={name: round(value, 6) for name, value in zip(names, fair)},
                overround=round(raw_sum - 1.0, 6), books=books, coverage=coverage,
                usable=True, method="proportional",
                reason="implied probabilities normalised proportionally across priced outcomes")


def fair_price(prob: float) -> Optional[int]:
    return prob_to_american(prob) if 0 < prob < 1 else None


def td_board(players: Iterable[Dict], market: Dict, model_reliability: float = 0.5,
             market_weight_floor: float = 0.35) -> Dict:
    """Rank TD scorers, separating likelihood from betting value."""
    if not market.get("usable") or market.get("coverage") == "thin":
        return dict(rows=[], most_likely=None, best_value=None, state="INSUFFICIENT DATA",
                    reason="market coverage is too thin to estimate a fair TD price"
                           + (f": {market['reason']}" if market.get("reason") else ""),
                    market=market)
    rows: List[Dict] = []
    for player in players:
        name = player.get("name")
        model = anytime_td_probability(team_tds=float(player.get("team_tds") or 0),
                                       opportunity_share=float(player.get("opportunity_share") or 0),
                                       multipliers=player.get("multipliers"))
        market_prob = market["probs"].get(name)
        if market_prob is None or not player.get("best_price"):
            continue
        blend = blend_market_model(model["prob"], market_prob,
                                   max(0.0, min(1.0, model_reliability)), market["books"])
        final = blend["final_prob"]
        price = float(player["best_price"])
        implied = american_to_prob(price)
        edge = (final - implied) * 100
        fair = fair_price(final)
        rows.append(dict(
            player=name, team=player.get("team"), market="player_anytime_td",
            best_price=price, book=player.get("book"), implied_prob=round(implied, 4),
            model_prob=model["prob"], market_prob=market_prob, final_prob=round(final, 4),
            fair_price=fair, edge_pct=round(edge, 2),
            expected_tds=model["expected_tds"],
            ev_per_unit=round(final * (american_to_decimal(price) - 1) - (1 - final), 4),
            adjustments=model["adjustments"],
            provenance=dict(source="deterministic-model",
                            market="theoddsapi", model="poisson-race",
                            model_weight=blend.get("model_weight")),
            reasons=[f"Model {model['prob'] * 100:.1f}% vs no-vig market "
                     f"{market_prob * 100:.1f}%",
                     f"Best price {price:+.0f} at {player.get('book')}",
                     f"{market['books']} book(s) priced this market"]))
    if not rows:
        return dict(rows=[], most_likely=None, best_value=None, state="INSUFFICIENT DATA",
                    reason="no player with both a model input and a priced market outcome",
                    market=market)
    rows.sort(key=lambda r: -r["final_prob"])
    most_likely = rows[0]
    valued = sorted([r for r in rows if r["edge_pct"] > 0], key=lambda r: -r["ev_per_unit"])
    return dict(rows=rows, most_likely=most_likely, best_value=valued[0] if valued else None,
                state="OK", market=market)


# --------------------------------------------------------------------- props
def prop_opportunity_projection(market: str, usage: Dict, *, team_attempts: float = 34.0,
                                multipliers: Optional[Dict] = None) -> Dict:
    """Opportunity-first projection for yardage/reception props.

    receiving:  expected routes -> target probability -> catch -> yards/reception
    rushing:    expected carries x yards per carry
    passing:    expected attempts x yards per attempt
    """
    factors = _scaled_multipliers(multipliers)
    combined = 1.0
    for value in factors["values"].values():
        combined *= value
    games = max(1, int(usage.get("games") or 1))
    notes: List[str] = []

    if market in ("player_reception_yards", "player_rec_yards", "player_receptions"):
        targets = float(usage.get("targets") or 0) / games
        catches = float(usage.get("receptions") or 0) / games
        yards = float(usage.get("rec_yards") or 0) / games
        catch_rate = (catches / targets) if targets else 0.0
        ypr = (yards / catches) if catches else 0.0
        mean = catches * ypr * combined
        sd = max(4.0, mean * 0.75)
        notes.append(f"{catches:.2f} catches/game on {catch_rate:.0%} catch rate at {ypr:.1f} yds/catch")
        return dict(market=market, mean_yards=round(mean, 2), sd=round(sd, 2),
                    component=dict(targets_per_game=round(targets, 2),
                                   catches_per_game=round(catches, 2),
                                   catch_rate=round(catch_rate, 3), yards_per_catch=round(ypr, 2)),
                    multipliers=factors["values"], adjustments=factors["adjustments"], notes=notes)

    if market in ("player_rush_yards", "player_rush_yards"):
        carries = float(usage.get("carries") or 0) / games
        yards = float(usage.get("rush_yards") or 0) / games
        ypc = (yards / carries) if carries else 4.2
        mean = carries * ypc * combined
        sd = max(8.0, mean * 0.7)
        notes.append(f"{carries:.2f} carries/game at {ypc:.2f} yds/carry")
        return dict(market=market, mean_yards=round(mean, 2), sd=round(sd, 2),
                    component=dict(carries_per_game=round(carries, 2), yards_per_carry=round(ypc, 2)),
                    multipliers=factors["values"], adjustments=factors["adjustments"], notes=notes)

    if market in ("player_pass_yards", "player_pass_tds", "player_pass_attempts",
                  "player_pass_completions"):
        attempts = float(usage.get("pass_attempts") or 0) / games
        if market == "player_pass_attempts":
            mean, sd = attempts * combined, max(3.0, attempts * 0.28)
        elif market == "player_pass_completions":
            comps = float(usage.get("completions") or 0) / games
            mean, sd = comps * combined, max(3.0, comps * 0.3)
        elif market == "player_pass_yards":
            yards = float(usage.get("pass_yards") or 0) / games
            ypa = (yards / attempts) if attempts else 7.1
            mean, sd = attempts * ypa * combined, max(30.0, attempts * ypa * 0.55)
        else:
            tds = float(usage.get("pass_tds") or 0) / games
            mean, sd = tds * combined, max(0.6, tds * 0.9)
        notes.append(f"{attempts:.2f} attempts/game baseline")
        return dict(market=market, mean_yards=round(mean, 2), sd=round(sd, 2),
                    component=dict(attempts_per_game=round(attempts, 2)),
                    multipliers=factors["values"], adjustments=factors["adjustments"], notes=notes)

    if market in ("player_rush_attempts",):
        carries = float(usage.get("carries") or 0) / games
        return dict(market=market, mean_yards=round(carries * combined, 2),
                    sd=round(max(2.0, carries * 0.3), 2),
                    component=dict(carries_per_game=round(carries, 2)),
                    multipliers=factors["values"], adjustments=factors["adjustments"],
                    notes=[f"{carries:.2f} carries/game"])
    raise ValueError(f"unsupported prop market: {market}")


def prop_probability(projection: Dict, side: str, line: float) -> float:
    from ..pricing import prob_over
    p_over = prob_over(projection["mean_yards"], projection["sd"], line)
    return p_over if str(side).lower() == "over" else 1 - p_over