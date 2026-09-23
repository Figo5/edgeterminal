"""FastAPI application. Thin HTTP layer over the engine -- no analytics here."""
from __future__ import annotations

import logging
import json
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import calibration, db, engine, logs, parlay, props
from .agents import llm
from .config import ROOT, settings
from .providers import registry as R

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logs.install_redaction()
log = logging.getLogger("edgeterminal.api")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # uvicorn installs its own handlers after this module is imported, so
    # re-attach redaction once the real logging configuration exists.
    logs.install_redaction()
    db.init()
    engine.restore_families(force=True)
    if not db.q1("SELECT id FROM events LIMIT 1"):
        log.info("empty database — running an initial odds refresh")
        try:
            engine.refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("initial refresh failed: %s", e)
    yield


app = FastAPI(title="EdgeTerminal", version="0.4.0",
              description="Local sports-market research terminal. Not a sportsbook.",
              lifespan=_lifespan)

WEB = ROOT / "edgeterminal" / "web"


def _active(sport: Optional[str]) -> Optional[List[str]]:
    """Scope an optional `sport` argument to the active product sports."""
    if not sport:
        return list(settings.active_sports)
    key = sport.upper()
    if key not in settings.active_sports:
        raise HTTPException(410, f"EdgeTerminal currently serves "
                                 f"{'/'.join(settings.active_sports)} only — {key} is not active.")
    return [key]


# --------------------------------------------------------------------- models
class RefreshBody(BaseModel):
    sports: Optional[List[str]] = None


class ChatBody(BaseModel):
    message: str
    history: List[Dict[str, Any]] = Field(default_factory=list)
    event_id: Optional[str] = None
    # Conversation-scoped state returned by the previous turn and echoed back by
    # the client. Validated server-side like any other client input; it is what
    # carries "the game we were talking about" across a turn boundary.
    context: Optional[Dict[str, Any]] = None


class GradeBody(BaseModel):
    prediction_id: str
    won: Optional[bool] = None
    push: bool = False
    actual_value: Optional[float] = None
    notes: str = ""


# --------------------------------------------------------------------- system
@app.get("/api/health")
def health() -> Dict:
    return dict(ok=True, version=app.version, time=time.time(),
                llm=settings.has_llm(), demo_mode=settings.demo_mode)


@app.get("/api/config")
def config() -> Dict:
    lr = engine.last_refresh()
    return dict(
        sports=list(settings.active_sports), product_scope="NFL-only",
        preferred_books=settings.preferred_books,
        official_pick_confidence=settings.official_pick_confidence,
        lean_confidence=settings.lean_confidence, min_edge_pct=settings.min_edge_pct,
        demo_mode=settings.demo_mode, llm_enabled=settings.has_llm(),
        llm_model=settings.openai_model if settings.has_llm() else None,
        model_version=settings.model_version, confidence_version=settings.confidence_version,
        stale_odds_s=settings.stale_odds_s,
        last_refresh=lr, odds_age_s=engine.odds_age_s(),
        providers=R.statuses())


@app.get("/api/providers")
def providers() -> Dict:
    return dict(providers=R.statuses(), demo_mode=settings.demo_mode,
                props=R.prop_quota(),
                search_provider=settings.search_provider,
                odds_provider=settings.odds_provider,
                odds_key_configured=bool(settings.odds_api_key))


@app.post("/api/refresh")
def refresh(body: RefreshBody) -> Dict:
    requested = [s.upper() for s in (body.sports or settings.active_sports)]
    sports = [s for s in requested if s in settings.active_sports]
    ignored = sorted({s for s in requested if s not in settings.active_sports})
    result = engine.refresh(sports or list(settings.active_sports))
    try:
        engine.analyze_slate(sports=sports or list(settings.active_sports))
    except Exception as e:  # noqa: BLE001
        log.warning("post-refresh analysis failed: %s", e)
        result["analysis_error"] = str(e)
    out = dict(result, odds_age_s=engine.odds_age_s())
    if ignored:
        out["ignored_sports"] = ignored
        out["ignored_note"] = ("Dormant sports are not fetched while the product scope is "
                               + "/".join(settings.active_sports) + ".")
    return out


# ------------------------------------------------------------------ dashboard
def _headline(recs: List[Dict]) -> Dict:
    picks = [r for r in recs if r["state"] == "OFFICIAL PICK"]
    plus = [r for r in picks if r["price"] > 0] or [r for r in recs
                                                    if r["price"] > 0 and r["edge_pct"] > 0]
    by_conf = sorted(recs, key=lambda r: -r["confidence"])
    by_edge = sorted([r for r in recs if r["state"] in ("OFFICIAL PICK", "LEAN", "WATCHLIST")],
                     key=lambda r: -r["edge_pct"])
    return dict(
        best_bet=picks[0] if picks else None,
        best_plus_money=plus[0] if plus else None,
        highest_confidence=by_conf[0] if by_conf else None,
        biggest_edge=by_edge[0] if by_edge else None,
        official_pick_count=len(picks))


