"""LLM provider abstraction + the chat agent loop.

The model is used for synthesis, tool selection and explanation. It is never
the source of a number. Every factual claim it can make comes back from a tool
call against the local database and the providers.

Without an API key the terminal still answers: a deterministic keyword router
picks the same tools and formats the structured result directly.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import settings
from . import tools as T

log = logging.getLogger("edgeterminal.llm")

SYSTEM = """You are the analyst inside EdgeTerminal, an NFL-only betting research terminal.

Product scope: analyze NFL only. Do not analyze or recommend other sports.
Dormant backend adapters are retained for future expansion but are not active.

Hard rules:
- You have NO knowledge of current odds, injuries, statistics, schedules, scores,
  weather or results. Every such fact must come from a tool call in this
  conversation. If a tool cannot supply it, say it is unavailable. Never estimate,
  recall or infer a number that a tool did not return.
- Model probabilities, fair prices, edges, confidence scores and unit sizes are
  computed by the application. Report them; never invent or recompute them.
- Confidence and edge are different things. A high win probability at a bad price
  is a bad bet. Say so.
- PASS is a valid and frequent answer. If nothing clears the threshold, say that
  plainly instead of manufacturing a play. Never lower the bar because the user
  asked for a pick.
- Requests for a best bet/play, strongest bet/play, top play, or Official Pick must
  use find_best_bets at the application's configured production threshold. Omit
  min_confidence for those requests. Only request a lower threshold when the user
  explicitly asks for leans, watchlist plays, broader opportunities, or a numeric
  lower-confidence search. Never relabel a LEAN or WATCHLIST as an Official Pick.
- Never use the words lock, guaranteed, can't miss, free money or risk-free.
  Speak in probabilities and ranges.
- Distinguish reported/official sources from community chatter. Cite sources
  the tools return, with their timestamps.
- Preferred books are FanDuel and bet365, but always name whichever book has the
  best price.

Player props (higher variance, stricter rules):
- Prop prices come from the Odds API through get_player_props / analyze_td_scorers /
  analyze_player_prop / find_best_player_props. Never quote a prop price from memory,
  from a search snippet, or from your training data.
- Player usage comes from get_player_usage (nflverse play-by-play). Snap share and
  route participation are NOT available: say so instead of estimating them. Never
  invent a workload, a depth-chart position or a target share.
- Web research (search_player_context) supplies context only: roles, injuries,
  workload changes. It never supplies a probability, a price, an edge or a unit size.
  Cite every source you rely on with its reliability class and timestamp.
- If sources disagree, say that they disagree and treat the play as uncertain.
- TD questions have two different answers: "most likely" (highest modelled chance)
  and "best value" (best price versus fair odds). Do not conflate them.
- Props use the prop confidence ceiling and prop thresholds. Never present a prop as
  an Official Pick unless the tool returned that state, and never lower a prop
  threshold because the user wants a play.

