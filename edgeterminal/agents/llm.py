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

SYSTEM = """You are the analyst inside EdgeTerminal, a sports-market research terminal.

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
- Never use the words lock, guaranteed, can't miss, free money or risk-free.
  Speak in probabilities and ranges.
- Distinguish reported/official sources from community chatter. Cite sources
  the tools return, with their timestamps.
- Preferred books are FanDuel and bet365, but always name whichever book has the
  best price.

Style: dense, numeric, professional. Lead with the decision state (OFFICIAL PICK /
LEAN / WATCHLIST / PASS / NO EDGE), then the numbers, then the reasoning, then the
risks. Short lines. No filler. Markdown tables are welcome for multi-row answers."""


class LLMProvider:
    name = "none"
    available = False

    def chat(self, messages: List[Dict], tool_schemas: List[Dict]) -> Dict:
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self) -> None:
        from openai import OpenAI
        kw: Dict[str, Any] = dict(api_key=settings.openai_api_key)
        if settings.openai_base_url:
            kw["base_url"] = settings.openai_base_url
        self.client = OpenAI(**kw)
        self.model = settings.openai_model
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


def run_chat(user_message: str, history: Optional[List[Dict]] = None,
             context_note: Optional[str] = None) -> Dict:
    """One user turn. Returns answer text plus the full tool trace."""
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
            args = parse_args(c["arguments"])
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
    (r"\bgolf\b|\bpga\b|outright|top ?(5|10|20)|tournament", "run_golf_analysis"),
    (r"\bprop\b|receiving|rushing|passing yards|rebounds|assists", "run_prop_analysis"),
    (r"\banaly[sz]e\b|\bvs\b|\bversus\b|\bat\b.*\b(game|matchup)\b", "run_game_analysis"),
    (r"best bet|strongest|top play|what should i|any value|plus money|\+money",
     "find_best_bets"),
    (r"\bodds\b|\bprice\b|\bline\b", "get_current_odds"),
    (r"\bslate\b|\bgames\b|\bschedule\b|what'?s on", "list_events"),
    (r"\binjur|\bstatus\b|\bout\b", "get_injuries"),
    (r"\bweather\b|\bwind\b|\brain\b", "get_weather"),
    (r"\bprovider|\bdata source|\bstale\b", "get_provider_status"),
]
SPORT_WORDS = {"NFL": ["nfl"], "CFB": ["cfb", "college", "ncaa"],
               "NBA": ["nba", "basketball"], "PGA": ["pga", "golf"]}


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