@app.get("/api/dashboard")
def dashboard(sport: Optional[str] = None, refresh_analysis: bool = False) -> Dict:
    sports = _active(sport)
    recs = (engine.analyze_slate(sports=sports) if refresh_analysis
            else engine.cached_slate_recs(sports=sports))
    recs = [r for r in recs if r["sport"] in sports]
    picks = [r for r in recs if r["state"] == "OFFICIAL PICK"]
    watch = [r for r in recs if r["state"] in ("LEAN", "WATCHLIST", "WAIT", "WAIT FOR PRICE")][:18]
    lr = engine.last_refresh() or {}
    refreshed = lr.get("sports", {})
    serving_demo = settings.demo_mode or any(
        bool(refreshed.get(s, {}).get("is_demo")) for s in sports)
    return dict(
        generated_at=time.time(), odds_age_s=engine.odds_age_s(),
        last_refresh=lr or None, demo_mode=serving_demo,
        headline=dict(_headline(recs), best_parlay=None),
        best_bets=picks[:25], watchlist=watch,
        line_moves=engine.line_moves(limit=10, sports=sports),
        upcoming=engine.weekly_board() if sports == ["NFL"] else engine.slate(
            sport=sports[0], limit_events=20),
        counts=dict(total=len(recs),
                    official=len(picks),
                    leans=len([r for r in recs if r["state"] == "LEAN"]),
                    watch=len([r for r in recs if r["state"] == "WATCHLIST"]),
                    no_edge=len([r for r in recs if r["state"] in ("NO EDGE", "PASS")])),
        providers=R.statuses())


@app.get("/api/slate")
def slate(sport: Optional[str] = None, limit: int = 40) -> Dict:
    return dict(events=engine.slate(sport=_active(sport)[0], limit_events=limit),
                odds_age_s=engine.odds_age_s())


@app.get("/api/event/{event_id}")
def event(event_id: str, refresh_odds: bool = False) -> Dict:
    row = db.q1("SELECT sport FROM events WHERE id=?", (event_id,))
    if not row:
        raise HTTPException(404, f"unknown event {event_id}")
    _active(row["sport"])
    if refresh_odds:
        engine.refresh([row["sport"]])
    try:
        return engine.analyze_event(event_id)
    except Exception as e:  # noqa: BLE001
        log.exception("analysis failed")
        raise HTTPException(500, f"analysis failed: {e}")


@app.get("/api/best-bets")
def best_bets(min_confidence: Optional[float] = None, sport: Optional[str] = None,
              market: Optional[str] = None, plus_money_only: bool = False,
              limit: int = 40) -> Dict:
    sports = _active(sport)
    recs = engine.cached_slate_recs(sports=sports)
    recs = [r for r in recs if r["sport"] in sports]
    thr = settings.official_pick_confidence if min_confidence is None else min_confidence
    out = [r for r in recs if r["confidence"] >= thr and r["state"] == "OFFICIAL PICK"]
    if market:
        out = [r for r in out if r["market"] == market]
    if plus_money_only:
        out = [r for r in out if r["price"] > 0]
    return dict(threshold=thr, count=len(out), bets=out[:limit])


@app.get("/api/watchlist")
def watchlist(limit: int = 30, sport: Optional[str] = None) -> Dict:
    rows = engine.watchlist(limit=limit, sports=_active(sport))
    return dict(count=len(rows), watchlist=rows)


@app.get("/api/line-moves")
def line_moves(limit: int = 25, sport: Optional[str] = None) -> Dict:
    return dict(moves=engine.line_moves(limit=limit, sports=_active(sport)))


@app.get("/api/line-history")
def line_history(event_id: str, market: str, selection: str,
                 book: Optional[str] = None) -> Dict:
    return dict(history=db.odds_history(event_id, market, selection, book))


@app.get("/api/parlay")
def build_parlay(max_legs: int = 3, sport: Optional[str] = None,
                 event_id: Optional[str] = None, min_confidence: float = 70.0) -> Dict:
    sports = _active(sport)
    recs = engine.cached_slate_recs(sports=sports)
    res = parlay.build_best(recs, max_legs=max_legs, min_conf=min_confidence,
                            sports=sports,
                            required_event=event_id)
    if not res:
        return dict(parlay=None,
                    note="No parlay clears the quality bar right now. Padding legs to force one "
                         "would lower expected value, so the answer is none.")
    return dict(parlay=res)


@app.get("/api/golf")
def golf() -> Dict:
    raise HTTPException(410, "Golf is not part of the active product scope. EdgeTerminal "
                             "currently serves " + "/".join(settings.active_sports) + " only.")


