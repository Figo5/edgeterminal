"""Frozen NBA research model on sourced historical boxscore rows.

RESEARCH_ONLY / PASS. Nothing here calls a provider, the Odds API, a database,
or an LLM, and nothing here enables official picks. Every number comes from
caller-supplied, sourced boxscore rows recorded before the target tipoff and
old enough to have finished (tipoff minus a fixed feature lag). When sourced
inputs are missing or thin the functions return an explicit ``unavailable``
status — never an invented league-mean forecast.

All fixed parameters and the historical prop fit are fingerprinted into every
output. Opportunity and uncertainty parameters use only games before the
original October 1 training cutoff. No running-season outcomes are fitted.
The team game component retains its previous fixed baseline.

Unlike the legacy analytics/nba.py path, this research path applies no
arbitrary injury point or usage bumps: injuries only gate or annotate
selection, and out/doubtful/inactive statuses block it.

Selection ranks by strongest supported MODEL PROBABILITY (top-five confidence
research style). Implied-probability differences are descriptive only: they
never filter for positive edge and never order candidates.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .analytics.nba import HFA, PROP_SD_FLOOR, SD_MARGIN, SD_TOTAL
from .pricing import (american_to_decimal, american_to_prob, norm_cdf,
                      prob_over, prob_over_count)
from . import nba_prop_model
from .nba_model_manifest import ARTIFACT_SHA256, SERVING_SOURCE_SHA256

MODEL_VERSION = "nba-research-2.0.1"
GAME_COMPONENT_VERSION = "nba-game-baseline-1.1.0"
READINESS = "RESEARCH_ONLY"
DESIGNATION = "PASS"

# Canonical NBA abbreviations (ESPN convention).
NBA_TEAMS = frozenset({
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
})

MARKETS = ("points", "rebounds", "assists", "threes", "pra")
COUNT_MARKETS = ("rebounds", "assists", "threes")  # discrete counts -> Poisson/NB

# Fixed a-priori structural constants (documented in docs/research/nba_model.md).
MIN_TEAM_GAMES = 10        # sourced team rows required before tipoff
TEAM_WINDOW = 20           # most recent eligible team rows used
MIN_PROP_GAMES = 5         # sourced player rows required before tipoff
PROP_WINDOW = 15           # most recent eligible player rows used
REST_B2B_DAYS = 1.5        # < 36h between timestamped games is a back-to-back
B2B_HOME_ADJ = -1.4        # fixed rest penalties carried from analytics/nba.py
B2B_AWAY_ADJ = -1.8
MINUTES_CV_FLAG = 0.25     # minutes sd/mean above this is surfaced as volatile
SELECTIONS = 5             # exactly this many selections or wait
MAX_MARKETS_PER_PLAYER = 2 # per-player market cap, relaxed only if infeasible
QUOTE_STALENESS_S = 3600   # quotes older than this at `now` are rejected
FEATURE_LAG_S = 4 * 3600   # a game row is input-eligible once it could be final
POSTGAME_DELAY_S = 4 * 3600
BLOCKED_AVAILABILITY = ("out", "doubtful", "inactive")

PROP_PARAMETERS = nba_prop_model.load_parameters(
    Path(__file__).with_name('nba_prop_parameters.json'),ARTIFACT_SHA256,SERVING_SOURCE_SHA256)
PROP_FIT_SHA256 = hashlib.sha256(json.dumps(
    PROP_PARAMETERS, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

PARAMETERS = {
    "model_version": MODEL_VERSION,
    "game_component_version": GAME_COMPONENT_VERSION,
    "prop_fit_sha256": PROP_FIT_SHA256,
    "hfa": HFA, "sd_margin": SD_MARGIN, "sd_total": SD_TOTAL,
    "min_team_games": MIN_TEAM_GAMES, "team_window": TEAM_WINDOW,
    "min_prop_games": MIN_PROP_GAMES, "prop_window": PROP_WINDOW,
    "rest_b2b_days": REST_B2B_DAYS,
    "b2b_home_adj": B2B_HOME_ADJ, "b2b_away_adj": B2B_AWAY_ADJ,
    "minutes_cv_flag": MINUTES_CV_FLAG,
    "selections": SELECTIONS,
    "max_markets_per_player": MAX_MARKETS_PER_PLAYER,
    "quote_staleness_s": QUOTE_STALENESS_S,
    "feature_lag_s": FEATURE_LAG_S,
    "postgame_delay_s": POSTGAME_DELAY_S,
    "blocked_availability": sorted(BLOCKED_AVAILABILITY),
    "markets": list(MARKETS),
    "count_markets": list(COUNT_MARKETS),
    "prop_sd_floor": {k: PROP_SD_FLOOR[k] for k in MARKETS},
    "probability": {
        "integer_line_continuity_correction": True,
        "push_mass_excluded_from_over_under": True,
        "active_prop_model": "historical_standardized_residual_cdf",
        "historical_fallback": "poisson_or_negative_binomial_and_normal",
        "fit_eligible_from": PROP_PARAMETERS['eligible_from'],
    },
    "selection_rule": "rank_by_model_probability_implied_descriptive_only",
    "main_line_rule": "explicit_line_type_main_or_is_main_true_only",
}
PARAMETER_FINGERPRINT = hashlib.sha256(
    json.dumps(PARAMETERS, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


# --------------------------------------------------------------------- parsing

def parse_time(value: object) -> Optional[float]:
    """Epoch seconds for an epoch number, datetime, or ISO-8601 string.

    Naive datetimes are treated as UTC. A date-only string becomes 00:00:00 UTC
    on that date. Unparseable values return None (fail closed).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def valid_number(value: object, nonnegative: bool = False) -> Optional[float]:
    """A finite float, or None. Booleans, NaN and infinities are not stats."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def _row_time(raw: Dict) -> Optional[float]:
    played = raw.get("played_at")
    return parse_time(played if played is not None else raw.get("date"))


def _base(**extra) -> Dict:
    out = {"model_version": MODEL_VERSION,
           "parameter_fingerprint": PARAMETER_FINGERPRINT,
           "readiness": READINESS, "designation": DESIGNATION}
    out.update(extra)
    return out


def historical_evaluation() -> Dict:
    """Frozen historical acceptance report; no fitting or database access."""
    report = json.loads(json.dumps(PROP_PARAMETERS['historical_validation']))
    report.update(model_version=MODEL_VERSION,parameter_fingerprint=PARAMETER_FINGERPRINT,
                  artifact_sha256=PROP_FIT_SHA256,
                  source_catalog=PROP_PARAMETERS['source_catalog'])
    return report


# -------------------------------------------------------------- team histories

def _team_rows(history: Sequence[Dict], team: str, tipoff: float
               ) -> Tuple[List[Dict], int]:
    """Valid sourced rows for one team, oldest first, plus conflict count.

    A row is eligible only when its game could have finished before tipoff
    (row time at most tipoff - FEATURE_LAG_S), so same-day simultaneous games
    never enter prior inputs. Rows sharing a game_id must carry identical
    numeric stats; a conflicting duplicate taints that game and every row for
    it is rejected (fail closed). Identical duplicates collapse to one row.
    """
    cutoff = tipoff - FEATURE_LAG_S
    rows: List[Dict] = []
    seen: Dict[str, Tuple] = {}
    tainted = set()
    for raw in history or []:
        if not isinstance(raw, dict) or raw.get("team") != team:
            continue
        when = _row_time(raw)
        if when is None or when > cutoff:
            continue
        pts = valid_number(raw.get("points"), nonnegative=True)
        opp = valid_number(raw.get("opponent_points"), nonnegative=True)
        if pts is None or opp is None:
            continue
        poss = None
        if raw.get("possessions") is not None:
            poss = valid_number(raw.get("possessions"))
            if poss is not None and poss <= 0:
                poss = None
        gid = raw.get("game_id")
        if gid is not None:
            key = str(gid)
            sig = (pts, opp, poss)
            if key in seen:
                if seen[key] != sig:
                    tainted.add(key)
                continue
            seen[key] = sig
        rows.append({"ts": when, "points": pts, "opponent_points": opp,
                     "possessions": poss,
                     "game_id": None if gid is None else str(gid)})
    rows = [r for r in rows if r["game_id"] not in tainted]
    rows.sort(key=lambda r: r["ts"])
    return rows[-TEAM_WINDOW:], len(tainted)


def _rest(rows: List[Dict], label: str, tipoff: float,
          history_complete: bool, notes: List[str]) -> Tuple[str, float]:
    """Rest context from timestamped prior games; missing context is unknown."""
    if not history_complete:
        notes.append(f"{label} schedule context incomplete — rest unknown, "
                     "no back-to-back adjustment assumed")
        return "unknown", 0.0
    days = (tipoff - rows[-1]["ts"]) / 86400.0
    if days < REST_B2B_DAYS:
        adj = B2B_HOME_ADJ if label == "home" else B2B_AWAY_ADJ
        notes.append(f"{label} on a back-to-back ({days:.1f}d since last "
                     "sourced game): fixed {adj:+.1f} pt adjustment")
        return "b2b", adj
    return "normal", 0.0


def project_game(home: str, away: str, team_history: Sequence[Dict], tipoff,
                 history_complete: bool = True) -> Dict:
    """Projected scores/margin/total/home-win probability from sourced rows.

    team_history rows: date or played_at, team, opponent, points,
    opponent_points, optional possessions, home bool, game_id. Only records
    old enough to have finished before tipoff are eligible. With fewer than
    MIN_TEAM_GAMES sourced rows for either team the projection is
    unavailable — it is never replaced by a league-mean guess.
    """
    base = _base(sport="NBA", home=home, away=away,
                 game_component_version=GAME_COMPONENT_VERSION)
    tip = parse_time(tipoff)
    if tip is None:
        return dict(base, status="unavailable", reason="invalid_tipoff")
    base["tipoff"] = tip
    if home not in NBA_TEAMS or away not in NBA_TEAMS or home == away:
        return dict(base, status="unavailable", reason="unknown_team")
    h, h_conflicts = _team_rows(team_history, home, tip)
    a, a_conflicts = _team_rows(team_history, away, tip)
    base["input_conflicts"] = h_conflicts + a_conflicts
    if len(h) < MIN_TEAM_GAMES or len(a) < MIN_TEAM_GAMES:
        return dict(base, status="unavailable",
                    reason="insufficient_team_history",
                    observations={"home": len(h), "away": len(a)})

    pace_adjusted = all(r["possessions"] for r in h + a)
    if pace_adjusted:
        h_off = statistics.mean(r["points"] / r["possessions"] * 100 for r in h)
        h_def = statistics.mean(r["opponent_points"] / r["possessions"] * 100 for r in h)
        a_off = statistics.mean(r["points"] / r["possessions"] * 100 for r in a)
        a_def = statistics.mean(r["opponent_points"] / r["possessions"] * 100 for r in a)
        pace = (statistics.mean(r["possessions"] for r in h) +
                statistics.mean(r["possessions"] for r in a)) / 2
        ph = (h_off + a_def) / 2 / 100 * pace
        pa = (a_off + h_def) / 2 / 100 * pace
    else:
        pace = None
        ph = (statistics.mean(r["points"] for r in h) +
              statistics.mean(r["opponent_points"] for r in a)) / 2
        pa = (statistics.mean(r["points"] for r in a) +
              statistics.mean(r["opponent_points"] for r in h)) / 2
    ph += HFA / 2
    pa -= HFA / 2

    notes: List[str] = []
    rest_home, adj_home = _rest(h, "home", tip, history_complete, notes)
    rest_away, adj_away = _rest(a, "away", tip, history_complete, notes)
    ph += adj_home
    pa += adj_away

    margin, total = ph - pa, ph + pa
    return dict(base, status="ok",
                proj_home=round(ph, 2), proj_away=round(pa, 2),
                margin=round(margin, 2), total=round(total, 2),
                home_win_probability=round(norm_cdf(margin / SD_MARGIN), 4),
                sd_margin=SD_MARGIN, sd_total=SD_TOTAL,
                pace_adjusted=pace_adjusted,
                expected_pace=round(pace, 1) if pace is not None else None,
                observations={"home": len(h), "away": len(a)},
                rest={"home": rest_home, "away": rest_away},
                rest_days={"home": round((tip - h[-1]["ts"]) / 86400.0, 2),
                           "away": round((tip - a[-1]["ts"]) / 86400.0, 2)},
                uncertainty={"sd_margin": SD_MARGIN, "sd_total": SD_TOTAL,
                             "epistemic": "descriptive baseline; no injury, "
                                          "matchup or lineup adjustment"},
                notes=notes)


# ------------------------------------------------------------- player histories

_STAT_FIELDS = ("points", "rebounds", "assists", "threes")


def _player_rows(history: Sequence[Dict], player_id: str, market: str,
                 tipoff: float) -> Tuple[List[Dict], int]:
    """Valid rows for one player, oldest first, plus conflict count.

    Rows with unknown minutes or non-numeric stats are excluded (fail closed);
    sparse identities never borrow another player's rows. Duplicate game_id
    rows must carry identical numeric stats; conflicting duplicates taint the
    game and are rejected. The FEATURE_LAG_S finish rule matches team rows.
    """
    cutoff = tipoff - FEATURE_LAG_S
    rows: List[Dict] = []
    seen: Dict[str, Tuple] = {}
    tainted = set()
    for raw in history or []:
        if not isinstance(raw, dict) or str(raw.get("player_id")) != player_id:
            continue
        when = _row_time(raw)
        if when is None or when > cutoff:
            continue
        minutes = valid_number(raw.get("minutes"))
        if minutes is None or minutes <= 0:
            continue
        nums = {k: valid_number(raw.get(k), nonnegative=True)
                for k in _STAT_FIELDS}
        if market == "pra":
            if any(nums[k] is None for k in ("points", "rebounds", "assists")):
                continue
            stat = sum(nums[k] for k in ("points", "rebounds", "assists"))
        else:
            if nums[market] is None:
                continue
            stat = nums[market]
        gid = raw.get("game_id")
        if gid is not None:
            key = str(gid)
            sig = (minutes,) + tuple(nums[k] for k in _STAT_FIELDS)
            if key in seen:
                if seen[key] != sig:
                    tainted.add(key)
                continue
            seen[key] = sig
        rows.append({"ts": when, "minutes": minutes, "stat": stat,
                     "game_id": None if gid is None else str(gid)})
    rows = [r for r in rows if r["game_id"] not in tainted]
    rows.sort(key=lambda r: r["ts"])
    return rows[-PROP_WINDOW:], len(tainted)


# --------------------------------------------------------------- probabilities

def _clamp(p: float) -> float:
    return max(0.0, min(1.0, p))


def _count_over_push(mean: float, sd: float, k: int) -> Tuple[float, float]:
    """P(X >= k+1) and P(X == k) for a count prop (Poisson or neg-binomial)."""
    mean = max(1e-6, float(mean))
    var = sd ** 2 if sd else mean
    if var <= mean * 1.05:
        cum, term, push = 0.0, math.exp(-mean), 0.0
        for i in range(0, k + 1):
            if i:
                term *= mean / i
            if i == k:
                push = term
            cum += term
        return _clamp(1.0 - cum), _clamp(push)
    r = mean * mean / (var - mean)
    p = r / (r + mean)
    cum, term, push = 0.0, p ** r, 0.0
    for i in range(0, k + 1):
        if i:
            term *= (r + i - 1) / i * (1 - p)
        if i == k:
            push = term
        cum += term
    return _clamp(1.0 - cum), _clamp(push)


def probabilities(market: str, mu: float, sd: float, line: float
                  ) -> Tuple[float, float, float]:
    """(p_over, p_under, p_push); push mass is never folded into a side.

    Integer lines are true push points for counting stats: count markets take
    the exact discrete mass, and the normal approximation for points/pra uses
    a continuity correction (over is X >= line+1, under is X <= line-1).
    Half lines have no push mass.
    """
    if abs(line - round(line)) < 1e-9:
        if market in COUNT_MARKETS:
            p_over, p_push = _count_over_push(mu, sd, int(round(line)))
            p_under = 1.0 - p_over - p_push
        else:
            hi = (line + 0.5 - mu) / sd
            lo = (line - 0.5 - mu) / sd
            p_push = norm_cdf(hi) - norm_cdf(lo)
            p_over = 1.0 - norm_cdf(hi)
            p_under = norm_cdf(lo)
    else:
        p_push = 0.0
        if market in COUNT_MARKETS:
            p_over = prob_over_count(mu, line, sd)
        else:
            p_over = prob_over(mu, sd, line)
        p_under = 1.0 - p_over
    return _clamp(p_over), _clamp(p_under), _clamp(p_push)


def project_prop(player_id: str, market: str, player_history: Sequence[Dict],
                 tipoff, line: Optional[float] = None) -> Dict:
    """Opportunity projection with frozen historical error calibration.

    Older historical targets before the fitted calibration end retain the
    unfitted baseline, preventing future calibration data from leaking into
    historical evaluations. Live parameters never refit during refresh.
    """
    if market not in MARKETS:
        raise ValueError(f"unsupported market {market!r}")
    base = _base(player_id=str(player_id), market=market)
    tip = parse_time(tipoff)
    if tip is None:
        return dict(base, status="unavailable", reason="invalid_tipoff")
    base["tipoff"] = tip
    rows, conflicts = _player_rows(player_history, str(player_id), market, tip)
    base["input_conflicts"] = conflicts
    if len(rows) < MIN_PROP_GAMES:
        return dict(base, status="unavailable", reason="insufficient_games",
                    n=len(rows))

    minutes = [r["minutes"] for r in rows]
    stats = [r["stat"] for r in rows]
    proj_minutes = statistics.mean(minutes)
    mu = statistics.mean(s / m for s, m in zip(stats, minutes)) * proj_minutes
    upgraded = tip >= PROP_PARAMETERS['eligible_from']
    if upgraded:
        mu, proj_minutes = nba_prop_model.predict(rows, tip, market, PROP_PARAMETERS)
    sd_raw = statistics.stdev(stats) if len(stats) > 1 else 0.0
    sd = max(sd_raw, PROP_SD_FLOOR[market])
    minutes_sd = statistics.stdev(minutes) if len(minutes) > 1 else 0.0
    minutes_flag = ("volatile" if proj_minutes > 0 and
                    minutes_sd / proj_minutes > MINUTES_CV_FLAG else "stable")

    out = dict(base, status="ok", projection=round(mu, 2),
               raw_projection=mu, raw_sd=sd, sd=round(sd, 2), n=len(rows),
               projected_minutes=round(proj_minutes, 1),
               minutes_sd=round(minutes_sd, 2), minutes_flag=minutes_flag,
               probability_over=None, probability_under=None,
               probability_push=None,
               probability_method=(('historical_standardized_residual_cdf' if
                                    PROP_PARAMETERS['markets'][market]['probability_method']=='empirical'
                                    else 'historical_validated_parametric') if upgraded
                                   else 'historical_unfitted_baseline'),
               point_method=(PROP_PARAMETERS['markets'][market]['point_method'] if upgraded
                             else 'baseline'),
               calibration_n=(PROP_PARAMETERS['markets'][market]['calibration_n'] if upgraded else 0),
               limits=["historical current-roster sample has survivorship bias",
                       "no opponent, pace, matchup or injury adjustment",
                       "historical distribution evaluation uses no sportsbook lines",
                       "integer lines carry push mass excluded from both "
                       "sides"])
    if line is not None:
        number = valid_number(line)
        if number is None:
            raise ValueError("invalid prop line")
        p_over, p_under, p_push = forecast_probabilities(out, number)
        out["line"] = number
        out["probability_over"] = round(p_over, 4)
        out["probability_under"] = round(p_under, 4)
        out["probability_push"] = round(p_push, 4)
    return out


def forecast_probabilities(projection: Dict, line: float) -> Tuple[float, float, float]:
    """Use the saved projection's own distribution for both display and rank."""
    market = projection['market']
    mu, sd = projection['raw_projection'], projection.get('raw_sd', projection['sd'])
    if projection.get('probability_method') in {
            'historical_standardized_residual_cdf','historical_validated_parametric'}:
        return nba_prop_model.probabilities(market, mu, sd, line, PROP_PARAMETERS)
    return probabilities(market, mu, sd, line)


