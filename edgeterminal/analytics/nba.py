"""NBA game and prop models.

Possession-based: projected points = efficiency x pace. Player props are driven
by projected minutes and usage rate before recent box scores, because minutes
are what actually change night to night.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..pricing import norm_cdf, prob_over, prob_over_count, push_adjusted_cover

HFA = 2.2
SD_MARGIN = 11.6
SD_TOTAL = 16.5
LEAGUE_ORTG = 114.5


def project_game(home: str, away: str, stats: Dict[str, Dict],
                 injuries: Optional[List[Dict]] = None,
                 home_b2b: bool = False, away_b2b: bool = False,
                 altitude: bool = False) -> Dict:
    hs, as_ = stats.get(home), stats.get(away)
    if not hs or not as_:
        raise ValueError(f"missing team stats for {home if not hs else away}")

    pace = (float(hs.get("pace") or 99.0) + float(as_.get("pace") or 99.0)) / 2

    def _rtg(row, key, ppg_key, fallback):
        # Prefer a real efficiency rating; derive one from points per game when
        # the source only carries box-score totals (e.g. ESPN standings).
        v = row.get(key) or row.get(fallback)
        if v:
            return float(v)
        ppg = row.get(ppg_key)
        return float(ppg) / pace * 100 if ppg else LEAGUE_ORTG

    h_ortg = _rtg(hs, "off_rating", "points_for", "ortg")
    h_drtg = _rtg(hs, "def_rating", "points_against", "drtg")
    a_ortg = _rtg(as_, "off_rating", "points_for", "ortg")
    a_drtg = _rtg(as_, "def_rating", "points_against", "drtg")

    ph = (h_ortg + a_drtg) / 2 / 100 * pace + HFA / 2
    pa = (a_ortg + h_drtg) / 2 / 100 * pace - HFA / 2

    notes: List[str] = []
    certainty = 1.0
    for inj in (injuries or []):
        st = (inj.get("status") or "").lower()
        mult = {"out": 1.0, "doubtful": 0.7, "questionable": 0.4, "probable": 0.1}.get(st, 0.25)
        # star-agnostic default; a real minutes model would use on/off data
        pts = 2.6 * mult
        if inj.get("team") == home:
            ph -= pts
        elif inj.get("team") == away:
            pa -= pts
        else:
            continue
        notes.append(f"{inj.get('team')}: {inj.get('player')} {inj.get('status')} — ~{pts:.1f} pts")
        if st in ("questionable", "doubtful"):
            certainty -= 0.2

    if home_b2b:
        ph -= 1.4; notes.append(f"{home} on the second night of a back-to-back")
    if away_b2b:
        pa -= 1.8; notes.append(f"{away} on the second night of a back-to-back (with travel)")
    if altitude:
        pa -= 1.0; notes.append("Altitude penalty applied to the visiting team")

    margin, total = ph - pa, ph + pa
    return dict(sport="NBA", home=home, away=away, proj_home=round(ph, 2), proj_away=round(pa, 2),
                margin=round(margin, 2), total=round(total, 2), pace=round(pace, 1),
                sd_margin=SD_MARGIN, sd_total=SD_TOTAL,
                home_win_prob=round(1 - norm_cdf((0 - margin) / SD_MARGIN), 4),
                fair_home_spread=round(-margin * 2) / 2, fair_total=round(total * 2) / 2,
                availability_certainty=round(max(0.2, certainty), 3), notes=notes)


spread_prob = lambda proj, team, line: push_adjusted_cover(
    proj["margin"] if team == proj["home"] else -proj["margin"], proj["sd_margin"], line)


def total_prob(proj: Dict, side: str, line: float) -> float:
    p = prob_over(proj["total"], proj["sd_total"], line)
    return p if side.lower() == "over" else 1 - p


def moneyline_prob(proj: Dict, team: str) -> float:
    return proj["home_win_prob"] if team == proj["home"] else 1 - proj["home_win_prob"]


PROP_SD_FLOOR = {"points": 5.5, "rebounds": 2.2, "assists": 1.8, "threes": 1.3, "pra": 7.0}


def project_prop(player: Dict, prop: str, game_proj: Optional[Dict] = None,
                 injuries: Optional[List[Dict]] = None,
                 minutes_override: Optional[float] = None) -> Dict:
    props = player.get("props", {})
    if prop not in props:
        raise ValueError(f"no baseline for {player.get('name')} {prop}")
    mean, sd = props[prop]
    usage = player.get("usage", {}) or {}
    base_min = float(usage.get("minutes", 32))
    minutes = float(minutes_override if minutes_override is not None else base_min)
    adjustments: List[str] = []
    m = float(mean) * (minutes / base_min if base_min else 1.0)
    if abs(minutes - base_min) > 0.5:
        adjustments.append(f"Minutes projection {minutes:.1f} vs {base_min:.1f} baseline")

    if game_proj:
        pace_mult = 1.0 + (game_proj.get("pace", 99) - 99.0) * 0.004
        if abs(pace_mult - 1) > 0.005:
            m *= pace_mult
            adjustments.append(f"Pace {game_proj['pace']:.1f}: x{pace_mult:.3f}")
        team = player.get("team")
        margin = game_proj["margin"] if team == game_proj["home"] else -game_proj["margin"]
        if abs(margin) > 11:
            m *= 0.955
            adjustments.append(f"Blowout risk ({margin:+.1f} projected margin): garbage-time minutes haircut")

    certainty = 1.0
    for inj in (injuries or []):
        if inj.get("player") == player.get("name"):
            st = (inj.get("status") or "").lower()
            certainty = {"out": 0.0, "doubtful": 0.3, "questionable": 0.55, "probable": 0.85}.get(st, 0.8)
            adjustments.append(f"Listed {inj.get('status')} — minutes and availability at risk")
        elif inj.get("team") == player.get("team") and (inj.get("status") or "").lower() == "out":
            m *= 1.045
            adjustments.append(f"Teammate {inj.get('player')} out — usage bump applied")

    sd_final = max(float(sd), PROP_SD_FLOOR.get(prop, float(sd) * 0.3))
    return dict(mean=round(m, 2), sd=round(sd_final, 2), n=int(usage.get("games", 0)),
                cv=round(sd_final / max(1e-6, m), 3), availability_certainty=certainty,
                adjustments=adjustments, usage=usage)


COUNT_PROPS = ('threes', 'assists', 'rebounds', 'steals', 'blocks')


def prop_prob(projection: Dict, side: str, line: float, prop: Optional[str] = None) -> float:
    """Continuous props use a normal; discrete counts use Poisson / neg-binomial."""
    if prop in COUNT_PROPS or (projection.get("mean", 99) < 12 and projection.get("discrete")):
        p_over = prob_over_count(projection["mean"], line, projection.get("sd"))
    else:
        p_over = prob_over(projection["mean"], projection["sd"], line)
    return p_over if side.lower() == "over" else 1 - p_over
