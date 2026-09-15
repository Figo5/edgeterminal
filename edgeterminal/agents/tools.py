"""Tools the chat model may call.

The model never receives the database. It picks a tool, the tool runs
deterministic Python, and the model narrates the structured result. This is the
only way it is allowed to talk about odds, stats, injuries or probabilities.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from .. import db, engine, parlay
from ..calibration import performance
from ..config import SPORTS, settings
from ..providers import registry as R

MAX_CHARS = 14000


def _trim(obj: Any) -> Any:
    s = json.dumps(obj, default=str)
    if len(s) <= MAX_CHARS:
        return obj
    if isinstance(obj, list):
        keep = max(1, int(len(obj) * MAX_CHARS / len(s)))
        return obj[:keep] + [{"_truncated": f"{len(obj) - keep} more rows omitted"}]
    return {"_truncated": "payload too large", "preview": s[:MAX_CHARS]}


def _find_event(query: str) -> Optional[Dict]:
    q = (query or "").strip().lower()
    if not q:
        return None
    row = db.q1("SELECT * FROM events WHERE lower(id)=?", (q,))
    if row:
        return dict(row)
    rows = db.q("SELECT * FROM events ORDER BY start_time ASC")
    words = [w for w in q.replace("@", " ").replace(" vs ", " ").split() if len(w) > 2]
    best, best_score = None, 0
    for r in rows:
        hay = f"{r['name']} {r['home_team']} {r['away_team']} {r['id']}".lower()
        sc = sum(1 for w in words if w in hay)
        if sc > best_score:
            best, best_score = dict(r), sc
    return best if best_score else None


# ------------------------------------------------------------------- the tools
def list_events(sport: Optional[str] = None, limit: int = 25) -> Dict:
    evs = engine.slate(sport=sport, limit_events=limit)
    return dict(count=len(evs), last_odds_refresh=engine.last_refresh(),
                events=_trim([dict(id=e["id"], sport=e["sport"], name=e["name"],
                                   start_time=e["start_time"], venue=e.get("venue"),
                                   markets=e.get("markets", []), odds_count=e.get("odds_count"))
                              for e in evs]))


def get_event(query: str) -> Dict:
    ev = _find_event(query)
    if not ev:
        return dict(error=f"No stored event matches '{query}'. Try list_events first.")
    return dict(event=ev, odds_age_s=engine.odds_age_s(ev["id"]))


def get_current_odds(query: str, market: Optional[str] = None) -> Dict:
    ev = _find_event(query)
    if not ev:
        return dict(error=f"No stored event matches '{query}'.")
    rows = db.latest_odds(event_id=ev["id"])
    if market:
        rows = [r for r in rows if r["market"] == market]
    if not rows:
        return dict(error="No stored odds for that event. Ask the user to press Refresh Odds.")
    return dict(event=ev["name"], odds_age_s=engine.odds_age_s(ev["id"]),
                preferred_books=settings.preferred_books, odds=_trim(rows))


def get_line_history(query: str, market: str, selection: str,
                     book: Optional[str] = None) -> Dict:
    ev = _find_event(query)
    if not ev:
        return dict(error=f"No stored event matches '{query}'.")
    hist = db.odds_history(ev["id"], market, selection, book)
    return dict(event=ev["name"], market=market, selection=selection,
                snapshots=len(hist), history=_trim(hist))


def get_team_stats(sport: str, team: Optional[str] = None) -> Dict:
    try:
        stats, src, demo = R.team_stats(sport)
    except Exception as e:  # noqa: BLE001
        return dict(error=f"team stats unavailable: {e}")
    from ..analytics.ratings import shrink_stats
    stats, w = shrink_stats(stats, sport)
    if team:
        hit = {k: v for k, v in stats.items() if team.lower() in k.lower()}
        return dict(source=src, is_demo=demo, shrink_weight=round(w, 3), stats=hit)
    return dict(source=src, is_demo=demo, shrink_weight=round(w, 3), stats=_trim(stats))


def get_injuries(sport: str, team: Optional[str] = None) -> Dict:
    rows, src, demo = R.injuries(sport, team)
    return dict(source=src, is_demo=demo, count=len(rows), injuries=_trim(rows[:40]))


def get_news(sport: str, query: Optional[str] = None) -> Dict:
    rows, src, demo = R.news(sport, query)
    return dict(source=src, is_demo=demo, news=_trim(rows[:10]))


def get_weather(query: str) -> Dict:
    ev = _find_event(query)
    if not ev:
        return dict(error=f"No stored event matches '{query}'.")
    meta = ev.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    wx, src, demo = R.weather((meta or {}).get("lat"), (meta or {}).get("lon"),
                              ev.get("start_time"), indoor=bool((meta or {}).get("indoor")),
                              city=(meta or {}).get("city"), state=(meta or {}).get("state"),
                              sport=ev.get("sport"))
    if not wx:
        return dict(error="Weather unavailable for this venue — do not estimate it.",
                    source=src)
    return dict(event=ev["name"], source=src, is_demo=demo, weather=wx)


def run_game_analysis(query: str, markets: Optional[List[str]] = None) -> Dict:
    ev = _find_event(query)
    if not ev:
        return dict(error=f"No stored event matches '{query}'.")
    if (engine.odds_age_s(ev["id"]) or 1e9) > settings.stale_odds_s:
        engine.refresh([ev["sport"]])
    res = engine.analyze_event(ev["id"], markets=markets)
    recs = res.get("recommendations", [])
    return dict(event=res["event"]["name"], projection=res.get("projection"),
                weather=res.get("weather"), data_quality=res.get("data_quality_tier"),
                warnings=res.get("warnings"), injuries=_trim(res.get("injuries", [])[:10]),
                sources=_trim(res.get("sources", [])),
                recommendations=_trim([_slim(r) for r in recs[:18]]))


def run_prop_analysis(query: str, player: Optional[str] = None) -> Dict:
    res = run_game_analysis(query, markets=["player_prop"])
    if "error" in res:
        return res
    recs = res.get("recommendations", [])
    if player:
        recs = [r for r in recs if player.lower() in r["selection"].lower()]
        if not recs:
            return dict(error=f"No priced prop market found for '{player}' in this event. "
                              f"The prop may not be offered, or usage data is missing — "
                              f"do not invent a projection.")
    return dict(res, recommendations=recs)


def run_golf_analysis(market: Optional[str] = None, min_price: Optional[int] = None) -> Dict:
    ev = db.q1("SELECT * FROM events WHERE sport='PGA' ORDER BY start_time ASC LIMIT 1")
    if not ev:
        return dict(error="No golf event stored. Ask the user to press Refresh Odds.")
    res = engine.analyze_event(ev["id"], markets=[market] if market else None)
    recs = res.get("recommendations", [])
    if min_price is not None:
        recs = [r for r in recs if r["price"] >= min_price]
    golf = res.get("golf") or {}
    probs = golf.get("probs") or {}
    likeliest = sorted(probs.items(), key=lambda kv: -kv[1]["win"])[:5]
    return dict(tournament=(golf.get("tournament") or {}).get("name"),
                course=(golf.get("tournament") or {}).get("venue"),
                rounds_left=golf.get("rounds_left"),
                most_likely_winners=[dict(player=n, win_prob=round(v["win"], 4)) for n, v in likeliest],
                data_quality=res.get("data_quality_tier"),
                recommendations=_trim([_slim(r) for r in recs[:20]]),
                sources=_trim(res.get("sources", [])))


def find_best_bets(min_confidence: Optional[float] = None, sport: Optional[str] = None,
                   market: Optional[str] = None, plus_money_only: bool = False,
                   limit: int = 12, include_leans: bool = False,
                   only_leans: bool = False) -> Dict:
    recs = engine.cached_slate_recs()
    thr = settings.official_pick_confidence if min_confidence is None else float(min_confidence)
    allowed = {"LEAN"} if only_leans else ({"OFFICIAL PICK", "LEAN", "WATCHLIST"}
                                                if include_leans else {"OFFICIAL PICK"})
    out = [r for r in recs if r["confidence"] >= thr and r["state"] in allowed]
    if sport:
        out = [r for r in out if r["sport"] == sport.upper()]
    if market:
        out = [r for r in out if r["market"] == market]
    if plus_money_only:
        out = [r for r in out if r["price"] > 0]
    official = [r for r in out if r["state"] == "OFFICIAL PICK"]
    return dict(threshold=thr, official_pick_count=len(official), returned=len(out[:limit]),
                note=("No market currently clears the official-pick threshold. Saying so is the "
                      "correct answer; do not lower the bar to produce a play."
                      if not out and not include_leans else
                      ("This is an explicitly requested lower-confidence search; entries retain "
                       "their LEAN/WATCHLIST labels and are not Official Picks."
                       if include_leans else "")),
                bets=_trim([_slim(r) for r in out[:limit]]))


def get_watchlist(limit: int = 12) -> Dict:
    rows = engine.watchlist(limit=limit)
    return dict(count=len(rows), watchlist=_trim([
        dict(_slim(r), trigger=r.get("trigger_note")) for r in rows]))


def get_line_moves(limit: int = 10, sport: Optional[str] = None) -> Dict:
    rows = engine.line_moves(limit=limit, sports=[sport.upper()] if sport else None)
    if not rows:
        return dict(count=0, note="Only one odds snapshot stored so far — movement needs at "
                                  "least two refreshes. Do not guess at movement.")
    return dict(count=len(rows), moves=_trim(rows))


def build_best_parlay(max_legs: int = 3, sport: Optional[str] = None,
                      event: Optional[str] = None, min_confidence: float = 70.0) -> Dict:
    recs = engine.cached_slate_recs()
    ev_id = None
    if event:
        e = _find_event(event)
        ev_id = e["id"] if e else None
    res = parlay.build_best(recs, max_legs=max_legs, min_conf=min_confidence,
                            sports=[sport.upper()] if sport else None, required_event=ev_id)
    if not res:
        return dict(parlay=None,
                    note="No parlay currently clears the quality bar. Two or more independently "
                         "positive-edge legs are required; padding legs to build one would be "
                         "worse than passing.")
    return dict(parlay=dict(res, legs=[_slim(l) for l in res["legs"]]))


def get_model_performance() -> Dict:
    perf = performance()
    if not perf["graded"]:
        return dict(perf, note="No graded predictions yet, so hit rate, CLV and calibration are "
                               "all unavailable. Predictions are being stored and will grade as "
                               "results are ingested.")
    return perf


def search_web(query: str, limit: int = 5) -> Dict:
    rows, src, demo = R.web_search(query, limit)
    if not rows:
        return dict(error=f"Web search unavailable ({src}). Do not substitute recalled "
                          f"information for a live source.")
    return dict(source=src, is_demo=demo, results=_trim(rows),
                note="Treat community sources as context only, never as confirmation.")


def get_provider_status() -> Dict:
    return dict(providers=R.statuses(), demo_mode=settings.demo_mode,
                last_refresh=engine.last_refresh())


# ----------------------------------------------------------------- player props
def _props(event_id: str, **kw) -> Dict:
    """Indirection so tests (and the debug view) can inject the prop workflow."""
    from .. import props as propsmod
    return propsmod.analyze_props(event_id, **kw)


def _resolve_event(event_id: Optional[str]) -> Optional[str]:
    """Explicit event first, otherwise the next NFL game that has not kicked off.

    Ordering every stored event by kickoff and taking the first one returned the
    oldest *finished* game in the database, so an unscoped question was answered
    about a game that had already been played. Completed games are skipped.
    """
    if event_id:
        return event_id
    from .. import integrity as I
    now = time.time()
    rows = db.q("SELECT id, start_time FROM events WHERE sport='NFL' ORDER BY start_time ASC")
    if not settings.demo_mode:
        # Live/demo isolation: a seeded demo fixture must never be chosen as the
        # real next game when the terminal is running against live providers.
        rows = [r for r in rows if not str(r["id"]).startswith("demo-")]
    upcoming = []
    for row in rows:
        kickoff = I.parse_time(row["start_time"])
        if kickoff is None or kickoff >= now:
            upcoming.append(row)
    for row in upcoming:
        if db.latest_odds(event_id=row["id"]):
            return row["id"]
    if upcoming:
        return upcoming[0]["id"]
    return rows[-1]["id"] if rows else None


def _prop_digest(out: Dict, limit: int = 8) -> Dict:
    """Trim a prop board to what the model should talk about."""
    if out.get("error"):
        return dict(error=out["error"])
    keys = ("player", "team", "market", "market_label", "selection", "side", "line",
            "book", "price", "implied_prob", "model_prob", "market_prob", "final_prob",
            "fair_price", "edge_pct", "ev_per_unit", "confidence", "units", "state",
            "source_agreement", "books", "usage_available")
    rows = [{k: r[k] for k in keys if k in r}
            for r in (out.get("recommendations") or [])[:limit]]
    # Verified prices that are deliberately NOT recommendations. Without these the
    # assistant cannot answer "show me the prices even if nothing qualifies".
    price_rows = [{k: r[k] for k in keys + ("probability_basis", "reasons") if k in r}
                  for r in (out.get("prices") or [])[:limit]]
    citations = []
    for item in (out.get("research") or {}).get("citations", [])[:8]:
        citations.append(dict(title=item.get("title"), url=item.get("url"),
                              publisher=item.get("publisher"), reliability=item.get("reliability"),
                              relevance=item.get("relevance")))
    return dict(event=out.get("event"), state=out.get("state"), category=out.get("category"),
                recommendations=rows,
                sources=citations,
                most_likely=_slim_prop(out.get("most_likely")),
                best_value=_slim_prop(out.get("best_value")),
                market_favourite=_slim_prop(out.get("market_favourite")),
                prices=price_rows,
                market=dict(books=(out.get("market") or {}).get("books"),
                            coverage=(out.get("market") or {}).get("coverage"),
                            usable=(out.get("market") or {}).get("usable"),
                            priced=(out.get("market") or {}).get("priced"),
                            no_vig_players=(out.get("market") or {}).get("no_vig_players"),
                            method=(out.get("market") or {}).get("method"),
                            reason=(out.get("market") or {}).get("reason")),
                research=dict(queries_used=(out.get("research") or {}).get("queries_used"),
                              conflicts=(out.get("research") or {}).get("conflicts"),
                              citations=citations),
                gaps=out.get("gaps"), warnings=out.get("warnings"),
                price_source=out.get("price_source"), is_demo=out.get("is_demo"),
                note="Probabilities, fair prices, edges, confidence and units are computed by "
                     "the application. Research only supplies bounded, sourced context factors. "
                     "`prices` are verified sportsbook prices only and are NOT recommendations; "
                     "rows with market_prob = null have no same-player Yes/No quote, so no fair "
                     "market probability exists and no edge may be claimed for them. "
                     "Confidence is a data-quality score, never a probability that the bet wins.")


def _slim_prop(row: Optional[Dict]) -> Optional[Dict]:
    if not row:
        return None
    keys = ("player", "team", "market", "line", "book", "price", "implied_prob",
            "model_prob", "market_prob", "final_prob", "fair_price",
            "edge_pct", "ev_per_unit", "confidence", "state", "sources")
    return {k: row[k] for k in keys if k in row}


def analyze_td_scorers(event_id: Optional[str] = None, research: bool = True) -> Dict:
    eid = _resolve_event(event_id)
    if not eid:
        return dict(error="No NFL event available. Refresh odds first.")
    return _prop_digest(_props(eid, category="td", research=research))


def analyze_player_prop(event_id: Optional[str] = None, player: str = "",
                        market: Optional[str] = None, category: Optional[str] = None) -> Dict:
    eid = _resolve_event(event_id)
    if not eid:
        return dict(error="No NFL event available. Refresh odds first.")
    out = _props(eid, category=category)
    digest = _prop_digest(out, limit=40)
    needle = (player or "").strip().lower()
    wanted = (market or "").strip().lower()
    rows = [r for r in digest["recommendations"]
            if (not needle or needle in (r.get("player") or "").lower())
            and (not wanted or wanted == (r.get("market") or "").lower())]
    digest["recommendations"] = rows[:12]
    if not rows:
        digest["note"] = (f"No priced {market or 'prop'} market for {player or 'that player'} in "
                          "this game. The market may not be offered yet, or the player may be "
                          "unavailable.")
    return digest


def find_best_player_props(event_id: Optional[str] = None, market: Optional[str] = None,
                           min_confidence: Optional[float] = None, limit: int = 10) -> Dict:
    eid = _resolve_event(event_id)
    if not eid:
        return dict(error="No NFL event available. Refresh odds first.")
    out = _props(eid, category=None)
    threshold = float(min_confidence if min_confidence is not None
                      else settings.prop_lean_confidence)
    rows = [r for r in (out.get("recommendations") or [])
            if r["confidence"] >= threshold and r["edge_pct"] > 0
            and (not market or r["market"] == market)]
    official = [r for r in rows if r["state"] == "OFFICIAL PICK"]
    keys = ("player", "market", "market_label", "selection", "line", "book", "price",
            "final_prob", "fair_price", "edge_pct", "confidence", "units", "state", "sources")
    return dict(event=out.get("event"), threshold=threshold,
                official_pick_count=len(official),
                recommendations=[{k: r[k] for k in keys if k in r} for r in rows[:limit]],
                note=("No player prop currently clears the prop threshold and the deterministic "
                      "gates. Saying so is the correct answer." if not official else
                      "Official props clear prop-specific confidence, coverage, freshness and "
                      "source-agreement gates."))


def get_player_props(event_id: Optional[str] = None, category: Optional[str] = None,
                     player: Optional[str] = None) -> Dict:
    eid = _resolve_event(event_id)
    if not eid:
        return dict(error="No NFL event available. Refresh odds first.")
    out = _props(eid, category=category)
    rows = out.get("recommendations") or []
    if player:
        needle = player.lower()
        rows = [r for r in rows if needle in (r.get("player") or "").lower()]
    seen, unique = set(), []
    for row in rows:
        key = (row.get("player"), row.get("market"), row.get("line"), row.get("side"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(player=row.get("player"), market=row.get("market"),
                           market_label=row.get("market_label"), side=row.get("side"),
                           line=row.get("line"), book=row.get("book"), price=row.get("price"),
                           books=row.get("books"), state=row.get("state")))
    return dict(event=out.get("event"), category=category or "all",
                price_source=out.get("price_source"), markets_requested=out.get("markets_requested"),
                count=len(unique), props=unique[:40],
                market=dict(coverage=(out.get("market") or {}).get("coverage"),
                            books=(out.get("market") or {}).get("books")),
                cache_note="Prop prices are fetched on demand and cached briefly to protect "
                           "Odds API quota.")


def get_player_usage(player: str = "", team: Optional[str] = None,
                     season: Optional[int] = None) -> Dict:
    from ..analytics import usage as usagemod
    plays = R.nflverse_plays(season)
    if not plays:
        return dict(error="No locally cached nflverse play-by-play for that season. Player usage "
                          "is unavailable rather than estimated.")
    rows = usagemod.aggregate_player_usage(plays, season=season or _current_season(), team=team)
    if not player:
        ranked = sorted(rows.values(), key=lambda r: -r["touches"])[:12]
        return dict(season=season or _current_season(), players=[_slim_usage(r) for r in ranked],
                    source="nflverse-pbp",
                    missing=list(usagemod.MISSING_METRICS))
    report = usagemod.usage_report(plays, season=season or _current_season(), player=player,
                                   team=team)
    if not report.get("available"):
        return dict(error=report.get("reason"), player=player, source="nflverse-pbp")
    return dict(player=report["player"], season=season or _current_season(),
                usage=_slim_usage(report["usage"]),
                role_stability=report["role_stability"], stability_reason=report["stability_reason"],
                red_zone_share=report["red_zone_share"], missing=report["missing"],
                source="nflverse-pbp",
                note="Snap share and route participation are not derivable from play-by-play and "
                     "are reported as missing rather than guessed.")


def _slim_usage(row: Dict) -> Dict:
    keys = ("name", "team", "games", "carries", "targets", "receptions", "rush_yards",
            "rec_yards", "pass_attempts", "pass_yards", "total_tds", "red_zone_touches",
            "goal_line_touches", "touches_per_game", "carries_per_game", "targets_per_game",
            "td_per_game", "snap_share", "missing", "source", "season")
    return {k: row[k] for k in keys if k in row}


def _current_season() -> int:
    import datetime as _dt
    return _dt.datetime.utcnow().year


def search_player_context(player: str, team: Optional[str] = None,
                          question: Optional[str] = None, topic: str = "role") -> Dict:
    """Targeted research for one named uncertainty. Never a broad web crawl."""
    from ..research import provider as rprovider
    from ..research import sources as rssources
    query = question or f"{player} {team or ''} expected role workload injury status".strip()
    found = rprovider.shared().search(query, topic=topic, limit=settings.research_results_per_query,
                                      player=player, team=team)
    if not found["items"]:
        return dict(player=player, query=query, results=[],
                    note="No current sources found. Do not substitute recalled information.",
                    errors=found["errors"])
    return dict(player=player, query=query, cache=found["cache"],
                backends_used=found["backends_used"],
                results=[dict(title=i["title"], url=i["url"], publisher=i["publisher"],
                              reliability=i["reliability"], relevance=i["relevance"],
                              published_at=i["published_at"], retrieved_at=i["retrieved_at"],
                              snippet=i["snippet"][:240]) for i in found["items"]],
                reliability_scale=list(rssources.ORDER),
                note="Confirmation requires PRIMARY/HIGH sources; community posts are context only.")


def _slim(r: Dict) -> Dict:
    keys = ("id", "event_id", "event_name", "sport", "start_time", "market", "market_label",
            "selection", "side", "line", "book", "price", "model_prob", "implied_prob",
            "fair_price", "edge_pct", "confidence", "units", "state", "data_quality",
            "breakeven_price", "trigger_note", "stale", "is_demo")
    out = {k: r[k] for k in keys if k in r}
    if "line_movement" in r:
        mv = r["line_movement"]
        out["line_movement"] = dict(open_line=mv.get("open_line"), delta=mv.get("delta"),
                                    snapshots=mv.get("snapshots"))
    if "reasoning" in r:
        out["reasoning"] = r["reasoning"][:6]
    if "risks" in r:
        out["risks"] = r["risks"][:4]
    if "consensus" in r:
        out["model_consensus"] = r["consensus"]
    if "all_quotes" in r:
        out["all_quotes"] = r["all_quotes"][:6]
    return out


REGISTRY: Dict[str, Callable[..., Dict]] = {
    "list_events": list_events, "get_event": get_event, "get_current_odds": get_current_odds,
    "get_line_history": get_line_history, "get_team_stats": get_team_stats,
    "get_injuries": get_injuries, "get_news": get_news, "get_weather": get_weather,
    "run_game_analysis": run_game_analysis, "run_prop_analysis": run_prop_analysis,
    "find_best_bets": find_best_bets,
    "get_watchlist": get_watchlist, "get_line_moves": get_line_moves,
    "build_best_parlay": build_best_parlay, "get_model_performance": get_model_performance,
    "search_web": search_web, "get_provider_status": get_provider_status,
    "get_player_props": get_player_props, "get_player_usage": get_player_usage,
    "search_player_context": search_player_context,
    "analyze_td_scorers": analyze_td_scorers, "analyze_player_prop": analyze_player_prop,
    "find_best_player_props": find_best_player_props,
}


def _p(required=(), **props):
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


SPORT_ENUM = {"type": "string", "enum": ["NFL"]}

SCHEMAS: List[Dict] = [
    dict(name="list_events", description="List stored events/slate with their best primary market prices.",
         parameters=_p(sport=SPORT_ENUM, limit={"type": "integer"})),
    dict(name="get_event", description="Resolve a fuzzy team/game name to a stored event.",
         parameters=_p(required=("query",), query={"type": "string"})),
    dict(name="get_current_odds", description="Latest stored odds across books for one event.",
         parameters=_p(required=("query",), query={"type": "string"}, market={"type": "string"})),
    dict(name="get_line_history", description="Every stored odds snapshot for one selection, for line movement.",
         parameters=_p(required=("query", "market", "selection"), query={"type": "string"}, market={"type": "string"},
                       selection={"type": "string"}, book={"type": "string"})),
    dict(name="get_team_stats", description="Opponent-adjusted team rates, regressed to a league prior.",
         parameters=_p(required=("sport",), sport=SPORT_ENUM, team={"type": "string"})),
    dict(name="get_injuries", description="Current injury report rows.",
         parameters=_p(required=("sport",), sport=SPORT_ENUM, team={"type": "string"})),
    dict(name="get_news", description="Recent news items with trust labels.",
         parameters=_p(required=("sport",), sport=SPORT_ENUM, query={"type": "string"})),
    dict(name="get_weather", description="Forecast for an outdoor event's venue. Never estimate weather yourself.",
         parameters=_p(required=("query",), query={"type": "string"})),
    dict(name="run_game_analysis", description="Full analysis pipeline for one game: projection, every market priced, confidence, states, sources.",
         parameters=_p(required=("query",), query={"type": "string"},
                       markets={"type": "array", "items": {"type": "string"}})),
    dict(name="run_prop_analysis", description="Player prop markets for one game, optionally filtered to a player.",
         parameters=_p(required=("query",), query={"type": "string"}, player={"type": "string"})),

    dict(name="find_best_bets", description=(
             "Ranked slate recommendations. For best bet, best play, strongest bet/play, or "
             "Official Pick requests, omit min_confidence: the tool defaults to the application's "
             "configured production Official Pick threshold and returns only deterministically "
             "qualified Official Picks. Never lower min_confidence unless the user explicitly "
             "asks for a numeric lower threshold, leans, watchlist plays, or broader opportunities."),
         parameters=_p(min_confidence={"type": "number"}, sport=SPORT_ENUM,
                       market={"type": "string"}, plus_money_only={"type": "boolean"},
                       limit={"type": "integer"}, include_leans={"type": "boolean"},
                       only_leans={"type": "boolean"})),
    dict(name="get_watchlist", description="Markets that do not qualify yet and their trigger conditions.",
         parameters=_p(limit={"type": "integer"})),
    dict(name="get_line_moves", description="Largest observed line movement across stored snapshots.",
         parameters=_p(limit={"type": "integer"}, sport=SPORT_ENUM)),
    dict(name="build_best_parlay", description="Build the single best correlation-adjusted parlay. Returns none if nothing qualifies.",
         parameters=_p(max_legs={"type": "integer"}, sport=SPORT_ENUM,
                       event={"type": "string"}, min_confidence={"type": "number"})),
    dict(name="get_model_performance", description="Hit rate, ROI, CLV and confidence calibration from graded predictions.",
         parameters=_p()),
    dict(name="search_web", description="Live web research for late-breaking information. Cite what you use.",
         parameters=_p(required=("query",), query={"type": "string"}, limit={"type": "integer"})),
    dict(name="get_provider_status", description="Which data providers are live, failed, or serving demo fixtures.",
         parameters=_p()),

    dict(name="get_player_props", description=(
             "Player prop markets for one NFL game, fetched on demand from the Odds API and "
             "cached briefly. Use before answering any prop price question."),
         parameters=_p(event_id={"type": "string"},
                       category={"type": "string",
                                 "enum": ["all", "best", "td", "passing", "rushing", "receiving"]},
                       player={"type": "string"})),
    dict(name="get_player_usage", description=(
             "Reproducible player usage from nflverse play-by-play: carries, targets, catches, "
             "yards, TDs, red-zone and goal-line touches. Snap share is reported as missing "
             "rather than estimated."),
         parameters=_p(player={"type": "string"}, team={"type": "string"},
                       season={"type": "integer"})),
    dict(name="search_player_context", description=(
             "Targeted web research for one named uncertainty (role, workload, injury status). "
             "Returns sources with a reliability class. Never a broad crawl; use only for a "
             "specific gap."),
         parameters=_p(required=("player",), player={"type": "string"}, team={"type": "string"},
                       question={"type": "string"},
                       topic={"type": "string",
                              "enum": ["role", "injury", "depth_chart", "weather", "news",
                                       "general"]})),
    dict(name="analyze_td_scorers", description=(
             "Deterministic anytime-TD board: models each player's scoring probability, "
             "normalises the juiced multi-outcome market, and separates the MOST LIKELY scorer "
             "from the BEST VALUE bet. Returns PASS or INSUFFICIENT DATA when coverage is thin."),
         parameters=_p(event_id={"type": "string"}, research={"type": "boolean"})),
    dict(name="analyze_player_prop", description=(
             "Opportunity-first projection and market comparison for one player's prop markets."),
         parameters=_p(event_id={"type": "string"}, player={"type": "string"},
                       market={"type": "string"}, category={"type": "string"})),
    dict(name="find_best_player_props", description=(
             "Ranked player-prop recommendations that clear the prop-specific deterministic "
             "gates. Never lower the prop threshold to manufacture a play."),
         parameters=_p(event_id={"type": "string"}, market={"type": "string"},
                       min_confidence={"type": "number"}, limit={"type": "integer"})),
]


def call(name: str, args: Dict) -> Dict:
    fn = REGISTRY.get(name)
    if not fn:
        return dict(error=f"unknown tool {name}")
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return dict(error=f"bad arguments for {name}: {e}")
    except Exception as e:  # noqa: BLE001
        return dict(error=f"{name} failed: {type(e).__name__}: {e}")
