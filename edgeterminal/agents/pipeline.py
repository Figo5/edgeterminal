"""Multi-agent evaluation. Each analyst is a deterministic function that emits a
structured AgentSignal -- never a prose blob. The Final Betting Strategist
combines them and is explicitly allowed to disagree with every one of them.

The LLM is not in this path. It writes the narrative afterwards, from these
same structured objects.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..config import settings
from ..pricing import american_to_prob, devig, edge_pct
from ..schemas import AgentSignal

STRENGTH_BANDS = [(0.66, "strong"), (0.33, "moderate"), (0.12, "slight")]


def _strength(direction: float) -> str:
    a = abs(direction)
    for thr, label in STRENGTH_BANDS:
        if a >= thr:
            return label
    return "neutral"


def _sig(agent: str, direction: float, note: str, **inputs) -> AgentSignal:
    d = max(-1.0, min(1.0, direction))
    return AgentSignal(agent=agent, direction=round(d, 3), strength=_strength(d),
                       note=note, inputs=inputs)


# ---------------------------------------------------------------- Quantitative
def quantitative(model_prob: float, price: float, market_fair_prob: Optional[float],
                 projection: Optional[Dict] = None, market: str = "spread",
                 line: Optional[float] = None) -> AgentSignal:
    e = edge_pct(model_prob, price)
    direction = max(-1.0, min(1.0, e / 7.0))
    bits = [f"Model {model_prob*100:.1f}% vs price implied {american_to_prob(price)*100:.1f}% "
            f"({e:+.1f} pts)"]
    if market_fair_prob is not None:
        bits.append(f"consensus no-vig {market_fair_prob*100:.1f}%")
    if projection:
        if market == "total" and "total" in projection:
            bits.append(f"projected total {projection['total']:.1f} vs line {line}")
        elif market in ("spread", "moneyline") and "margin" in projection:
            bits.append(f"projected margin {projection['margin']:+.1f}")
        elif "mean" in projection:
            bits.append(f"projection {projection['mean']:.1f} (sd {projection['sd']:.1f}) vs line {line}")
    return _sig("Quantitative Analyst", direction, "; ".join(bits),
                edge_pct=round(e, 2), model_prob=round(model_prob, 4))


# --------------------------------------------------------------------- Market
def market_analyst(quotes: List[Dict], best_price: float, model_prob: float,
                   movement: Optional[Dict] = None,
                   market_fair_prob: Optional[float] = None) -> AgentSignal:
    notes: List[str] = []
    direction = 0.0
    prices = [q["price"] for q in quotes if q.get("price") is not None]
    if len(prices) >= 2:
        spread_cents = max(american_to_prob(min(prices, key=american_to_prob)),
                           american_to_prob(max(prices, key=american_to_prob)))
        best_imp = american_to_prob(best_price)
        worst_imp = max(american_to_prob(p) for p in prices)
        shop = (worst_imp - best_imp) * 100
        if shop >= 0.8:
            direction += 0.28
            notes.append(f"Price dispersion across {len(prices)} books worth {shop:.1f} pts "
                         f"of implied probability — best number is materially better")
        else:
            notes.append(f"{len(prices)} books quoting, tight market ({shop:.1f} pts dispersion)")
    if market_fair_prob is not None:
        gap = (model_prob - market_fair_prob) * 100
        direction += max(-0.5, min(0.5, gap / 10.0))
        notes.append(f"Model sits {gap:+.1f} pts vs the no-vig market consensus")
    if movement and movement.get("delta") is not None and movement.get("magnitude", 0) > 0:
        mag = movement["magnitude"]
        toward = movement.get("toward_selection")
        if toward is True:
            direction -= min(0.45, mag / 3.0)
            notes.append(f"Market has already moved {mag:g} toward this side — "
                         f"some of the original edge may be gone")
        elif toward is False:
            direction += min(0.3, mag / 4.0)
            notes.append(f"Market has moved {mag:g} away from this side — better number than open")
        else:
            notes.append(f"Line has moved {mag:g} since the first stored snapshot")
    else:
        notes.append("No stored line history yet — movement unknown")
    return _sig("Market Analyst", direction, "; ".join(notes))


# ------------------------------------------------------------------- Matchup
def matchup_analyst(projection: Optional[Dict], selection: str, market: str,
                    stats: Optional[Dict] = None, side: Optional[str] = None) -> AgentSignal:
    if not projection:
        return _sig("Matchup Analyst", 0.0, "No structural projection available for this market")
    notes: List[str] = []
    direction = 0.0
    if market in ("spread", "moneyline") and "margin" in projection:
        m = projection["margin"]
        home = projection.get("home")
        own = m if selection == home else -m
        direction = max(-1.0, min(1.0, own / 9.0))
        notes.append(f"Efficiency model projects {selection} by {own:+.1f} "
                     f"({projection.get('proj_home')}-{projection.get('proj_away')} projected score)")
    elif market == "total" and "total" in projection:
        base = 44.0 if projection.get("sport") == "NFL" else (
            52.0 if projection.get("sport") == "CFB" else 224.0)
        tilt = (projection["total"] - base) / (6.0 if projection.get("sport") != "NBA" else 12.0)
        direction = max(-1.0, min(1.0, tilt if (side or "").lower() == "over" else -tilt))
        notes.append(f"Projected total {projection['total']:.1f} vs a {base:.0f} baseline environment")
    elif "mean" in projection:
        notes.append(f"Usage-first projection {projection['mean']:.1f} "
                     f"(cv {projection.get('cv', 0):.2f}, n={projection.get('n', 0)})")
        for a in (projection.get("adjustments") or [])[:3]:
            notes.append(a)
        direction = 0.0
    for n in (projection.get("notes") or [])[:2]:
        notes.append(n)
    return _sig("Matchup Analyst", direction, "; ".join(notes) or "No matchup edge identified")


# ------------------------------------------------------- News & Injury research
def news_analyst(injuries: List[Dict], news: List[Dict], selection: str,
                 teams: Optional[List[str]] = None, player: Optional[str] = None) -> AgentSignal:
    notes: List[str] = []
    direction = 0.0
    relevant = []
    for inj in injuries:
        if player and inj.get("player") == player:
            relevant.append(inj)
            st = (inj.get("status") or "").lower()
            if st in ("out", "doubtful"):
                direction -= 0.9
            elif st == "questionable":
                direction -= 0.35
        elif teams and inj.get("team") in teams and (inj.get("status") or "").lower() in (
                "out", "doubtful", "questionable"):
            relevant.append(inj)
    if relevant:
        notes.append("; ".join(f"{r.get('player')} ({r.get('position') or '?'}) {r.get('status')}"
                               for r in relevant[:4]))
    else:
        notes.append("No material availability news found for either side")
    verified = [n for n in news if n.get("trust") in ("official", "reported")]
    community = [n for n in news if n.get("trust") == "community"]
    if verified:
        notes.append(f"{len(verified)} reported/official items reviewed")
    if community:
        notes.append(f"{len(community)} community items treated as context only, not confirmation")
    return _sig("News & Injury Researcher", direction, "; ".join(notes),
                injuries_considered=len(relevant), news_considered=len(news))


# ----------------------------------------------------------------------- Risk
def risk_analyst(model_prob: float, price: float, confidence_inputs: Dict,
                 projection: Optional[Dict], market: str,
                 movement: Optional[Dict] = None) -> AgentSignal:
    """Deliberately adversarial: looks only for reasons this loses."""
    risks: List[str] = []
    penalty = 0.0
    cv = (projection or {}).get("cv")
    if cv and cv > 0.4:
        risks.append(f"High projection variance (cv {cv:.2f}) — the distribution is wide "
                     f"relative to the edge")
        penalty -= 0.25
    n = confidence_inputs.get("sample_size_n", 0)
    if n and n < 8:
        risks.append(f"Only {n} observations behind the projection")
        penalty -= 0.2
    if confidence_inputs.get("availability_certainty", 1.0) < 0.8:
        risks.append("Lineup/availability is not confirmed — a late scratch changes the number")
        penalty -= 0.25
    if confidence_inputs.get("data_quality", 1.0) < 0.6:
        risks.append("Input data is incomplete — treat the projection as a wide estimate")
        penalty -= 0.3
    if market == "player_prop":
        risks.append("Props are low-limit markets: books move them fast and correlation "
                     "with game script is high")
        penalty -= 0.1
    e = edge_pct(model_prob, price)
    if e > 9:
        risks.append(f"A {e:.1f} pt edge against a liquid market is more often a model or data "
                     f"error than a genuine mispricing — verify the line and the inputs")
        penalty -= 0.35
    if e > 15:
        risks.append("Edge of this size usually means the stored price is stale or the line has "
                     "been taken down; do not treat it as free value")
        penalty -= 0.35
    if movement and movement.get("toward_selection") is True and movement.get("magnitude", 0) >= 1:
        risks.append("The number has already moved our way; the remaining edge is the tail of a move")
        penalty -= 0.2
    if not risks:
        risks.append("No structural red flags identified in the available inputs")
    return _sig("Risk Analyst", penalty, "; ".join(risks))


# --------------------------------------------------------- Final Strategist
def strategist(signals: List[AgentSignal], confidence: float, edge: float,
               data_quality: str, stale: bool, breakeven: Optional[float],
               price: float, market: str, books_quoting: int = 1,
               sample_size_n: int = 0, unresolved_major_injury: bool = False) -> Dict:
    """Deterministic final decision. Returns state + trigger note + rationale.

    The strategist may override the sub-analysts: a unanimous bullish panel with
    a 1-point edge is still a PASS, and a strong quant edge with an unconfirmed
    lineup is still only a LEAN.
    """
    pick_thr = settings.official_pick_confidence
    lean_thr = settings.lean_confidence
    min_edge = settings.min_edge_pct
    risk = next((s for s in signals if s.agent == "Risk Analyst"), None)
    veto = bool(risk and risk.direction <= -0.6)

    if stale:
        return dict(state="STALE", trigger=None,
                    why="Stored odds for this market are older than the freshness window; "
                        "refresh before treating this as actionable.")
    if edge <= 0:
        return dict(state="NO EDGE", trigger=None,
                    why=f"Price implies {american_to_prob(price)*100:.1f}%, the model is at or below "
                        f"that. Nothing to bet.")
    if edge < min_edge:
        return dict(state="WAIT FOR PRICE", trigger=f"Playable at {breakeven:+d} or better" if breakeven else None,
                    why=f"Edge of {edge:.1f} pts is inside the {min_edge:.1f} pt noise floor. "
                        f"Current price is no longer playable; wait for a better number.")
    if data_quality in ("limited", "poor"):
        return dict(state="WATCHLIST", trigger="Awaiting better data coverage",
                    why=f"Data quality is {data_quality} for this market; the edge is not trustworthy enough "
                        "to size.")
    if unresolved_major_injury:
        return dict(state="WAIT", trigger="Awaiting resolution of a major-player injury",
                    why="A high-impact player has an unresolved status; the projection is not stable enough to play.")
    if books_quoting < 2:
        return dict(state="WATCHLIST", trigger="Awaiting a second independent book price",
                    why="Only one book/source is quoting this selection; market coverage is too thin for an official pick.")
    if sample_size_n < 8:
        return dict(state="WATCHLIST", trigger="Awaiting a deeper current sample",
                    why=f"Only {sample_size_n} observations support the projection; official-pick depth is insufficient.")
    if edge > 15 and market not in ("outright", "top20", "top10", "top5"):
        return dict(state="WATCHLIST",
                    trigger="Re-check the price at the book before acting",
                    why=f"A {edge:.1f} pt edge on a {market} market is outside what real "
                        f"mispricings look like. Far more likely the stored price is stale or "
                        f"the number moved. Flagged for verification rather than played.")
    if confidence >= pick_thr and not veto:
        return dict(state="OFFICIAL PICK", trigger=None,
                    why=f"Confidence {confidence:.0f} clears the {pick_thr:.0f} threshold with a "
                        f"{edge:.1f} pt price edge.")
    if veto and confidence >= pick_thr:
        return dict(state="LEAN", trigger="Risk analyst veto — resolve the flagged risk first",
                    why="Confidence qualifies but the risk analyst flagged a material failure mode, "
                        "so this is downgraded to a lean.")
    if confidence >= lean_thr:
        return dict(state="LEAN", trigger=f"Official at {pick_thr:.0f} confidence"
                                          f" (currently {confidence:.0f})",
                    why=f"Real edge but confidence {confidence:.0f} is below the "
                        f"{pick_thr:.0f} official threshold.")
    return dict(state="PASS", trigger=None,
                why=f"Confidence {confidence:.0f} is too low to act on regardless of the "
                    f"{edge:.1f} pt edge.")


def consensus(signals: List[AgentSignal]) -> Dict:
    """Compact Model Consensus block, including how much the analysts disagree."""
    dirs = [s.direction for s in signals if s.agent != "Risk Analyst"]
    if not dirs:
        return dict(mean=0.0, disagreement=0.0, label="no signal", rows=[])
    mean = sum(dirs) / len(dirs)
    var = sum((d - mean) ** 2 for d in dirs) / len(dirs)
    disagreement = min(1.0, var / 0.35)
    label = ("aligned" if disagreement < 0.25 else
             "mixed" if disagreement < 0.6 else "split")
    return dict(mean=round(mean, 3), disagreement=round(disagreement, 3), label=label,
                rows=[dict(agent=s.agent, direction=s.direction, strength=s.strength,
                           note=s.note) for s in signals])
