"""Read-only NFL presentation views over stored research and market records."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict

from . import db, engine, nfl_props, paper_research, shadow
from .analytics import football
from .config import ROOT, settings
from .integrity import parse_time
from .nfl_market import board_rows

VISIBLE_PROP_MARKETS = {"passing_yards", "rushing_yards", "rushing_attempts", "receptions", "receiving_yards"}


def _quote(game: dict, market: str, selection: str) -> dict | None:
    return next((q for q in game.get("markets", []) if q["market"] == market and
                 q["selection"] == selection), None)


def _prediction(game: dict) -> tuple[dict | None, float | None]:
    kickoff = parse_time(game["start_time"])
    if kickoff is None:
        return None, None
    row = db.q1("SELECT * FROM nfl_game_predictions WHERE game_id=? AND created_at<? "
                "ORDER BY created_at DESC LIMIT 1", (game["id"], kickoff))
    if not row:
        return None, None
    snapshot = db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=?", (row["snapshot_id"],))
    payload = json.loads(snapshot["payload"]) if snapshot else {}
    projection = payload.get("projection") or {
        "proj_home": row["projected_home_points"], "proj_away": row["projected_away_points"],
        "margin": row["projected_margin"], "total": row["projected_total"],
        "home_win_prob": row["home_win_probability"]}
    return projection, row["created_at"]


def _probability(projection: dict | None, market: str, line: float | None,
                 home: str) -> float | None:
    if not projection or line is None:
        return None
    try:
        if market == "spread" and projection.get("sd_margin") is not None:
            return football.spread_prob({**projection, "home": home}, home, float(line))
        if market == "total" and projection.get("sd_total") is not None:
            return football.total_prob(projection, "Over", float(line))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def _game(game: dict) -> dict:
    projection, projection_at = _prediction(game)
    home, away = game["home_team"], game["away_team"]
    kickoff = parse_time(game["start_time"])
    # The normal slate deliberately suppresses stale prices. Product views may
    # show the last observed pregame quote, but must label its age explicitly.
    latest = db.q(
        "SELECT * FROM (SELECT o.*, ROW_NUMBER() OVER (PARTITION BY book,market,selection "
        "ORDER BY captured_at DESC,id DESC) AS quote_rank FROM odds_snapshots o "
        "WHERE event_id=? AND captured_at<? AND id NOT IN "
        "(SELECT CAST(ref_id AS INTEGER) FROM quarantine WHERE kind='odds')) "
        "WHERE quote_rank=1", (game["id"], kickoff))
    markets = board_rows(latest, game)
    game = {**game, "markets": markets}
    home_prob = projection.get("home_win_prob") if projection else None
    preferred = home if home_prob is not None and home_prob >= .5 else away if home_prob is not None else None
    home_ml, away_ml = _quote(game, "moneyline", home), _quote(game, "moneyline", away)
    home_spread, away_spread = _quote(game, "spread", home), _quote(game, "spread", away)
    over, under = _quote(game, "total", "Over"), _quote(game, "total", "Under")
    cover_home = _probability(projection, "spread", home_spread["line"] if home_spread else None, home)
    over_prob = _probability(projection, "total", over["line"] if over else None, home)
    price_times = [q.get("captured_at") for q in game.get("markets", []) if q.get("captured_at")]
    market_at = max(price_times) if price_times else None
    return {"id": game["id"], "away_team": away, "home_team": home,
            "kickoff_utc": game["start_time"], "venue": game.get("venue"), "status": game.get("status"),
            "projection": {"away_score": projection.get("proj_away"),
                           "home_score": projection.get("proj_home"),
                           "home_margin": projection.get("margin"),
                           "total": projection.get("total"),
                           "home_win_probability": home_prob,
                           "away_win_probability": 1 - home_prob if home_prob is not None else None,
                           "preferred_team": preferred} if projection else None,
            "projection_at": projection_at,
            "market": {"moneyline": {"home": home_ml, "away": away_ml},
                       "spread": {"home": home_spread, "away": away_spread,
                                  "home_cover_probability": cover_home,
                                  "away_cover_probability": 1 - cover_home if cover_home is not None else None},
                       "total": {"over": over, "under": under,
                                 "over_probability": over_prob,
                                 "under_probability": 1 - over_prob if over_prob is not None else None}},
            "market_observed_at": market_at,
            "market_is_stale": market_at is None or time.time() - market_at > settings.stale_odds_s,
            "designation": "PASS"}


def weekly_board() -> dict:
    games = [_game(game) for game in engine.slate("NFL", limit_events=40)]
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS", "as_of": time.time(),
            "games": games, "game_count": len(games),
            "latest_market_observed_at": max((g["market_observed_at"] or 0 for g in games), default=0) or None}


def _open_paper_positions(game_id: str) -> dict[tuple, dict]:
    rows = db.q("SELECT * FROM paper_positions WHERE game_id=?", (game_id,))
    out = {}
    for row in rows:
        settled = db.q1("SELECT result FROM paper_settlement_revisions WHERE position_id=? "
                        "ORDER BY revision DESC LIMIT 1", (row["id"],))
        if not settled or settled["result"] == "pending":
            out[(row["player_id"], row["market"], row["sportsbook"], row["line"], row["side"])] = {
                "id": row["id"], "side": row["side"], "line": row["line"],
                "entry_price": row["odds"], "created_at": row["created_at"]}
    return out


def player_props(game_id: str) -> dict:
    """Latest observed main lines joined to already-sealed point/probability records."""
    event = db.q1("SELECT start_time FROM events WHERE id=?", (game_id,))
    kickoff = parse_time(event["start_time"]) if event else None
    if kickoff is None:
        raise ValueError("unknown canonical NFL game")
    board = nfl_props.board(game_id)
    batch_id = board["batch_id"]
    pairs = db.q("SELECT p.payload,p.pair_sha256,p.freeze_sha,s.* FROM paper_prop_quote_pairings p "
                 "JOIN nfl_prop_snapshots s ON s.id=p.quote_id WHERE s.canonical_game_id=? "
                 "AND s.batch_id=? AND s.line_type='main' AND s.retrieved_at<? "
                 "AND (s.provider_updated_at IS NULL OR s.provider_updated_at<?)",
                 (game_id, batch_id, kickoff, kickoff)) if batch_id else []
    source_kind = "latest_observation"
    if not pairs:
        source_kind = "archived_freeze"
        freeze = db.q1("SELECT * FROM nfl_prop_research_freezes WHERE payload LIKE ? "
                       "ORDER BY frozen_at DESC LIMIT 1", (f'%"canonical_game_id":"{game_id}"%',))
        if freeze and hashlib.sha256(freeze["payload"].encode()).hexdigest() == freeze["freeze_sha"]:
            frozen = {q["quote_id"]: q for q in json.loads(freeze["payload"]).get("projections", [])}
            source_rows = db.q("SELECT * FROM nfl_prop_snapshots WHERE batch_id=? AND canonical_game_id=? "
                               "AND line_type='main' AND retrieved_at<?",
                               (freeze["source_batch_id"], game_id, kickoff))
            pairs = [{**row, "frozen_projection": frozen.get(row["id"])} for row in source_rows
                     if row["id"] in frozen]
    groups: dict[tuple, dict] = {}
    paper = _open_paper_positions(game_id)
    for row in pairs:
        if row["market"] not in VISIBLE_PROP_MARKETS or row["line"] is None or \
                row["price"] is None or abs(row["price"]) < 100 or row["side"] not in {"over", "under"}:
            continue
        if "frozen_projection" in row:
            projection = row["frozen_projection"]
        else:
            if hashlib.sha256(row["payload"].encode()).hexdigest() != row["pair_sha256"]:
                continue
            freeze = db.q1("SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?", (row["freeze_sha"],))
            if not freeze or hashlib.sha256(freeze["payload"].encode()).hexdigest() != row["freeze_sha"]:
                continue
            projection = json.loads(row["payload"])["quote"]
        if not projection or any(projection.get(k) is None for k in
                                 ("projection", "probability_over", "probability_under")):
            continue
        if not all(math.isfinite(float(projection[k])) for k in
                   ("projection", "probability_over", "probability_under")) or \
                not all(0 <= projection[k] <= 1 for k in ("probability_over", "probability_under")):
            continue
        key = (row["player_id"], row["market"], row["sportsbook"], row["line"])
        item = groups.setdefault(key, {"player_id": row["player_id"], "player": row["player_name"],
                                      "team": row["team"], "market": row["market"],
                                      "sportsbook": row["sportsbook"], "line": row["line"],
                                      "over_price": None, "under_price": None,
                                      "projection": projection["projection"],
                                      "probability_over": projection["probability_over"],
                                      "probability_under": projection["probability_under"],
                                      "quote_observed_at": row["provider_updated_at"] or row["retrieved_at"],
                                      "paper_position": None})
        item[f"{row['side']}_price"] = row["price"]
        item["quote_observed_at"] = max(item["quote_observed_at"],
                                        row["provider_updated_at"] or row["retrieved_at"])
        entry = paper.get((*key, row["side"]))
        if entry:
            item["paper_position"] = entry
    rows = list(groups.values())
    for item in rows:
        if item["probability_over"] >= item["probability_under"]:
            item["stronger_side"], item["confidence"] = "over", item["probability_over"]
        else:
            item["stronger_side"], item["confidence"] = "under", item["probability_under"]
        item["two_sided"] = item["over_price"] is not None and item["under_price"] is not None
        item["quote_is_stale"] = time.time() - item["quote_observed_at"] > settings.stale_odds_s
    rows.sort(key=lambda r: (-r["confidence"], r["player"], r["market"], r["sportsbook"]))
    latest = max((r["quote_observed_at"] for r in rows), default=None)
    return {"game_id": game_id, "source_kind": source_kind, "snapshot_at": latest,
            "rows": rows, "count": len(rows), "two_sided_count": sum(r["two_sided"] for r in rows),
            "readiness": "RESEARCH_ONLY", "designation": "PASS"}


def matchup(game_id: str) -> dict:
    board = weekly_board()
    game = next((g for g in board["games"] if g["id"] == game_id), None)
    if not game:
        raise ValueError("unknown current NFL game")
    props = player_props(game_id)
    snapshot = db.q1(
        "SELECT s.payload,p.created_at FROM nfl_game_predictions p "
        "JOIN nfl_game_snapshots s ON s.id=p.snapshot_id "
        "WHERE p.game_id=? AND p.created_at<? ORDER BY p.created_at DESC LIMIT 1",
        (game_id, parse_time(game["kickoff_utc"])))
    stored = json.loads(snapshot["payload"]) if snapshot else {}
    context = {"weather": stored.get("weather"), "injuries": stored.get("injuries") or [],
               "observed_at": snapshot["created_at"] if snapshot else None}
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS", "as_of": board["as_of"],
            "game": game, "props": props, "context": context}


def performance() -> dict:
    prop_path = ROOT / "data/research/nfl_prop_forward_report.json"
    prop = json.loads(prop_path.read_text()) if prop_path.exists() else {}
    prospective = prop.get("prospective_2026", {})
    paper = paper_research.dashboard()
    return {"readiness": "RESEARCH_ONLY", "designation": "PASS",
            "prospective_2026": {"games": shadow.performance(),
                                 "player_props": {key: prospective.get(key) for key in
                                                  ("distinct_player_markets_graded",
                                                   "quote_sides_graded", "pending_quote_sides",
                                                   "markets")},
                                 "paper": {k: paper[k] for k in
                                           ("starting_bankroll", "current_bankroll", "open_positions",
                                            "settled_positions", "record", "profit_loss", "roi")}},
            "retrospective_research": {
                "available": bool(prop.get("retrospective_validation")),
                "detail_location": "research report archive"}}


RESEARCH_DOCUMENTS = {
    "game-methodology": "reports/nfl_model_v2/methodology.md",
    "game-validation": "reports/nfl_model_v2/forward_validation_methodology.md",
    "game-integrity": "reports/nfl_model_v2/week03_integrity_2026.md",
    "promotion-criteria": "reports/nfl_model_v2/promotion_criteria.md",
    "prop-grading-calibration": "docs/research/nfl_prop_grading_calibration.md",
    "settlement-rules": "docs/research/nfl_prop_settlement_rules.md",
    "paper-policy": "docs/research/experimental_paper_v1.md",
    "paper-operations": "docs/research/paper_refresh_operations.md",
}


def research_index() -> dict:
    """Artifact pointers and recorded freeze hashes; never refresh or repair."""
    integrity_path = ROOT / "reports/nfl_model_v2/week03_integrity_2026.json"
    integrity = json.loads(integrity_path.read_text()) if integrity_path.exists() else {}
    artifact_path = ROOT / "reports/nfl_model_v2/forward_2026_week03.json"
    actual_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest() if artifact_path.exists() else None
    prop_shas = [row["freeze_sha"] for row in db.q(
        "SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at")]
    return {
        "week3_game_freeze": {
            "recorded_sha256": integrity.get("freeze_sha256"),
            "current_sha256": actual_sha,
            "matches_record": actual_sha == integrity.get("freeze_sha256") if actual_sha else False,
            "game_count": integrity.get("game_count"),
        },
        "prop_freeze_shas": prop_shas,
        "documents": [{"title": slug.replace("-", " ").title(),
                       "url": f"/api/product/research-document/{slug}"}
                      for slug in RESEARCH_DOCUMENTS],
        "readiness": "RESEARCH_ONLY",
    }