# ------------------------------------------------------------------ selection

def _excluded(quote: Dict, reason: str) -> Dict:
    return {"player_id": quote.get("player_id"),
            "market": quote.get("market"),
            "game_id": quote.get("game_id"),
            "sportsbook": quote.get("sportsbook"),
            "line": quote.get("line"), "reason": reason}


def _main_line_provenance(quote: Dict) -> Optional[str]:
    """None only for explicit, consistent main-line provenance.

    The parent adapter sets line_type="main" and is_main=True. Anything
    alternate, conflicting, or undocumented is rejected — main lines are never
    inferred from cross-book tolerance.
    """
    line_type = quote.get("line_type")
    is_main = quote.get("is_main")
    lt_main = isinstance(line_type, str) and line_type.strip().lower() == "main"
    lt_alt = line_type is not None and not lt_main
    im_main = is_main is True
    im_alt = is_main is not None and is_main is not True
    if (lt_main and im_alt) or (im_main and lt_alt):
        return "conflicting_main_line_metadata"
    if lt_alt or im_alt:
        return "alternate_line"
    if lt_main or im_main:
        return None
    return "missing_main_line_provenance"


def select_props(quotes: Sequence[Dict], player_history: Sequence[Dict],
                 tipoff, now, injuries: Optional[Sequence[Dict]] = None,
                 count: int = SELECTIONS) -> Dict:
    """Rank verified pre-tipoff main-line quotes into five research selections.

    Candidates are ranked by strongest supported MODEL PROBABILITY (top-five
    confidence style); the implied-probability difference stays descriptive
    and never filters or orders. One selection per distinct player/stat
    market, at most two markets per player when feasible, exactly ``count``
    selections or wait. Quotes need verified identities, explicit main-line
    provenance, and an observed_at strictly before tipoff, not after ``now``
    and no older than QUOTE_STALENESS_S. Out/doubtful/inactive players are
    blocked; questionable and unknown availability are surfaced.
    """
    tip, now_ts = parse_time(tipoff), parse_time(now)
    if tip is None or now_ts is None:
        raise ValueError("select_props requires valid tipoff and now")
    if now_ts >= tip:
        raise ValueError("select_props requires now before tipoff")
    blocked, availability_flags = set(), {}
    for inj in injuries or []:
        if not isinstance(inj, dict):
            continue
        pid, status = inj.get("player_id"), str(inj.get("status") or "").lower()
        if pid is None:
            continue
        if status in BLOCKED_AVAILABILITY:
            blocked.add(str(pid))
        elif status == "questionable":
            availability_flags[str(pid)] = "questionable"
        elif status == "unknown":
            availability_flags[str(pid)] = "availability_unknown"

    excluded: List[Dict] = []
    pool: List[Dict] = []
    for q in quotes or []:
        if not isinstance(q, dict):
            continue
        pid = q.get("player_id")
        if q.get("identity_verified") is not True:
            excluded.append(_excluded(q, "identity_unverified"))
            continue
        if q.get("market") not in MARKETS:
            excluded.append(_excluded(q, "unsupported_market"))
            continue
        if q.get("side") not in ("over", "under"):
            excluded.append(_excluded(q, "invalid_side"))
            continue
        line = valid_number(q.get("line"))
        price = valid_number(q.get("price"))
        if line is None:
            excluded.append(_excluded(q, "invalid_line"))
            continue
        if price is None or abs(price) < 100:
            excluded.append(_excluded(q, "invalid_price"))
            continue
        provenance = _main_line_provenance(q)
        if provenance is not None:
            excluded.append(_excluded(q, provenance))
            continue
        observed = parse_time(q.get("observed_at"))
        if observed is None:
            excluded.append(_excluded(q, "invalid_observed_at"))
            continue
        if observed >= tip:
            excluded.append(_excluded(q, "after_tipoff"))
            continue
        if observed > now_ts:
            excluded.append(_excluded(q, "future_quote"))
            continue
        if now_ts - observed > QUOTE_STALENESS_S:
            excluded.append(_excluded(q, "stale_quote"))
            continue
        if str(pid) in blocked:
            excluded.append(_excluded(q, "injury_out_or_doubtful"))
            continue
        pool.append(dict(q, _line=line, _price=price, _observed=observed))

    # One candidate per distinct (game, player, market): the quote whose
    # model-supported side is strongest; ties break to the better price, then
    # deterministically. No edge filtering, no cross-book line inference.
    groups: Dict[Tuple, List[Dict]] = {}
    for q in pool:
        groups.setdefault((q.get("game_id"), str(q.get("player_id")),
                           q["market"]), []).append(q)
    candidates: List[Dict] = []
    for (game_id, pid, market), group in sorted(groups.items()):
        proj = project_prop(pid, market, player_history, tip)
        if proj["status"] != "ok":
            excluded.append({"player_id": pid, "market": market,
                             "game_id": game_id,
                             "reason": "projection_unavailable"})
            continue
        best = None
        for q in group:
            p_over, p_under, p_push = forecast_probabilities(proj, q["_line"])
            # The strongest side is chosen by model probability among the
            # actually-quoted sides, each with its own real price.
            p_side = p_over if q["side"] == "over" else p_under
            key = (-p_side, -american_to_decimal(q["_price"]),
                   str(q.get("sportsbook")), q["side"])
            if best is None or key < best[0]:
                best = (key, q, p_side, p_push)
        _, quote, p_side, p_push = best
        side = quote["side"]
        implied = american_to_prob(quote["_price"])
        flags = []
        if pid in availability_flags:
            flags.append(availability_flags[pid])
        if proj["minutes_flag"] == "volatile":
            flags.append("minutes_volatile")
        candidates.append({
            "game_id": game_id, "player_id": pid,
            "player": quote.get("player"), "team": quote.get("team"),
            "market": market, "side": side, "line": quote["_line"],
            "price": quote["_price"], "sportsbook": quote.get("sportsbook"),
            "observed_at": quote["_observed"], "identity_verified": True,
            "projection": proj["projection"], "sd": proj["sd"], "n": proj["n"],
            "point_method": proj['point_method'],
            "probability_method": proj['probability_method'],
            "calibration_n": proj['calibration_n'],
            "projected_minutes": proj['projected_minutes'],
            "probability": round(p_side, 4),
            "probability_push": round(p_push, 4),
            "implied_probability": round(implied, 4),
            "edge": round(p_side - implied, 4),  # descriptive only, never ranked
            "flags": flags, "model_version": MODEL_VERSION,
            "parameter_fingerprint": PARAMETER_FINGERPRINT})

    candidates.sort(key=lambda c: (-c["probability"], c["player_id"],
                                   c["market"]))
    selected: List[Dict] = []
    per_player: Dict[str, int] = {}
    remainder: List[Dict] = []
    for cand in candidates:
        if per_player.get(cand["player_id"], 0) < MAX_MARKETS_PER_PLAYER:
            if len(selected) < count:
                selected.append(cand)
                per_player[cand["player_id"]] = \
                    per_player.get(cand["player_id"], 0) + 1
                continue
        remainder.append(cand)
    if len(selected) < count:
        # The two-per-player cap is relaxed only when it is infeasible.
        for cand in remainder:
            if len(selected) >= count:
                break
            selected.append(cand)

    status = "ok" if len(selected) >= count else "wait"
    return _base(status=status,
                 selections=selected if status == "ok" else [],
                 candidates=candidates,
                 eligible_candidates=len(candidates),
                 required=count,
                 excluded=excluded,
                 tipoff=tip, generated_at=now_ts)