Style: dense, numeric, professional. Lead with the decision state (OFFICIAL PICK /
LEAN / WATCHLIST / PASS / NO EDGE), then the numbers, then the reasoning, then the
risks. Short lines. No filler. Markdown tables are welcome for multi-row answers."""


class LLMProvider:
    name = "none"
    available = False

    def chat(self, messages: List[Dict], tool_schemas: List[Dict]) -> Dict:
        raise NotImplementedError

    def web_search(self, query: str, limit: int = 5) -> List[Dict]:
        """Current web research. Providers that cannot do this return nothing."""
        return []


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self) -> None:
        from openai import OpenAI
        kw: Dict[str, Any] = dict(api_key=settings.openai_api_key)
        if settings.openai_base_url:
            kw["base_url"] = settings.openai_base_url
        self.client = OpenAI(**kw)
        self.model = settings.openai_model
        self.search_model = settings.openai_search_model or settings.openai_model
        self.available = True

    def chat(self, messages: List[Dict], tool_schemas: List[Dict]) -> Dict:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages,
            tools=[{"type": "function", "function": s} for s in tool_schemas] or None,
            temperature=0.2, timeout=90)
        msg = resp.choices[0].message
        return dict(content=msg.content or "",
                    tool_calls=[dict(id=tc.id, name=tc.function.name,
                                     arguments=tc.function.arguments)
                                for tc in (msg.tool_calls or [])],
                    usage=dict(getattr(resp, "usage", None).__dict__) if getattr(resp, "usage", None) else {})

    def web_search(self, query: str, limit: int = 5) -> List[Dict]:
        """OpenAI's own web-search tool via the Responses API.

        Only real, cited results are returned: a response without URL
        annotations is reported as "no research" rather than as prose the model
        might have recalled from training.
        """
        resp = self.client.responses.create(
            model=self.search_model,
            tools=[{"type": "web_search"}],
            input=query, timeout=90)
        text = (getattr(resp, "output_text", "") or "").strip()
        out: List[Dict] = []
        for item in (getattr(resp, "output", None) or []):
            for chunk in (getattr(item, "content", None) or []):
                for mark in (getattr(chunk, "annotations", None) or []):
                    url = getattr(mark, "url", None)
                    if not url:
                        continue
                    out.append(dict(title=getattr(mark, "title", None) or url, url=url,
                                    snippet=text[:400], publisher=None,
                                    backend="openai-web-search"))
        seen, unique = set(), []
        for row in out:
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            unique.append(row)
        return unique[:limit]


def web_search(query: str, limit: int = 5,
               provider_instance: Optional[LLMProvider] = None) -> List[Dict]:
    """Module-level web research. Never raises: no LLM means no LLM research."""
    inst = provider_instance or provider()
    if inst is None:
        return []
    try:
        return inst.web_search(query, limit=limit) or []
    except Exception as e:  # noqa: BLE001 - research is best-effort
        log.warning("LLM web search unavailable: %s", type(e).__name__)
        return []


def provider() -> Optional[LLMProvider]:
    if not settings.has_llm():
        return None
    try:
        return OpenAIProvider()
    except Exception as e:  # noqa: BLE001
        log.warning("OpenAI provider unavailable: %s", type(e).__name__)
        return None


# ------------------------------------------------------------------ safe JSON
def parse_args(raw: str) -> Dict:
    """LLM tool arguments are untrusted text. Never let a malformed blob crash."""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                val = json.loads(m.group(0))
                return val if isinstance(val, dict) else {}
            except json.JSONDecodeError:
                pass
    log.warning("unparseable tool arguments: %.200s", raw)
    return {}


MAX_ROUNDS = 6

_OFFICIAL_INTENT = re.compile(
    r"\b(?:best|strongest|top)(?:\s+\w+){0,3}\s+(?:bet|play|wager|pick)\b|"
    r"\bofficial\s+pick\b", re.I)
_LOWER_INTENT = re.compile(
    r"\b(?:leans?|watchlist|broader\s+opportunit(?:y|ies))\b|"
    r"\b\d{2,3}\s*\+?\s*(?:confidence|conf)\b", re.I)


def normalize_tool_args(name: str, args: Dict, user_message: str) -> Dict:
    """Enforce user intent at the trusted tool boundary, not in model prose."""
    clean = dict(args or {})
    if name != "find_best_bets":
        return clean
    low_requested = bool(_LOWER_INTENT.search(user_message))
    official_requested = bool(_OFFICIAL_INTENT.search(user_message)) and not low_requested
    if official_requested:
        supplied = clean.get("min_confidence")
        try:
            supplied_threshold = float(supplied) if supplied is not None else 0.0
        except (TypeError, ValueError):
            supplied_threshold = 0.0
        clean["min_confidence"] = max(settings.official_pick_confidence, supplied_threshold)
        clean.pop("include_leans", None)
        clean.pop("only_leans", None)
    elif low_requested:
        clean["include_leans"] = True
        if re.search(r"\bleans?\b", user_message, re.I):
            clean["only_leans"] = True
    else:
        # Ambiguous ranked-bet searches use the production default, regardless of
        # an unsolicited lower value from the model.
        clean.pop("min_confidence", None)
        clean.pop("include_leans", None)
        clean.pop("only_leans", None)
    return clean


def run_chat(user_message: str, history: Optional[List[Dict]] = None,
             context_note: Optional[str] = None) -> Dict:
    """One user turn. Returns answer text plus the full tool trace."""
    if re.search(r"\b(?:cfb|ncaaf|college football|nba|basketball|pga|golf)\b",
                 user_message, re.I):
        return dict(answer="EdgeTerminal is currently NFL-only. No analysis or recommendation "
                           "was generated for that sport.", tool_trace=[],
                    provider="scope-router", model=None)
    p = provider()
    trace: List[Dict] = []
    if p is None:
        return fallback_answer(user_message, trace, context_note)

    messages: List[Dict] = [{"role": "system", "content": SYSTEM}]
    if context_note:
        messages.append({"role": "system", "content": context_note})
    for h in (history or [])[-8:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    started = time.time()
    log.info("LLM path active provider=%s model=%s", p.name, getattr(p, "model", None))
    for _round in range(MAX_ROUNDS):
        try:
            out = p.chat(messages, T.SCHEMAS)
        except Exception as e:  # noqa: BLE001
            log.error("LLM call failed: %s", type(e).__name__)
            fb = fallback_answer(user_message, trace, context_note)
            fb["answer"] = (f"_The language model call failed ({type(e).__name__}). "
                            f"Falling back to the deterministic analyser._\n\n" + fb["answer"])
            fb["llm_error"] = type(e).__name__
            return fb
        calls = out.get("tool_calls") or []
        if not calls:
            if not trace:
                log.warning("LLM returned without grounding tool; using deterministic fallback")
                return fallback_answer(user_message, trace, context_note)
            return dict(answer=out.get("content", "").strip(), tool_trace=trace,
                        provider=p.name, model=getattr(p, "model", None),
                        elapsed_s=round(time.time() - started, 2))
        messages.append({"role": "assistant", "content": out.get("content") or None,
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"],
                                                      "arguments": c["arguments"]}}
                                        for c in calls]})
        for c in calls:
            args = normalize_tool_args(c["name"], parse_args(c["arguments"]), user_message)
            result = T.call(c["name"], args)
            log.info("LLM tool call tool=%s ok=%s", c["name"], "error" not in result)
            trace.append(dict(tool=c["name"], arguments=args,
                              ok="error" not in result,
                              result_preview=json.dumps(result, default=str)[:400]))
            payload = json.dumps(T._trim(result), default=str)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": payload})
    return dict(answer="Stopped after the tool-call limit. Narrow the question and try again.",
                tool_trace=trace, provider=p.name, elapsed_s=round(time.time() - started, 2))


# ------------------------------------------------------- no-key fallback router
ROUTES: List[Tuple[str, str]] = [
    (r"\bparlay\b", "build_best_parlay"),
    (r"\bwatchlist\b|close to becoming|almost a play", "get_watchlist"),
    (r"\bmoved\b|line move|movement|steam", "get_line_moves"),
    (r"\bcalibrat|performance|hit rate|clv|track record", "get_model_performance"),
    # Prop research: the specific ask decides the tool, never the other way round.
    (r"td scorer|touchdown scorer|most likely to score|first td|anytime td|"
     r"who scores|goal line back", "analyze_td_scorers"),
    (r"best .*(prop|receiving|rushing|passing)|prop value|best value prop|"
     r"player prop", "find_best_player_props"),
    (r"usage|snap|carries|targets|carr(?:y|ies) share", "get_player_usage"),
    (r"\bprop\b|receiving|rushing|passing yards", "analyze_player_prop"),
    (r"\banaly[sz]e\b|\bvs\b|\bversus\b|\bat\b.*\b(game|matchup)\b", "run_game_analysis"),
    (r"best bet|strongest|top play|what should i|any value|plus money|\+money",
     "find_best_bets"),
    (r"\bodds\b|\bprice\b|\bline\b", "get_current_odds"),
    (r"\bslate\b|\bgames\b|\bschedule\b|what'?s on", "list_events"),
    (r"\binjur|\bstatus\b|\bout\b", "get_injuries"),
    (r"\bweather\b|\bwind\b|\brain\b", "get_weather"),
    (r"\bprovider|\bdata source|\bstale\b", "get_provider_status"),
]
SPORT_WORDS = {"NFL": ["nfl", "football"]}


def _route(msg: str) -> Tuple[str, Dict]:
    low = msg.lower()
    sport = next((s for s, ws in SPORT_WORDS.items() if any(w in low for w in ws)), None)
    for pattern, tool in ROUTES:
        if re.search(pattern, low):
            args: Dict[str, Any] = {}
            if tool in ("find_best_bets", "get_line_moves", "list_events", "get_injuries"):
                if sport:
                    args["sport"] = sport
            if tool == "find_best_bets":
                m = re.search(r"(\d{2,3})\s*\+?\s*(?:confidence|conf)", low)
                if m:
                    args["min_confidence"] = float(m.group(1))
                if "plus money" in low or "+money" in low or "plus-money" in low:
                    args["plus_money_only"] = True
            if tool == "build_best_parlay":
                m = re.search(r"(\d)\s*-?\s*leg", low)
                args["max_legs"] = int(m.group(1)) if m else 3
                if sport:
                    args["sport"] = sport
            if tool in ("run_game_analysis", "run_prop_analysis", "get_current_odds",
                        "get_weather"):
                args["query"] = msg
            if tool == "run_golf_analysis":
                for pat, mk in ((r"outright|winner|to win", "outright"), (r"top ?5", "top5"),
                                (r"top ?10", "top10"), (r"top ?20", "top20"),
                                (r"matchup", "matchup"), (r"make (the )?cut", "make_cut")):
                    if re.search(pat, low):
                        args["market"] = mk
                        break
                m = re.search(r"(?:over|above|plus|\+)\s*\+?(\d{3,5})", low)
                if m:
                    args["min_price"] = int(m.group(1))
            return tool, args
    return ("find_best_bets", {"sport": sport} if sport else {})


def fallback_answer(user_message: str, trace: List[Dict],
                    context_note: Optional[str] = None) -> Dict:
    """Deterministic answer path used when no LLM key is configured."""
    tool, args = _route(user_message)
    # The UI supplies an explicit event context. Keep ambiguous "this game"
    # questions scoped to that event instead of silently answering slate-wide.
    if context_note and tool == "find_best_bets" and re.search(r"\b(this|the) game\b", user_message.lower()):
        match = re.search(r"event id ([^\s.]+)", context_note)
        if match:
            tool, args = "run_game_analysis", {"query": match.group(1)}
    result = T.call(tool, args)
    trace.append(dict(tool=tool, arguments=args, ok="error" not in result,
                      result_preview=json.dumps(result, default=str)[:400]))
    return dict(answer=format_result(tool, result), tool_trace=trace,
                provider="deterministic-router", model=None,
                note="No OPENAI_API_KEY configured — answering directly from the analytics "
                     "engine without natural-language synthesis.")


def _citations(rows) -> str:
    lines = []
    for item in (rows or [])[:6]:
        title = item.get("title") or item.get("url") or "source"
        reliability = item.get("reliability") or item.get("trust") or "unverified"
        url = item.get("url")
        stamp = item.get("retrieved_at")
        when = time.strftime("%H:%M UTC", time.gmtime(stamp)) if stamp else "time unknown"
        lines.append(f"- [{reliability.upper()}] {title}{f' — {url}' if url else ''} ({when})")
    return "\n".join(lines)


def _fmt_td_board(res: Dict) -> str:
    if res.get("error"):
        return f"**Unavailable.** {res['error']}"
    rows = res.get("recommendations") or []
    market = res.get("market") or {}
    head = (f"**{res.get('state')}** — anytime-TD board for "
            f"{(res.get('event') or {}).get('name') or 'the next game'}.\n"
            f"- no-vig market: {market.get('usable')} · {market.get('books')} book(s) · "
            f"coverage {market.get('coverage')} · overround {market.get('overround')}\n")
    if not rows:
        return head + "\n" + (res.get("note") or "No TD market was priced, so nothing is ranked.")
    parts = [head]
    most = res.get("most_likely")
    best = res.get("best_value")
    if most:
        parts.append(f"\n**Most likely scorer:** {most['player']} — "
                     f"{most.get('final_prob', 0) * 100:.1f}% "
                     f"(fair {most.get('fair_price')}, best {most.get('price')} at {most.get('book')})")
    if best:
        parts.append(f"**Best value:** {best['player']} — {best.get('edge_pct')} pts of edge, "
                     f"{best.get('price')} at {best.get('book')} (fair {best.get('fair_price')})")
    elif rows:
        parts.append("**Best value:** none — no priced scorer carries positive expected value.")
    for row in rows[:8]:
        parts.append(f"- {row.get('state')} · {row['player']} {row.get('price')} at {row.get('book')} "
                     f"| model {row.get('model_prob')} vs market {row.get('market_prob')} "
                     f"→ final {row.get('final_prob')} | fair {row.get('fair_price')} | "
                     f"edge {row.get('edge_pct')} pts | conf {row.get('confidence')}")
    citations = _citations(((res.get("research") or {}).get("citations")) or [])
    if citations:
        parts.append("\nSources:\n" + citations)
    return "\n".join(parts)


def _fmt_props(tool: str, res: Dict) -> str:
    if res.get("error"):
        return f"**Unavailable.** {res['error']}"
    rows = res.get("recommendations") or res.get("props") or []
    head = f"**{res.get('state') or res.get('category') or 'Player props'}** — "
    head += f"{(res.get('event') or {}).get('name') or 'next game'}\n"
    if res.get("price_source"):
        head += f"- prices: {res['price_source']} · coverage "
        head += f"{(res.get('market') or {}).get('coverage')} · "
        head += f"{(res.get('market') or {}).get('books')} book(s)\n"
    if not rows:
        return head + "\n" + (res.get("note") or "No priced prop markets are available.")
    parts = [head]
    for row in rows[:8]:
        line = f" {row['line']:g}" if row.get("line") is not None else ""
        side = f" {row['side'].title()}" if row.get("side") else ""
        parts.append(f"- {row.get('state')} · {row['player']}{side}{line} ({row.get('market_label')}) "
                     f"{row.get('price')} at {row.get('book')} | final {row.get('final_prob')} | "
                     f"fair {row.get('fair_price')} | edge {row.get('edge_pct')} pts | "
                     f"conf {row.get('confidence')}/{row.get('units')}u"
                     + (f" | books {row['books']}" if row.get("books") else ""))
    citations = _citations(((res.get("research") or {}).get("citations")) or [])
    if citations:
        parts.append("\nSources:\n" + citations)
    if res.get("note") and res.get("note") not in parts:
        parts.append(f"\n_{res['note']}_")
    return "\n".join(parts)


def _fmt_usage(res: Dict) -> str:
    if res.get("error"):
        return f"**Unavailable.** {res['error']}"
    usage = res.get("usage")
    if not usage:
        rows = res.get("players") or []
        head = f"**Usage leaders** ({res.get('season')}, {res.get('source')})\n"
        return head + "\n".join(
            f"- {r.get('name')} ({r.get('team')}): {r.get('carries')} car, {r.get('targets')} tgt, "
            f"{r.get('total_tds')} TD in {r.get('games')} games" for r in rows[:10])
    return (f"**{usage.get('name')}** ({usage.get('team')}) — {usage.get('games')} games, "
            f"{res.get('source')}\n"
            f"- {usage.get('carries')} carries / {usage.get('rush_yards')} yds · "
            f"{usage.get('targets')} targets / {usage.get('receptions')} rec / "
            f"{usage.get('rec_yards')} yds · {usage.get('total_tds')} TD\n"
            f"- red zone: {usage.get('red_zone_touches')} touches · goal line: "
            f"{usage.get('goal_line_touches')} · red-zone share {res.get('red_zone_share')}\n"
            f"- role stability: {res.get('role_stability')} ({res.get('stability_reason')})\n"
            f"- **not available:** {', '.join(res.get('missing') or [])} — not estimated")


def _fmt_research(res: Dict) -> str:
    rows = res.get("results") or []
    if not rows:
        return ("**No current sources found.** " + (res.get("note") or "")
                + ("\nErrors: " + "; ".join(res["errors"]) if res.get("errors") else ""))
    head = (f"**Research: {res.get('player')}** — query “{res.get('query')}” "
            f"({res.get('cache')} cache, backends: {', '.join(res.get('backends_used') or [])})\n")
    return head + "\n".join(
        f"- [{r.get('reliability')}] {r.get('title')} — {r.get('publisher')} "
        f"({r.get('relevance')})" + (f" {r.get('url')}" if r.get("url") else "")
        + (f"\n  {r.get('snippet')}" if r.get("snippet") else "") for r in rows[:6])


def _fmt_rec(r: Dict) -> str:
    line = f" {r['line']:g}" if r.get("line") is not None else ""
    return (f"**{r['state']}** — {r['selection']}{line} ({r.get('market_label') or r['market']}, "
            f"{r.get('event_name') or ''})\n"
            f"- {r['book']} {r['price']:+.0f} | fair {r['fair_price']:+.0f} | "
            f"model {r['model_prob']*100:.1f}% vs implied {r['implied_prob']*100:.1f}% "
            f"({r['edge_pct']:+.1f} pts)\n"
            f"- confidence {r['confidence']:.0f}/100 · {r['units']}u · data {r['data_quality']}"
            + (f" · playable to {r['breakeven_price']:+.0f}" if r.get("breakeven_price") else "")
            + (f"\n- trigger: {r['trigger_note']}" if r.get("trigger_note") else ""))


def _recs(rows) -> List[Dict]:
    """Drop the truncation marker rows _trim may append."""
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("state")]


def format_result(tool: str, res: Dict) -> str:
    if "error" in res:
        return f"**Unavailable.** {res['error']}"
    if tool == "analyze_td_scorers":
        return _fmt_td_board(res)
    if tool in ("find_best_player_props", "analyze_player_prop", "get_player_props"):
        return _fmt_props(tool, res)
    if tool == "get_player_usage":
        return _fmt_usage(res)
    if tool == "search_player_context":
        return _fmt_research(res)
    if tool == "find_best_bets":
        bets = _recs(res.get("bets"))
        if not bets:
            return ("**No bets meet the threshold right now.**\n\n" + (res.get("note") or "")
                    + "\nPress Refresh Odds, or lower the confidence filter to inspect leans.")
        head = (f"{res['official_pick_count']} official pick(s) at the "
                f"{res['threshold']:.0f} confidence threshold.\n\n")
        return head + "\n\n".join(_fmt_rec(b) for b in bets)
    if tool == "build_best_parlay":
        p = res.get("parlay")
        if not p:
            return "**No parlay qualifies.** " + (res.get("note") or "")
        legs = "\n".join(f"{i+1}. {l['selection']}"
                         + (f" {l['line']:g}" if l.get("line") is not None else "")
                         + f" — {l['book']} {l['price']:+.0f} (model {l['model_prob']*100:.1f}%, "
                           f"conf {l['confidence']:.0f})" for i, l in enumerate(p["legs"]))
        return (f"**BEST PARLAY — {p['state']}**\n\n{legs}\n\n"
                f"Combined {p['combined_price']:+d} | correlation-adjusted "
                f"{p['correlation_adjusted_prob']*100:.1f}% vs implied {p['implied_prob']*100:.1f}% "
                f"({p['edge_pct']:+.1f} pts) | confidence {p['confidence']:.0f} | {p['units']}u\n\n"
                + ("\n".join(f"- {n}" for n in p.get("correlation_notes", [])) or
                   "- Legs are close to independent.")
                + "\n\n" + "\n".join(f"- {r}" for r in p.get("risks", [])[:3]))
    if tool in ("run_game_analysis", "run_prop_analysis", "run_golf_analysis"):
        recs = _recs(res.get("recommendations"))
        header = res.get("event") or res.get("tournament") or "Analysis"
        proj = res.get("projection") or {}
        bits = [f"### {header}"]
        if proj.get("proj_home") is not None:
            bits.append(f"Projection: {proj['home']} {proj['proj_home']} — {proj['away']} "
                        f"{proj['proj_away']} (margin {proj['margin']:+.1f}, total "
                        f"{proj['total']:.1f}, home win {proj['home_win_prob']*100:.1f}%)")
        if res.get("data_quality"):
            bits.append(f"Data quality: {res['data_quality']}")
        for w in (res.get("warnings") or [])[:3]:
            bits.append(f"⚠ {w}")
        actionable = [r for r in recs if r["state"] in ("OFFICIAL PICK", "LEAN", "WATCHLIST")]
        if not actionable:
            bits.append("\n**No market in this event clears the edge threshold. PASS.**")
        else:
            bits.append("")
            bits += [_fmt_rec(r) for r in actionable[:6]]
        return "\n".join(bits)
    if tool == "get_watchlist":
        rows = _recs(res.get("watchlist"))
        if not rows:
            return "Watchlist is empty — nothing is close enough to qualify."
        return "\n\n".join(_fmt_rec(r) for r in rows[:8])
    if tool == "get_line_moves":
        if not res.get("moves"):
            return res.get("note", "No movement recorded yet.")
        return "\n".join(
            f"- **{m['selection']}** ({m['market']}, {m['event_name']}): "
            f"{m['open_line'] if m['open_line'] is not None else m['open_price']} → "
            f"{m['current_line'] if m['current_line'] is not None else m['current_price']} "
            f"({m['delta']:+g} {m['unit']}, {m['snapshots']} snapshots)"
            for m in res["moves"][:10])
    if tool == "get_model_performance":
        if res.get("note"):
            return f"**Model performance**\n\n{res['note']}"
        o = res["overall"]
        return (f"**Model performance** — {res['graded']} graded, {res['official_picks']} official "
                f"picks.\nHit rate {o['hit_rate']:.1%} | ROI {o['roi_pct']}% | "
                f"avg CLV {o['avg_clv']}%")
    return "```json\n" + json.dumps(res, indent=2, default=str)[:3000] + "\n```"
