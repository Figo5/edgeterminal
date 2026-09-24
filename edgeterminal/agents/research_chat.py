"""Grounded, read-only answers to NFL research questions.

This path deliberately uses the same presentation views as the NFL UI. It never
evaluates PAPER policy, creates a position, or changes the official PASS state.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from .. import paper_research, product_views

PASS_NOTE = "Official EdgeTerminal betting status remains PASS; these are research estimates."
MARKET_NAMES = {
    "passing_yards": "passing yards", "rushing_yards": "rushing yards",
    "rushing_attempts": "rushing attempts", "receptions": "receptions",
    "receiving_yards": "receiving yards",
}


def _when(stamp):
    if not stamp:
        return "unknown time"
    return dt.datetime.fromtimestamp(float(stamp), ZoneInfo("America/New_York")).strftime("%b %-d, %-I:%M %p ET")


def _price(value):
    return f"{int(value):+d}" if value is not None else "price unavailable"


def _intent(message, ctx):
    low = message.lower()
    if re.search(r"\b(official pick|official bet|should i bet|what should i play)\b", low):
        return None
    if re.search(r"\b(paper|mock|portfolio)\b", low):
        return "paper"
    if re.search(r"\bparlay\b", low):
        return "parlay"
    if re.search(r"\b\d{2,3}\s*%?\s*(?:confidence|or higher|or above|plus)\b|\b(?:above|over|at least)\s*\d{2,3}\s*%", low):
        return "threshold"
    if re.search(r"\b(over\s*under|over/under|o/u|total)\b", low):
        return "total"
    if re.search(r"\b(moneyline|spread|who do you like|who wins)\b", low):
        return "game"
    if re.search(r"\b(best|top|strongest|favorite)\s+(?:\w+\s+)?prop\b", low):
        return "prop"
    if re.search(r"\b(best bet|best play|best line|favorite bet|what do you like|strongest play)\b", low):
        return "best"
    if re.search(r"\banything else\b", low) or ("what about" in low and not re.search(
            r"\b(receiving|rushing|passing|receptions?|touchdowns?|yards?|spread|moneyline|total)\b", low)):
        return "followup"
    return None


def _requested_market(message, ctx, intent):
    low = message.lower()
    if intent == "total":
        return "total"
    if "receiving" in low:
        return {"receiving_yards", "receptions"}
    if "rushing" in low:
        return {"rushing_yards", "rushing_attempts"}
    if "passing" in low:
        return {"passing_yards"}
    market = ctx.get("market")
    if intent in {"prop", "followup"} and market:
        return {"player_reception_yards": {"receiving_yards"},
                "player_receptions": {"receptions"},
                "player_rush_yards": {"rushing_yards"},
                "player_pass_yards": {"passing_yards"}}.get(market)
    return None


def _player_filter(message, rows, prior):
    low = message.lower()
    names = sorted({r["player"] for r in rows}, key=len, reverse=True)
    for name in names:
        if name.lower() in low:
            return name
    # A last name is enough only when it identifies exactly one player here.
    for name in names:
        parts = name.split()
        surname = (parts[-2] if parts[-1].rstrip(".").lower() in {"jr", "sr", "ii", "iii", "iv"}
                   and len(parts) > 1 else parts[-1]).lower()
        if len(surname) >= 4 and re.search(rf"\b{re.escape(surname)}\b", low):
            matches = [n for n in names if surname in {p.lower() for p in n.split()}]
            if len(matches) == 1:
                return name
    return (prior or {}).get("player")


def _prop_sides(rows, market_filter=None, player=None):
    out = []
    for r in rows:
        if market_filter and r["market"] not in market_filter:
            continue
        if player and r["player"] != player:
            continue
        for side in ("over", "under"):
            price, prob = r.get(f"{side}_price"), r.get(f"probability_{side}")
            if price is None or prob is None or r.get("line") is None:
                continue
            out.append({"kind": "prop", "player": r["player"], "market": r["market"],
                        "side": side, "line": r["line"], "price": price,
                        "probability": prob, "projection": r["projection"],
                        "book": r["sportsbook"], "observed_at": r["quote_observed_at"],
                        "stale": r["quote_is_stale"], "paper": bool(r["paper_position"]),
                        "team": r["team"]})
    return out


def _game_sides(game):
    p, m = game.get("projection"), game.get("market") or {}
    if not p:
        return []
    choices = [
        ("moneyline", "home", "home_win_probability", game["home_team"]),
        ("moneyline", "away", "away_win_probability", game["away_team"]),
        ("spread", "home", "home_cover_probability", game["home_team"]),
        ("spread", "away", "away_cover_probability", game["away_team"]),
        ("total", "over", "over_probability", "Over"),
        ("total", "under", "under_probability", "Under"),
    ]
    out = []
    for market, side, prob_key, label in choices:
        quote = (m.get(market) or {}).get(side)
        prob = p.get(prob_key) if market == "moneyline" else (m.get(market) or {}).get(prob_key)
        if not quote or quote.get("price") is None or prob is None:
            continue
        out.append({"kind": "game", "market": market, "side": label, "line": quote.get("line"),
                    "price": quote["price"], "probability": prob, "projection":
                    p.get("home_margin") if market == "spread" else p.get("total") if market == "total" else None,
                    "book": quote.get("book"), "observed_at": quote.get("captured_at"),
                    "stale": game.get("market_is_stale", True)})
    return out


def _key(r):
    return (r.get("player"), r["market"], r["side"], r.get("line"))


def _rank(rows):
    # The same side/line may be quoted by several books. Show the freshest one,
    # then prefer the better price; never merge different alternate lines.
    best = {}
    for r in rows:
        k = _key(r)
        old = best.get(k)
        if old is None or (r.get("observed_at") or 0, r["price"]) > (old.get("observed_at") or 0, old["price"]):
            best[k] = r
    return sorted(best.values(), key=lambda r: (-r["probability"], r.get("player") or "", r["market"]))


def _describe(r):
    pct = f"{r['probability']:.0%}"
    if r["kind"] == "prop":
        side = "O" if r["side"] == "over" else "U"
        text = (f"{r['player']} {side}{r['line']:g} {MARKET_NAMES[r['market']]} — {pct} model probability; "
                f"projection {r['projection']:.2f}; {r['book']} {_price(r['price'])}")
        if r["paper"]:
            text += "; PAPER position recorded"
    elif r["market"] == "moneyline":
        text = f"{r['side']} moneyline — {pct} model probability; {r['book']} {_price(r['price'])}"
    else:
        line = f"{r['line']:+g}" if r["market"] == "spread" else f"{r['line']:g}"
        text = f"{r['side']} {line} {r['market']} — {pct} model probability; {r['book']} {_price(r['price'])}"
    if r["stale"]:
        text += f"; last observed {_when(r.get('observed_at'))} (stale)"
    return text


def _game_answer(game, total_only=False):
    p = game.get("projection")
    if not p:
        return "No stored pregame game projection is available for this matchup."
    lines = [f"Model projects {game['away_team']} {p['away_score']:.1f} – {game['home_team']} {p['home_score']:.1f}." ]
    m = game.get("market") or {}
    if not total_only:
        ml = m.get("moneyline") or {}
        home_ml, away_ml = ml.get("home"), ml.get("away")
        moneyline = (f" Observed moneyline: {game['home_team']} {_price(home_ml.get('price'))} / "
                     f"{game['away_team']} {_price(away_ml.get('price'))} ({home_ml.get('book')})."
                     if home_ml and away_ml else "")
        lines.append(f"{game['home_team']} win probability: {p['home_win_probability']:.0%}.{moneyline}")
        spread = m.get("spread") or {}
        home = spread.get("home")
        if home:
            lines.append(f"Spread: {game['home_team']} {home['line']:+g} ({home.get('book')}, {_price(home.get('price'))}); model home margin {p['home_margin']:+.1f}; home cover probability " +
                         (f"{spread['home_cover_probability']:.0%}." if spread.get("home_cover_probability") is not None else "unavailable."))
    total = m.get("total") or {}
    over = total.get("over")
    if over:
        prob = total.get("over_probability")
        lines.append(f"Total: market {over['line']:g} ({over.get('book')}, Over {_price(over.get('price'))}); model {p['total']:.1f}; " +
                     (f"Over {prob:.0%}, Under {1-prob:.0%}." if prob is not None else "side probabilities unavailable."))
    else:
        lines.append(f"Model total: {p['total']:.1f}; no valid stored total line is available.")
    if game.get("market_is_stale"):
        lines.append(f"Market quotes are last observed {_when(game.get('market_observed_at'))} and are stale.")
    return "\n".join(lines)


def _correlation(legs):
    if len(legs) < 2:
        return ""
    linked = any(a.get("team") and a.get("team") == b.get("team") and
                 a["market"] == "passing_yards" and b["market"] in {"receptions", "receiving_yards"}
                 for a in legs for b in legs if a is not b)
    return ("Correlation: same-team passing and receiving outcomes can move together; "
            "the joint probability is not established." if linked else
            "Correlation between legs is not measured; these probabilities cannot be multiplied as if independent.")


def answer(message, ctx):
    """Return (answer, context additions) or None for the legacy chat route."""
    intent = _intent(message, ctx or {})
    if intent is None:
        return None
    if (ctx or {}).get("schedule", {}).get("uncertain"):
        return None
    prior = (ctx or {}).get("prior") or {}
    if intent == "paper":
        board = paper_research.dashboard()
        positions = board.get("open") or []
        lines = [f"PAPER portfolio: {len(positions)} open position(s); bankroll ${board['current_bankroll']:,.2f}."]
        lines += [f"{p['player_name']} {p['side'].title()} {p['line']:g} {MARKET_NAMES.get(p['market'], p['market'])} — {p['sportsbook']} {_price(p['odds'])}; ${p['stake']:g} mock stake."
                  for p in positions]
        lines.append("PAPER / EXPERIMENTAL — NOT A REAL BETTING RECOMMENDATION. Official status remains PASS.")
        return "\n".join(lines), {}
    res = (ctx or {}).get("resolution") or {}
    if res.get("state") != "OK" or not res.get("event_id"):
        return None
    try:
        view = product_views.matchup(res["event_id"])
    except (ValueError, KeyError):
        name = (res.get("event") or {}).get("name") or "that scheduled matchup"
        return f"No stored model research is available for {name}. " + PASS_NOTE, {}
    game = view["game"]
    title = f"{game['away_team']} at {game['home_team']}"
    rows = view["props"]["rows"]
    player = _player_filter(message, rows, prior) if intent in {"prop", "followup", "best"} else None
    # A new best-bet question should not silently inherit an old player filter.
    if intent == "best" and "what about" not in message.lower():
        player = None
    market_filter = _requested_market(message, ctx or {}, intent)
    if intent == "followup" and player and player != prior.get("player"):
        market_filter = None  # "What about Penix?" asks across his props.
        ctx["market"] = None
    if intent in {"game", "total"}:
        return f"{title}:\n{_game_answer(game, intent == 'total')}\n{PASS_NOTE}", {"player": None, "shown": []}
    if intent == "followup" and ctx.get("market") == "total":
        projection = game.get("projection") or {}
        over = ((game.get("market") or {}).get("total") or {}).get("over")
        difference = (projection.get("total") - over["line"] if over and
                      projection.get("total") is not None else None)
        detail = (f"The model is {difference:+.1f} points versus that observed total line. "
                  if difference is not None else "")
        return (f"For {title}, another way to read the total: {detail}\n"
                f"{_game_answer(game, True)}\n{PASS_NOTE}"), {"player": None, "shown": []}
    sides = _prop_sides(rows, market_filter if isinstance(market_filter, set) else None, player)
    if intent in {"best", "threshold", "parlay"} and not player and not market_filter:
        sides += _game_sides(game)
    ranked = _rank(sides)
    if intent == "threshold":
        match = re.search(r"\b(\d{2,3})\s*%", message) or re.search(r"\b(\d{2,3})\s*(?:confidence|or higher|or above|plus)\b", message)
        cutoff = int(match.group(1)) / 100 if match else prior.get("threshold")
        if cutoff is None or not 0 <= cutoff <= 1:
            return "Specify a confidence percentage from 0% to 100%.", {}
        qualifying = [r for r in ranked if r["probability"] >= cutoff]
        lines = [f"{title}: {len(qualifying)} model-backed side(s) at {cutoff:.0%}+ confidence."]
        lines += [f"{i}. {_describe(r)}" for i, r in enumerate(qualifying, 1)]
        lines.append(PASS_NOTE)
        return "\n".join(lines), {"threshold": cutoff, "player": player, "shown": [_key(r) for r in qualifying]}
    if intent == "parlay":
        m = re.search(r"\b([2-5])\s*(?:-|\s*)leg\b", message.lower())
        size = int(m.group(1)) if m else prior.get("parlay_size", 3)
        if not 2 <= size <= 5:
            size = 3
        # One side per player and market. Prefer distinct market types when close.
        legs, used, used_players = [], set(), set()
        for diversify in (True, False):
            for r in ranked:
                exposure = (r.get("player") or "game", r["market"])
                player_key = r.get("player")
                if exposure in used or (player_key and player_key in used_players) or \
                        (diversify and any(x["market"] == r["market"] for x in legs)):
                    continue
                legs.append(r); used.add(exposure)
                if player_key:
                    used_players.add(player_key)
                if len(legs) == size:
                    break
            if len(legs) == size:
                break
        lines = [f"Research Parlay for {title} ({len(legs)} model-backed legs):"]
        lines += [f"{i}. {_describe(r)}" for i, r in enumerate(legs, 1)]
        if len(legs) < size:
            lines.append(f"Only {len(legs)} usable legs are available for the requested {size}; no extra leg was invented.")
        if legs:
            lines.append(_correlation(legs))
        lines.append("Combined odds and joint win probability are unavailable. " + PASS_NOTE)
        return "\n".join(lines), {"parlay_size": size, "shown": [_key(r) for r in legs], "player": None}
    if intent == "followup" and "anything else" in message.lower():
        shown = {tuple(x) for x in prior.get("shown", []) if isinstance(x, (list, tuple))}
        exposures = {(x[0], x[1]) for x in shown}
        ranked = [r for r in ranked if _key(r) not in shown and
                  (r.get("player"), r["market"]) not in exposures]
    if intent == "best" and ranked:
        distinct, exposures = [], set()
        for r in ranked:
            exposure = (r.get("player") or "game", r["market"])
            if exposure not in exposures:
                distinct.append(r)
                exposures.add(exposure)
        picks = distinct[:3] if len(distinct) > 1 and distinct[0]["probability"] - distinct[1]["probability"] <= .03 else distinct[:1]
    else:
        picks = ranked[:1]
    label = "Best player prop" if intent == "prop" else "Research lean"
    lines = [f"{label} for {title}" + (f" — {player}" if player else "") + ":"]
    if picks:
        lines += [f"{i}. {_describe(r)}" for i, r in enumerate(picks, 1)]
    else:
        lines.append("No valid priced model-backed side is stored for that player or market.")
    lines.append(PASS_NOTE)
    return "\n".join(lines), {"player": player, "shown": [_key(r) for r in picks]}
