"""Deterministic touchdown and prop probability models.

Everything here is arithmetic on structured inputs. Web research contributes
*bounded multipliers* (see `research.features`); it never contributes a
probability directly.

TD probability = Poisson race over expected opportunities:

    expected_tds = team_tds x opportunity_share x bounded multipliers
    p(at least one) = 1 - exp(-expected_tds)

This is an **experimental** model, not a calibrated one.

Anytime-TD outcomes are not mutually exclusive, so player probabilities are
never normalised against each other; see `no_vig_td_market`.

First-TD is **disabled for this release**: `first_td_probabilities` is retained
and unit-tested but is deliberately wired to no recommendation path, and the
prop workflow refuses the `player_1st_td` market outright.
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
    """First-scorer probability from a race among every expected scorer.

    DISABLED for this release: no caller in the recommendation path. First-TD
    settles on a single game-level event, and this race share has never been
    validated against realised first scorers, so it must not price a pick.
    Anytime probabilities are never substituted for it.
    """
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
def _median(values: List[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def no_vig_td_market(quotes: Dict[str, List], full_coverage_books: int = 2) -> Dict:
    """Fair anytime-TD probabilities, estimated one player at a time.

    Anytime-TD outcomes are **not mutually exclusive**: several players score in
    the same game, so their Yes probabilities legitimately sum far above 1.
    Normalising them against each other (dividing by their sum) is therefore
    invalid, and it makes every player's number depend on which other players
    happen to be priced. This function never does that.

    The only de-vig performed here is the two-outcome kind that is actually
    complementary: the **same player's** Yes and No from the **same book**, where
    Yes and No do partition the outcome space. Those two prices are de-vigged
    proportionally and the fair Yes is the median across the books that quote
    both sides.

    When a player has no No price anywhere, a market no-vig probability is
    *unavailable* -- it is reported as `None`, not guessed. Yes quotes from other
    books do not supply the missing No, and no No price is ever fabricated. The
    raw (vig-inclusive) implied probability is still reported for display, and is
    always an over-estimate of the true probability.

    `quotes` maps player -> list of rows, each `{"book", "side", "price"}`.
    A bare number is accepted and treated as an unattributed Yes price.
    """
    players: Dict[str, Dict] = {}
    max_books = 0
    for player, entries in (quotes or {}).items():
        by_book: Dict[str, Dict[str, float]] = {}
        for entry in entries or []:
            if isinstance(entry, (int, float)):
                book, side, price = "_", "yes", float(entry)
            else:
                if entry.get("price") is None:
                    continue
                price = float(entry["price"])
                book = (entry.get("book") or "_").strip().lower() or "_"
                side = (entry.get("side") or "yes").strip().lower()
            if side not in ("yes", "no"):
                continue
            # Keep the friendliest price per side per book.
            current = by_book.setdefault(book, {}).get(side)
            if current is None or american_to_prob(price) < american_to_prob(current):
                by_book[book][side] = price
        yes_probs: List[float] = []
        paired: List[float] = []
        overrounds: List[float] = []
        for sides in by_book.values():
            if "yes" in sides:
                yes_probs.append(american_to_prob(sides["yes"]))
            if "yes" in sides and "no" in sides:
                p_yes = american_to_prob(sides["yes"])
                p_no = american_to_prob(sides["no"])
                total = p_yes + p_no
                if total <= 0:
                    continue
                paired.append(devig([p_yes, p_no])[0])
                overrounds.append(total - 1.0)
        if not yes_probs:
            continue
        max_books = max(max_books, len(by_book))
        players[player] = dict(
            raw_implied=round(_median(yes_probs), 6),
            no_vig=round(_median(paired), 6) if paired else None,
            overround=round(_median(overrounds), 6) if overrounds else None,
            books=len(by_book), two_sided_books=len(paired),
            basis="two_sided_devig" if paired else "raw_implied_with_vig")
    priced = len(players)
    with_no_vig = sum(1 for row in players.values() if row["no_vig"] is not None)
    if not priced:
        return dict(players={}, probs={}, raw_probs={}, books=0, priced=0,
                    no_vig_players=0, coverage="insufficient", usable=False,
                    method="same-player-two-sided",
                    reason="no usable anytime-TD prices were returned")
    if with_no_vig:
        reason = (f"{with_no_vig} of {priced} player(s) had a same-book Yes and No "
                  f"quote, so a fair probability was estimated for those only")
    else:
        reason = ("no book returned a No price for any player, so market no-vig "
                  "probabilities are unavailable; raw implied prices still include the vig")
    return dict(players=players,
                probs={name: row["no_vig"] for name, row in players.items()
                       if row["no_vig"] is not None},
                raw_probs={name: row["raw_implied"] for name, row in players.items()},
                books=max_books, priced=priced, no_vig_players=with_no_vig,
                coverage="full" if max_books >= full_coverage_books else "thin",
                usable=True, method="same-player-two-sided", reason=reason)


def fair_price(prob: float) -> Optional[int]:
    return prob_to_american(prob) if 0 < prob < 1 else None


def td_board(players: Iterable[Dict], market: Dict, model_reliability: float = 0.5,
             market_weight_floor: float = 0.35) -> Dict:
    """Rank anytime-TD scorers, keeping three states strictly apart.

    A. verified price  -- every priced player is listed, always.
    B. experimental    -- a model probability, only where usage supports one.
    C. recommendable   -- only where a model *and* a same-player no-vig market
                          probability both exist; `edge_pct`/`ev_per_unit` are
                          `None` otherwise rather than filled with a guess.

    Edge is never derived from the market alone: comparing a no-vig probability
    against the vigged price it came from yields the vig back as fake "edge".
    """
    market = market or {}
    entries = market.get("players") or {}
    rows: List[Dict] = []
    for player in players:
        name = player.get("name")
        entry = entries.get(name)
        price = player.get("best_price")
        if not entry or price is None:
            continue
        price = float(price)
        implied = entry["raw_implied"]
        market_prob = entry["no_vig"]
        share = float(player.get("opportunity_share") or 0)
        team_tds = float(player.get("team_tds") or 0)
        # No opportunity input means no model. A floor probability here would be a
        # number invented from absent usage, so the model stays unavailable.
        model = (anytime_td_probability(team_tds=team_tds, opportunity_share=share,
                                        multipliers=player.get("multipliers"))
                 if share > 0 and team_tds > 0 else None)
        if model is not None and market_prob is not None:
            blend = blend_market_model(model["prob"], market_prob,
                                       max(0.0, min(1.0, model_reliability)), entry["books"])
            final, basis = blend["final_prob"], "model+market"
            model_weight = blend.get("model_weight")
        elif model is not None:
            final, basis, model_weight = model["prob"], "model-only", 1.0
        else:
            final, basis, model_weight = None, "market-only", None
        edge = ev = fair = None
        # Edge and EV are recommendation numbers. They are computed only where a
        # model AND a same-player no-vig market probability both exist; an
        # uncalibrated model measured against a vigged price is not "value".
        if model is not None and market_prob is not None and final is not None:
            edge = round((final - implied) * 100, 2)
            ev = round(final * (american_to_decimal(price) - 1) - (1 - final), 4)
            fair = fair_price(final)
        reasons = [f"Best price {price:+.0f} at {player.get('book')} "
                   f"({implied * 100:.1f}% implied, vig included)"]
        if market_prob is not None:
            reasons.append(f"No-vig {market_prob * 100:.1f}% from {entry['two_sided_books']} "
                           f"book(s) quoting this player's Yes and No")
        else:
            reasons.append("No-vig unavailable: no book quoted a No price for this player")
        reasons.append(f"Model {model['prob'] * 100:.1f}%" if model else
                       "Model unavailable: no usable opportunity input")
        rows.append(dict(
            player=name, team=player.get("team"), market="player_anytime_td",
            best_price=price, book=player.get("book"),
            implied_prob=implied, model_prob=model["prob"] if model else None,
            market_prob=market_prob, final_prob=final, probability_basis=basis,
            fair_price=fair, edge_pct=edge, ev_per_unit=ev,
            expected_tds=model["expected_tds"] if model else None,
            recommendable=bool(model is not None and market_prob is not None),
            books=entry["books"], adjustments=model["adjustments"] if model else [],
            provenance=dict(source="deterministic-model", market="theoddsapi",
                            model="poisson-race" if model else None,
                            market_method=entry["basis"], model_weight=model_weight),
            reasons=reasons))
    if not rows:
        return dict(rows=[], most_likely=None, most_likely_market=None, best_value=None,
                    state="INSUFFICIENT DATA",
                    reason=market.get("reason") or "no priced anytime-TD outcome",
                    market=market)
    rows.sort(key=lambda r: -(r["final_prob"] if r["final_prob"] is not None
                              else r["implied_prob"]))
    modelled = [r for r in rows if r["model_prob"] is not None]
    valued = sorted([r for r in rows if r["recommendable"] and (r["edge_pct"] or 0) > 0],
                    key=lambda r: -r["ev_per_unit"])
    return dict(rows=rows,
                most_likely=modelled[0] if modelled else None,
                most_likely_market=max(rows, key=lambda r: r["implied_prob"]),
                best_value=valued[0] if valued else None,
                modelled=len(modelled), state="OK", market=market)


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