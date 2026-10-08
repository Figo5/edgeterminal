"""FastAPI application. Thin HTTP layer over the engine -- no analytics here."""
from __future__ import annotations

import logging
import json
import time
from urllib.parse import urlsplit
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import calibration, db, engine, logs, nfl_forward_grading, nfl_market_capture, nfl_props, paper_research, product_views, research_refresh, parlay, props, shadow
from .agents import llm
from .config import ROOT, settings
from .providers import registry as R
from . import beta_access, beta_api, product_insights, visit_refresh

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
    if settings.hosted_mode:
        if urlsplit(settings.public_url).scheme != 'https' or settings.odds_provider != 'espn':
            raise RuntimeError('Hosted beta requires an HTTPS PUBLIC_URL and ODDS_PROVIDER=espn')
        # Hosted HTTP processes only serve stored data. The worker owns requests.
        yield
        return
    if not db.q1("SELECT id FROM events LIMIT 1"):
        log.info("empty database — running an initial odds refresh")
        try:
            engine.refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("initial refresh failed: %s", e)
    try:
        shadow.freeze_pending()
        shadow.sync_results()
    except Exception as e:  # noqa: BLE001
        log.warning("initial shadow sync failed: %s", type(e).__name__)
    try:
        research_sync=nfl_forward_grading.sync_all(freeze_dir=ROOT / "reports/nfl_model_v2")
        if any("error" in row for row in research_sync["sync"]):
            log.warning("initial research grade sync has pending errors: %s",research_sync["sync"])
    except Exception as e:  # noqa: BLE001
        log.warning("initial research grade sync failed: %s", type(e).__name__)
    yield


app = FastAPI(title="EdgeTerminal", version="0.4.0",
              description="Local sports-market research terminal. Not a sportsbook.",
              lifespan=_lifespan)
app.include_router(beta_api.router)


