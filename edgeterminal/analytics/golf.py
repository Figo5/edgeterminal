"""Golf model: strokes-gained driven Monte Carlo over the full field.

Separates "most likely to win" from "best value at the current price" -- the
two are frequently different players and the terminal reports both.

The named field we hold stats for is usually a subset of the real field, so the
simulation fills the remainder with shadow players at a field-average baseline.
Otherwise a 25-name field would make "top 20" nearly certain.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

SIMS = 20000
ROUND_SD = 2.85          # per-round scoring sd vs own mean, strokes
FIELD_SD_FLOOR = 2.0
DEFAULT_FIELD_SIZE = 144
CUT_LINE = 65            # top 65 and ties, the standard PGA TOUR cut


def adjusted_sg(p: Dict) -> float:
    """Blend baseline SG with form, course fit and (lightly) course history.

    Course history is deliberately the smallest term: it is mostly a proxy for
    skill and fit that we already count, and over-weighting it is the classic
    golf-betting error.
    """
    sg = float(p.get("sg_total", 0.0))
    form = float(p.get("form", 0.5)) - 0.5          # -0.5 .. 0.5
    fit = float(p.get("course_fit", 0.5)) - 0.5
    hist = float(p.get("history", 0.5)) - 0.5
    return sg + 0.55 * form + 0.45 * fit + 0.12 * hist


_cache: Dict[Tuple, Dict] = {}


def field_probabilities(field: Dict[str, Dict], rounds_left: int = 4,
                        positions: Optional[Dict[str, float]] = None,
                        field_size: int = DEFAULT_FIELD_SIZE,
                        sims: int = SIMS) -> Dict[str, Dict]:
    """Win / top-5 / top-10 / top-20 / make-cut probabilities.

    positions: strokes behind the leader, for live (mid-tournament) states.
    """
    names = sorted(field)
    key = (tuple(names), rounds_left, tuple(sorted((positions or {}).items())), field_size, sims)
    if key in _cache:
        return _cache[key]

    skills = np.array([adjusted_sg(field[n]) for n in names], dtype=float)
    n_named = len(names)
    n_shadow = max(0, field_size - n_named)
    shadow_skill = float(skills.mean()) - 0.55 if n_named else 0.0

    mu = np.concatenate([skills, np.full(n_shadow, shadow_skill)])
    sd = np.maximum(FIELD_SD_FLOOR, ROUND_SD * (1.0 - 0.06 * mu))
    start = np.zeros(mu.shape)
    if positions:
        for i, n in enumerate(names):
            start[i] = float(positions.get(n, 0.0))

    r = max(1, int(rounds_left))
    rng = np.random.default_rng(97531)
    # total score to par: sum over remaining rounds of N(-skill, sd)
    totals = start + rng.normal(loc=-mu * r, scale=sd * math.sqrt(r), size=(sims, mu.size))
    ranks = totals.argsort(axis=1).argsort(axis=1)          # 0 = leader
    named = ranks[:, :n_named]

    out: Dict[str, Dict] = {}
    win = (named == 0).mean(axis=0)
    t5 = (named < 5).mean(axis=0)
    t10 = (named < 10).mean(axis=0)
    t20 = (named < 20).mean(axis=0)
    cut = (named < min(CUT_LINE, mu.size)).mean(axis=0)
    for i, n in enumerate(names):
        out[n] = dict(win=float(win[i]), top5=float(t5[i]), top10=float(t10[i]),
                      top20=float(t20[i]), make_cut=float(cut[i]),
                      skill=float(math.exp(skills[i])), adjusted_sg=round(float(skills[i]), 3))
    _cache[key] = out
    return out


def matchup_prob(field: Dict[str, Dict], a: str, b: str, rounds_left: int = 4) -> float:
    """P(a beats b) head to head over the remaining rounds."""
    sa, sb = adjusted_sg(field[a]), adjusted_sg(field[b])
    diff = (sa - sb) * max(1, rounds_left)
    sd = ROUND_SD * math.sqrt(2 * max(1, rounds_left))
    return 0.5 * (1 + math.erf(diff / (sd * math.sqrt(2))))


def course_fit_notes(p: Dict, course: Dict) -> List[str]:
    notes = []
    length = int(course.get("course_length", 7200) or 7200)
    if p.get("ott", 0) > 0.6 and length > 7250:
        notes.append(f"Long setup ({length}y) suits a {p['ott']:+.2f} SG:OTT profile")
    if p.get("app", 0) > 0.8:
        notes.append(f"Approach-heavy test: {p['app']:+.2f} SG:APP is the largest single component edge")
    if p.get("putt", 0) < -0.05:
        notes.append(f"Putting is the weak link ({p['putt']:+.2f} SG:PUTT) — the highest-variance component")
    if p.get("form", 0.5) < 0.45:
        notes.append("Recent form below the field-average baseline")
    if p.get("history", 0.5) > 0.7:
        notes.append("Strong course history (weighted lightly — largely collinear with fit)")
    return notes
