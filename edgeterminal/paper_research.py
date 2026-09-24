"""Prospective PAPER portfolio. Never feeds the product pick engine."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .config import ROOT
from .providers.normalize import normalize_team

POLICY_PATH = ROOT / "data/research/paper_policy_example.json"
EXPERIMENTAL_POLICY_PATH = ROOT / "data/research/experimental_paper_v1.json"
ACCOUNT = "paper-nfl"
MARKETS = {"passing_yards", "rushing_yards", "rushing_attempts", "receptions",
           "receiving_yards", "moneyline"}
RESULTS = {"won": "win", "lost": "loss", "push": "push", "void": "void"}
SUPPORTED_PROP_BOOKS = {"fanduel", "draftkings", "betmgm", "caesars", "espnbet", "bovada"}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid {label}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not math.isfinite(number):
        raise ValueError(f"invalid {label}")
    return number


def implied_probability(price: int) -> float:
    raw = _number(price, "American odds")
    if not raw.is_integer():
        raise ValueError("American odds must be integer")
    value = int(raw)
    if value == 0 or -100 < value < 100:
        raise ValueError("invalid American odds")
    return (-value / (100 - value)) if value < 0 else (100 / (100 + value))


def _valid_probability(value: object) -> bool:
    try:
        return 0 <= _number(value, "model probability") <= 1
    except ValueError:
        return False


def _valid_price(value: object) -> bool:
    try:
        implied_probability(value)
        return True
    except (TypeError, ValueError):
        return False


def _valid_sigma(value: object) -> bool:
    try:
        return _number(value, "uncertainty") > 0
    except ValueError:
        return False


def _valid_stat(value: object) -> bool:
    try:
        return _number(value, "stat") >= 0
    except ValueError:
        return False


def example_policy() -> dict:
    return json.loads(POLICY_PATH.read_text())


def active_policy() -> dict:
    """Versioned, enabled research policy loaded by the unattended worker."""
    return json.loads(EXPERIMENTAL_POLICY_PATH.read_text())


def policy_hash(policy: dict) -> str:
    return hashlib.sha256(_json(policy).encode()).hexdigest()


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict) or not isinstance(policy.get("version"), str) or not policy["version"]:
        raise ValueError("paper policy needs a version")
    if type(policy.get("enabled")) is not bool:
        raise ValueError("paper policy enabled must be boolean")
    markets = policy.get("eligible_markets")
    if not isinstance(markets, list) or not markets or not set(markets) <= MARKETS:
        raise ValueError("paper policy has unsupported markets")
    for key in ("minimum_price", "maximum_price"):
        implied_probability(policy[key])
    if int(policy["minimum_price"]) > int(policy["maximum_price"]):
        raise ValueError("paper policy price range is reversed")
    for key in ("quote_freshness_seconds", "maximum_positions_per_game",
                "maximum_open_positions", "starting_bankroll", "maximum_stake_fraction"):
        if _number(policy.get(key), key) <= 0:
            raise ValueError(f"paper policy {key} must be positive")
    if int(policy["maximum_positions_per_game"]) != policy["maximum_positions_per_game"]:
        raise ValueError("maximum positions per game must be integer")
    if int(policy["maximum_open_positions"]) != policy["maximum_open_positions"]:
        raise ValueError("maximum open positions must be integer")
    if policy["maximum_stake_fraction"] > 1:
        raise ValueError("maximum stake fraction exceeds bankroll")
    for key in ("min_model_line_difference", "min_probability_difference"):
        if policy.get(key) is not None and _number(policy[key], key) < 0:
            raise ValueError(f"paper policy {key} must be nonnegative")
    if policy["enabled"] and (policy.get("min_model_line_difference") is None or
                              policy.get("min_probability_difference") is None):
        raise ValueError("enabled paper policy must explicitly set both filters")
    books = policy.get("supported_sportsbooks")
    if books is not None and (not isinstance(books, list) or not books or
                             len(books) != len(set(books)) or not set(books) <= SUPPORTED_PROP_BOOKS):
        raise ValueError("paper policy has unsupported sportsbooks")
    for key in ("maximum_positions_per_player_market", "maximum_positions_per_slate"):
        if key in policy and (isinstance(policy[key], bool) or
                             _number(policy[key], key) <= 0 or
                             int(policy[key]) != policy[key]):
            raise ValueError(f"paper policy {key} must be a positive integer")
    if "maximum_open_exposure_fraction" in policy and not 0 < _number(
            policy["maximum_open_exposure_fraction"], "maximum_open_exposure_fraction") <= 1:
        raise ValueError("invalid maximum open exposure fraction")
    if "slate_timezone" in policy:
        ZoneInfo(policy["slate_timezone"])
    if "require_main_line" in policy and type(policy["require_main_line"]) is not bool:
        raise ValueError("require_main_line must be boolean")
    if "availability_max_age_seconds" in policy and _number(
            policy["availability_max_age_seconds"], "availability_max_age_seconds") <= 0:
        raise ValueError("availability_max_age_seconds must be positive")
    stake = policy.get("stake_policy")
    if not isinstance(stake, dict) or stake.get("type") != "flat_dollars" or _number(stake.get("amount"), "stake") <= 0:
        raise ValueError("paper policy needs positive flat_dollars stake")
    return policy


def register_policy(policy: dict, now: float | None = None) -> str:
    validate_policy(policy)
    body = _json(policy)
    digest = policy_hash(policy)
    old = db.q1("SELECT sha256 FROM paper_policies WHERE version=?", (policy["version"],))
    if old:
        if old["sha256"] != digest:
            raise ValueError("paper policy version already registered with different content")
        return policy["version"]
    db.ex("INSERT INTO paper_policies VALUES(?,?,?,?)",
          (policy["version"], digest, body, time.time() if now is None else now))
    return policy["version"]


def _account(policy: dict, now: float) -> None:
    previous = db.q1("SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,))
    if previous:
        if previous["starting_bankroll"] != policy["starting_bankroll"]:
            raise ValueError("existing paper account starting bankroll cannot be rewritten")
        return
    db.ex("INSERT INTO paper_accounts VALUES(?,?,?)",
          (ACCOUNT, policy["starting_bankroll"], now))


def _latest_settlements() -> dict[str, dict]:
    rows = db.q("SELECT * FROM paper_settlement_revisions WHERE revision=(SELECT MAX(s.revision) "
                "FROM paper_settlement_revisions s WHERE s.position_id=paper_settlement_revisions.position_id)")
    return {row["position_id"]: row for row in rows}


def _balances() -> tuple[float, float, list[dict], dict[str, dict]]:
    account = db.q1("SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,))
    start = float(account["starting_bankroll"]) if account else float(example_policy()["starting_bankroll"])
    positions = db.q("SELECT * FROM paper_positions WHERE account_id=? ORDER BY created_at,id", (ACCOUNT,))
    settled = _latest_settlements()
    pnl = sum(s["profit_loss"] for s in settled.values() if s["result"] != "pending")
    current = start + pnl
    reserved = sum(p["stake"] for p in positions if settled.get(p["id"], {}).get("result", "pending") == "pending")
    return current, current - reserved, positions, settled


def _clear_injury(player_name: str, game_id: str, quote_time: float, now: float,
                  max_age: float = 24 * 3600) -> bool:
    rows = db.q("SELECT payload,fetched_at FROM nfl_research_context_observations "
                "WHERE game_id=? AND subject=? AND kind IN ('offensive_injury','qb_availability') "
                "AND fetched_at<=? ORDER BY fetched_at DESC LIMIT 1", (game_id, player_name, now))
    if not rows or rows[0]["fetched_at"] < quote_time - max_age or now - rows[0]["fetched_at"] > max_age:
        return False
    status = str(json.loads(rows[0]["payload"]).get("status") or "").lower()
    return status in {"active", "available", "healthy", "cleared"}


def _frozen_quotes(now: float) -> list[tuple[str, dict, dict]]:
    out = []
    for freeze in db.q("SELECT freeze_sha,frozen_at,payload FROM nfl_prop_research_freezes ORDER BY frozen_at"):
        if freeze["frozen_at"] > now or hashlib.sha256(freeze["payload"].encode()).hexdigest() != freeze["freeze_sha"]:
            continue
        payload = json.loads(freeze["payload"])
        for quote in payload.get("projections", []):
            out.append((freeze["freeze_sha"], payload["metadata"], quote))
    return out


def _game_moneylines(now: float) -> list[tuple[dict, dict, dict]]:
    """Verified game freeze + its registered immutable moneyline observations."""
    out = []
    for frozen in db.q("SELECT * FROM nfl_research_freezes WHERE frozen_at<=?", (now,)):
        path = Path(frozen["file_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != frozen["freeze_sha"]:
            continue
        payload = json.loads(path.read_bytes())
        if payload.get("status") != "RESEARCH_ONLY" or payload.get("designation") != "PASS":
            continue
        games = {g["game_id"]: g for g in payload.get("games", [])}
        for quote in db.q("SELECT * FROM nfl_research_market_freezes WHERE freeze_sha=? AND market='moneyline'",
                          (frozen["freeze_sha"],)):
            game = games.get(quote["game_id"])
            if game and quote["source_quote_id"] is not None:
                out.append((frozen, game, quote))
    return out


def _game_context_clear(game: dict, now: float) -> bool:
    parts = game["game_id"].split("_")
    if len(parts) != 4:
        return False
    canonical = f"nfl-{parts[0]}-reg-{int(parts[1]):02d}-{parts[2].lower()}-{parts[3].lower()}"
    # A source must affirm both starters. Absence of a report is not clearance.
    for team in (parts[2], parts[3]):
        row = db.q1("SELECT payload FROM nfl_research_context_observations WHERE game_id=? "
                    "AND kind='qb_start' AND subject=? AND fetched_at<=? "
                    "ORDER BY fetched_at DESC LIMIT 1", (canonical, team, now))
        if not row or not json.loads(row["payload"]).get("player_id"):
            return False
    injuries = db.q("SELECT payload FROM nfl_research_context_observations WHERE game_id=? "
                    "AND kind IN ('offensive_injury','defensive_injury','qb_availability') "
                    "AND fetched_at<=?", (canonical, now))
    return all(str(json.loads(row["payload"]).get("status") or "").lower() in
               {"active", "available", "healthy", "cleared"} for row in injuries)


def opportunity_distribution(now: float | None = None) -> dict:
    """Descriptive model-line gaps; counts include stale archived Week 3 quotes."""
    stamp = time.time() if now is None else now
    by_market: dict[str, list[float]] = defaultdict(list)
    current = 0
    for _, _, q in _frozen_quotes(stamp):
        if q.get("market") not in MARKETS or q.get("projection") is None or q.get("line") is None:
            continue
        gap = float(q["projection"]) - float(q["line"])
        by_market[q["market"]].append(gap)
        if q["observed_at"] <= stamp < q["kickoff_utc"] and stamp - q["observed_at"] <= 15 * 60:
            current += 1
    return {"as_of": stamp, "source": "immutable prop research freezes",
            "note": "Descriptive gaps only; archived quotes may be stale. No threshold selected.",
            "fresh_pregame_quote_sides": current,
            "markets": {market: {"n": len(values), "min": min(values), "median": statistics.median(values),
                                "max": max(values), "positive": sum(v > 0 for v in values),
                                "negative": sum(v < 0 for v in values)}
                        for market, values in sorted(by_market.items())}}


def current_opportunities(now: float | None = None) -> list[dict]:
    stamp = time.time() if now is None else now
    rows = []
    for freeze_sha, _, quote in _frozen_quotes(stamp):
        if (quote.get("market") not in MARKETS or quote.get("line") is None or
                quote.get("projection") is None or quote.get("price") is None or
                not _valid_price(quote["price"]) or
                not _valid_probability(quote.get("probability_over")) or
                not _valid_probability(quote.get("probability_under")) or
                not quote["observed_at"] <= stamp < quote["kickoff_utc"] or
                stamp - quote["observed_at"] > 15 * 60):
            continue
        rows.append({"freeze_sha": freeze_sha, "quote_id": quote["quote_id"],
                     "game_id": quote["canonical_game_id"], "player": quote["player_name"],
                     "market": quote["market"], "side": quote["side"], "line": quote["line"],
                     "price": quote["price"], "sportsbook": quote["sportsbook"],
                     "projection": quote["projection"],
                     "model_line_difference": quote["projection"] - quote["line"],
                     "model_probability": quote["probability_over"] if quote["side"] == "over"
                     else quote["probability_under"],
                     "market_implied_probability": implied_probability(quote["price"]),
                     "quote_observed_at": quote["observed_at"], "kickoff_utc": quote["kickoff_utc"]})
    return rows


def _slate_day(kickoff: float, policy: dict) -> str:
    return datetime.fromtimestamp(kickoff, ZoneInfo(policy.get("slate_timezone", "America/New_York"))).date().isoformat()


def _ordered_frozen_quotes(now: float) -> list[tuple[str, dict, dict]]:
    # Freshest qualifying book wins within a player/market. A stable ID breaks ties.
    return sorted(_frozen_quotes(now), key=lambda item: (-item[2]["observed_at"], item[2]["quote_id"]))


def _prop_position_limit(policy: dict, quote: dict, stake: float) -> str | None:
    current, available, positions, settled = _balances()
    if sum(p["game_id"] == quote["canonical_game_id"] and
           p["player_id"] == quote["player_id"] and p["market"] == quote["market"]
           for p in positions if p["source_kind"] == "frozen_prop_research") >= policy.get(
               "maximum_positions_per_player_market", 1):
        return "competing_quote_player_market_already_selected"
    if sum(p["game_id"] == quote["canonical_game_id"] for p in positions) >= policy["maximum_positions_per_game"]:
        return "game_position_limit"
    if sum(_slate_day(p["kickoff_utc"], policy) == _slate_day(quote["kickoff_utc"], policy)
           for p in positions) >= policy.get("maximum_positions_per_slate", float("inf")):
        return "slate_position_limit"
    open_positions = [p for p in positions if settled.get(p["id"], {}).get("result", "pending") == "pending"]
    if len(open_positions) >= policy["maximum_open_positions"]:
        return "open_position_limit"
    if (stake > available or stake > current * policy["maximum_stake_fraction"] or
            sum(p["stake"] for p in open_positions) + stake >
            current * policy.get("maximum_open_exposure_fraction", 1.0) + 1e-9):
        return "bankroll_constraint"
    return None


def evaluate(policy: dict, now: float | None = None) -> dict:
    """Create immutable positions from exact, freshly frozen prop quote sides."""
    stamp = time.time() if now is None else now
    register_policy(policy, stamp)
    _account(policy, stamp)
    counts: Counter[str] = Counter()
    if not policy["enabled"]:
        return {"policy_version": policy["version"], "positions_created": 0,
                "status": "disabled", "skipped": {"policy_disabled": 1}}
    for freeze_sha, metadata, quote in _ordered_frozen_quotes(stamp):
        qid = quote["quote_id"]
        reason = None
        if db.q1("SELECT 1 FROM paper_positions WHERE quote_id=?", (qid,)):
            continue
        source = db.q1("SELECT * FROM nfl_prop_snapshots WHERE id=?", (qid,))
        mapped = (db.q1("SELECT game_id FROM nfl_provider_games WHERE provider=? AND provider_game_id=?",
                        (source["provider"], source["provider_event_id"])) if source else None)
        alias = (db.q1("SELECT player_id FROM nfl_prop_player_aliases WHERE provider=? AND provider_player_id=?",
                       (source["provider"], source["provider_player_id"])) if source else None)
        player = (db.q1("SELECT player_name FROM nfl_prop_players WHERE player_id=?",
                        (source["player_id"],)) if source else None)
        if (not source or source["batch_id"] != metadata.get("source_batch_id") or
                any(source[field] != quote.get(alias) for field, alias in
                    (("canonical_game_id", "canonical_game_id"), ("player_id", "player_id"),
                     ("player_name", "player_name"),
                     ("market", "market"), ("line", "line"), ("side", "side"),
                     ("price", "price"), ("sportsbook", "sportsbook"),
                     ("retrieved_at", "observed_at"), ("kickoff_utc", "kickoff_utc")))):
            reason = "source_quote_mismatch"
        elif (not mapped or mapped["game_id"] != quote["canonical_game_id"] or
              not alias or alias["player_id"] != quote["player_id"] or
              not player or player["player_name"] != quote["player_name"] or
              not source["player_id"] or not source["provider_player_id"] or
              not source["player_name"] or not source["team"] or not source["opponent"] or
              source["team"] == source["opponent"] or
              {normalize_team(source["team"]), normalize_team(source["opponent"])} !=
              {normalize_team(code) for code in quote["canonical_game_id"].split("-")[-2:]}):
            reason = "identity_unresolved"
        elif quote.get("market") not in policy["eligible_markets"]:
            reason = "market_ineligible"
        elif policy.get("supported_sportsbooks") and quote["sportsbook"] not in policy["supported_sportsbooks"]:
            reason = "sportsbook_unsupported"
        elif policy.get("require_main_line") and source["line_type"] != "main":
            reason = "alternate_line_ineligible"
        elif quote.get("line") is None or quote.get("price") is None:
            reason = "missing_line_or_price"
        elif quote.get("projection") is None or quote.get("uncertainty_sigma") is None:
            reason = "missing_projection"
        elif (quote.get("side") not in {"over", "under"} or
              not _valid_stat(quote["line"]) or not _valid_stat(quote["projection"]) or
              not _valid_price(quote["price"]) or
              not _valid_probability(quote.get("probability_over")) or
              not _valid_probability(quote.get("probability_under")) or
              not _valid_sigma(quote["uncertainty_sigma"])):
            reason = "invalid_price_or_probability"
        elif stamp >= quote["kickoff_utc"] or stamp < quote["observed_at"]:
            reason = "not_prospective"
        elif stamp - quote["observed_at"] > policy["quote_freshness_seconds"]:
            reason = "stale_quote"
        elif not policy["minimum_price"] <= quote["price"] <= policy["maximum_price"]:
            reason = "price_outside_policy"
        elif not _clear_injury(quote["player_name"], quote["canonical_game_id"],
                               quote["observed_at"], stamp,
                               policy.get("availability_max_age_seconds", 24 * 3600)):
            reason = "injury_status_unresolved"
        else:
            probability = quote["probability_over"] if quote["side"] == "over" else quote["probability_under"]
            market_probability = implied_probability(quote["price"])
            difference = quote["projection"] - quote["line"]
            signed_gap = difference if quote["side"] == "over" else -difference
            if signed_gap < policy["min_model_line_difference"] or probability - market_probability < policy["min_probability_difference"]:
                reason = "model_market_difference_below_policy"
        if reason:
            counts[reason] += 1
            db.ex("INSERT OR IGNORE INTO paper_opportunity_skips VALUES(?,?,?,?)",
                  (policy["version"], qid, reason, stamp))
            continue
        stake = float(policy["stake_policy"]["amount"])
        reason = _prop_position_limit(policy, quote, stake)
        if reason:
            counts[reason] += 1
            db.ex("INSERT OR IGNORE INTO paper_opportunity_skips VALUES(?,?,?,?)",
                  (policy["version"], qid, reason, stamp))
            continue
        signature = metadata.get("model_hash") or hashlib.sha256(
            _json({"model_version": quote["model_version"], "metadata": metadata}).encode()).hexdigest()
        pid = hashlib.sha256(f"{policy['version']}:{freeze_sha}:{qid}".encode()).hexdigest()
        db.ex("INSERT OR IGNORE INTO paper_positions(id,account_id,policy_version,freeze_sha,quote_id,"
              "game_id,player_id,player_name,market,side,line,odds,sportsbook,model_projection,"
              "model_probability,market_probability,quote_observed_at,created_at,kickoff_utc,"
              "model_version,model_hash,stake,policy_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (pid, ACCOUNT, policy["version"], freeze_sha, qid, quote["canonical_game_id"],
               quote["player_id"], quote["player_name"], quote["market"], quote["side"],
               quote["line"], quote["price"], quote["sportsbook"], quote["projection"],
               probability, market_probability, quote["observed_at"], stamp,
               quote["kickoff_utc"], quote["model_version"], signature, stake, policy_hash(policy)))
        counts["created"] += 1
    if "moneyline" in policy["eligible_markets"]:
        counts.update(_evaluate_game_moneylines(policy, stamp))
    return {"policy_version": policy["version"], "positions_created": counts.pop("created", 0),
            "status": "experimental_paper_only", "skipped": dict(counts)}


def _evaluate_game_moneylines(policy: dict, now: float) -> Counter[str]:
    counts: Counter[str] = Counter()
    for freeze, game, quote in _game_moneylines(now):
        qid = -int(quote["source_quote_id"])
        if db.q1("SELECT 1 FROM paper_positions WHERE quote_id=?", (qid,)):
            continue
        reason = None
        snapshot = db.q1("SELECT * FROM odds_snapshots WHERE id=?", (quote["source_quote_id"],))
        if (not snapshot or snapshot["event_id"] is None or
                snapshot["book"] != quote["book"] or snapshot["market"] != "moneyline" or
                snapshot["selection"] != quote["selection"] or snapshot["price"] != quote["price"]):
            reason = "source_quote_mismatch"
        elif quote["price"] is None:
            reason = "missing_line_or_price"
        elif not _valid_price(quote["price"]) or not _valid_probability(game["model"].get("home_win_prob")):
            reason = "invalid_price_or_probability"
        elif not quote["fetched_at"] <= now < quote["kickoff_utc"]:
            reason = "not_prospective"
        elif now - quote["fetched_at"] > policy["quote_freshness_seconds"]:
            reason = "stale_quote"
        elif not policy["minimum_price"] <= quote["price"] <= policy["maximum_price"]:
            reason = "price_outside_policy"
        elif not _game_context_clear(game, now):
            reason = "qb_or_injury_context_unresolved"
        home, away = game["home"], game["away"]
        if quote["selection"] == home:
            side, probability = "home", game["model"]["home_win_prob"]
        elif quote["selection"] == away:
            side, probability = "away", 1 - game["model"]["home_win_prob"]
        else:
            side, probability = None, None
            reason = "selection_identity_mismatch"
        if reason is None and probability - implied_probability(quote["price"]) < policy["min_probability_difference"]:
            reason = "model_market_difference_below_policy"
        if reason is None:
            current, available, positions, settled = _balances()
            stake = float(policy["stake_policy"]["amount"])
            if (sum(p["game_id"] == game["game_id"] for p in positions) >= policy["maximum_positions_per_game"] or
                    sum(settled.get(p["id"], {}).get("result", "pending") == "pending" for p in positions)
                    >= policy["maximum_open_positions"]):
                reason = "position_limit"
            elif stake > available or stake > current * policy["maximum_stake_fraction"]:
                reason = "bankroll_constraint"
        if reason:
            counts[reason] += 1
            db.ex("INSERT OR IGNORE INTO paper_opportunity_skips VALUES(?,?,?,?)",
                  (policy["version"], qid, reason, now))
            continue
        pid = hashlib.sha256(f"{policy['version']}:{freeze['freeze_sha']}:{qid}".encode()).hexdigest()
        db.ex("INSERT OR IGNORE INTO paper_positions(id,account_id,policy_version,freeze_sha,quote_id,"
              "game_id,player_id,player_name,market,side,line,odds,sportsbook,model_projection,"
              "model_probability,market_probability,quote_observed_at,created_at,kickoff_utc,"
              "model_version,model_hash,stake,source_kind,policy_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (pid, ACCOUNT, policy["version"], freeze["freeze_sha"], qid, game["game_id"],
               "", "", "moneyline", side, None, int(quote["price"]), quote["book"],
               game["model"]["margin"], probability, implied_probability(quote["price"]),
               quote["fetched_at"], now, quote["kickoff_utc"], freeze["model_version"],
               freeze["model_hash"], stake, "frozen_game_research", policy_hash(policy)))
        counts["created"] += 1
    return counts


def settle(now: float | None = None) -> int:
    """Append one paper revision per new sourced prop-grade revision."""
    stamp = time.time() if now is None else now
    added = 0
    for position in db.q("SELECT * FROM paper_positions"):
        if position["source_kind"] == "frozen_game_research":
            grades = db.q("SELECT * FROM nfl_research_grades WHERE freeze_sha=? AND game_id=? "
                          "ORDER BY revision", (position["freeze_sha"], position["game_id"]))
        else:
            grades = db.q("SELECT * FROM nfl_prop_grade_revisions WHERE freeze_sha=? AND quote_id=? "
                          "ORDER BY revision", (position["freeze_sha"], position["quote_id"]))
        for grade in grades:
            previous = db.q1("SELECT MAX(grade_revision) AS last_grade,MAX(revision) AS last_revision "
                             "FROM paper_settlement_revisions WHERE position_id=?", (position["id"],))
            if previous["last_grade"] is not None and grade["revision"] <= previous["last_grade"]:
                continue
            body = json.loads(grade["payload"])
            if position["source_kind"] == "frozen_game_research":
                home, away = grade["home_score"], grade["away_score"]
                result = ("pending" if home == away else "win" if
                          (home > away) == (position["side"] == "home") else "loss")
                evidence_sha = hashlib.sha256(grade["payload"].encode()).hexdigest()
            else:
                matching = (body.get("sportsbook") == position["sportsbook"] and
                            body.get("frozen_line") == position["line"] and
                            body.get("frozen_side") == position["side"] and
                            body.get("frozen_price") == position["odds"])
                result = RESULTS.get(body.get("sportsbook_settlement_interpretation")) if matching else None
                evidence_sha = grade["evidence_sha"]
            result = result or "pending"
            stake = position["stake"]
            pnl = (stake * (position["odds"] / 100 if position["odds"] > 0 else 100 / -position["odds"]) if result == "win"
                   else -stake if result == "loss" else 0.0)
            digest = hashlib.sha256(grade["payload"].encode()).hexdigest()
            db.ex("INSERT INTO paper_settlement_revisions VALUES(?,?,?,?,?,?,?,?)",
                  (position["id"], (previous["last_revision"] or 0) + 1, grade["revision"],
                   result, pnl, evidence_sha, digest, stamp))
            added += 1
    return added


def needs_settlement() -> bool:
    for position in db.q("SELECT id,source_kind,freeze_sha,quote_id,game_id FROM paper_positions"):
        if position["source_kind"] == "frozen_game_research":
            latest = db.q1("SELECT MAX(revision) AS n FROM nfl_research_grades WHERE freeze_sha=? "
                           "AND game_id=?", (position["freeze_sha"], position["game_id"]))
        else:
            latest = db.q1("SELECT MAX(revision) AS n FROM nfl_prop_grade_revisions WHERE freeze_sha=? "
                           "AND quote_id=?", (position["freeze_sha"], position["quote_id"]))
        used = db.q1("SELECT MAX(grade_revision) AS n FROM paper_settlement_revisions WHERE position_id=?",
                     (position["id"],))
        if (latest["n"] or 0) > (used["n"] or 0):
            return True
    return False


def dashboard(now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    current, available, positions, settled = _balances()
    account = db.q1("SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,))
    records = Counter(settled[p["id"]]["result"] for p in positions if p["id"] in settled)
    open_positions = [p for p in positions if settled.get(p["id"], {}).get("result", "pending") == "pending"]
    resolved = [p for p in positions if settled.get(p["id"], {}).get("result") in {"win", "loss", "push", "void"}]
    profit = sum(settled[p["id"]]["profit_loss"] for p in resolved)
    wagered = sum(p["stake"] for p in resolved)
    def group(key: str) -> dict:
        out: dict[str, dict] = defaultdict(lambda: {"n": 0, "profit_loss": 0.0})
        for p in resolved:
            row = out[p[key]]
            row["n"] += 1
            row["profit_loss"] += settled[p["id"]]["profit_loss"]
        return dict(out)
    return {"label": "PAPER / EXPERIMENTAL / NOT A REAL BETTING RECOMMENDATION",
            "starting_bankroll": account["starting_bankroll"] if account else example_policy()["starting_bankroll"],
            "current_bankroll": round(current, 2), "available_bankroll": round(available, 2),
            "open_positions": len(open_positions), "settled_positions": len(resolved),
            "record": {key: records[key] for key in ("win", "loss", "push", "void")},
            "units": round(sum(settled[p["id"]]["profit_loss"] / p["stake"] for p in resolved), 3),
            "profit_loss": round(profit, 2),
            "roi": round(profit / wagered, 6) if wagered else None,
            "by_market": group("market"), "by_model_version": group("model_version"),
            "by_policy_version": group("policy_version"),
            "open": open_positions[-20:],
            "current_opportunities": current_opportunities(stamp),
            "recent": [{**p, "settlement": settled.get(p["id"])} for p in positions[-20:][::-1]],
            "settled": [{**p, "settlement": settled[p["id"]]} for p in resolved[-20:][::-1]],
            "opportunity_distribution": opportunity_distribution(stamp),
            "skip_reasons": dict(Counter(r["reason"] for r in db.q(
                "SELECT reason FROM paper_opportunity_skips")))}


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only PAPER portfolio")
    parser.add_argument("action", choices=("status", "evaluate", "settle", "distribution"))
    parser.add_argument("--policy", type=Path, default=EXPERIMENTAL_POLICY_PATH)
    args = parser.parse_args()
    db.init()
    if args.action == "evaluate":
        result = evaluate(json.loads(args.policy.read_text()))
    elif args.action == "settle":
        result = {"new_settlement_revisions": settle()}
    elif args.action == "distribution":
        result = opportunity_distribution()
    else:
        result = dashboard()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