@app.get("/api/performance")
def performance() -> Dict:
    return dict(readiness="RESEARCH_ONLY", forward_shadow_status="NOT_STARTED",
                graded_nfl_game_predictions=0,
                note="Automatic NFL forward game grading has not been implemented. "
                     "Use /api/model-report for seen historical replays.")


@app.get("/api/model-report")
def model_report() -> Dict:
    """Read-only historical diagnostics; never presented as forward results."""
    path = ROOT / "reports" / "nfl_model_v1" / "backtest_results.json"
    if not path.exists():
        return dict(readiness="RESEARCH_ONLY", historical={},
                    note="Historical report has not been generated yet")
    return dict(readiness="RESEARCH_ONLY", historical=json.loads(path.read_text()),
                note="2023–2025 retrospective development diagnostics; no forward shadow record")


# ------------------------------------------------------------------- props
@app.get("/api/event/{event_id}/props")
def event_props(event_id: str, category: Optional[str] = None,
                player: Optional[str] = None, research: bool = True) -> Dict:
    """Player props are fetched on demand here and nowhere else."""
    row = db.q1("SELECT sport FROM events WHERE id=?", (event_id,))
    if not row:
        raise HTTPException(404, f"unknown event {event_id}")
    _active(row["sport"])
    if not settings.props_enabled:
        raise HTTPException(410, "player props are disabled in this deployment")
    out = props.analyze_props(event_id, category=category, research=research)
    if out.get("error"):
        raise HTTPException(409, out["error"])
    if player:
        needle = player.lower()
        out["recommendations"] = [r for r in out["recommendations"]
                                  if needle in (r.get("player") or "").lower()]
    out["quota"] = R.prop_quota()
    return out


@app.get("/api/props/td")
def td_scorers(event_id: Optional[str] = None, research: bool = True) -> Dict:
    from .agents import tools as T
    board = T.analyze_td_scorers(event_id=event_id, research=research)
    if board.get("error"):
        raise HTTPException(409, board["error"])
    board["quota"] = R.prop_quota()
    return board


@app.get("/api/props/best")
def best_props(event_id: Optional[str] = None, market: Optional[str] = None,
               min_confidence: Optional[float] = None) -> Dict:
    from .agents import tools as T
    out = T.find_best_player_props(event_id=event_id, market=market,
                                   min_confidence=min_confidence)
    out["quota"] = R.prop_quota()
    return out


@app.get("/api/debug/research")
def debug_research(event_id: Optional[str] = None, queries: int = 25) -> Dict:
    """Development trace: is research actually being used, and how much did it cost?"""
    if not settings.debug_endpoints:
        raise HTTPException(404, "debug endpoints are disabled")
    from .research import cache as rcache
    from .research import provider as rprovider
    cache = rprovider.shared().cache
    payload = dict(cache=cache.stats(), cache_entries=cache.describe(limit=queries),
                   queries=rprovider.shared().trace(limit=queries),
                   prop_fetches=R.recent_prop_fetches(),
                   quota=R.prop_quota(),
                   ttl_by_topic=rcache.TTL_BY_TOPIC,
                   research_enabled=settings.research_enabled,
                   query_budget=settings.research_query_budget,
                   openai_search_model=settings.openai_search_model or "(chat model)")
    if event_id:
        payload["board"] = props.analyze_props(event_id, research=True)
    return payload


@app.post("/api/performance/recompute")
def recompute() -> Dict:
    return dict(scopes=calibration.recompute())


@app.post("/api/grade")
def grade(body: GradeBody) -> Dict:
    try:
        return calibration.grade_prediction(body.prediction_id, body.won, body.push,
                                            body.actual_value, notes=body.notes)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/close-lines/{event_id}")
def close_lines(event_id: str) -> Dict:
    return dict(rows=calibration.snapshot_closing_lines(event_id))


@app.post("/api/chat")
def chat(body: ChatBody) -> Dict:
    # The selected event is context, not an instruction: an explicit day or team in the
    # message outranks it. Precedence is decided in agents.context, not in prose here.
    note = None
    if body.event_id:
        ev = db.q1("SELECT name, sport FROM events WHERE id=?", (body.event_id,))
        if ev:
            note = (f"The user is currently viewing {ev['name']} ({ev['sport']}), event id "
                    f"{body.event_id}.")
    try:
        return llm.run_chat(body.message, body.history, note,
                            selected_event_id=body.event_id,
                            prior_context=body.context)
    except Exception as e:  # noqa: BLE001
        log.error("chat failed: %s", type(e).__name__)
        return JSONResponse(status_code=500,
                            content=dict(answer=f"Chat failed: {type(e).__name__}",
                                         tool_trace=[]))


@app.get("/api/predictions")
def predictions(limit: int = 100, state: Optional[str] = None) -> Dict:
    sql = "SELECT * FROM predictions"
    params: List[Any] = []
    if state:
        sql += " WHERE state=?"
        params.append(state)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return dict(predictions=db.q(sql, params))


# ------------------------------------------------------------------ static UI
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
