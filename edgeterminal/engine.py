"""The analysis engine: research orchestration, market evaluation, decisions.

Flow for a single event (section 15 of the spec):
  identify event -> odds -> line history -> team stats -> player usage ->
  injuries -> news -> weather -> projections -> market evaluation ->
  agent signals -> confidence -> strategist decision -> sources

Every number below is computed here or in analytics/*. The LLM is only invited
in afterwards, to narrate what these structures already say.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import db
from .agents import pipeline as ap
from .analytics import football, nba
from .analytics import golf as golfmod
from .analytics.ratings import shrink_stats
from .analytics.nfl_features import ranking_score, select_best_model_quote
from .config import SPORTS, settings
from .confidence import (ConfidenceInputs, agreement_from_signals, bucket_of,
                         data_quality_score, quality_tier, score)
from .confidence import QUALITY_CAPS  # noqa: F401  (documented ceiling table)
from .pricing import (american_to_prob, breakeven_price, devig, edge_pct,
                      ev_per_unit, line_move, pick_best_price, prob_to_american,
                      recommend_units)
from .providers import registry as R
from .schemas import AgentSignal, Quote, SourceRef

log = logging.getLogger("edgeterminal.engine")

MARKET_LABELS = {"moneyline": "Moneyline", "spread": "Spread", "total": "Total",
                 "player_prop": "Player Prop", "outright": "Outright Winner",
                 "top5": "Top 5", "top10": "Top 10", "top20": "Top 20",
                 "matchup": "Matchup", "make_cut": "Make Cut"}

PRIMARY_MARKETS = ("moneyline", "spread", "total")
_ctx_cache: Dict[str, Tuple[float, Dict]] = {}
CTX_TTL = 600.0


# ===================================================================== refresh
def refresh(sports: Optional[List[str]] = None) -> Dict:
    """Manual odds refresh. Nothing in this app polls -- this runs only when the
    user presses Refresh Odds or an analysis needs fresher numbers."""
    sports = sports or list(SPORTS)
    out: Dict[str, Any] = {"sports": {}, "at": time.time()}
    for sport in sports:
        entry: Dict[str, Any] = {}
        osrc, odemo, rows = "unavailable", False, []
        try:
            rows, osrc, odemo = R.fetch_odds(sport)
        except Exception as e:  # noqa: BLE001
            entry["odds_error"] = str(e)
        R.set_family(sport, odemo)
        try:
            events, esrc, edemo = R.list_events(sport, prefer=osrc)
            for e in events:
                e.setdefault("sport", sport)
                e["meta"] = dict(e.get("meta") or {}, is_demo=bool(edemo), source=esrc)
                db.upsert_event(e)
            entry.update(events=len(events), events_source=esrc)
        except Exception as e:  # noqa: BLE001
            entry.update(events=0, events_error=str(e))
        known = {e["id"] for e in db.q("SELECT id FROM events WHERE sport=?", (sport,))}
        usable = [r for r in rows if r.get("price") is not None and r["event_id"] in known]
        dropped = len(rows) - len(usable)
        n = db.store_odds(usable, source=osrc, is_demo=odemo) if usable else 0
        entry.update(odds_rows=n, odds_source=osrc, is_demo=odemo)
        if dropped:
            entry["odds_dropped_unmatched"] = dropped
        out["sports"][sport] = entry
    db.kv_set("last_refresh", out)
    # A full refresh also refreshes events, so it is a schedule retrieval too.
    for sport, entry in out["sports"].items():
        if entry.get("events_source"):
            db.kv_set(_schedule_key(sport),
                      dict(at=out["at"], source=entry["events_source"],
                           events=entry.get("events", 0), ok=True))
    _ctx_cache.clear()
    global _families_loaded
    _families_loaded = True
    return out


def last_refresh() -> Optional[Dict]:
    return db.kv_get("last_refresh")


# ------------------------------------------------------------------- schedule
SCHEDULE_FIELDS = ("start_time", "status", "home_team", "away_team", "name")


def _schedule_key(sport: str) -> str:
    return f"schedule_refreshed_at:{sport.upper()}"


def schedule_meta(sport: str = "NFL", now: Optional[float] = None) -> Dict:
    """When the stored schedule was last retrieved, and whether that is recent
    enough to resolve a relative date against.

    Falls back to the last full refresh for installs that predate schedule-only
    refreshes; `retrieved_at` is None when the schedule has never been fetched,
    which is uncertainty, not freshness.
    """
    meta = db.kv_get(_schedule_key(sport)) or {}
    at = meta.get("at")
    if at is None:
        at = (db.kv_get("last_refresh") or {}).get("at")
        meta = dict(meta, source=meta.get("source") or "last_refresh")
    at = float(at) if at is not None else None
    # The turn's clock, not wall time: a frozen-clock caller must get a freshness
    # answer consistent with the timestamps it is reasoning about.
    stamp = float(now if now is not None else time.time())
    age = (stamp - at) if at is not None else None
    return dict(retrieved_at=at, age_s=(round(age, 1) if age is not None else None),
                stale=(age is None or age > settings.stale_schedule_s),
                limit_s=settings.stale_schedule_s,
                source=meta.get("source"), ok=meta.get("ok", True),
                error=meta.get("error"))


def refresh_schedule(sport: str = "NFL") -> Dict:
    """Refresh only the schedule, on demand.

    Deliberately narrower than `refresh()`: it calls the events endpoint, which
    costs no Odds API quota, and never touches odds. Events are upserted only --
    a fixture missing from one provider response is an omission, not a
    cancellation, so nothing is ever deleted and historical rows are preserved.

    Returns what changed, so callers can invalidate exactly the cached context
    that depended on a kickoff, status or identity that just moved.
    """
    sport = sport.upper()
    before = {r["id"]: dict(r) for r in db.q(
        "SELECT id, start_time, status, home_team, away_team, name FROM events WHERE sport=?",
        (sport,))}
    prefer = ((db.kv_get("last_refresh") or {}).get("sports", {})
              .get(sport, {}) or {}).get("odds_source")
    try:
        events, source, is_demo = R.list_events(sport, prefer=prefer)
    except Exception as e:  # noqa: BLE001 - a failed refresh must not break a turn
        log.warning("schedule refresh failed for %s: %s", sport, e)
        db.kv_set(_schedule_key(sport), dict(
            at=(db.kv_get(_schedule_key(sport)) or {}).get("at"),
            source=None, ok=False, error=f"{type(e).__name__}: {e}"))
        return dict(ok=False, sport=sport, error=f"{type(e).__name__}: {e}",
                    changed=[], events=0)
    if not events:
        # An empty response is not "every game was cancelled". Say so and keep
        # the stored schedule exactly as it was.
        db.kv_set(_schedule_key(sport), dict(
            at=(db.kv_get(_schedule_key(sport)) or {}).get("at"),
            source=source, ok=False, error="provider returned no events"))
        return dict(ok=False, sport=sport, error="provider returned no events",
                    changed=[], events=0)

    changed: List[str] = []
    for e in events:
        e.setdefault("sport", sport)
        e["meta"] = dict(e.get("meta") or {}, is_demo=bool(is_demo), source=source)
        old = before.get(e["id"])
        if old is not None:
            for field in SCHEDULE_FIELDS:
                if field in e and str(e.get(field) or "") != str(old.get(field) or ""):
                    changed.append(e["id"])
                    break
        db.upsert_event(e)

    # Anything whose kickoff, status or identity moved must not keep serving the
    # analysis built from the old value.
    for eid in changed:
        _ctx_cache.pop(eid, None)
    db.kv_set(_schedule_key(sport), dict(at=time.time(), source=source,
                                         events=len(events), ok=True, error=None))
    log.info("schedule refresh sport=%s source=%s events=%d changed=%d",
             sport, source, len(events), len(changed))
    return dict(ok=True, sport=sport, source=source, events=len(events),
                changed=changed, is_demo=bool(is_demo), error=None)


_families_loaded = False


def restore_families(force: bool = False) -> None:
    """Re-derive which sports are on demo odds from stored snapshots.

    Without this a fresh process would price demo lines against live team stats
    and manufacture edges out of the mismatch.
    """
    global _families_loaded
    if _families_loaded and not force:
        return
    for row in db.q("""SELECT o.sport, o.is_demo, o.captured_at AS t
                     FROM odds_snapshots o
                     JOIN (SELECT sport, MAX(captured_at) AS t FROM odds_snapshots GROUP BY sport) latest
                       ON latest.sport=o.sport AND latest.t=o.captured_at
                     GROUP BY o.sport"""):
        R.set_family(row["sport"], bool(row["is_demo"]))
    _families_loaded = True


def odds_age_s(event_id: Optional[str] = None) -> Optional[float]:
    sql = "SELECT MAX(captured_at) AS t FROM odds_snapshots"
    params: List[Any] = []
    if event_id:
        sql += " WHERE event_id=?"
        params.append(event_id)
    row = db.q1(sql, params)
    return (time.time() - row["t"]) if row and row["t"] else None


def quote_is_stale(captured_at: Optional[float]) -> bool:
    return not captured_at or (time.time() - float(captured_at)) > settings.stale_odds_s


# ==================================================================== context
def _venue_coords(ev: Dict) -> Tuple[Optional[float], Optional[float], bool]:
    meta = ev.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    return meta.get("lat"), meta.get("lon"), bool(meta.get("indoor"))


def prior_feature_note(stats: Dict[str, Dict], event: Dict) -> Optional[str]:
    """Name the teams in this event whose nflverse inputs came from last season."""
    teams = sorted(t for t in {event.get("home_team"), event.get("away_team")} - {None, ""}
                   if (stats.get(t) or {}).get("prior_season_features"))
    if not teams:
        return None
    season = datetime.now(timezone.utc).year - 1
    return (f"nflverse efficiency inputs for {', '.join(teams)} come from the {season} season — "
            "those teams have not played enough of this season to be measured yet.")


def build_context(event: Dict, want_weather: bool = True) -> Dict:
    """Gather every input an analysis needs, recording what succeeded."""
    restore_families()
    eid = event["id"]
    hit = _ctx_cache.get(eid)
    if hit and time.time() - hit[0] < CTX_TTL:
        return hit[1]

    sport = event["sport"]
    ctx: Dict[str, Any] = dict(event=event, sport=sport, sources=[], quality_parts={},
                               warnings=[], retrieved_at=time.time())

    # --- odds provenance/freshness (the model never treats an old price as current)
    odds_rows = db.latest_odds(event_id=eid)
    newest_odds = max((r.get("captured_at") or 0 for r in odds_rows), default=0)
    odds_age = time.time() - newest_odds if newest_odds else None
    ctx["odds_age_s"] = odds_age
    ctx["quality_parts"]["odds_fresh"] = odds_age is not None and odds_age <= settings.stale_odds_s
    for source, source_url, provider_at in sorted({
            (r.get("source") or "odds", r.get("source_url"),
             r.get("provider_updated_at") or r.get("captured_at")) for r in odds_rows},
            key=lambda item: (item[0], str(item[1]), item[2] or 0)):
        ctx["sources"].append(dict(kind="odds", title=f"NFL market prices ({source})",
                                   url=source_url, publisher=source,
                                   retrieved_at=provider_at, trust="demo" if any(
                                       r.get("is_demo") for r in odds_rows if r.get("source") == source)
                                   else "reported"))
    if odds_age is None:
        ctx["warnings"].append("Real odds unavailable — no official bet can be produced.")
    elif odds_age > settings.stale_odds_s:
        ctx["warnings"].append(f"Odds are stale ({odds_age / 60:.0f} minutes old); refresh before acting.")

    # --- team stats
    try:
        stats, src, demo = R.team_stats(sport)
        stats, mean_w = shrink_stats(stats, sport)
        ctx.update(stats=stats, stats_source=src, stats_demo=demo, stats_weight=mean_w)
        ctx["quality_parts"]["team_stats"] = 1.0
        stats_at = max((r.get("retrieved_at") or 0 for r in stats.values()), default=time.time())
        ctx["sources"].append(dict(kind="stats", title=f"{sport} team performance summary ({src})",
                                   url="https://site.api.espn.com/apis/v2/sports/football/nfl/standings" if sport == "NFL" else None,
                                   publisher=src, retrieved_at=stats_at,
                                   trust="official" if not demo else "demo"))
        if "nflverse" in src:
            ctx["sources"].append(dict(
                kind="stats", title="nflverse play-by-play advanced team features",
                url="https://github.com/nflverse/nflverse-data/releases/tag/pbp",
                publisher="nflverse", retrieved_at=stats_at, trust="reported"))
            note = prior_feature_note(stats, event)
            if note:
                ctx["warnings"].append(note)
        if mean_w < 0.35:
            ctx["warnings"].append(
                f"Season sample is thin (effective weight {mean_w:.2f}); team rates are heavily "
                f"regressed to the league prior and confidence is reduced accordingly.")
    except Exception as e:  # noqa: BLE001
        ctx.update(stats={}, stats_source="unavailable", stats_demo=False, stats_weight=0.0)
        ctx["quality_parts"]["team_stats"] = False
        ctx["warnings"].append(f"Team statistics unavailable ({e}). No projection can be built.")

    # --- injuries
    inj, isrc, idemo = R.injuries(sport)
    teams = [event.get("home_team"), event.get("away_team")]
    current_stats = ctx.get("stats") or {}
    event_stats = [current_stats[t] for t in teams if t in current_stats]
    prior_share = (sum(bool((r.get("extra") or {}).get("prior_season_fallback"))
                       for r in event_stats) / len(event_stats)) if event_stats else 1.0
    ctx["prior_season_share"] = prior_share
    ctx["quality_parts"]["current_season"] = 1.0 - prior_share
    ctx["injuries"] = [i for i in inj if i.get("team") in teams] if any(teams) else inj[:20]
    ctx["injuries_source"] = isrc
    inj_at = max((i.get("retrieved_at") or 0 for i in inj), default=0)
    ctx["quality_parts"]["injuries"] = float(bool(inj_at) and
        time.time() - inj_at <= settings.stale_injury_s)
    if inj:
        ctx["sources"].append(dict(kind="injuries", title=f"{sport} injury report ({isrc})",
                                   url=inj[0].get("source_url"), publisher=isrc,
                                   retrieved_at=max(i.get("retrieved_at") or 0 for i in inj),
                                   trust="official" if not idemo else "demo"))
    else:
        ctx["warnings"].append("Injury data unavailable — availability certainty reduced.")

    # --- news
    query = event.get("name") or sport
    nws, nsrc, ndemo = R.news(sport, query)
    ctx["news"] = nws[:8]
    ctx["news_source"] = nsrc
    news_at = max((n.get("retrieved_at") or 0 for n in nws), default=0)
    ctx["quality_parts"]["news"] = float(bool(news_at) and
        time.time() - news_at <= settings.stale_news_s)
    for n in ctx["news"][:5]:
        ctx["sources"].append(dict(kind="news", title=n.get("title"), url=n.get("url"),
                                   publisher=n.get("publisher"),
                                   retrieved_at=n.get("retrieved_at") or time.time(),
                                   trust=n.get("trust", "reported")))

    # --- weather
    if want_weather and sport in ("NFL", "CFB", "PGA"):
        lat, lon, indoor = _venue_coords(event)
        meta = event.get("meta") or {}
        wx, wsrc, wdemo = R.weather(lat, lon, event.get("start_time"), indoor=indoor,
                                    city=meta.get("city"), state=meta.get("state"), sport=sport)
        ctx["weather"] = wx
        ctx["weather_source"] = wsrc
        wx_at = (wx or {}).get("retrieved_at") or 0
        ctx["quality_parts"]["weather"] = float(bool(wx) and bool(wx_at) and
            time.time() - wx_at <= settings.stale_weather_s)
        if wx:
            ctx["sources"].append(dict(kind="weather", title=f"Forecast for {event.get('venue') or 'venue'}",
                                       url="https://open-meteo.com/", publisher=wsrc,
                                       retrieved_at=wx.get("retrieved_at") or time.time(),
                                       trust="official" if not wdemo else "demo"))
        else:
            ctx["warnings"].append(
                "Weather data unavailable — total and passing-volume markets carry extra "
                "uncertainty and confidence is reduced.")
    else:
        ctx["weather"] = {"indoor": True} if sport == "NBA" else None
        ctx["quality_parts"]["weather"] = True

    # --- players
    players: Dict[str, Dict] = {}
    for t in teams:
        if not t:
            continue
        pool, _src, _d = R.player_pool(sport, t)
        for p in pool:
            players[p["name"]] = p
    ctx["players"] = players
    ctx["quality_parts"]["player_usage"] = bool(players)
    if not players and sport in ("NFL", "NBA"):
        ctx["warnings"].append("No player usage baselines for this event — player props cannot be "
                               "priced and are excluded rather than guessed.")

    # --- projection
    ctx["projection"] = None
    try:
        if sport in ("NFL", "CFB") and ctx["stats"]:
            ctx["projection"] = football.project_game(
                sport, event["home_team"], event["away_team"], ctx["stats"],
                weather=ctx.get("weather"), injuries=ctx["injuries"])
        elif sport == "NBA" and ctx["stats"]:
            ctx["projection"] = nba.project_game(event["home_team"], event["away_team"],
                                                 ctx["stats"], injuries=ctx["injuries"])
    except Exception as e:  # noqa: BLE001
        ctx["warnings"].append(f"Projection failed: {e}")

    weights = {"odds_fresh": 2.5, "team_stats": 2.0, "current_season": 1.4, "injuries": 1.2,
               "news": 0.6, "weather": 0.8, "player_usage": 0.8}
    dq = data_quality_score(ctx["quality_parts"], weights)
    if sport == "CFB":
        dq *= 0.82        # college data is structurally worse; cap it honestly
    dq *= 0.55 + 0.45 * min(1.0, ctx.get("stats_weight", 0.0) / 0.6 + 0.25)
    ctx["data_quality"] = round(min(1.0, dq), 3)
    ctx["data_quality_tier"] = quality_tier(ctx["data_quality"])

    _ctx_cache[eid] = (time.time(), ctx)
    return ctx


def golf_context() -> Dict:
    restore_families()
    hit = _ctx_cache.get("__golf__")
    if hit and time.time() - hit[0] < CTX_TTL:
        return hit[1]
    tour, tsrc, tdemo = R.golf_tournament()
    field, fsrc, fdemo = R.golf_field()
    ctx: Dict[str, Any] = dict(tournament=tour, field=field, sources=[], warnings=[],
                               quality_parts={"field_stats": bool(field), "tournament": bool(tour)})
    if not field:
        ctx["warnings"].append("Golf field statistics unavailable — no golf pricing produced.")
        ctx["data_quality"] = 0.2
        ctx["probs"] = {}
    else:
        meta = (tour or {}).get("meta") or {}
        rounds_left = {"pre-tournament": 4, "after-r1": 3, "after-r2": 2,
                       "after-r3": 1, "final": 1}.get(meta.get("round_state", "pre-tournament"), 4)
        positions = {p["name"]: p.get("strokes_behind", 0.0)
                     for p in (tour or {}).get("leaderboard", []) or []}
        ctx["rounds_left"] = rounds_left
        ctx["probs"] = golfmod.field_probabilities(field, rounds_left=rounds_left,
                                                   positions=positions or None)
        ctx["data_quality"] = 0.78
        ctx["sources"].append(dict(kind="stats", title=f"Field strokes-gained profiles ({fsrc})",
                                   publisher=fsrc, retrieved_at=time.time(),
                                   trust="demo" if fdemo else "official"))
    nws, nsrc, _ = R.news("PGA")
    ctx["news"] = nws[:6]
    for n in ctx["news"][:4]:
        ctx["sources"].append(dict(kind="news", title=n.get("title"), url=n.get("url"),
                                   publisher=n.get("publisher"), retrieved_at=time.time(),
                                   trust=n.get("trust", "reported")))
    ctx["data_quality_tier"] = quality_tier(ctx.get("data_quality", 0.5))
    _ctx_cache["__golf__"] = (time.time(), ctx)
    return ctx


# ============================================================ market evaluation
def _parse_prop(selection: str) -> Optional[Tuple[str, str, str]]:
    """'Joe Burrow Over passing_yards' -> (player, side, prop)."""
    for side in ("Over", "Under"):
        tok = f" {side} "
        if tok in selection:
            who, prop = selection.split(tok, 1)
            return who.strip(), side, prop.strip()
    return None


def model_probability(ctx: Dict, market: str, selection: str,
                      line: Optional[float]) -> Optional[Dict]:
    """Returns {prob, projection, kind, player} or None when we cannot price it.

    Returning None is a feature: the terminal shows nothing rather than a guess.
    """
    sport = ctx["sport"]
    proj = ctx.get("projection")
    if market in ("moneyline", "spread", "total") and not proj:
        return None
    mod = football if sport in ("NFL", "CFB") else nba

    if market == "moneyline":
        return dict(prob=mod.moneyline_prob(proj, selection), projection=proj, kind="game")
    if market == "spread":
        if line is None:
            return None
        return dict(prob=mod.spread_prob(proj, selection, line), projection=proj, kind="game")
    if market == "total":
        if line is None:
            return None
        return dict(prob=mod.total_prob(proj, selection, line), projection=proj, kind="game")
    if market == "player_prop":
        parsed = _parse_prop(selection)
        if not parsed or line is None:
            return None
        who, side, prop = parsed
        player = (ctx.get("players") or {}).get(who)
        if not player:
            return None
        try:
            pp = mod.project_prop(dict(player, name=who), prop, proj,
                                  injuries=ctx.get("injuries"))
        except Exception:  # noqa: BLE001
            return None
        return dict(prob=mod.prop_prob(pp, side, line, prop), projection=pp, kind="prop",
                    player=who, prop=prop, side=side)
    return None


def golf_model_probability(gctx: Dict, market: str, selection: str) -> Optional[Dict]:
    probs = gctx.get("probs") or {}
    field = gctx.get("field") or {}
    if market in ("outright", "top5", "top10", "top20", "make_cut"):
        key = {"outright": "win", "top5": "top5", "top10": "top10",
               "top20": "top20", "make_cut": "make_cut"}[market]
        pr = probs.get(selection)
        if not pr:
            return None
        return dict(prob=pr[key], projection=dict(field.get(selection, {}), **pr),
                    kind="golf", player=selection)
    if market == "matchup" and ": " in selection:
        pair, who = selection.split(": ", 1)
        if " vs " not in pair:
            return None
        a, b = pair.split(" vs ", 1)
        if who not in (a, b) or a not in field or b not in field:
            return None
        p = golfmod.matchup_prob(field, who, b if who == a else a,
                                 rounds_left=gctx.get("rounds_left", 4))
        return dict(prob=p, projection=field.get(who, {}), kind="golf", player=who)
    return None


def _movement(event_id: str, market: str, selection: str, current_line: Optional[float],
              current_price: float, is_team_side: bool) -> Dict:
    hist = db.odds_history(event_id, market, selection)
    if not hist:
        return {"delta": None, "direction": "unknown", "magnitude": 0.0,
                "open_line": None, "open_price": None, "snapshots": 0}
    first = hist[0]
    prev = hist[-2] if len(hist) > 1 else first
    line_field = current_line if current_line is not None else None
    if line_field is not None and first.get("line") is not None:
        mv = line_move(first["line"], line_field)
        toward = (line_field > first["line"]) if is_team_side else None
    else:
        mv = line_move(american_to_prob(first["price"]) * 100,
                       american_to_prob(current_price) * 100)
        toward = mv["delta"] > 0 if mv["delta"] is not None else None
    return dict(mv, open_line=first.get("line"), open_price=first.get("price"),
                prev_line=prev.get("line"), prev_price=prev.get("price"),
                open_at=first.get("captured_at"), snapshots=len(hist),
                toward_selection=toward)


def _consensus_fair(rows: List[Dict], market: str, event_id: str,
                    selection: str, selected_line: Optional[float] = None) -> Optional[float]:
    """No-vig consensus probability for this selection, from paired outcomes."""
    if market == "moneyline":
        sides = sorted({r["selection"] for r in rows
                        if r["event_id"] == event_id and r["market"] == "moneyline"})
    elif market == "total":
        sides = ["Over", "Under"]
    elif market == "spread":
        sides = sorted({r["selection"] for r in rows
                        if r["event_id"] == event_id and r["market"] == "spread"})
    elif market == "player_prop":
        parsed = _parse_prop(selection)
        if not parsed:
            return None
        who, side, prop = parsed
        sides = [f"{who} Over {prop}", f"{who} Under {prop}"]
    else:
        return None
    if len(sides) != 2 or selection not in sides:
        return None
    means = []
    for s in sides:
        expected_line = (-selected_line if market == "spread" and s != selection and
                         selected_line is not None else selected_line)
        ps = [american_to_prob(r["price"]) for r in rows
              if r["event_id"] == event_id and r["market"] == market and r["selection"] == s
              and (market == "moneyline" or r.get("line") == expected_line)]
        if not ps:
            return None
        means.append(sum(ps) / len(ps))
    fair = devig(means)
    return fair[sides.index(selection)]


def evaluate_market(event: Dict, ctx: Dict, market: str, selection: str,
                    quotes: List[Dict], all_rows: List[Dict],
                    calibration_delta: float = 0.0) -> Optional[Dict]:
    """Full evaluation of one selection. Returns a recommendation dict or None."""
    sport = event["sport"]
    is_golf = sport == "PGA"

    # Price every quote against the model at ITS OWN line, then take the best.
    candidates: List[Dict] = []
    distinct_books = len({q["book"] for q in quotes})
    for qr in quotes:
        line = qr.get("line")
        mp = (golf_model_probability(ctx, market, selection) if is_golf
              else model_probability(ctx, market, selection, line))
        if not mp:
            continue
        market_prob = None if is_golf else _consensus_fair(
            all_rows, market, event["id"], selection, line)
        candidates.append(dict(quote=qr, model_result=mp,
                               raw_model_prob=float(mp["prob"]), market_prob=market_prob))
    if not candidates:
        return None
    first_projection = candidates[0]["model_result"].get("projection") or {}
    reliability = 1.0 - float(first_projection.get("uncertainty", 0.5))
    picked = select_best_model_quote(candidates, reliability if sport == "NFL" else 1.0,
                                     distinct_books if sport == "NFL" else 0)
    best_q, mp = picked["quote"], picked["model_result"]

    price = float(best_q["price"])
    line = best_q.get("line")
    raw_model_p = float(mp["prob"])
    market_fair = picked.get("market_prob")
    projection = mp.get("projection")
    blend = picked.get("blend") if sport == "NFL" else None
    model_p = blend["final_prob"] if blend else raw_model_p
    implied = american_to_prob(price)
    edge = edge_pct(model_p, price)
    best_ev = ev_per_unit(model_p, price)
    fair = prob_to_american(model_p)

    is_team_side = selection in (event.get("home_team"), event.get("away_team"))
    mv = _movement(event["id"], market, selection, line, price, is_team_side)

    player = mp.get("player")
    signals: List[AgentSignal] = [
        ap.quantitative(model_p, price, market_fair, projection, market, line),
        ap.market_analyst([dict(q) for q in quotes], price, model_p, mv, market_fair),
        ap.matchup_analyst(projection if mp.get("kind") != "golf" else None, selection, market,
                           ctx.get("stats"), mp.get("side")),
        ap.news_analyst(ctx.get("injuries") or [], ctx.get("news") or [], selection,
                        teams=[event.get("home_team"), event.get("away_team")], player=player),
    ]
    agreement = agreement_from_signals([s.direction for s in signals])

    n = 0
    if projection:
        n = int(projection.get("n") or 0) or int((ctx.get("stats") or {}).get(
            event.get("home_team"), {}).get("games") or 0)
    cv = float((projection or {}).get("cv") or 0.32)
    availability = float((projection or {}).get("availability_certainty")
                         or (ctx.get("projection") or {}).get("availability_certainty") or 0.85)
    unresolved_major = any(
        (i.get("status") or "").lower() in ("questionable", "doubtful") and
        (i.get("position") or "").upper() in ("QB", "LT", "C", "WR", "CB", "DE", "EDGE")
        for i in (ctx.get("injuries") or []))
    if unresolved_major:
        availability = min(availability, 0.65)

    dq = float(ctx.get("data_quality", 0.5))
    penalties: List[Dict] = []
    if ctx.get("weather") is None and sport in ("NFL", "CFB") and market in ("total", "player_prop"):
        penalties.append(dict(reason="weather data unavailable", points=3))
    if not ctx.get("injuries"):
        penalties.append(dict(reason="no injury feed", points=2))

    ci = ConfidenceInputs(
        edge_pct=edge, model_agreement=agreement, data_quality=dq, sample_size_n=n,
        market_type=market, line_move_magnitude=float(mv.get("magnitude") or 0.0),
        books_quoting=len(quotes), availability_certainty=availability,
        projection_cv=cv, context_certainty=0.85 if ctx.get("news") else 0.6,
        penalties=penalties, prior_season_share=float(ctx.get("prior_season_share") or 0),
        unresolved_major_injury=unresolved_major)
    risk = ap.risk_analyst(model_p, price, ci.__dict__, projection, market, mv)
    signals.append(risk)

    conf = score(ci, calibration_delta=calibration_delta, version=settings.confidence_version)
    stale = quote_is_stale(best_q.get("captured_at"))
    be = breakeven_price(model_p, settings.min_edge_pct)
    decision = ap.strategist(signals, conf.score, edge, conf.tier, stale, be, price, market,
                             books_quoting=len(quotes), sample_size_n=n,
                             unresolved_major_injury=unresolved_major)
    units = recommend_units(conf.score, edge, market, dq, agreement) \
        if decision["state"] in ("OFFICIAL PICK", "LEAN") else 0.0

    reasoning = [decision["why"]] + [f"{s.agent}: {s.note}" for s in signals
                                     if s.agent != "Risk Analyst"]
    risks = [r.strip() for r in (risk.note or "").split(";") if r.strip()]
    if mv.get("snapshots", 0) > 1 and mv.get("magnitude"):
        d = mv.get("open_line")
        reasoning.append(
            f"Line history: opened {d if d is not None else mv.get('open_price')}, "
            f"now {line if line is not None else price} across {mv['snapshots']} stored snapshots.")

    rid = hashlib.sha1(f"{event['id']}|{market}|{selection}|{line}|{price}|"
                       f"{settings.model_version}".encode()).hexdigest()[:16]
    rec = dict(
        id=rid, event_id=event["id"], event_name=event.get("name"), sport=sport,
        start_time=event.get("start_time"), market=market,
        market_label=MARKET_LABELS.get(market, market.title()),
        selection=selection, side=mp.get("side"), line=line,
        book=best_q["book"], price=price,
        all_quotes=sorted([dict(book=q["book"], price=q["price"], line=q.get("line"))
                           for q in quotes], key=lambda q: american_to_prob(q["price"])),
        model_prob=round(model_p, 4), raw_model_prob=round(raw_model_p, 4),
        market_blend=blend, implied_prob=round(implied, 4), fair_price=fair,
        market_fair_prob=round(market_fair, 4) if market_fair is not None else None,
        edge_pct=round(edge, 2), ev_per_unit=round(best_ev, 4),
        confidence=conf.score, confidence_bucket=bucket_of(conf.score),
        confidence_components=conf.contributions, confidence_caps=conf.cap_applied,
        calibration_delta=conf.calibration_delta,
        units=units, state=decision["state"], data_quality=conf.tier,
        breakeven_price=be, trigger_note=decision.get("trigger"),
        line_movement=mv, signals=[s.model_dump() for s in signals],
        consensus=ap.consensus(signals),
        reasoning=reasoning, risks=risks,
        sources=ctx.get("sources", []),
        model_version=settings.model_version, confidence_version=settings.confidence_version,
        created_at=time.time(), stale=stale,
        selected_quote_at=best_q.get("captured_at"),
        selected_odds_age_s=(time.time() - best_q["captured_at"]
                             if best_q.get("captured_at") else None),
        is_demo=bool(ctx.get("stats_demo")) or any(
            s.get("trust") == "demo" for s in ctx.get("sources", [])),
        warnings=ctx.get("warnings", []),
    )
    rec["rank_score"] = ranking_score(
        confidence=conf.score, edge_pct=edge, ev_pct=best_ev * 100,
        data_quality=dq, market_depth=distinct_books,
        uncertainty=float((projection or {}).get("uncertainty", cv)))
    return rec


# ================================================================ event analysis
def _quotes_by_selection(rows: List[Dict]) -> Dict[Tuple[str, str], List[Dict]]:
    out: Dict[Tuple[str, str], List[Dict]] = {}
    for r in rows:
        out.setdefault((r["market"], r["selection"]), []).append(
            dict(book=r["book"], price=r["price"], line=r.get("line"),
                 captured_at=r.get("captured_at")))
    return out


def analyze_event(event_id: str, persist: bool = True,
                  markets: Optional[List[str]] = None) -> Dict:
    ev = db.q1("SELECT * FROM events WHERE id=?", (event_id,))
    if not ev:
        raise ValueError(f"unknown event {event_id}")
    ev = dict(ev)
    if isinstance(ev.get("meta"), str):
        try:
            ev["meta"] = json.loads(ev["meta"])
        except Exception:  # noqa: BLE001
            ev["meta"] = {}

    rows = db.latest_odds(event_id=event_id)
    if not rows:
        return dict(event=ev, error="No stored odds for this event. Press Refresh Odds.",
                    recommendations=[], projection=None, sources=[], warnings=[],
                    injuries=[], news=[], stats={}, weather=None,
                    data_quality=0.0, data_quality_tier="poor",
                    odds_age_s=None, model_version=settings.model_version,
                    analyzed_at=time.time())

    sport = ev["sport"]
    ctx = golf_context() if sport == "PGA" else build_context(ev)
    if sport == "PGA":
        ctx = dict(ctx, sport="PGA", event=ev, injuries=[], players={},
                   stats={}, projection=None)

    cal = calibration_deltas()
    recs: List[Dict] = []
    for (market, selection), quotes in _quotes_by_selection(rows).items():
        if markets and market not in markets:
            continue
        try:
            delta = cal.get(f"{sport}:{market}", cal.get(sport, 0.0))
            rec = evaluate_market(ev, ctx, market, selection, quotes, rows, delta)
        except Exception as e:  # noqa: BLE001
            log.exception("evaluate_market failed for %s %s", market, selection)
            continue
        if rec:
            recs.append(rec)

    recs.sort(key=lambda r: (-_state_rank(r["state"]), -r.get("rank_score", 0),
                             -r["confidence"], -r["edge_pct"]))
    if persist:
        for r in recs:
            if r["state"] in ("OFFICIAL PICK", "LEAN", "WATCHLIST", "WAIT", "WAIT FOR PRICE"):
                _persist(r)
        db.add_sources(event_id, ctx.get("sources", []))

    out = dict(event=ev, projection=ctx.get("projection"), weather=ctx.get("weather"),
               injuries=ctx.get("injuries", []), news=ctx.get("news", []),
               sources=ctx.get("sources", []), warnings=ctx.get("warnings", []),
               data_quality=ctx.get("data_quality"), data_quality_tier=ctx.get("data_quality_tier"),
               stats={k: v for k, v in (ctx.get("stats") or {}).items()
                      if k in (ev.get("home_team"), ev.get("away_team"))},
               recommendations=recs, odds_age_s=odds_age_s(event_id),
               model_version=settings.model_version, analyzed_at=time.time())
    if sport == "PGA":
        out["golf"] = dict(tournament=ctx.get("tournament"), rounds_left=ctx.get("rounds_left"),
                           probs=ctx.get("probs", {}), field=ctx.get("field", {}))
    if persist:
        db.ex("INSERT OR REPLACE INTO analyses(id,event_id,sport,payload,model_version,created_at)"
              " VALUES(?,?,?,?,?,?)",
              (str(uuid.uuid4()), event_id, sport, json.dumps(out, default=str),
               settings.model_version, time.time()))
    return out


STATE_RANK = {"OFFICIAL PICK": 6, "LEAN": 5, "WATCHLIST": 4, "WAIT": 3, "WAIT FOR PRICE": 3,
              "NO EDGE": 1, "PASS": 1, "STALE": 2, "NO LONGER PLAYABLE": 0}


def _state_rank(state: str) -> int:
    return STATE_RANK.get(state, 0)


def _persist(r: Dict) -> None:
    db.save_prediction(dict(
        id=r["id"], event_id=r["event_id"], sport=r["sport"], market=r["market"],
        selection=r["selection"], side=r.get("side"), line=r.get("line"), book=r["book"],
        price=r["price"], model_prob=r["model_prob"], implied_prob=r["implied_prob"],
        fair_price=r["fair_price"], edge_pct=r["edge_pct"], ev_per_unit=r["ev_per_unit"],
        confidence=r["confidence"], confidence_bucket=r["confidence_bucket"], units=r["units"],
        state=r["state"], data_quality=r["data_quality"], breakeven_price=r.get("breakeven_price"),
        reasoning=r["reasoning"], risks=r["risks"], components=r["confidence_components"],
        signals=r["signals"], sources=r["sources"], model_version=r["model_version"],
        confidence_version=r["confidence_version"],
        data_snapshot=dict(selected_quote_at=r.get("selected_quote_at"),
                           selected_odds_age_s=r.get("selected_odds_age_s"),
                           data_quality=r.get("data_quality"),
                           line_movement=r.get("line_movement"), warnings=r.get("warnings"),
                           source_coverage=[dict(kind=s.get("kind"), publisher=s.get("publisher"),
                                                 retrieved_at=s.get("retrieved_at"))
                                            for s in r.get("sources", [])]),
        created_at=r["created_at"],
        trigger_note=r.get("trigger_note")))


# =================================================================== slate wide
def _event_demo(e: Dict) -> bool:
    # Legacy fixture rows predate the meta flag; their stable IDs remain authoritative.
    if str(e.get("id") or "").startswith("demo-"):
        return True
    meta = e.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    return bool(meta.get("is_demo"))


def slate(sport: Optional[str] = None, limit_events: int = 40) -> List[Dict]:
    sql = "SELECT * FROM events"
    params: List[Any] = []
    if sport:
        sql += " WHERE sport=?"
        params.append(sport)
    sql += " ORDER BY start_time ASC"
    evs = db.q(sql, params)
    out = []
    for e in evs:
        e = dict(e)
        if str(e.get("status") or "").lower() in {
                "status_final", "final", "completed", "post", "closed", "cancelled"}:
            continue
        if _event_demo(e) != R.family_is_demo(e.get("sport")):
            continue
        rows = db.latest_odds(event_id=e["id"])
        summary: Dict[str, Any] = {}
        for r in rows:
            if r["market"] in PRIMARY_MARKETS:
                k = (r["market"], r["selection"])
                cur = summary.get(k)
                if not cur or american_to_prob(r["price"]) < american_to_prob(cur["price"]):
                    summary[k] = dict(book=r["book"], price=r["price"], line=r.get("line"))
        e["markets"] = [dict(market=k[0], selection=k[1], **v) for k, v in summary.items()]
        e["odds_count"] = len(rows)
        out.append(e)
    return out[:limit_events]


def analyze_slate(sports: Optional[List[str]] = None, max_events: int = 24,
                  markets: Optional[List[str]] = None) -> List[Dict]:
    """Analyse every event with stored odds and return a flat recommendation list."""
    sports = sports or list(SPORTS)
    recs: List[Dict] = []
    seen = 0
    for sport in sports:
        for ev in db.q("SELECT id, meta, status FROM events WHERE sport=? ORDER BY start_time ASC", (sport,)):
            if str(ev.get("status") or "").lower() in {
                    "status_final", "final", "completed", "post", "closed", "cancelled"}:
                continue
            if _event_demo(dict(ev)) != R.family_is_demo(sport):
                continue
            if seen >= max_events:
                break
            if not db.latest_odds(event_id=ev["id"]):
                continue
            seen += 1
            try:
                res = analyze_event(ev["id"], persist=True, markets=markets)
            except Exception as e:  # noqa: BLE001
                log.warning("analysis failed for %s: %s", ev["id"], e)
                continue
            recs += res.get("recommendations", [])
    recs.sort(key=lambda r: (-_state_rank(r["state"]), -r.get("rank_score", 0),
                             -r["confidence"], -r["edge_pct"]))
    db.kv_set("last_slate_analysis", dict(at=time.time(), count=len(recs)))
    return recs


def cached_slate_recs(max_age_s: float = 1800, **kw) -> List[Dict]:
    """Reuse the last stored analyses when they are fresh enough, else re-run."""
    rows = db.q("SELECT payload FROM analyses WHERE created_at > ? ORDER BY created_at DESC",
                (time.time() - max_age_s,))
    if not rows:
        return analyze_slate(**kw)
    best: Dict[str, Dict] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:  # noqa: BLE001
            continue
        for r in payload.get("recommendations", []):
            prior = best.get(r["id"])
            if not prior or r["created_at"] > prior["created_at"]:
                best[r["id"]] = r
    recs = list(best.values())
    recs = [r for r in recs if bool(r.get("is_demo")) == R.family_is_demo(r.get("sport"))]
    if kw.get("sports"):
        allowed = set(kw["sports"])
        recs = [r for r in recs if r.get("sport") in allowed]
    recs.sort(key=lambda r: (-_state_rank(r["state"]), -r.get("rank_score", 0),
                             -r["confidence"], -r["edge_pct"]))
    return recs


def best_bets(min_confidence: Optional[float] = None, sports: Optional[List[str]] = None,
              markets: Optional[List[str]] = None, plus_money_only: bool = False,
              limit: int = 40, refresh_analysis: bool = False) -> List[Dict]:
    thr = settings.official_pick_confidence if min_confidence is None else min_confidence
    recs = analyze_slate(sports=sports) if refresh_analysis else cached_slate_recs(sports=sports)
    out = [r for r in recs if r["confidence"] >= thr and r["state"] == "OFFICIAL PICK"]
    if sports:
        out = [r for r in out if r["sport"] in sports]
        out = [r for r in out if bool(r.get("is_demo")) == R.family_is_demo(r["sport"])]
    if markets:
        out = [r for r in out if r["market"] in markets]
    if plus_money_only:
        out = [r for r in out if r["price"] > 0]
    return out[:limit]


def watchlist(limit: int = 30, sports: Optional[List[str]] = None) -> List[Dict]:
    recs = cached_slate_recs(sports=sports)
    out = [r for r in recs if r["state"] in ("LEAN", "WATCHLIST", "WAIT", "WAIT FOR PRICE")]
    if sports:
        out = [r for r in out if bool(r.get("is_demo")) == R.family_is_demo(r["sport"])]
    out.sort(key=lambda r: (-r["confidence"], -r["edge_pct"]))
    return out[:limit]


def line_moves(limit: int = 20, sports: Optional[List[str]] = None) -> List[Dict]:
    """Biggest observed movement across stored snapshots."""
    where = ""
    params: List[Any] = []
    if sports:
        where = f" WHERE sport IN ({','.join('?' * len(sports))})"
        params += sports
    rows = db.q(f"""
        SELECT event_id, sport, market, selection, book, is_demo,
               MIN(captured_at) AS first_at, MAX(captured_at) AS last_at, COUNT(*) AS n
        FROM odds_snapshots{where}
        GROUP BY event_id, market, selection, book HAVING n > 1
        ORDER BY n DESC LIMIT 400""", params)
    moves = []
    for r in rows:
        if bool(r.get("is_demo")) != R.family_is_demo(r.get("sport")):
            continue
        hist = db.odds_history(r["event_id"], r["market"], r["selection"], r["book"])
        if len(hist) < 2:
            continue
        first, last = hist[0], hist[-1]
        if first.get("line") is not None and last.get("line") is not None:
            delta = last["line"] - first["line"]
            unit = "pts"
        else:
            delta = american_to_prob(last["price"]) * 100 - american_to_prob(first["price"]) * 100
            unit = "pts implied"
        if abs(delta) < 0.25:
            continue
        ev = db.q1("SELECT name, start_time FROM events WHERE id=?", (r["event_id"],)) or {}
        moves.append(dict(
            event_id=r["event_id"], event_name=ev.get("name"), start_time=ev.get("start_time"),
            sport=r["sport"], market=r["market"], selection=r["selection"], book=r["book"],
            open_line=first.get("line"), open_price=first["price"],
            current_line=last.get("line"), current_price=last["price"],
            prev_line=hist[-2].get("line"), prev_price=hist[-2]["price"],
            delta=round(delta, 2), unit=unit, snapshots=len(hist),
            first_at=first["captured_at"], last_at=last["captured_at"],
            direction="toward " + r["selection"] if delta < 0 else "away from " + r["selection"],
            note=(f"The market has moved {abs(delta):.2g} {unit} on {r['selection']} since the "
                  f"first stored snapshot. Cause is not asserted without supporting reporting.")))
    moves.sort(key=lambda m: -abs(m["delta"]))
    return moves[:limit]


# ================================================================= calibration
def calibration_deltas() -> Dict[str, float]:
    """Confidence adjustments learned from graded history.

    Only statistically-backed adjustments are applied, and they are clamped.
    The LLM cannot write to this table.
    """
    rows = db.q("SELECT scope, bucket, n, hit_rate, expected, delta FROM calibration "
                "WHERE computed_at = (SELECT MAX(computed_at) FROM calibration)")
    out: Dict[str, float] = {}
    for r in rows:
        if (r["n"] or 0) >= 25 and r["delta"] is not None:
            out[r["scope"]] = max(-8.0, min(4.0, float(r["delta"])))
    return out
