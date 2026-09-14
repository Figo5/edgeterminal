"""FastAPI application. Thin HTTP layer over the engine -- no analytics here."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import calibration, db, engine, parlay
from .agents import llm
from .config import ROOT, SPORTS, settings
from .providers import registry as R

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("edgeterminal.api")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
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


# --------------------------------------------------------------------- models
class RefreshBody(BaseModel):
    sports: Optional[List[str]] = None


class ChatBody(BaseModel):
    message: str
    history: List[Dict[str, Any]] = Field(default_factory=list)
    event_id: Optional[str] = None


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
        sports=list(SPORTS), preferred_books=settings.preferred_books,
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
                search_provider=settings.search_provider,
                odds_provider=settings.odds_provider,
                odds_key_configured=bool(settings.odds_api_key))


@app.post("/api/refresh")
def refresh(body: RefreshBody) -> Dict:
    sports = [s.upper() for s in (body.sports or list(SPORTS)) if s.upper() in SPORTS]
    result = engine.refresh(sports or None)
    try:
        engine.analyze_slate(sports=sports or None)
    except Exception as e:  # noqa: BLE001
        log.warning("post-refresh analysis failed: %s", e)
        result["analysis_error"] = str(e)
    return dict(result, odds_age_s=engine.odds_age_s())


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
    sports = [sport.upper()] if sport else None
    recs = (engine.analyze_slate(sports=sports) if refresh_analysis
            else engine.cached_slate_recs(sports=sports))
    if sports:
        recs = [r for r in recs if r["sport"] in sports]
    picks = [r for r in recs if r["state"] == "OFFICIAL PICK"]
    watch = [r for r in recs if r["state"] in ("LEAN", "WATCHLIST", "WAIT", "WAIT FOR PRICE")][:18]
    try:
        best_parlay = parlay.build_best(recs, max_legs=3)
    except Exception as e:  # noqa: BLE001
        log.warning("parlay build failed: %s", e)
        best_parlay = None
    golf_ev = db.q1("SELECT * FROM events WHERE sport='PGA' ORDER BY start_time LIMIT 1")
    golf_recs = [r for r in recs if r["sport"] == "PGA"][:8]
    lr = engine.last_refresh() or {}
    refreshed = lr.get("sports", {})
    active_sports = sports or list(SPORTS)
    serving_demo = settings.demo_mode or any(
        bool(refreshed.get(s, {}).get("is_demo")) for s in active_sports)
    return dict(
        generated_at=time.time(), odds_age_s=engine.odds_age_s(),
        last_refresh=lr or None, demo_mode=serving_demo,
        headline=dict(_headline(recs), best_parlay=best_parlay),
        best_bets=picks[:25], watchlist=watch,
        line_moves=engine.line_moves(limit=10, sports=sports),
        upcoming=engine.slate(sport=sport, limit_events=20),
        golf=dict(event=dict(golf_ev) if golf_ev else None, recommendations=golf_recs),
        counts=dict(total=len(recs),
                    official=len(picks),
                    leans=len([r for r in recs if r["state"] == "LEAN"]),
                    watch=len([r for r in recs if r["state"] == "WATCHLIST"]),
                    no_edge=len([r for r in recs if r["state"] in ("NO EDGE", "PASS")])),
        providers=R.statuses())


@app.get("/api/slate")
def slate(sport: Optional[str] = None, limit: int = 40) -> Dict:
    return dict(events=engine.slate(sport=sport.upper() if sport else None,
                                    limit_events=limit),
                odds_age_s=engine.odds_age_s())


@app.get("/api/event/{event_id}")
def event(event_id: str, refresh_odds: bool = False) -> Dict:
    row = db.q1("SELECT sport FROM events WHERE id=?", (event_id,))
    if not row:
        raise HTTPException(404, f"unknown event {event_id}")
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
    recs = engine.cached_slate_recs()
    thr = settings.official_pick_confidence if min_confidence is None else min_confidence
    out = [r for r in recs if r["confidence"] >= thr and r["state"] == "OFFICIAL PICK"]
    if sport:
        out = [r for r in out if r["sport"] == sport.upper()]
    if market:
        out = [r for r in out if r["market"] == market]
    if plus_money_only:
        out = [r for r in out if r["price"] > 0]
    return dict(threshold=thr, count=len(out), bets=out[:limit])


@app.get("/api/watchlist")
def watchlist(limit: int = 30, sport: Optional[str] = None) -> Dict:
    rows = engine.watchlist(limit=limit, sports=[sport.upper()] if sport else None)
    return dict(count=len(rows), watchlist=rows)


@app.get("/api/line-moves")
def line_moves(limit: int = 25, sport: Optional[str] = None) -> Dict:
    return dict(moves=engine.line_moves(limit=limit,
                                        sports=[sport.upper()] if sport else None))


@app.get("/api/line-history")
def line_history(event_id: str, market: str, selection: str,
                 book: Optional[str] = None) -> Dict:
    return dict(history=db.odds_history(event_id, market, selection, book))


@app.get("/api/parlay")
def build_parlay(max_legs: int = 3, sport: Optional[str] = None,
                 event_id: Optional[str] = None, min_confidence: float = 70.0) -> Dict:
    sports = [sport.upper()] if sport else None
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
    ev = db.q1("SELECT * FROM events WHERE sport='PGA' ORDER BY start_time LIMIT 1")
    if not ev:
        return dict(event=None, note="No golf event stored. Refresh odds.")
    return engine.analyze_event(ev["id"])


@app.get("/api/performance")
def performance() -> Dict:
    return calibration.performance()


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
    note = None
    if body.event_id:
        ev = db.q1("SELECT name, sport FROM events WHERE id=?", (body.event_id,))
        if ev:
            note = (f"The user is currently viewing {ev['name']} ({ev['sport']}), event id "
                    f"{body.event_id}. Resolve ambiguous references to this event.")
    try:
        return llm.run_chat(body.message, body.history, note)
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