@app.middleware('http')
async def beta_boundary(request: Request, call_next):
    """Prevent public users from reaching legacy ingestion, grading or debug APIs."""
    host = request.url.hostname
    expected = urlsplit(settings.public_url).hostname
    valid_host = host == expected if settings.hosted_mode else host in {'127.0.0.1', 'localhost', '::1', 'testserver'}
    if not valid_host:
        return JSONResponse({'detail': 'Unknown application host'}, status_code=400)
    path = request.url.path
    if settings.hosted_mode:
        readable = {'/api/health', '/api/config', '/api/product/nfl', '/api/product/performance',
                    '/api/product/status', '/api/product/methodology', '/api/product/nfl/refresh',
                    '/api/product/nfl/check'}
        member_path = path.startswith('/api/account') or path in {
            '/api/auth/request-link', '/api/auth/signin', '/api/auth/signout'}
        matchup_path = path.startswith('/api/product/nfl/nfl-')
        if path.startswith('/api/') and not (member_path or path in readable or matchup_path):
            return JSONResponse({'detail': 'This endpoint is private to the research operator'}, status_code=403)
        if path == '/api/product/nfl/refresh':
            return JSONResponse({'detail': 'The background worker manages refreshes'}, status_code=403)
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        expected_origin = settings.public_url if settings.hosted_mode else str(request.base_url).rstrip('/')
        if (origin and origin.rstrip('/') != expected_origin) or (settings.hosted_mode and not origin):
            return JSONResponse({'detail': 'A same-origin request is required'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'A same-origin request is required'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if settings.hosted_mode:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    if path.startswith('/api/') or path == '/':
        response.headers['Cache-Control'] = 'no-store'
    return response

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
    if settings.hosted_mode:
        return {'sports': ['NFL'], 'product_scope': 'NFL-only', 'hosted_mode': True,
                'model_version': settings.model_version, 'confidence_version': settings.confidence_version,
                'demo_mode': False, 'llm_enabled': False}
    lr = engine.last_refresh()
    return dict(
        hosted_mode=False,
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


@app.get("/api/research/refresh-status")
def research_refresh_status() -> Dict:
    """Read stored worker state; this endpoint never ingests data."""
    return research_refresh.status()


@app.post("/api/research/refresh")
def research_refresh_now() -> Dict:
    """Manual check through the same cadence and quota guarded worker as launchd."""
    try:
        result = research_refresh.run(record_tick=False)
    except Exception as exc:  # noqa: BLE001 - never return provider details to product UI
        log.error("manual research refresh failed: %s", type(exc).__name__)
        raise HTTPException(503, "Research refresh failed; see Research for details") from None
    if result.get("status") == "already_running":
        return {"ok": True, "did_work": False, "busy": True, "steps": {},
                "updated_at": datetime.now(timezone.utc).isoformat()}
    if result.get("success") is False:
        raise HTTPException(503, "Research refresh failed; see Research for details")
    return {"ok": True, "did_work": bool(result.get("steps")), "busy": False,
            "steps": {name: bool(step.get("ok")) for name, step in result.get("steps", {}).items()},
            "updated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/research/paper")
def paper_dashboard() -> Dict:
    """Read the separate PAPER ledger; never issue product recommendations."""
    return paper_research.dashboard()


@app.get("/api/product/nfl")
def product_nfl_board(request: Request, week: Optional[int] = None) -> Dict:
    """Read stored weekly projections and labeled pregame observations only."""
    if week is not None and not 1 <= week <= 18:
        raise HTTPException(422, "Week must be between 1 and 18")
    return beta_access.board(product_views.weekly_board(week), request)


@app.post("/api/product/nfl/refresh")
def product_nfl_refresh() -> Dict:
    """Fetch current ESPN schedule and game prices without paid provider calls."""
    if settings.demo_mode:
        demo = engine.refresh(["NFL"])["sports"]["NFL"]
        engine.ensure_weekly_projections()
        return {"ok": True, "season": None, "week": None, "week_changed": False,
                "schedule_changed": False, "odds_rows": demo.get("odds_rows", 0),
                "odds_games": demo.get("events", 0), "games_without_quotes": 0,
                "projections_missing": 0, "odds_source": "demo",
                "checked_at": datetime.now(timezone.utc).isoformat()}
    previous = db.kv_get("nfl_current_schedule") or {}
    result = engine.refresh_schedule("NFL", force=True)
    if not result.get("ok"):
        raise HTTPException(503, "NFL schedule check failed; showing last stored week")
    try:
        odds = nfl_market_capture.capture_current()
    except Exception as exc:  # noqa: BLE001 - do not leak provider details
        log.warning("manual ESPN odds refresh failed: %s", type(exc).__name__)
        raise HTTPException(503, "NFL odds refresh failed; showing last stored prices") from None
    projections = engine.ensure_weekly_projections()
    try:
        research = research_refresh.run(record_tick=False, force_prop_quotes=True)
    except Exception as exc:  # noqa: BLE001 - game odds remain valid after a research failure
        log.warning("manual research check failed after odds refresh: %s", type(exc).__name__)
        research = {"success": False, "steps": {}}
    research_steps = research.get("steps", {})
    current = db.kv_get("nfl_current_schedule") or {}
    week = current.get("week")
    return {"ok": True, "season": current.get("season"), "week": week,
            "week_changed": (previous.get("season"), previous.get("week")) !=
                            (current.get("season"), week),
            "schedule_changed": bool(result.get("changed")),
            "odds_rows": odds["rows_stored"],
            "odds_games": odds.get("games_with_quotes", 0),
            "games_without_quotes": len(odds.get("games_without_quotes", [])),
            "odds_source": "espn",
            "props_refreshed": any((name.startswith("prop_near_game_") or
                                     (name == "top_five_capture" and step.get("result", {}).get("status") == "captured")) and step.get("ok")
                                   for name, step in research_steps.items()),
            "research_check_failed": research.get("success") is False,
            "projections_missing": len(projections["missing"]),
            "checked_at": datetime.now(timezone.utc).isoformat()}


@app.post('/api/product/nfl/check')
def product_visit_check() -> Dict:
    """A page visit may run due work; it never forces a provider request."""
    try:
        return visit_refresh.check()
    except ValueError:
        raise HTTPException(503, 'Automatic updates require ESPN research mode') from None


@app.get("/api/product/nfl/{game_id}")
def product_nfl_matchup(game_id: str, request: Request) -> Dict:
    """Read-only matchup and paired prop projections; no ingestion or picks."""
    try:
        return beta_access.matchup(product_views.matchup(game_id), request)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/product/performance")
def product_performance(request: Request) -> Dict:
    return beta_access.performance(product_views.performance(), request)


@app.get('/api/product/status')
def product_status() -> Dict:
    return product_insights.health()


@app.get('/api/product/methodology')
def product_methodology() -> Dict:
    return {'title': 'How the record works', 'readiness': 'RESEARCH_ONLY', 'designation': 'PASS',
            'notes': ['Five distinct player/stat selections lock before kickoff when five supported main lines are available.',
                      'Player prop model families and residual uncertainty are fixed. Training ends before 2026 Week 3; later completed weeks update inputs only.',
                      'Model probability is an estimate. Its difference from the quoted price is not a validated betting edge.',
                      'Live selections and archived rankings have separate records. Archive rankings were applied after games to forecasts saved before kickoff.',
                      'Latest verified source revisions determine outcomes. Missing stats stay pending; corrections remain visible.',
                      'Game forecast records use the saved prediction and its original line. Missing historical forecasts are never recreated.']}


@app.get("/api/product/research-index")
def product_research_index() -> Dict:
    return product_views.research_index()


@app.get("/api/product/research-document/{slug}")
def product_research_document(slug: str):
    relative = product_views.RESEARCH_DOCUMENTS.get(slug)
    if not relative:
        raise HTTPException(404, "unknown research document")
    path = ROOT / relative
    if not path.is_file():
        raise HTTPException(404, "research document unavailable")
    return FileResponse(path, media_type="text/plain; charset=utf-8")


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
    if "NFL" in (sports or list(settings.active_sports)):
        try:
            shadow.freeze_pending()
            result["shadow_sync"] = shadow.sync_results()
        except Exception as e:  # noqa: BLE001
            log.warning("post-refresh shadow sync failed: %s", type(e).__name__)
            result["shadow_error"] = type(e).__name__
        try:
            result["research_sync"] = nfl_forward_grading.sync_all(
                freeze_dir=ROOT / "reports/nfl_model_v2")
        except Exception as e:  # noqa: BLE001
            log.warning("post-refresh research grade sync failed: %s", type(e).__name__)
            result["research_error"] = type(e).__name__
    out = dict(result, odds_age_s=engine.odds_age_s())
    if ignored:
        out["ignored_sports"] = ignored
        out["ignored_note"] = ("Dormant sports are not fetched while the product scope is "
                               + "/".join(settings.active_sports) + ".")
    return out


# ------------------------------------------------------------------ dashboard
def _headline(recs: List[Dict]) -> Dict:
    picks = [r for r in recs if r["state"] == "OFFICIAL PICK"]
    plus = [r for r in picks if r["price"] > 0]
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
    upcoming = (engine.weekly_board() if sports == ["NFL"] else engine.slate(
        sport=sports[0], limit_events=20))
    if sports == ["NFL"]:
        shadow.freeze_pending()
    return dict(
        generated_at=time.time(), odds_age_s=engine.odds_age_s(),
        last_refresh=lr or None, demo_mode=serving_demo,
        headline=dict(_headline(recs), best_parlay=None),
        best_bets=picks[:25], watchlist=watch,
        line_moves=engine.line_moves(limit=10, sports=sports),
        upcoming=upcoming,
        shadow=shadow.performance() if sports == ["NFL"] else None,
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
    return shadow.performance()


@app.get("/api/model-report")
def model_report() -> Dict:
    """Read-only historical diagnostics; never presented as forward results."""
    path = ROOT / "reports" / "nfl_model_v1" / "backtest_results.json"
    if not path.exists():
        return dict(readiness="RESEARCH_ONLY", historical={},
                    note="Historical report has not been generated yet")
    return dict(readiness="RESEARCH_ONLY", historical=json.loads(path.read_text()),
                note="2023–2025 retrospective development diagnostics; forward results are separate")


# ------------------------------------------------------------------- props
@app.get("/api/research/nfl/prop-board/{game_id}")
def nfl_prop_board(game_id: str, market: Optional[str] = None) -> Dict:
    """Stored research observations; never triggers a provider or pick engine."""
    try:
        from . import nfl_prop_lab
        return nfl_prop_lab.research_board(game_id, market=market)
    except ValueError as exc:
        raise HTTPException(404 if "unknown canonical" in str(exc) else 400, str(exc)) from exc


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
