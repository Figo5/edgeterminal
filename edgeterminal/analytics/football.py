"""NFL / CFB game and prop models.

Deterministic. The LLM never touches these numbers.

Projection is an opponent-adjusted points model: each side's projected score is
the average of its own offensive output and the opponent's defensive allowance,
shifted by home-field, rest, weather and injury adjustments. Margin and total
are then turned into market probabilities with sport-specific score
distributions.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..pricing import norm_cdf, prob_over, prob_over_count, push_adjusted_cover
from .nfl_features import effective_sample_weight

# Score distribution parameters. NFL margins are famously ~13.0-13.5 sd;
# college is wider because team quality varies far more.
PARAMS = {
    "NFL": dict(hfa=1.7, sd_margin=13.2, sd_total=10.4, key_numbers=(3, 7, 10, 6, 4)),
    "CFB": dict(hfa=2.6, sd_margin=16.5, sd_total=13.5, key_numbers=(3, 7, 10, 14)),
}


def _rest_adj(days_rest: Optional[float]) -> float:
    if days_rest is None:
        return 0.0
    if days_rest >= 10:
        return 0.8
    if days_rest <= 4:
        return -0.9
    return 0.0


def weather_adjustment(weather: Optional[Dict], sport: str) -> Tuple[float, List[str]]:
    """Points to remove from the projected total. Never invented: returns 0 when
    weather is genuinely unavailable, and the caller records the uncertainty."""
    if not weather or weather.get("indoor"):
        return 0.0, []
    notes: List[str] = []
    adj = 0.0
    wind = weather.get("wind_mph")
    if wind is not None:
        if wind >= 20:
            adj -= 3.2; notes.append(f"Wind {wind:.0f}mph: passing efficiency and deep shots suppressed")
        elif wind >= 15:
            adj -= 1.8; notes.append(f"Wind {wind:.0f}mph: modest downward pressure on the total")
        elif wind >= 11:
            adj -= 0.7; notes.append(f"Wind {wind:.0f}mph: marginal")
    precip = weather.get("precip_chance")
    if precip is not None and precip >= 60:
        adj -= 1.1; notes.append(f"Precipitation likely ({precip:.0f}%)")
    temp = weather.get("temp_f")
    if temp is not None and temp <= 25:
        adj -= 1.0; notes.append(f"Cold ({temp:.0f}F): ball handling and kicking degrade")
    return adj, notes


def injury_adjustment(injuries: List[Dict], team: str, sport: str) -> Tuple[float, float, List[str]]:
    """Returns (points adjustment for this team, availability certainty, notes).

    Positional weights are coarse on purpose -- a precise injury-value model
    needs player-level data we may not have, and pretending otherwise would
    inflate confidence.
    """
    weights = {"QB": 4.5, "RB": 0.8, "WR": 1.0, "TE": 0.6, "OL": 0.9,
               "CB": 0.9, "S": 0.6, "LB": 0.5, "DL": 0.8, "K": 0.3}
    status_mult = {"out": 1.0, "doubtful": 0.75, "questionable": 0.35,
                   "probable": 0.1, "active": 0.0}
    adj, notes = 0.0, []
    certainty = 1.0
    for inj in injuries:
        if inj.get("team") != team:
            continue
        pos = (inj.get("position") or "").upper()
        st = (inj.get("status") or "").lower()
        mult = status_mult.get(st, 0.3)
        w = weights.get(pos, 0.5)
        loss = w * mult
        if loss > 0.05:
            adj -= loss
            notes.append(f"{inj.get('player')} ({pos or '?'}) {inj.get('status')} — ~{loss:.1f} pts")
        if st in ("questionable", "doubtful"):
            certainty -= 0.18 if w >= 1.0 else 0.07
    if sport == "CFB":
        # college availability reporting is materially worse
        certainty = min(certainty, 0.62)
    return adj, max(0.15, certainty), notes


def project_game(sport: str, home: str, away: str, stats: Dict[str, Dict],
                 weather: Optional[Dict] = None, injuries: Optional[List[Dict]] = None,
                 home_rest: Optional[float] = None, away_rest: Optional[float] = None,
                 neutral: bool = False) -> Dict:
    p = PARAMS[sport]
    hs, as_ = stats.get(home), stats.get(away)
    if not hs or not as_:
        raise ValueError(f"missing team stats for {home if not hs else away}")

    h_off = float(hs.get("points_for", hs.get("off", 0)))
    h_def = float(hs.get("points_against", hs.get("deff", 0)))
    a_off = float(as_.get("points_for", as_.get("off", 0)))
    a_def = float(as_.get("points_against", as_.get("deff", 0)))

    hfa = 0.0 if neutral else p["hfa"]
    ph = (h_off + a_def) / 2 + hfa / 2
    pa = (a_off + h_def) / 2 - hfa / 2

    feature_groups: Dict[str, float] = {}
    if sport == "NFL":
        # One bounded efficiency group avoids independently over-weighting
        # correlated EPA, success rate, yards/play and scoring inputs.
        epa_gap = float(hs.get("epa_play", 0)) - float(as_.get("epa_play", 0))
        success_gap = float(hs.get("success_rate", 0.45)) - float(as_.get("success_rate", 0.45))
        efficiency_points = max(-3.0, min(3.0, epa_gap * 6.0 + success_gap * 8.0))
        ph += efficiency_points / 2
        pa -= efficiency_points / 2
        feature_groups["efficiency_points"] = round(efficiency_points, 3)

    notes: List[str] = []
    inj = injuries or []
    h_inj, h_cert, h_notes = injury_adjustment(inj, home, sport)
    a_inj, a_cert, a_notes = injury_adjustment(inj, away, sport)
    ph += h_inj; pa += a_inj
    notes += [f"{home}: {n}" for n in h_notes] + [f"{away}: {n}" for n in a_notes]

    ph += _rest_adj(home_rest) / 2
    pa += _rest_adj(away_rest) / 2

    wx_adj, wx_notes = weather_adjustment(weather, sport)
    if wx_adj:
        ph += wx_adj / 2; pa += wx_adj / 2
    notes += wx_notes

    margin = ph - pa
    total = ph + pa
    sd_m = p["sd_margin"]
    sd_t = p["sd_total"]
    sample_n = min(int(hs.get("games", 0) or 0), int(as_.get("games", 0) or 0))
    sample = effective_sample_weight(sample_n)
    uncertainty = sample["uncertainty"]
    if sport == "NFL":
        sd_m *= 1.0 + uncertainty * 0.15
        sd_t *= 1.0 + uncertainty * 0.12
    # CFB: efficiency gaps are larger and less reliable -> widen a little
    if sport == "CFB":
        sd_m *= 1.03

    return dict(
        sport=sport, home=home, away=away,
        proj_home=round(ph, 2), proj_away=round(pa, 2),
        margin=round(margin, 2), total=round(total, 2),
        sd_margin=round(sd_m, 4), sd_total=round(sd_t, 4),
        home_win_prob=round(1 - norm_cdf((0 - margin) / sd_m), 4),
        fair_home_spread=round(-margin * 2) / 2,
        fair_total=round(total * 2) / 2,
        availability_certainty=round(min(h_cert, a_cert), 3),
        weather_points=round(wx_adj, 2),
        sample_size_n=sample_n, uncertainty=uncertainty,
        feature_groups=feature_groups, feature_version="nfl-features-1" if sport == "NFL" else "base-1",
        notes=notes,
    )


def spread_prob(proj: Dict, team: str, line: float) -> float:
    margin = proj["margin"] if team == proj["home"] else -proj["margin"]
    return push_adjusted_cover(margin, proj["sd_margin"], line)


def total_prob(proj: Dict, side: str, line: float) -> float:
    p_over = prob_over(proj["total"], proj["sd_total"], line)
    return p_over if side.lower() == "over" else 1 - p_over


def moneyline_prob(proj: Dict, team: str) -> float:
    return proj["home_win_prob"] if team == proj["home"] else 1 - proj["home_win_prob"]


# ------------------------------------------------------------------ props
PROP_SD_FLOOR = {"passing_yards": 45.0, "rushing_yards": 22.0, "receiving_yards": 20.0,
                 "receptions": 1.6, "passing_tds": 0.9}


def project_prop(player: Dict, prop: str, game_proj: Optional[Dict] = None,
                 opponent_factor: float = 1.0, injuries: Optional[List[Dict]] = None) -> Dict:
    """Project a player prop from usage first, box score second.

    Usage (snap rate, target share, carry share) is the stable signal; raw
    recent production is noisy and already reflected in the market. Game script
    from the projected margin scales pass/rush volume.
    """
    props = player.get("props", {})
    if prop not in props:
        raise ValueError(f"no baseline for {player.get('name')} {prop}")
    mean, sd = props[prop]
    usage = player.get("usage", {}) or {}
    n = int(usage.get("games", 0))

    adjustments: List[str] = []
    m = float(mean)

    # game script: leading teams run, trailing teams pass
    if game_proj:
        team = player.get("team")
        margin = game_proj["margin"] if team == game_proj["home"] else -game_proj["margin"]
        if prop.startswith("passing") or prop.startswith("receiving") or prop == "receptions":
            script = 1.0 - margin * 0.006
        elif prop.startswith("rushing"):
            script = 1.0 + margin * 0.008
        else:
            script = 1.0
        if abs(script - 1.0) > 0.015:
            m *= script
            adjustments.append(f"Game script ({margin:+.1f} projected margin): volume x{script:.3f}")
        # total environment
        base_total = 44.0 if game_proj["sport"] == "NFL" else 52.0
        pace = 1.0 + (game_proj["total"] - base_total) * 0.004
        if abs(pace - 1.0) > 0.01:
            m *= pace
            adjustments.append(f"Projected total {game_proj['total']:.1f}: volume x{pace:.3f}")

    if opponent_factor != 1.0:
        m *= opponent_factor
        adjustments.append(f"Opponent positional tendency: x{opponent_factor:.3f}")

    snap = usage.get("snap_rate")
    if snap is not None and snap < 0.75:
        adjustments.append(f"Snap rate {snap:.0%} — rotational role caps the ceiling")

    # availability of the player himself
    certainty = 1.0
    for inj in (injuries or []):
        if inj.get("player") == player.get("name"):
            st = (inj.get("status") or "").lower()
            certainty = {"out": 0.0, "doubtful": 0.3, "questionable": 0.6,
                         "probable": 0.88}.get(st, 0.8)
            adjustments.append(f"Listed {inj.get('status')} — availability risk")

    sd_final = max(float(sd), PROP_SD_FLOOR.get(prop, float(sd) * 0.3))
    return dict(mean=round(m, 2), sd=round(sd_final, 2), n=n,
                cv=round(sd_final / max(1e-6, m), 3),
                availability_certainty=certainty, adjustments=adjustments,
                usage=usage)


COUNT_PROPS = ('passing_tds', 'receptions', 'rushing_tds', 'receiving_tds', 'interceptions')


def prop_prob(projection: Dict, side: str, line: float, prop: Optional[str] = None) -> float:
    """Continuous props use a normal; discrete counts use Poisson / neg-binomial."""
    if prop in COUNT_PROPS or (projection.get("mean", 99) < 12 and projection.get("discrete")):
        p_over = prob_over_count(projection["mean"], line, projection.get("sd"))
    else:
        p_over = prob_over(projection["mean"], projection["sd"], line)
    return p_over if side.lower() == "over" else 1 - p_over


def regression_flags(player: Dict, prop: str, line: float, projection: Dict) -> List[str]:
    """Explicit checks against the 'hit the over 5 straight' trap."""
    flags = []
    mean = projection["mean"]
    base = player.get("props", {}).get(prop, (mean, 0))[0]
    if line > base * 1.12:
        flags.append(f"Line ({line}) sits {(line/base-1)*100:.0f}% above the baseline projection — "
                     "market has already priced recent production")
    if line < base * 0.88:
        flags.append(f"Line ({line}) sits {(1-line/base)*100:.0f}% below baseline — check for a role "
                     "or injury change the model has not ingested")
    usage = player.get("usage", {}) or {}
    if usage.get("games", 0) < 5:
        flags.append(f"Only {usage.get('games', 0)} games of usage data — small sample")
    return flags