# ----------------------------------------------------------------- evaluation

def _collect_games(team_history: Sequence[Dict]) -> Tuple[List[Dict], int, int]:
    """Unique games from team rows; conflicting duplicate rows are dropped."""
    games: Dict[str, Optional[Dict]] = {}
    conflicts = skipped = 0
    for raw in team_history or []:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        team, opp = raw.get("team"), raw.get("opponent")
        when = _row_time(raw)
        pts = valid_number(raw.get("points"), nonnegative=True)
        opp_pts = valid_number(raw.get("opponent_points"), nonnegative=True)
        home_flag = raw.get("home")
        if (not isinstance(team, str) or not isinstance(opp, str) or
                when is None or pts is None or opp_pts is None or
                not isinstance(home_flag, bool)):
            skipped += 1
            continue
        gid = raw.get("game_id") or f"{when}:{min(team, opp)}:{max(team, opp)}"
        game = {"ts": when, "home": team if home_flag else opp,
                "away": opp if home_flag else team,
                "home_points": pts if home_flag else opp_pts,
                "away_points": opp_pts if home_flag else pts}
        if str(gid) in games:
            if games[str(gid)] != game:
                conflicts += 1
                games[str(gid)] = None  # conflicting sourced rows: unusable
        else:
            games[str(gid)] = game
    return ([g for g in games.values() if g is not None], conflicts, skipped)


