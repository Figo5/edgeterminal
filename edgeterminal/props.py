"""Player-prop research workflow.

The order of operations is fixed and deliberate:

    structured odds  ->  structured usage  ->  research only for the gaps
                     ->  bounded deterministic features  ->  model  ->  market blend
                     ->  deterministic confidence/units/state

Web research can change *inputs* (bounded multipliers) and *confidence*, never
arithmetic. Every recommendation carries the sources it used.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import db
from . import integrity as I
from .analytics import td as tdmod
from .analytics import usage as usagemod
from .config import settings
from .pricing import american_to_decimal, american_to_prob, breakeven_price, ev_per_unit
from .providers import normalize
from .research import features as rfeatures
from .research import planner as rplanner
from .research import provider as rprovider
from .research import sources as rssources

log = logging.getLogger("edgeterminal.props")

TD_MARKETS = ("player_anytime_td", "player_1st_td")
MARKET_LABELS = {
    "player_anytime_td": "Anytime TD", "player_1st_td": "First TD",
    "player_pass_yards": "Passing yards", "player_pass_tds": "Passing TDs",
    "player_pass_attempts": "Pass attempts", "player_pass_completions": "Completions",
    "player_rush_yards": "Rushing yards", "player_rush_attempts": "Rush attempts",
    "player_receptions": "Receptions", "player_reception_yards": "Receiving yards",
}
DEFAULT_TEAM_ATTEMPTS = 34.0
PROP_MIN_EDGE_PCT = 2.5
ONE_SIDED_KEYS = ("player_anytime_td", "player_1st_td")
# A projection built from one or two games is an artefact, not a measurement.
MIN_PRICING_GAMES = 4


# --------------------------------------------------------------------- helpers
def _registry():
    from .providers import registry as R
    return R


def _default_deps() -> Dict[str, Callable]:
    return dict(fetch_props=None, injuries=None, plays=None, research=None)


def implied_team_totals(event: Dict, odds_rows: Iterable[Dict]) -> Dict[str, Any]:
    """Split the game total using the spread, so each team has a TD environment."""
    total_line = None
    spread: Dict[str, float] = {}
    for row in odds_rows:
        selection = row.get("selection") or ""
        if row.get("market") == "total" and str(selection).lower() == "over":
            total_line = row.get("line") if total_line is None else total_line
        if row.get("market") == "spread" and row.get("line") is not None:
            spread.setdefault(selection, float(row["line"]))
    home, away = event.get("home_team"), event.get("away_team")
    if total_line is None or home not in spread:
        return dict(available=False, reason="game total/spread unavailable for this event",
                    totals={}, total_line=total_line, spread=spread)
    total = float(total_line)
    home_line = float(spread[home])
    home_total = total / 2 - home_line / 2
    away_total = total - home_total
    return dict(available=True, total_line=total, spread=spread,
                totals={home: round(home_total, 2), away: round(away_total, 2)},
                method="total split by the home handicap")


def best_prices(rows: Iterable[Dict]) -> Dict[str, Dict]:
    """Best available price per (player, market, side, line, book-set)."""
    out: Dict[str, Dict] = {}
    for row in rows:
        price = row.get("price")
        if price is None:
            continue
        key = _price_key(row)
        current = out.get(key)
        if current is None or american_to_prob(float(price)) < american_to_prob(float(current["price"])):
            out[key] = dict(row, price=float(price))
    return out


def _price_key(row: Dict) -> str:
    side = row.get("side") or "-"
    line = row.get("line")
    line_part = "" if line is None else f":{line}"
    return f"{row.get('player') or row.get('selection')}|{row.get('market')}|{side}{line_part}"


def group_quotes(rows: Iterable[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for row in rows:
        grouped.setdefault(_price_key(row), []).append(row)
    return grouped


def quote_stamp(row: Dict) -> Optional[float]:
    """When this quote was last known to be current.

    The book's own `last_update` is the truer statement of a price's age than
    the moment we fetched it, so it wins where the provider supplies one. Both
    travel with the row into the cache, which is what makes a cache hit report
    the original age instead of resetting it to zero.
    """
    for field in ("provider_updated_at", "captured_at"):
        value = row.get(field)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def quote_age_s(rows: Iterable[Dict], now: Optional[float] = None) -> Optional[float]:
    stamp = float(now if now is not None else time.time())
    stamps = [t for t in (quote_stamp(r) for r in rows) if t is not None]
    return round(stamp - max(stamps), 1) if stamps else None


# ------------------------------------------------------------------- confidence
PROP_CONFIDENCE_WEIGHTS = {
    "usage_sample": 22.0, "role_stability": 12.0, "market_coverage": 14.0,
    "price_freshness": 10.0, "source_reliability": 10.0, "source_agreement": 10.0,
    "model_market_agreement": 10.0, "player_identity": 6.0,
}


def prop_confidence(*, games: int, role_stable: bool, books: int, age_s: Optional[float],
                    best_reliability: Optional[str], source_agreement: float,
                    model_prob: float, market_prob: float,
                    identity_confident: bool,
                    cap: Optional[float] = None) -> Dict:
    """Bounded, componentised confidence. Props cannot reach game-market ceilings."""
    ceiling = float(settings.prop_confidence_cap if cap is None else cap)
    sample = min(1.0, max(0.0, games / 8.0))
    stability = 1.0 if role_stable else 0.45
    coverage = min(1.0, books / 3.0)
    if age_s is None:
        freshness = 0.5
    else:
        freshness = max(0.0, 1.0 - age_s / max(1.0, settings.stale_odds_s))
    reliability = {"PRIMARY": 1.0, "HIGH": 0.85, "MEDIUM": 0.6, "LOW": 0.3,
                   "UNVERIFIED": 0.15}.get(best_reliability or "", 0.4)
    agreement = max(0.0, min(1.0, source_agreement))
    gap = abs(float(model_prob) - float(market_prob))
    model_market = max(0.0, 1.0 - gap / 0.25)
    identity = 1.0 if identity_confident else 0.5
    parts = dict(usage_sample=sample, role_stability=stability, market_coverage=coverage,
                 price_freshness=freshness, source_reliability=reliability,
                 source_agreement=agreement, model_market_agreement=model_market,
                 player_identity=identity)
    raw = sum(PROP_CONFIDENCE_WEIGHTS[name] * value for name, value in parts.items())
    total_weight = sum(PROP_CONFIDENCE_WEIGHTS.values())
    score = raw / total_weight * 100.0
    return dict(confidence=round(min(ceiling, score), 1), cap=ceiling,
                components={k: round(v, 3) for k, v in parts.items()})


def prop_units(confidence: float, edge_pct: float, market: str) -> float:
    """Conservative ladder; TD props never exceed the configured ceiling."""
    ceiling = settings.prop_max_units.get(market, settings.prop_default_max_units)
    if edge_pct <= 0 or confidence < settings.prop_lean_confidence:
        return 0.0
    if confidence >= settings.prop_official_confidence and edge_pct >= 4.0:
        units = ceiling
    elif confidence >= settings.prop_lean_confidence:
        units = min(ceiling, 0.5 if market in ONE_SIDED_KEYS else 0.75)
    else:
        units = 0.0
    return round(units, 2)


def prop_state(*, confidence: float, edge_pct: float, books: int, age_s: Optional[float],
               agreement: float, usage_available: bool, is_demo: bool,
               price_source_type: str = "STRUCTURED",
               eligible_web: bool = False,
               usage_basis: Optional[str] = None) -> str:
    """The prop state machine. Never forces a play; never promotes a lean.

    A prior-season usage baseline can inform a watchlist entry but cannot support
    a staked Official Pick: the role evidence is from a different season.
    """
    if price_source_type == "WEB_FALLBACK" and not eligible_web:
        return "INSUFFICIENT DATA"
    if not usage_available:
        return "INSUFFICIENT DATA"
    if books < 2:
        return "INSUFFICIENT DATA"
    if age_s is None:
        # Freshness could not be established. Unverified is not the same as
        # fresh, so the price is browsable but carries no recommendation.
        return "INSUFFICIENT DATA"
    if age_s > settings.stale_odds_s:
        return "STALE"
    if agreement < 1.0:
        return "WATCHLIST"
    if edge_pct <= 0:
        return "PASS"
    if usage_basis == "prior_season":
        return "LEAN" if confidence >= settings.prop_lean_confidence else "WATCHLIST"
    if confidence >= settings.prop_official_confidence and edge_pct >= PROP_MIN_EDGE_PCT:
        return "OFFICIAL PICK"
    if confidence >= settings.prop_lean_confidence:
        return "LEAN"
    threshold = breakeven_price(0.5 + edge_pct / 200.0)
    if threshold and edge_pct < PROP_MIN_EDGE_PCT:
        return "WAIT FOR PRICE"
    return "WATCHLIST"


# ------------------------------------------------------------------ the workflow
def analyze_props(event_id: str, category: Optional[str] = None, research: bool = True,
                  deps: Optional[Dict[str, Callable]] = None,
                  now: Optional[float] = None,
                  query_budget: Optional[int] = None,
                  markets: Optional[List[str]] = None) -> Dict:
    """Run the hybrid prop workflow for one event."""
    from .providers import theoddsapi as oddsmod

    stamp = float(now if now is not None else time.time())
    deps = {**_default_deps(), **(deps or {})}
    trace: Dict[str, Any] = dict(stages=[], queries=[], cache={}, features={}, adjustments={})
    warnings: List[str] = []

    event = db.q1("SELECT * FROM events WHERE id=?", (event_id,))
    if not event:
        return dict(error=f"unknown event {event_id}")
    event = dict(event)
    if event.get("sport") != "NFL":
        return dict(error="EdgeTerminal serves NFL props only")

    markets = list(markets) if markets else oddsmod.markets_for_category(category)
    trace["stages"].append(dict(stage="markets", markets=markets, category=category or "all"))

    # 1. structured prices -------------------------------------------------
    fetch = deps.get("fetch_props") or (lambda eid, mk, **kw: _registry().event_props(
        eid, markets=mk, category=category, force=kw.get("force", False), event=event))
    fetched = fetch(event_id, markets) or {}
    rows = list(fetched.get("rows") or [])
    source = fetched.get("source") or "theoddsapi"
    is_demo = bool(fetched.get("is_demo"))
    price_error = fetched.get("error")
    identity = fetched.get("identity") or dict(state=I.IDENTITY_OK, reasons=[])
    if identity.get("state") != I.IDENTITY_OK:
        # Expired / mismatched / ambiguous / unavailable upstream: report it and
        # stop. Never price a different game, and never fall back to another date.
        reason = "; ".join(identity.get("reasons") or []) or "event could not be validated"
        trace["stages"].append(dict(stage="prices", source=source, rows=0,
                                    cache=fetched.get("cache"), error=None,
                                    identity=identity, quota=fetched.get("quota")))
        return dict(event=dict(id=event["id"], name=event.get("name"),
                               home_team=event.get("home_team"), away_team=event.get("away_team"),
                               start_time=event.get("start_time"), venue=event.get("venue")),
                    generated_at=stamp, category=category or "all", markets_requested=markets,
                    price_source=source, is_demo=is_demo, state=identity["state"],
                    identity=identity, recommendations=[], most_likely=None, best_value=None,
                    prices=[], market_favourite=None,
                    official=[], skipped=[], players=0, warnings=[reason],
                    research=dict(steps=[], citations=[], queries_used=0, conflicts=[]),
                    gaps=[], debug=trace,
                    note="No prop analysis was produced: the stored event could not be matched "
                         "to a live provider event.")
    trace["stages"].append(dict(stage="prices", source=source, rows=len(rows),
                                cache=fetched.get("cache"), error=price_error,
                                quota=fetched.get("quota"), identity=identity))
    if price_error:
        warnings.append(f"prop prices unavailable: {price_error}")

    # 2. structured usage + context ---------------------------------------
    odds_rows = [dict(r) for r in db.latest_odds(event_id=event_id)]
    environment = implied_team_totals(event, odds_rows)
    trace["stages"].append(dict(stage="environment", available=environment["available"],
                                totals=environment.get("totals"),
                                method=environment.get("method")))
    if not environment["available"]:
        warnings.append(environment["reason"])

    injuries = list((deps.get("injuries") or (lambda: _registry().injuries("NFL")))() or [])
    plays = (deps.get("plays") or (lambda: _registry().nflverse_plays()))()
    season = int(deps.get("season") or (stamp and _season_for(stamp)))
    usage_rows = usagemod.aggregate_player_usage(plays, season=season, retrieved_at=stamp) \
        if plays else {}
    # Early in a season most players have no current-season rows. The previous
    # season is a legitimate *labelled* baseline; a missing usage row is not.
    prior_plays = (deps.get("prior_plays") or (lambda: _registry().nflverse_plays(season - 1)))() \
        if plays else []
    prior_usage_rows = usagemod.aggregate_player_usage(prior_plays, season=season - 1) \
        if prior_plays else {}
    trace["stages"].append(dict(stage="prior_usage", players=len(prior_usage_rows),
                                season=season - 1))
    trace["stages"].append(dict(stage="usage", players=len(usage_rows), season=season,
                                source="nflverse-pbp" if plays else "unavailable"))
    if not usage_rows:
        warnings.append("no structured player usage available — props cannot be priced")

    players = _prop_players(rows)
    # 3. planner -> research only the gaps --------------------------------
    usage_games = max((r["games"] for r in usage_rows.values()), default=0)
    plan = rplanner.plan_prop_research(
        request=dict(kind="td_scorers" if category == "td" else "prop",
                     event_name=event.get("name")),
        known=dict(has_odds=bool(rows), has_injuries=bool(injuries),
                   has_usage=bool(usage_rows), usage_games=usage_games,
                   has_role=usage_games >= rplanner.MIN_USAGE_GAMES,
                   role_stable=usage_games >= rplanner.MIN_USAGE_GAMES,
                   has_implied_total=environment["available"],
                   has_matchup=bool(injuries), has_weather=False),
        players=[p["name"] for p in players[:3]],
        # Team-level gaps (weather, injuries, implied total) concern the fixture, so
        # they fall back to the event name. Player-level gaps use the team structured
        # usage resolved for that player, never an arbitrary side of the game.
        team=None, player_teams=_resolve_player_teams(
            [p["name"] for p in players[:3]], usage_rows, prior_usage_rows))
    trace["stages"].append(dict(stage="plan", gaps=[s["gap"] for s in plan],
                                queries=[s["query"] for s in plan]))

    research_result = dict(steps=[], items=[], queries_used=0)
    budget = int(query_budget if query_budget is not None else settings.research_query_budget)
    if research and settings.research_enabled and plan:
        runner = deps.get("research") or (lambda p: rprovider.shared().research_plan(
            p, budget=budget, team=event.get("home_team")))
        try:
            research_result = runner(plan) or research_result
        except Exception as e:  # noqa: BLE001 - research is best effort
            warnings.append(f"web research unavailable: {type(e).__name__}")
            log.warning("research failed: %s", type(e).__name__)
    trace["queries"] = [dict(gap=s.get("gap"), query=s.get("query"), cache=s.get("cache"),
                             results=len(s.get("items") or []))
                        for s in research_result.get("steps", [])]
    trace["cache"] = rprovider.shared().cache.stats() if not deps.get("research") else {}

    citations = [dict(title=i.get("title"), url=i.get("url"), publisher=i.get("publisher"),
                      reliability=i.get("reliability"), relevance=i.get("relevance"),
                      retrieved_at=i.get("retrieved_at"), published_at=i.get("published_at"))
                 for i in research_result.get("items", [])]

    # 4. bounded research features per player -----------------------------
    per_player_features: Dict[str, Dict] = {}
    for player in players:
        scoped = [i for i in research_result.get("items", [])
                  if player["name"].lower() in f"{i.get('title','')} {i.get('snippet','')}".lower()]
        extracted = rfeatures.extract(scoped or research_result.get("items", []),
                                      player=player["name"], now=stamp)
        per_player_features[player["name"]] = extracted
        if extracted["features"]:
            trace["features"][player["name"]] = {
                name: dict(value=f["value"], delta=f["delta"],
                           reliability=f["provenance"]["reliability"])
                for name, f in extracted["features"].items()}
            trace["adjustments"][player["name"]] = extracted["multipliers"]

    conflicts = sorted({c["feature"] for item in per_player_features.values()
                        for c in item["conflicts"]})
    trace["stages"].append(dict(stage="features", players=len(per_player_features),
                                conflicts=conflicts))

    # 5. model + market blend ---------------------------------------------
    board = _build_board(event=event, players=players, rows=rows, usage_rows=usage_rows,
                         prior_usage_rows=prior_usage_rows,
                         per_player_features=per_player_features, environment=environment,
                         injuries=injuries, market=markets, research_steps=research_result,
                         now=stamp, is_demo=is_demo, citations=citations)
    # Price provenance, shown rather than implied. `price_age_s` is measured from
    # the quotes themselves, so a cache hit reports the original age; `cached`
    # says whether this board came from the prop cache, which is a separate fact
    # from how old the prices are.
    price_age = _todays_age(rows, stamp)
    if price_age is None and rows:
        warnings.append("price age could not be established; these quotes are not "
                        "verified fresh and carry no recommendation")
    elif price_age is not None and price_age > settings.stale_odds_s:
        warnings.append(f"prices are {int(price_age)}s old, past the "
                        f"{int(settings.stale_odds_s)}s staleness limit")
    board.update(event=dict(id=event["id"], name=event.get("name"),
                            home_team=event.get("home_team"), away_team=event.get("away_team"),
                            start_time=event.get("start_time"), venue=event.get("venue")),
                 generated_at=stamp, category=category or "all", markets_requested=markets,
                 price_source=source, is_demo=is_demo, warnings=warnings, identity=identity,
                 price_age_s=price_age,
                 price_retrieved_at=fetched.get("retrieved_at"),
                 price_cache=fetched.get("cache"),
                 price_stale=(price_age is None or price_age > settings.stale_odds_s),
                 research=dict(steps=research_result.get("steps", []), citations=citations,
                               queries_used=research_result.get("queries_used", 0),
                               budget=budget, conflicts=conflicts),
                 gaps=[dict(gap=s["gap"], query=s["query"], priority=s.get("priority"))
                       for s in plan],
                 debug=trace)
    return board


def _season_for(stamp: float) -> int:
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(stamp).year


def _prop_players(rows: Iterable[Dict]) -> List[Dict]:
    names: Dict[str, Dict] = {}
    for row in rows:
        name = row.get("player") or row.get("selection")
        if not name:
            continue
        names.setdefault(name, dict(name=name, team=None, markets=set()))
        names[name]["markets"].add(row.get("market"))
    return [dict(v, markets=sorted(v["markets"])) for v in names.values()]


def _build_board(*, event: Dict, players: List[Dict], rows: List[Dict], usage_rows: Dict,
                 prior_usage_rows: Optional[Dict] = None,
                 per_player_features: Dict, environment: Dict, injuries: List[Dict],
                 market: List[str], research_steps: Dict, now: float, is_demo: bool,
                 citations: List[Dict]) -> Dict:
    grouped = group_quotes([r for r in rows if r.get("player") or r.get("selection")])
    recommendations: List[Dict] = []
    prices: List[Dict] = []          # verified sportsbook prices, no recommendation
    skipped: List[Dict] = []
    player_verdicts: List[Dict] = []
    citations_by_url = {c["url"]: c for c in citations if c.get("url")}
    prior_usage_rows = prior_usage_rows or {}

    def _usage_for(name: str):
        """Usage plus an identity verdict.

        Current-season usage first, then a clearly labelled prior-season baseline.
        A name that matches more than one person, or resolves to another roster,
        is refused rather than silently attributed to the busier player.
        """
        current = [dict(row, basis="current_season")
                   for row in usagemod.find_candidates(usage_rows, name)]
        prior = [dict(row, basis="prior_season")
                 for row in usagemod.find_candidates(prior_usage_rows, name)]
        candidates = current + prior
        verdict = I.classify_player(name, candidates, event)
        player_verdicts.append(dict(verdict, market_rows=len(candidates)))
        if not I.is_model_eligible(verdict) or not candidates:
            return None, None, verdict
        # Prefer this season's row for the player the verdict confirmed, but never
        # price off a sample too small to be a measurement: fall back to the
        # labelled prior season instead of turning one game into certainty.
        confirmed = [row for row in candidates if row.get("team") == verdict.get("team")]
        usable = [row for row in confirmed
                  if int(row.get("games") or 0) >= MIN_PRICING_GAMES]
        if not usable:
            verdict = dict(verdict, state=I.UNRESOLVED,
                           reason=f"usage sample too small to price "
                                  f"({max([int(r.get('games') or 0) for r in confirmed] or [0])} "
                                  f"game(s); {MIN_PRICING_GAMES} required)")
            player_verdicts.append(dict(verdict, market_rows=len(candidates)))
            return None, None, verdict
        chosen = next((row for row in usable if row["basis"] == "current_season"), usable[0])
        return chosen, chosen["basis"], verdict

    # --- touchdown markets -------------------------------------------------
    td_quotes: Dict[str, List[Dict]] = {}
    td_prices: Dict[str, Dict] = {}
    for key, rows_ in grouped.items():
        # ANYTIME ONLY. player_1st_td is a different settlement event at much
        # longer odds; pooling it here contaminated the anytime price series and
        # re-labelled first-TD quotes as anytime picks.
        if rows_[0].get("market") != "player_anytime_td":
            continue
        for row in rows_:
            if row.get("price") is None:
                continue
            player = row.get("player") or row.get("selection")
            side = (row.get("side") or "yes").strip().lower()
            td_quotes.setdefault(player, []).append(
                dict(book=row.get("book"), side=side, price=float(row["price"])))
            if side != "yes":
                continue        # a No quote informs the de-vig, it is not a pick
            runner = td_prices.get(player)
            # Best = highest payout, i.e. lowest implied probability -- the same test
            # best_prices() uses. Comparing the raw numbers picked the most negative
            # price, so a -500 quote beat -330 and the worst available price on the
            # board was reported as the best one.
            if runner is None or american_to_prob(float(row["price"])) < american_to_prob(
                    float(runner["price"])):
                td_prices[player] = row
    normalised = tdmod.no_vig_td_market(td_quotes) if td_quotes else dict(usable=False,
                                                                          reason="no anytime-TD market returned")
    if td_quotes and normalised.get("usable"):
        candidates = []
        for player in players:
            name = player["name"]
            if name not in td_prices:
                continue
            usage, basis, verdict = _usage_for(name)
            if not usage:
                skipped.append(dict(market="player_anytime_td", player=name,
                                    reason=verdict.get("reason"),
                                    identity=verdict.get("state")))
                continue
            team = (usage or {}).get("team") or event.get("home_team")
            team_total = environment.get("totals", {}).get(team)
            if team_total is None:
                skipped.append(dict(market="player_anytime_td", player=name,
                                    reason="no team implied total for this event"))
                continue
            env = tdmod.team_td_environment(implied_total=team_total)
            share = _td_opportunity_share(usage, team_total)
            feature = per_player_features.get(name) or {}
            candidates.append(dict(name=name, team=team, team_tds=env["projected_team_tds"],
                                   opportunity_share=share, best_price=td_prices[name]["price"],
                                   book=td_prices[name].get("book"),
                                   multipliers=feature.get("multipliers"),
                                   environment=env, usage=usage, feature=feature,
                                   usage_basis=basis))
        board = tdmod.td_board(candidates, normalised, model_reliability=_model_reliability(
            usage_rows, environment, normalised))
        for row in board.get("rows", []):
            name = row["player"]
            feature = per_player_features.get(name) or {}
            usage, basis, _verdict = _usage_for(name)
            books = len({q["book"] for q in grouped.get(_price_key(td_prices[name]), [])})
            if not row.get("recommendable"):
                # State A/B: a verified price, and the model detail we do have.
                # Without both a model and a same-player no-vig probability there
                # is no defensible edge, so this is browsable but never a pick.
                prices.append(dict(
                    market="player_anytime_td", player=name, team=row.get("team"),
                    price=row["best_price"], book=row["book"],
                    implied_prob=row["implied_prob"], model_prob=row["model_prob"],
                    market_prob=row["market_prob"], state="NOT RECOMMENDABLE",
                    probability_basis=row["probability_basis"], books=books,
                    reasons=row["reasons"], captured_at=td_prices[name].get("captured_at")))
                continue
            conf = prop_confidence(
                games=int((usage or {}).get("games") or 0),
                role_stable=bool(feature.get("source_agreement") and
                                 (usage or {}).get("games", 0) >= 4),
                books=books, age_s=_todays_age(rows, now),
                best_reliability=_best_reliability(feature),
                source_agreement=feature.get("source_agreement", 1.0),
                model_prob=row["model_prob"], market_prob=row["market_prob"],
                identity_confident=bool(usage))
            state = prop_state(confidence=conf["confidence"], edge_pct=row["edge_pct"],
                               books=books, age_s=_todays_age(rows, now),
                               agreement=feature.get("source_agreement", 1.0),
                               usage_available=bool(usage), is_demo=is_demo,
                               usage_basis=basis)
            recommendations.append(_rec(
                event=event, player=name, team=row.get("team"), market="player_anytime_td",
                side=None, line=None, price=row["best_price"], book=row["book"],
                model_prob=row["model_prob"], market_prob=row["market_prob"],
                final_prob=row["final_prob"], edge_pct=row["edge_pct"], confidence=conf,
                units=prop_units(conf["confidence"], row["edge_pct"], "player_anytime_td"),
                state=state, usage=usage, feature=feature, environment=row.get("expected_tds"),
                books=books, citations=citations, citations_by_url=citations_by_url,
                now=now, usage_basis=basis))

    # --- two-sided yardage / count markets --------------------------------
    # Group by (player, market, line) and NOT by side: `grouped` keys include the
    # side, so a per-key group only ever held Over or Under. De-vigging one side
    # against itself returned market_prob = 1.0 for every yardage prop.
    sided: Dict[tuple, List[Dict]] = {}
    for row in rows:
        market_name = row.get("market")
        if market_name in TD_MARKETS or row.get("price") is None:
            continue
        name = row.get("player") or row.get("selection")
        if not name:
            continue
        sided.setdefault((name, market_name, row.get("line")), []).append(row)

    for key, quotes in sided.items():
        sample = quotes[0]
        market_name = sample.get("market")
        player_name = sample.get("player") or sample.get("selection")
        usage, basis, verdict = _usage_for(player_name)
        if not usage:
            # No confirmed participant identity and no structured usage means no
            # honest projection: report it, do not price a zero-variance guess
            # (and never an automatic Under) against a real market.
            skipped.append(dict(market=sample.get("market"), player=player_name,
                                reason=verdict.get("reason"), identity=verdict.get("state")))
            continue
        feature = per_player_features.get(player_name) or {}
        books = len({q.get("book") for q in quotes})
        line = next((q.get("line") for q in quotes if q.get("line") is not None), None)
        try:
            projection = tdmod.prop_opportunity_projection(market_name, usage or {},
                                                           multipliers=feature.get("multipliers"))
        except (ValueError, KeyError, ZeroDivisionError, TypeError) as exc:
            # Unsupported market, or a usage row that cannot support the model.
            # Recorded rather than silently dropped.
            skipped.append(dict(market=market_name, player=player_name, reason=str(exc)))
            continue
        sides = {}
        for side in ("over", "under"):
            side_rows = [q for q in quotes if (q.get("side") or "").lower() == side
                         and (q.get("line") == line)]
            if not side_rows:
                continue
            sides[side] = max(side_rows, key=lambda q: -american_to_prob(float(q["price"])))
        if not sides or line is None:
            continue
        implied = {side: american_to_prob(float(q["price"])) for side, q in sides.items()}
        if len(sides) < 2:
            # One side alone cannot be de-vigged: dividing it by itself returns
            # 1.0, i.e. a certainty invented from a single price. Show the price,
            # recommend nothing.
            side, quote = next(iter(sides.items()))
            prices.append(dict(
                market=market_name, player=player_name, team=(usage or {}).get("team"),
                side=side.title(), line=line, price=float(quote["price"]),
                book=quote.get("book"), implied_prob=round(implied[side], 4),
                model_prob=round(tdmod.prop_probability(projection, side, float(line)), 4),
                market_prob=None, state="NOT RECOMMENDABLE",
                probability_basis="model-only", books=books,
                captured_at=quote.get("captured_at"),
                reasons=[f"Only the {side.title()} side is priced at {line}",
                         "No-vig unavailable: de-vig needs both Over and Under"]))
            continue
        overround = sum(implied.values())
        market_prob = {side: value / overround for side, value in implied.items()}
        for side, quote in sides.items():
            model_p = tdmod.prop_probability(projection, side, float(line))
            reliability = _model_reliability(usage_rows, environment, dict(books=books))
            from .analytics.nfl_features import blend_market_model
            blend = blend_market_model(model_p, market_prob[side], reliability, books)
            final_p = blend["final_prob"]
            price = float(quote["price"])
            edge = (final_p - american_to_prob(price)) * 100
            conf = prop_confidence(
                games=int((usage or {}).get("games") or 0),
                role_stable=bool(usage and (usage.get("games") or 0) >= 4),
                books=books, age_s=_todays_age(quotes, now),
                best_reliability=_best_reliability(feature),
                source_agreement=feature.get("source_agreement", 1.0),
                model_prob=model_p, market_prob=market_prob[side],
                identity_confident=bool(usage))
            state = prop_state(confidence=conf["confidence"], edge_pct=edge, books=books,
                               age_s=_todays_age(quotes, now),
                               agreement=feature.get("source_agreement", 1.0),
                               usage_available=bool(usage), is_demo=is_demo,
                               usage_basis=basis)
            recommendations.append(_rec(
                event=event, player=player_name, team=(usage or {}).get("team"),
                market=market_name, side=side, line=float(line), price=price,
                book=quote.get("book"), model_prob=round(model_p, 4),
                market_prob=round(market_prob[side], 4), final_prob=round(final_p, 4),
                edge_pct=round(edge, 2), confidence=conf,
                units=prop_units(conf["confidence"], edge, market_name), state=state,
                usage=usage, feature=feature, books=books, citations=citations,
                citations_by_url=citations_by_url, now=now, projection=projection,
                overround=overround, usage_basis=basis))

    rank = {"OFFICIAL PICK": 0, "LEAN": 1, "WAIT FOR PRICE": 2, "WATCHLIST": 3,
            "STALE": 4, "PASS": 5, "INSUFFICIENT DATA": 6}
    recommendations.sort(key=lambda r: (rank.get(r["state"], 9), -r["confidence"], -r["edge_pct"]))
    td_rows = [r for r in recommendations if r["market"] == "player_anytime_td"]
    most_likely = max(td_rows, key=lambda r: r["final_prob"]) if td_rows else None
    valued = [r for r in td_rows if r["edge_pct"] > 0]
    best_value = max(valued, key=lambda r: r["ev_per_unit"]) if valued else None
    # Market-implied favourite, for when no model is supportable. This is a
    # likelihood read off the price (vig included), never a recommendation.
    td_prices_shown = [p for p in prices if p["market"] == "player_anytime_td"]
    market_favourite = (max(td_prices_shown + td_rows,
                            key=lambda r: r.get("implied_prob") or 0)
                        if (td_prices_shown or td_rows) else None)
    # A board full of INSUFFICIENT DATA rows has not "qualified" anything; it is
    # still insufficient. Verified browse-only prices do count as something to show.
    graded = [r for r in recommendations if r["state"] != "INSUFFICIENT DATA"]
    state = ("OFFICIAL PICK" if any(r["state"] == "OFFICIAL PICK" for r in recommendations)
             else "NO QUALIFYING PROP" if graded or prices else "INSUFFICIENT DATA")
    board = dict(recommendations=recommendations, most_likely=most_likely,
                 best_value=best_value, state=state, prices=prices,
                 market_favourite=market_favourite,
                 official=[r for r in recommendations if r["state"] == "OFFICIAL PICK"],
                 market=normalised, players=len(players), skipped=skipped)
    board["player_identity"] = dict(
        participants=[v for v in player_verdicts if v["state"] == "participant"],
        mismatches=[v for v in player_verdicts if v["state"] == "mismatch"],
        unresolved=[v for v in player_verdicts if v["state"] == "unresolved"])
    return board


def _todays_age(rows: Iterable[Dict], now: float) -> Optional[float]:
    """Age of the newest quote on the board, or None when it cannot be known.

    This used to return 0.0 whenever the rows carried no timestamp -- which was
    always, because prop rows were never stamped. Every price therefore read as
    zero seconds old: the stale-quote gate could not trip and confidence took
    full freshness credit. An unknown age is now None, and None is treated as
    unverified rather than fresh.
    """
    stamps = [t for t in (quote_stamp(r) for r in rows) if t is not None]
    return round(now - max(stamps), 1) if stamps else None


def _best_reliability(feature: Dict) -> Optional[str]:
    best = None
    for item in (feature.get("features") or {}).values():
        reliability = (item.get("provenance") or {}).get("reliability")
        if reliability and (best is None or rssources.RANK[reliability] < rssources.RANK[best]):
            best = reliability
    return best


# League-average team touchdowns per game, on the same basis team_td_environment
# uses (league total / points per TD). The player's rate is expressed relative to
# this so the environment adjustment does not double-count.
LEAGUE_BASELINE_TEAM_TDS = 22.5 / 6.95


def _resolve_player_teams(names: Iterable[str], usage_rows: Dict,
                          prior_usage_rows: Optional[Dict] = None) -> Dict[str, str]:
    """Map each player to the team its structured usage resolved to.

    A name that matches more than one player, or none, is simply left out: an
    unknown team is better than the wrong team.
    """
    out: Dict[str, str] = {}
    for name in names or []:
        teams = set()
        for source in (usage_rows or {}, prior_usage_rows or {}):
            for row in usagemod.find_candidates(source, name):
                if row.get("team"):
                    teams.add(row["team"])
        if len(teams) == 1:
            out[name] = teams.pop()
    return out


def _td_opportunity_share(usage: Dict, team_total: Optional[float] = None) -> float:
    """Share of team touchdowns, from the player's own realised scoring rate.

    `td_board` computes expected_tds = team_tds * share, so returning
    `td_per_game / LEAGUE_BASELINE_TEAM_TDS` makes that product equal the player's
    observed touchdowns per game, scaled by how this game's projected scoring
    environment compares with an average one. That is a rate projection: defensible,
    dimensionally consistent, and impossible to saturate.

    The previous formula, `(red_zone * 1.4 + goal_line * 2.0) / touches * 0.9`, was a
    per-touch ratio, not a share of team touchdowns. A depth player with a handful of
    touches near the goal line reached the 0.6 cap and came out at the model's 75%
    ceiling -- for players the market priced around +350 (about 18%).

    A player with no touchdowns in the sample returns 0.0, which leaves the model
    unavailable rather than inventing a floor probability.

    ponytail: red-zone share is richer than a raw TD rate but needs a real
    opportunity model to avoid the saturation above; revisit with sample-size
    shrinkage if TD props ever become recommendable.
    """
    td_rate = float(usage.get("td_per_game") or 0)
    if td_rate <= 0:
        return 0.0
    return round(min(0.6, td_rate / LEAGUE_BASELINE_TEAM_TDS), 4)


def _model_reliability(usage_rows: Dict, environment: Dict, market: Dict) -> float:
    games = max((r["games"] for r in (usage_rows or {}).values()), default=0)
    books = market.get("books") or 0
    reliability = 0.35
    reliability += min(0.25, games / 40.0)
    reliability += min(0.2, books / 10.0)
    if not environment.get("available"):
        reliability -= 0.1
    return round(max(0.1, min(0.75, reliability)), 3)


def _rec(*, event: Dict, player: str, team: Optional[str], market: str, side: Optional[str],
         line: Optional[float], price: float, book: Optional[str], model_prob: float,
         market_prob: float, final_prob: float, edge_pct: float, confidence: Dict,
         units: float, state: str, usage: Optional[Dict], feature: Dict, books: int,
         citations: List[Dict], citations_by_url: Dict, now: float,
         usage_basis: Optional[str] = None,
         environment: Optional[float] = None, projection: Optional[Dict] = None,
         overround: Optional[float] = None) -> Dict:
    implied = american_to_prob(float(price))
    fair = tdmod.fair_price(final_prob)
    reasons = [f"Model {final_prob * 100:.1f}% vs no-vig market {market_prob * 100:.1f}%",
               f"Best price {price:+.0f} at {book} across {books} book(s)"]
    if usage:
        reasons.append(f"{usage['games']} games of usage: {usage['carries']:.0f} carries, "
                       f"{usage['targets']:.0f} targets, {usage['total_tds']:.0f} TDs")
    if environment is not None:
        reasons.append(f"{environment:.2f} expected TDs modelled for this player")
    for name, item in (feature.get("features") or {}).items():
        reasons.append(f"{name.replace('_', ' ')} x{item['value']:.2f} "
                       f"({item['provenance']['reliability']}: {item['provenance']['claim'][:80]})")
    risks: List[str] = []
    for conflict in feature.get("conflicts", []):
        risks.append(f"sources disagree on {conflict['feature'].replace('_', ' ')}")
    if not usage:
        risks.append("no structured usage for this player — the projection is not trustworthy")
    if books < 2:
        risks.append("only one book priced this market")
    if (usage or {}).get("games", 0) < 4:
        risks.append("small usage sample")
    if usage_basis == "prior_season":
        risks.append(f"usage baseline is the {int((usage or {}).get('season') or 0)} season — "
                     "no current-season snaps have been played yet")
        confidence = dict(confidence,
                          confidence=round(confidence["confidence"] * 0.9, 1))
    used_citations = [citations_by_url[c["url"]] for c in citations if c.get("url")]
    usage_summary = dict(
    games=(usage or {}).get("games"), carries=(usage or {}).get("carries"),
    targets=(usage or {}).get("targets"), receptions=(usage or {}).get("receptions"),
    rush_yards=(usage or {}).get("rush_yards"), rec_yards=(usage or {}).get("rec_yards"),
    total_tds=(usage or {}).get("total_tds"),
    red_zone_touches=(usage or {}).get("red_zone_touches"),
    goal_line_touches=(usage or {}).get("goal_line_touches"),
    snap_share=(usage or {}).get("snap_share"),
    missing=(usage or {}).get("missing")) if usage else None
    return dict(
        id=uuid.uuid4().hex[:16], event_id=event["id"], event_name=event.get("name"),
        sport="NFL", player=player, team=team, market=market,
        market_label=MARKET_LABELS.get(market, market), side=side, line=line,
        selection=f"{player}{' ' + side.title() if side else ''}",
        price=price, book=book, implied_prob=round(implied, 4),
        model_prob=round(float(model_prob), 4), market_prob=round(float(market_prob), 4),
        final_prob=round(float(final_prob), 4), fair_price=fair, edge_pct=round(edge_pct, 2),
        breakeven_price=breakeven_price(final_prob), ev_per_unit=round(ev_per_unit(final_prob, price), 4),
        confidence=confidence["confidence"], confidence_cap=confidence["cap"],
        confidence_components=confidence["components"], units=units, state=state,
        usage_basis=usage_basis,
        data_quality="limited", usage_available=bool(usage),
        books=books, overround=overround,
        projection=projection, research_context=[f["provenance"] for f in
                                                (feature.get("features") or {}).values()],
        adjustments=feature.get("multipliers", {}),
        source_agreement=feature.get("source_agreement", 1.0),
        price_source_type="STRUCTURED", is_demo=False,
        reasons=reasons, risks=risks, citations=used_citations,
        usage_summary=usage_summary,
        sources=[dict(kind="odds", title=f"{market} prices ({book or 'book'})",
                      publisher="theoddsapi", retrieved_at=now,
                      trust="reported")] + [
            dict(kind="research", title=c["title"], url=c["url"], publisher=c["publisher"],
                 retrieved_at=c["retrieved_at"], trust=c["reliability"].lower())
            for c in used_citations[:6]],
        created_at=now)