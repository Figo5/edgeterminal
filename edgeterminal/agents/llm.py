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

# Test seam: a frozen clock for the HTTP conversation tests. Production never
# sets it (run_chat falls back to the wall clock); when a test sets it, the
# whole turn -- including context.build, where "Thursday night" and "tonight"
# are resolved -- reasons about one deterministic instant. See
# tests/test_chat_multiturn_http.py.
_now_override: Optional[float] = None

SYSTEM = """You are the analyst inside EdgeTerminal, an NFL-only betting research terminal.

Product scope: analyze NFL only. Do not analyze or recommend other sports.
Dormant backend adapters are retained for future expansion but are not active.
All NFL game and player-prop markets are PASS by policy: model thresholds have
not passed independent validation and forward shadow grading has not started.
Do not call any NFL row a pick, lean, playable bet, or stake recommendation.

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
- Lower confidence requests never change the NFL PASS policy.
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
- "Best TD scorer" has FOUR distinct answers. Keep them apart and label which you
  are giving: (1) most likely by the model, (2) the market favourite (shortest
  price -- the market's opinion, not ours), (3) best estimated value (price versus
  fair odds), (4) whether any Official Pick exists (often none).
- Anytime-TD outcomes are NOT mutually exclusive: several players score in one game.
  Their probabilities do not and must not sum to 1. Never rescale them against each
  other and never present them as shares of a single event.
- A fair (no-vig) market probability exists only where one book quoted the SAME
  player's Yes and No. Where `market_prob` is null there is no fair probability:
  report the raw implied price as vig-inclusive, state that no-vig is unavailable,
  and claim NO edge, EV or fair price for that row.
- The `prices` list is verified sportsbook prices ONLY. It is never a recommendation.
  "Show me the prices even if nothing qualifies" is a normal request: answer it from
  `prices` and say plainly that no pick is attached.
- Confidence is a data-quality score from 0-100. It is NOT the probability the bet
  wins. Never write "80 confidence" as "80% chance to score".
- The model is experimental and not calibrated. Do not claim it is proven,
  profitable, or backtested.
- First-TD and last-TD are DISABLED this release. Do not price them, and never reuse
  an anytime-TD probability as a first-TD probability.
- Props use the prop confidence ceiling and prop thresholds. Never present a prop as
  an Official Pick unless the tool returned that state, and never lower a prop
  threshold because the user wants a play.

Event resolution (the application decides this, not you):
- A validated RESOLVED EVENT is supplied with each turn. Use that event id and no
  other. Event-scoped tools are pinned to it; a different id will be replaced.
- An explicit day or team in the user's message outranks the game shown in the UI and
  anything earlier in the conversation. "This game" means the selected one.
- A follow-up that changes only the market ("what about receiving yards?") keeps the
  event already resolved.
- Use get_schedule for any "when/which game" question. Never infer a weekday from a
  stored UTC timestamp: a Thursday-night kickoff carries the next day's UTC date.
- If the event is AMBIGUOUS, ask one short question naming the options and call no
  event-scoped tool. If UNAVAILABLE, say so and show the next games; never substitute
  a different game.
- Begin an event-specific answer with the matchup, date, time and timezone.
- The schedule's retrieval time is supplied each turn. If it is marked UNVERIFIED,
  say so in the answer, never describe the schedule as confirmed current, never
  substitute a different game, and issue no Official Pick or staked recommendation
  until the event can be verified. Prices may still be shown and labelled.

Answering vs recommending:
- A schedule or price question is answered on its own terms. Never require a betting
  recommendation to answer one, and never let "no Official Pick" suppress verified
  prices or research.
- Keep these separate and label them: the market favourite (from quoted odds), what
  current research supports qualitatively, what the experimental model estimates, and
  whether an Official Pick exists. Qualitative research is not a proven edge.
- For "who might score", give candidates and role evidence. Nobody is certain to score.

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


# Tools that act on one specific event. Their event id is validated, never trusted.
EVENT_SCOPED = {"analyze_td_scorers": "event_id", "analyze_player_prop": "event_id",
                "find_best_player_props": "event_id", "get_player_props": "event_id",
                "run_game_analysis": "query", "run_prop_analysis": "event_id",
                "get_current_odds": "query", "get_event": "query",
                "get_weather": "query", "get_line_history": "query"}
# Tools that act within one market. The resolved market is pinned the same way
# the resolved event is: a model that answers the right game in the wrong market
# is still answering a different question than the one asked.
MARKET_SCOPED = {"run_prop_analysis", "get_player_props", "find_best_player_props",
                 "analyze_player_prop"}


def enforce_event_scope(name: str, args: Dict, ctx: Optional[Dict]) -> Dict:
    """Pin event-scoped tools to the event the request actually resolved to.

    The model may propose any event id. Previously whatever it proposed was used, so
    a stale UI selection or a guess answered a question about a different game. The
    resolved id wins; an incompatible one is replaced and logged rather than fetched.
    """
    field = EVENT_SCOPED.get(name)
    if not field or not ctx:
        return args
    res = (ctx.get("resolution") or {})
    if res.get("state") != "OK" or not res.get("event_id"):
        return args
    resolved = res["event_id"]
    supplied = str(args.get(field) or "").strip()
    if supplied and supplied != resolved and supplied != (res.get("event") or {}).get("name"):
        log.warning("blocked event id from model tool=%s supplied=%s resolved=%s",
                    name, supplied[:12], resolved[:12])
    args = dict(args)
    args[field] = resolved
    # run_prop_analysis historically took the id in `query`; clear it so the same
    # id cannot be re-resolved by fuzzy name matching into a different game.
    if name == "run_prop_analysis":
        args.pop("query", None)
    if name in MARKET_SCOPED:
        args = _enforce_market_scope(name, args, ctx)
    return args


def _enforce_market_scope(name: str, args: Dict, ctx: Optional[Dict]) -> Dict:
    """Pin the market/category the request actually resolved to.

    "What about receiving yards" reached the tool as category="all", so a market
    switch returned the entire prop board. The resolved market wins, and a
    category is never widened to everything.
    """
    market = (ctx or {}).get("market")
    category = (ctx or {}).get("category")
    if not market:
        return args
    args = dict(args)
    if str(args.get("category") or "").lower() in ("all", "best", ""):
        args.pop("category", None)
    if category:
        supplied = args.get("category")
        if supplied and supplied != category:
            log.warning("blocked category from model tool=%s supplied=%s resolved=%s",
                        name, supplied, category)
        args["category"] = category
    if name == "run_prop_analysis":
        args["market"] = market
    return args


def normalize_tool_args(name: str, args: Dict, user_message: str,
                        ctx: Optional[Dict] = None) -> Dict:
    """Enforce user intent at the trusted tool boundary, not in model prose."""
    clean = enforce_event_scope(name, dict(args or {}), ctx)
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
             context_note: Optional[str] = None,
             selected_event_id: Optional[str] = None,
             now: Optional[float] = None,
             prior_context: Optional[Dict] = None) -> Dict:
    """One user turn. Returns answer text plus the full tool trace."""
    if re.search(r"\b(?:cfb|ncaaf|college football|nba|basketball|pga|golf)\b",
                 user_message, re.I):
        return dict(answer="EdgeTerminal is currently NFL-only. No analysis or recommendation "
                           "was generated for that sport.", tool_trace=[],
                    provider="scope-router", model=None)
    from . import context as C
    if now is None:
        now = _now_override  # deterministic clock under test; wall clock otherwise
    try:
        ctx = C.build(user_message, history, selected_event_id, now, prior=prior_context)
    except Exception as e:  # noqa: BLE001 - context must never break a turn
        log.error("context build failed: %s", type(e).__name__)
        ctx = None

    p = provider()
    trace: List[Dict] = []
    if p is None:
        return fallback_answer(user_message, trace, context_note, ctx)

    messages: List[Dict] = [{"role": "system", "content": SYSTEM}]
    if ctx:
        messages.append({"role": "system", "content": C.summary_note(ctx)})
    elif context_note:
        messages.append({"role": "system", "content": context_note})
    for h in (history or [])[-8:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    started = time.time()
    corrected = False
    log.info("LLM path active provider=%s model=%s", p.name, getattr(p, "model", None))
    for _round in range(MAX_ROUNDS):
        try:
            out = p.chat(messages, T.SCHEMAS)
        except Exception as e:  # noqa: BLE001
            log.error("LLM call failed: %s", type(e).__name__)
            fb = fallback_answer(user_message, trace, context_note, ctx)
            fb["answer"] = (f"_The language model call failed ({type(e).__name__}). "
                            f"Falling back to the deterministic analyser._\n\n" + fb["answer"])
            fb["llm_error"] = type(e).__name__
            return fb
        calls = out.get("tool_calls") or []
        if not calls:
            if not trace:
                log.warning("LLM returned without grounding tool; using deterministic fallback")
                return fallback_answer(user_message, trace, context_note, ctx)
            answer = out.get("content", "").strip()
            check = validate_answer(answer, ctx, trace)
            if not check["ok"] and not corrected:
                # One bounded corrective attempt, then an accurate partial answer.
                corrected = True
                log.warning("answer validation failed: %s", "; ".join(check["problems"]))
                messages.append({"role": "system", "content":
                                 "The drafted answer failed validation: "
                                 + "; ".join(check["problems"])
                                 + ". Answer only about the resolved event, using the tool "
                                   "results already returned. Do not mention another game."})
                continue
            if not check["ok"]:
                return dict(answer=_event_header(ctx) + answer
                            + "\n\n_Partial answer: this response could not be validated "
                              "against the resolved game (" + "; ".join(check["problems"])
                            + "). Treat the game-specific details above with caution._",
                            tool_trace=_public_trace(trace), provider=p.name,
                            model=getattr(p, "model", None), validation=check,
                            context=_state(ctx, validated=False),
                            elapsed_s=round(time.time() - started, 2))
            # Only an answer that is actually about one game gets the matchup header;
            # "are the providers up?" is not an event-specific question.
            event_specific = any(t.get("tool") in EVENT_SCOPED for t in trace)
            res_name = ((ctx or {}).get("resolution") or {}).get("event", {}).get("name", "")
            if event_specific and res_name and res_name.lower() not in answer.lower():
                answer = _event_header(ctx) + answer
            answer = _schedule_banner(ctx) + answer
            return dict(answer=answer, tool_trace=_public_trace(trace), validation=check,
                        schedule=(ctx or {}).get("schedule"),
                        context=_state(ctx, validated=True),
                        provider=p.name, model=getattr(p, "model", None),
                        elapsed_s=round(time.time() - started, 2))
        messages.append({"role": "assistant", "content": out.get("content") or None,
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"],
                                                      "arguments": c["arguments"]}}
                                        for c in calls]})
        for c in calls:
            args = normalize_tool_args(c["name"], parse_args(c["arguments"]), user_message, ctx)
            # After pinning, two differently-worded calls can become the same
            # call. Running it twice costs quota and can double-charge a provider
            # for one question, so the first result is reused.
            signature = (c["name"], json.dumps(args, sort_keys=True, default=str))
            done = next((t for t in trace if t.get("_sig") == signature), None)
            if done is not None:
                log.info("duplicate tool call skipped tool=%s", c["name"])
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": done.get("_payload", "{}")})
                continue
            result = T.call(c["name"], args)
            log.info("LLM tool call tool=%s ok=%s", c["name"], "error" not in result)
            entry = dict(tool=c["name"], arguments=args, ok="error" not in result,
                          result_preview=json.dumps(result, default=str)[:400])
            entry["_sig"] = signature
            trace.append(entry)
            trimmed = T._trim(result)
            # Hand the model the application's own rendering alongside the JSON, as a
            # field *inside* it so the payload stays valid JSON. Every number the model
            # should quote then appears verbatim in its context, which is what stops it
            # inventing plausible-looking market statistics (an observed failure: a
            # fabricated "overround 5.18% across 9 books").
            if (isinstance(trimmed, dict) and "error" not in result
                    and c["name"] in RENDERED_TOOLS):
                try:
                    rendered = format_result(c["name"], result)
                except Exception:  # noqa: BLE001 - never fail a chat turn on formatting
                    rendered = None
                if rendered:
                    trimmed = dict(trimmed, _authoritative_rendering=rendered[:4000],
                                   _rendering_note="Application-computed. Quote these numbers "
                                                   "exactly. Do not introduce any statistic that "
                                                   "is absent from it, such as overround or a "
                                                   "book count.")
            payload = json.dumps(trimmed, default=str)
            entry["_payload"] = payload
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": payload})
    return dict(answer="Stopped after the tool-call limit. Narrow the question and try again.",
                tool_trace=_public_trace(trace), provider=p.name, context=_state(ctx, validated=False),
                elapsed_s=round(time.time() - started, 2))


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


# Tools whose deterministic rendering is worth showing the model. Others fall back
# to a plain JSON dump in format_result, which would only duplicate the payload.
RENDERED_TOOLS = frozenset({
    "analyze_td_scorers", "find_best_player_props", "analyze_player_prop",
    "get_player_props", "find_best_bets", "build_best_parlay",
    "get_player_usage", "search_player_context",
})


def _public_trace(trace: List[Dict]) -> List[Dict]:
    """Drop the bookkeeping keys used for de-duplication before the trace ships.

    `_payload` is the full tool result handed to the model; sending it to the
    browser as well would duplicate the whole board in every response.
    """
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in trace]


def _state(ctx: Optional[Dict], validated: bool) -> Optional[Dict]:
    """Conversation state for the client to echo back on the next turn."""
    if not ctx:
        return None
    from . import context as C
    try:
        return C.conversation_state(ctx, validated=validated)
    except Exception:  # noqa: BLE001 - never fail a turn on bookkeeping
        return None


def validate_answer(answer: str, ctx: Optional[Dict], trace: List[Dict]) -> Dict:
    """Check a drafted answer against the validated context before it is shown.

    Narration is allowed to be conversational; it is not allowed to substitute its own
    event or to present another game's analysis as the answer. Failures are reported,
    and the caller makes at most one bounded corrective attempt.
    """
    problems: List[str] = []
    res = (ctx or {}).get("resolution") or {}
    if res.get("state") == "OK" and res.get("event"):
        ev = res["event"]
        used = {t.get("arguments", {}).get("event_id") for t in trace
                if isinstance(t.get("arguments"), dict)}
        used.discard(None)
        wrong = {u for u in used if u != res["event_id"]}
        if wrong:
            problems.append("a tool ran against an event other than the resolved one")
        # An answer naming the other team of a different fixture is the visible symptom.
        others = [e for e in _other_matchups(ctx) if e.lower() in answer.lower()]
        if others and ev["name"].lower() not in answer.lower():
            problems.append(f"the answer names {others[0]} but the request resolved to "
                            f"{ev['name']}")
    elif res.get("state") in ("AMBIGUOUS", "UNAVAILABLE"):
        if any(t.get("tool") in EVENT_SCOPED for t in trace):
            problems.append("an event-scoped tool ran while the event was not resolved")
    # A staked recommendation requires a verified event. When the schedule could
    # not be confirmed, an answer that still issues one is sent back once rather
    # than displayed -- the same bounded-correction path used for a wrong game.
    sched = (ctx or {}).get("schedule") or {}
    if sched.get("uncertain"):
        if re.search(r"\bOFFICIAL PICK\b", answer, re.I):
            problems.append("an Official Pick was issued while the schedule could not be "
                            "verified; withhold staked recommendations and say the event "
                            "is unconfirmed")
        elif not re.search(r"unverified|not confirmed|unconfirmed|could not be verified",
                           answer, re.I):
            problems.append("the schedule could not be verified but the answer does not "
                            "say so")
    return dict(ok=not problems, problems=problems)


def _other_matchups(ctx: Optional[Dict]) -> List[str]:
    res = (ctx or {}).get("resolution") or {}
    keep = (res.get("event") or {}).get("name")
    try:
        from . import context as C
        return [e["name"] for e in C._candidates(ctx["now_local"]) if e["name"] != keep]
    except Exception:  # noqa: BLE001
        return []


def _event_header(ctx: Optional[Dict]) -> str:
    """Matchup, date, time and timezone, rendered from validated data only."""
    res = (ctx or {}).get("resolution") or {}
    ev = res.get("event")
    if not ev:
        return ""
    return (f"**{ev['name']}** - {ev['kickoff_local'].strftime('%A %d %b, %H:%M %Z')}\n\n")


def _answer_without_event(ctx: Dict) -> Optional[str]:
    """Answers that must not reach an event-scoped tool.

    A schedule question needs no betting analysis, an ambiguous slot needs one
    question rather than a guess, and an unavailable slot must say so instead of
    analysing a different game.
    """
    from . import context as C
    res = ctx.get("resolution") or {}
    when = ctx["now_local"].strftime("%A %d %b %Y %H:%M")
    if res.get("state") == "AMBIGUOUS":
        alts = res.get("alternatives", [])
        opts = "\n".join(
            f"- {a['name']} - "
            f"{C.local_kickoff(a['kickoff_local']).strftime('%a %d %b %H:%M %Z')}"
            for a in alts[:12])
        if ctx.get("task") == "schedule":
            # A slate question wants the slate, not a disambiguation prompt.
            return f"{len(alts)} NFL games ({ctx['timezone']}):\n\n{opts}"
        return (f"{res['reason']} ({ctx['timezone']}). Which one do you mean?\n\n{opts}")
    if res.get("state") == "UNAVAILABLE":
        alts = res.get("alternatives") or []
        nxt = ""
        if alts:
            nxt = "\n\nNext scheduled:\n" + "\n".join(
                f"- {a['name']} - "
                f"{C.local_kickoff(a['kickoff_local']).strftime('%a %d %b %H:%M %Z')}"
                for a in alts[:5])
        return f"{res['reason'].capitalize()} (checked {when} {ctx['timezone']}).{nxt}"
    if ctx.get("task") == "schedule" and res.get("state") == "OK":
        ev = res["event"]
        return (f"{_event_header(ctx)}Kickoff is "
                f"{ev['kickoff_local'].strftime('%A %d %B at %H:%M %Z')}. "
                f"Current time {when} {ctx['timezone']}.")
    return None


# Tools whose invocation is an actual search request. Counting usage means
# counting these records -- not scanning output for hints that a search happened.
SEARCH_TOOLS = frozenset({"search_web", "search_player_context"})


def searches_used(trace: Optional[List[Dict]]) -> Optional[int]:
    """How many search invocations this turn actually made.

    Counted from recorded tool calls only. A citation URL is not evidence of a
    billable search: a `utm_source=openai` parameter says how a link was
    formatted, not that a search ran, and a cached result returns citations
    without any new request. With no trace to read, usage is UNKNOWN (None)
    rather than a guess -- and never reconstructed by re-running a paid call.
    """
    if trace is None:
        return None
    return sum(1 for t in trace if t.get("tool") in SEARCH_TOOLS)


RECOMMENDATION_TOOLS = frozenset({"find_best_bets", "find_best_player_props",
                                  "analyze_td_scorers", "analyze_player_prop",
                                  "run_prop_analysis", "build_parlay"})


def _schedule_banner(ctx: Optional[Dict]) -> str:
    """Visible uncertainty when the schedule behind an answer is unverified."""
    sched = (ctx or {}).get("schedule") or {}
    if not sched.get("uncertain"):
        return ""
    age = sched.get("age_s")
    when = "never retrieved" if age is None else f"last retrieved {int(age)}s ago"
    return (f"_Schedule unverified ({when}): {sched.get('note') or 'freshness unconfirmed'} "
            f"Kickoffs and statuses below are not confirmed current._\n\n")


def fallback_answer(user_message: str, trace: List[Dict],
                    context_note: Optional[str] = None,
                    ctx: Optional[Dict] = None) -> Dict:
    """Deterministic answer path used when no LLM key is configured."""
    tool, args = _route(user_message)
    # A recommendation rests on the event being the one the user asked about. If
    # the schedule could not be verified, the price is still browsable but the
    # staked recommendation is withheld rather than issued against an unconfirmed
    # kickoff. No other game is substituted.
    sched = (ctx or {}).get("schedule") or {}
    if sched.get("uncertain") and tool in RECOMMENDATION_TOOLS:
        age = sched.get("age_s")
        when = "never retrieved" if age is None else f"last retrieved {int(age)}s ago"
        trace.append(dict(tool="schedule_freshness", ok=False,
                          arguments=dict(limit_s=sched.get("limit_s")),
                          result_preview=json.dumps(
                              dict(retrieved_at=sched.get("retrieved_at"), age_s=age,
                                   refresh_attempted=sched.get("refresh_attempted"),
                                   refresh_ok=sched.get("refresh_ok"),
                                   error=sched.get("error"),
                                   mapping_problems=sched.get("mapping_problems")),
                              default=str)[:400]))
        return dict(
            answer=(f"No recommendation was produced: the schedule could not be verified "
                    f"({when}). {sched.get('note') or ''}\n\n"
                    "Prices can still be browsed, but a staked recommendation needs a "
                    "confirmed kickoff and event identity. Refresh and ask again."),
            tool_trace=_public_trace(trace), provider="deterministic-router", model=None,
            schedule=sched, recommendation_withheld=True,
            context=_state(ctx, validated=True))
    # Only a request that genuinely needs one specific game is blocked on event
    # resolution. Slate-wide tools (best bets, parlays, watchlist) scan every game and
    # must still run when one particular day happens to be empty.
    if ctx and (tool in EVENT_SCOPED or ctx.get("task") == "schedule"):
        answered = _answer_without_event(ctx)
        if answered:
            res = ctx.get("resolution") or {}
            # The resolution itself is the grounding step here, so it is recorded:
            # every displayed answer must be attributable to a tool result or to a
            # deterministic application step, never to unsourced narration.
            trace.append(dict(tool="resolve_event", ok=res.get("state") == "OK",
                              arguments=dict(day=(ctx.get("day_constraint") or {}).get("label"),
                                             teams=ctx.get("teams") or [],
                                             timezone=ctx.get("timezone")),
                              result_preview=json.dumps(
                                  dict(state=res.get("state"), event_id=res.get("event_id"),
                                       reason=res.get("reason"),
                                       reasons=res.get("reasons")), default=str)[:400]))
            return dict(answer=_schedule_banner(ctx) + answered, tool_trace=_public_trace(trace),
                        provider="deterministic-router", model=None,
                        event_state=res.get("state"), schedule=ctx.get("schedule"),
                        context=_state(ctx, validated=True))
    # The UI supplies an explicit event context. Keep ambiguous "this game"
    # questions scoped to that event instead of silently answering slate-wide.
    resolved = ((ctx or {}).get("resolution") or {}).get("event_id")
    if not resolved:
        m = re.search(r"event id ([^\s.]+)", context_note or "")
        resolved = m.group(1) if m else None
    if resolved and tool == "find_best_bets" and re.search(r"\b(this|the) game\b",
                                                           user_message.lower()):
        tool, args = "run_game_analysis", {"query": resolved}
    args = normalize_tool_args(tool, args, user_message, ctx)
    if resolved and tool in MARKET_SCOPED and (ctx or {}).get("market"):
        args = _enforce_market_scope(tool, args, ctx)
    if resolved and EVENT_SCOPED.get(tool):
        # Pin from the resolved id itself, not only via ctx: if context building failed
        # the selected game must still not be dropped in favour of the next kickoff.
        args = dict(args, **{EVENT_SCOPED[tool]: resolved})
    result = T.call(tool, args)
    trace.append(dict(tool=tool, arguments=args, ok="error" not in result,
                      result_preview=json.dumps(result, default=str)[:400]))
    return dict(answer=_schedule_banner(ctx) + format_result(tool, result), tool_trace=_public_trace(trace),
                provider="deterministic-router", model=None, schedule=sched or None,
                context=_state(ctx, validated=True),
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
    prices = res.get("prices") or []
    market = res.get("market") or {}
    priced = market.get("priced") or 0
    no_vig = market.get("no_vig_players") or 0
    head = (f"**{res.get('state')}** — anytime-TD board for "
            f"{(res.get('event') or {}).get('name') or 'the next game'}.\n"
            f"- {priced} player(s) priced across {market.get('books')} book(s) "
            f"(coverage {market.get('coverage')})\n"
            f"- fair market probability available for {no_vig} of them "
            f"(needs the same player's Yes *and* No from one book)\n")
    if market.get("reason"):
        head += f"- {market['reason']}\n"
    parts = [head]
    most = res.get("most_likely")
    fav = res.get("market_favourite")
    best = res.get("best_value")

    if most:
        parts.append(f"\n**Most likely (model):** {most['player']} — "
                     f"{(most.get('final_prob') or 0) * 100:.1f}% "
                     f"(experimental model, not calibrated; fair {most.get('fair_price')}, "
                     f"best {most.get('price')} at {most.get('book')})")
    else:
        parts.append("\n**Most likely (model):** unavailable — no player had both usable "
                     "usage and a fair market probability.")
    if fav:
        parts.append(f"**Market favourite:** {fav['player']} at {fav.get('price')} "
                     f"({(fav.get('implied_prob') or 0) * 100:.1f}% implied, vig included) "
                     f"— this is the market's opinion, not a pick.")
    if best:
        parts.append(f"**Best value:** {best['player']} — {best.get('edge_pct')} pts of edge, "
                     f"{best.get('price')} at {best.get('book')} (fair {best.get('fair_price')})")
    else:
        parts.append("**Best value:** not designated — NFL props remain PASS pending validation.")
    if not res.get("official"):
        parts.append("**Official Pick:** none for this market.")

    if rows:
        parts.append("\nAnalysed (model + fair market probability):")
        for row in rows[:8]:
            parts.append(f"- {row.get('state')} · {row['player']} {row.get('price')} at "
                         f"{row.get('book')} | model {row.get('model_prob')} vs market "
                         f"{row.get('market_prob')} → final {row.get('final_prob')} | fair "
                         f"{row.get('fair_price')} | edge {row.get('edge_pct')} pts | "
                         f"confidence {row.get('confidence')} (data quality, not win chance)")
    if prices:
        parts.append("\nVerified prices only — not recommendations, no edge claimed:")
        for row in prices[:8]:
            bits = [f"- {row['player']} {row.get('price')} at {row.get('book')} "
                    f"({(row.get('implied_prob') or 0) * 100:.1f}% implied, vig included)"]
            if row.get("model_prob") is not None:
                bits.append(f" | experimental model {row['model_prob']}")
            parts.append("".join(bits))
        parts.append("_No fair market probability exists for these, so no edge or EV is quoted._")
    if not rows and not prices:
        parts.append("\n" + (res.get("note") or
                              "No anytime-TD market was priced, so nothing is ranked."))
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
                    + "\nNFL markets remain PASS by policy; refresh data to inspect current prices.")
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
            reason = next((r.get("reasoning", [None])[0] for r in recs
                           if r.get("reasoning")), None)
            bits.append("\n**PASS.** " + (reason or
                        "No fresh verified market quote is available for a designation."))
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