def evaluate_history(team_history: Sequence[Dict], holdout_start,
                     history_complete: bool = True) -> Dict:
    """Chronological holdout assessment of the game baseline, no future leakage.

    The training cutoff is fixed at ``holdout_start`` before the first forward
    record: rows before it are the training sample, unique games at or after
    it are projected in chronological order using only their own pre-tipoff,
    finished (FEATURE_LAG_S) rows. Metrics are descriptive error/probability
    summaries over sourced rows — with no sourced rows this reports
    unavailable, not validation.
    """
    base = _base()
    cutoff = parse_time(holdout_start)
    if cutoff is None:
        raise ValueError("evaluate_history requires a valid holdout_start")
    base["training_cutoff"] = cutoff
    games, conflicts, skipped = _collect_games(team_history)
    forward = sorted((g for g in games if g["ts"] >= cutoff),
                     key=lambda g: g["ts"])
    if not forward:
        return dict(base, status="unavailable", reason="no_forward_records",
                    forward_games=0, conflicting_games=conflicts,
                    skipped_rows=skipped)

    per_game: List[Dict] = []
    margin_errs: List[float] = []
    total_errs: List[float] = []
    briers: List[float] = []
    insufficient = 0
    for game in forward:
        proj = project_game(game["home"], game["away"], team_history,
                            game["ts"], history_complete=history_complete)
        if proj["status"] != "ok":
            insufficient += 1
            continue
        actual_margin = game["home_points"] - game["away_points"]
        actual_total = game["home_points"] + game["away_points"]
        margin_errs.append(proj["margin"] - actual_margin)
        total_errs.append(proj["total"] - actual_total)
        home_won = 1.0 if actual_margin > 0 else 0.0
        briers.append((proj["home_win_probability"] - home_won) ** 2)
        per_game.append({
            "tipoff": game["ts"], "home": game["home"], "away": game["away"],
            "projected_margin": proj["margin"],
            "actual_margin": actual_margin,
            "margin_error": round(margin_errs[-1], 2),
            "projected_total": proj["total"], "actual_total": actual_total,
            "total_error": round(total_errs[-1], 2),
            "home_win_probability": proj["home_win_probability"],
            "parameter_fingerprint": PARAMETER_FINGERPRINT})

    training_rows = sum(
        1 for raw in team_history or []
        if isinstance(raw, dict) and _row_time(raw) is not None
        and _row_time(raw) < cutoff)
    n = len(margin_errs)
    mae_m = sum(abs(e) for e in margin_errs) / n if n else None
    rmse_m = math.sqrt(sum(e * e for e in margin_errs) / n) if n else None
    mae_t = sum(abs(e) for e in total_errs) / n if n else None
    rmse_t = math.sqrt(sum(e * e for e in total_errs) / n) if n else None
    return dict(base, status="ok",
                training_rows=training_rows,
                forward_games=len(forward),
                projected_games=n,
                insufficient_games=insufficient,
                conflicting_games=conflicts, skipped_rows=skipped,
                mae_margin=round(mae_m, 3) if mae_m is not None else None,
                rmse_margin=round(rmse_m, 3) if rmse_m is not None else None,
                mae_total=round(mae_t, 3) if mae_t is not None else None,
                rmse_total=round(rmse_t, 3) if rmse_t is not None else None,
                brier_home_win=round(sum(briers) / n, 4) if n else None,
                per_game=per_game,
                note="Descriptive holdout arithmetic on caller-supplied sourced "
                     "rows; not a claim of historical validation without "
                     "source provenance.")
