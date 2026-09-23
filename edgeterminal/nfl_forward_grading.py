"""Append-only grades and reports for pregame NFL research freezes."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from pathlib import Path
from statistics import mean

from . import db, nfl_identity as identity
from .integrity import parse_time
from .market_provenance import closing_observation
from .providers.espn import EspnEvents
from .providers.normalize import normalize_book

RESULT_WAIT_S = 4 * 3600
BUCKETS = ((0, 1), (1, 3), (3, 6), (6, float("inf")))


def _read(path):
    path = Path(path)
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _game_id(freeze, game):
    home, away = identity.team_code(game.get("home")), identity.team_code(game.get("away"))
    if not home or not away or home == away:
        raise ValueError("freeze has invalid teams")
    return f"nfl-{freeze['season']}-reg-{freeze['week']:02d}-{away}-{home}"


def _valid_freeze(payload):
    if payload.get("status") != "RESEARCH_ONLY" or payload.get("designation") != "PASS":
        raise ValueError("research freeze must remain PASS")
    if not isinstance(payload.get("season"), int) or not isinstance(payload.get("week"), int):
        raise ValueError("freeze needs season and week")
    if not payload.get("family") or not payload.get("code_sha256") or not payload.get("training_games"):
        raise ValueError("freeze lacks model provenance")
    games = payload.get("games") or []
    if not games or len(games) > 16:
        raise ValueError("invalid freeze game count")
    seen = set()
    for game in games:
        kickoff = parse_time(game.get("kickoff_et"))
        if kickoff is None or not payload["frozen_at"] < kickoff:
            raise ValueError("freeze is not before kickoff")
        canonical = _game_id(payload, game)
        if canonical in seen:
            raise ValueError("duplicate frozen game")
        seen.add(canonical)
        model = game.get("model") or {}
        try:
            values = [float(model[k]) for k in ("margin", "total", "home_win_prob")]
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid frozen projection") from exc
        if not all(math.isfinite(x) for x in values) or not 0 <= values[2] <= 1:
            raise ValueError("invalid frozen projection")
        quote=game.get("frozen_quote") or {}
        for value_key,time_key in (("home_handicap","spread_captured_at"),
                                   ("total","total_captured_at"),
                                   ("home_moneyline_price","moneyline_captured_at"),
                                   ("away_moneyline_price","moneyline_captured_at")):
            if quote.get(value_key) is not None:
                captured=parse_time(quote.get(time_key))
                if captured is None or captured>payload["frozen_at"] or captured>=kickoff:
                    raise ValueError("frozen market quote lacks prefreeze timestamp")


def _embedded_market(freeze_sha, payload, game):
    """Preserve only prices present in the original file at its freeze time."""
    quote = game.get("frozen_quote") or {}
    kickoff = parse_time(game["kickoff_et"])
    source = quote.get("source") or "frozen_research_file"
    specs = (("spread", game["home"], "home_handicap", "home_spread_price", "spread",
              "spread_source_quote_id"),
             ("total", "Over", "total", "over_price", "total","total_source_quote_id"),
             ("moneyline", game["home"], None, "home_moneyline_price", "moneyline",
              "moneyline_home_source_quote_id"),
             ("moneyline", game["away"], None, "away_moneyline_price", "moneyline",
              "moneyline_away_source_quote_id"))
    rows = []
    for market, selection, line_key, price_key, prefix, quote_id_key in specs:
        captured = quote.get(f"{prefix}_captured_at")
        if captured is None or not captured <= payload["frozen_at"] or not captured < kickoff:
            continue
        line = quote.get(line_key) if line_key else None
        price = quote.get(price_key)
        if line_key and line is None or price is None and market == "moneyline":
            continue
        rows.append((freeze_sha, game["game_id"], normalize_book(quote.get(f"{prefix}_book")),
                     market, selection, line, price, quote.get(f"{prefix}_provider") or source,
                     captured, kickoff,"current",quote.get("source_snapshot_id"),
                     quote.get(quote_id_key)))
    # The original 2026 W3 file contains lines and capture times, but no
    # prices. The immutable live shadow snapshot is the source of those lines.
    # Recover its exact observed batch, never a later quote.
    if not quote.get("spread_book") and quote.get("source") == "existing_immutable_live_shadow":
        canonical = _game_id(payload, game)
        shadow = db.q1("SELECT * FROM nfl_shadow_entries WHERE game_id=?",(canonical,))
        if shadow:
            saved = db.q1("SELECT payload FROM nfl_game_snapshots WHERE id=?",
                          (shadow["snapshot_id"],))
            if saved:
                batch = json.loads(saved["payload"]).get("market") or []
                spread = next((r for r in batch if r.get("market") == "spread" and
                               r.get("selection") == game["home"] and
                               r.get("line") == -quote.get("home_handicap", float("nan")) and
                               r.get("captured_at") == quote.get("spread_captured_at")),None)
                total = next((r for r in batch if r.get("market") == "total" and
                              r.get("selection") == "Over" and
                              r.get("line") == quote.get("total") and
                              r.get("captured_at") == quote.get("total_captured_at")),None)
                recovered = [r for r in (spread,total) if r]
                if recovered and all(r["captured_at"] <= payload["frozen_at"] and
                                     r["captured_at"] < kickoff for r in recovered):
                    # ML must come from the same book and capture batch as a
                    # matched frozen line, or its original availability is unproven.
                    for anchor in tuple(recovered):
                        recovered.extend(r for r in batch if r.get("market") == "moneyline" and
                                         r.get("book") == anchor["book"] and
                                         r.get("captured_at") == anchor["captured_at"] and
                                         r.get("selection") in (game["home"],game["away"]))
                    rows = [(freeze_sha,game["game_id"],normalize_book(r["book"]),
                             r["market"],r["selection"],r.get("line"),r.get("price"),
                             r.get("source") or "espn-odds",r["captured_at"],kickoff,
                             "current",shadow["snapshot_id"],r.get("id")) for r in
                            { (r["book"],r["market"],r["selection"]):r for r in recovered }.values()]
    return rows


def _attest_market(c, digest, stamp):
    sources=c.execute("SELECT DISTINCT game_id,source_snapshot_id FROM nfl_research_market_freezes "
                      "WHERE freeze_sha=? AND source_snapshot_id IS NOT NULL",(digest,)).fetchall()
    for source in sources:
        saved=c.execute("SELECT payload FROM nfl_game_snapshots WHERE id=?",
                        (source["source_snapshot_id"],)).fetchone()
        if saved is None:
            raise ValueError("market source snapshot disappeared")
        c.execute("INSERT OR IGNORE INTO nfl_research_market_attestations VALUES(?,?,?,?,?)",
                  (digest,source["game_id"],source["source_snapshot_id"],
                   hashlib.sha256(saved["payload"].encode()).hexdigest(),stamp))


def register_freeze(path, now=None):
    payload, digest = _read(path)
    _valid_freeze(payload)
    row = db.q1("SELECT freeze_sha FROM nfl_research_freezes WHERE season=? AND week=? AND family=?",
                (payload["season"], payload["week"], payload["family"]))
    if row:
        if row["freeze_sha"] != digest:
            raise ValueError("freeze hash changed after registration")
        # A prior registration may have lacked a price sidecar. Append only
        # exact-match rows traceable to the original immutable snapshot.
        with db.tx() as c:
            for game in payload["games"]:
                c.executemany("INSERT OR IGNORE INTO nfl_research_market_freezes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              _embedded_market(digest, payload, game))
            _attest_market(c,digest,time.time() if now is None else now)
        return digest
    names = sorted({name for game in payload["games"] for name in game.get("features", {})})
    feature_hash = payload.get("feature_hash") or hashlib.sha256(
        json.dumps([payload["code_sha256"], names], separators=(",", ":")).encode()).hexdigest()
    model_hash = payload.get("model_hash") or payload["code_sha256"]
    stamp = time.time() if now is None else now
    with db.tx() as c:
        c.execute("INSERT INTO nfl_research_freezes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (digest, payload["season"], payload["week"], payload["family"],
                   str(Path(path).resolve()), payload["frozen_at"],
                   payload.get("model_version") or payload["family"], model_hash,
                   payload.get("feature_version") or "feature-names-and-code", feature_hash,
                   payload["training_games"], stamp))
        for game in payload["games"]:
            c.executemany("INSERT INTO nfl_research_market_freezes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          _embedded_market(digest, payload, game))
        _attest_market(c,digest,stamp)
    return digest


def _score(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None


def _direction(value):
    return 1 if value > 1e-9 else -1 if value < -1e-9 else 0


def _decision(predicted, actual):
    prediction, outcome = _direction(predicted), _direction(actual)
    if outcome == 0:
        return "push"
    if prediction == 0:
        return "pass"
    return "win" if prediction == outcome else "loss"


def _validate_result(freeze, game, result, now):
    if str(result.get("status") or "").upper() not in {"STATUS_FINAL", "FINAL"}:
        return None
    expected = _game_id(freeze, game)
    if identity.canonical_id(result) != expected:
        return None
    mapped = db.mapped_nfl_game("espn", str(result.get("id") or ""))
    if mapped != expected:
        return None
    kickoff = parse_time(game["kickoff_et"])
    if abs((parse_time(result.get("start_time")) or 0) - kickoff) > 4*3600:
        return None
    meta = result.get("meta") or {}
    home, away = _score(meta.get("home_score")), _score(meta.get("away_score"))
    retrieved = parse_time(meta.get("retrieved_at"))
    if (home is None or away is None or retrieved is None or
            retrieved < kickoff or retrieved > now + 60):
        return None
    return home, away, retrieved, str(meta.get("source_url") or "ESPN scoreboard")


def _grade_payload(freeze, game, digest, result, validated):
    home, away, retrieved, source = validated
    model, quote = game["model"], game.get("frozen_quote") or {}
    margin, total, probability = (float(model[k]) for k in ("margin", "total", "home_win_prob"))
    actual_margin, actual_total = home-away, home+away
    market_total = quote.get("total")
    market = db.q("SELECT * FROM nfl_research_market_freezes WHERE freeze_sha=? AND game_id=?",
                  (digest, game["game_id"]))
    spread_row = next((r for r in market if r["market"] == "spread" and
                       r["selection"] == game["home"]),None)
    spread = spread_row["line"] if spread_row else (
        quote.get("home_handicap") if quote.get("source") != "existing_immutable_live_shadow" else None)
    total_row = next((r for r in market if r["market"] == "total" and
                      r["selection"] == "Over"),None)
    if quote.get("source") == "existing_immutable_live_shadow":
        market_total = total_row["line"] if total_row else None
    home_ml = next((r for r in market if r["market"] == "moneyline" and
                    r["selection"] == game["home"]), None)
    away_ml = next((r for r in market if r["market"] == "moneyline" and
                    r["selection"] == game["away"]), None)
    return dict(game_id=game["game_id"],home_team=game["home"],away_team=game["away"],
                kickoff_utc=parse_time(game["kickoff_et"]),freeze_sha256=digest,
                frozen_at=freeze["frozen_at"],model_family=freeze["family"],
                final_home_score=home,final_away_score=away,
                predicted_home_points=(total+margin)/2,
                predicted_away_points=(total-margin)/2,predicted_margin=margin,
                predicted_total=total,home_win_probability=probability,
                actual_margin=actual_margin,actual_total=actual_total,
                frozen_spread=spread,frozen_total=market_total,
                frozen_spread_field_in_original_file=quote.get("home_handicap"),
                market_provenance_note=("original file stored the negated home spread; "
                                        "ATS uses the exact-match immutable source quote"
                                        if spread_row and quote.get("source") == "existing_immutable_live_shadow"
                                        else None),
                frozen_spread_book=next((r["book"] for r in market if r["market"]=="spread"),
                                        quote.get("spread_book")),
                frozen_total_book=next((r["book"] for r in market if r["market"]=="total"),
                                       quote.get("total_book")),
                frozen_spread_price=next((r["price"] for r in market if r["market"]=="spread"),
                                         quote.get("home_spread_price")),
                frozen_total_price=next((r["price"] for r in market if r["market"]=="total"),
                                        quote.get("over_price")),
                frozen_moneyline={"home_price":home_ml["price"] if home_ml else quote.get("home_moneyline_price"),
                                  "away_price":away_ml["price"] if away_ml else quote.get("away_moneyline_price"),
                                  "book":home_ml["book"] if home_ml else quote.get("moneyline_book")},
                ats_result=_decision(margin+spread,actual_margin+spread) if spread is not None else "unavailable",
                total_result=_decision(total-market_total,actual_total-market_total)
                if market_total is not None else "unavailable",
                straight_up_result=_decision(margin,actual_margin),
                absolute_margin_error=abs(margin-actual_margin),
                absolute_total_error=abs(total-actual_total),
                brier_contribution=(probability-float(actual_margin>0))**2
                if actual_margin != 0 else None,
                result_source=source,result_provider="espn",
                result_request={"season":freeze["season"],"season_type":2,
                                "week":freeze["week"]},
                result_retrieved_at=retrieved)


def sync_freeze(path, provider=None, now=None):
    stamp = time.time() if now is None else now
    digest = register_freeze(path, now=stamp)
    freeze, _ = _read(path)
    due = [g for g in freeze["games"] if stamp >= parse_time(g["kickoff_et"])+RESULT_WAIT_S]
    counts = {"freeze_sha256":digest,"due":len(due),"graded":0,"corrected":0,"unmatched":0}
    if not due:
        return counts
    results = (provider or EspnEvents()).list_events_for_week(freeze["season"],2,freeze["week"])
    by_id = {identity.canonical_id(r):r for r in results if identity.canonical_id(r)}
    for game in due:
        result = by_id.get(_game_id(freeze, game))
        validated = _validate_result(freeze, game, result, stamp) if result else None
        if validated is None:
            counts["unmatched"] += 1
            continue
        grade = _grade_payload(freeze, game, digest, result, validated)
        old = db.q1("SELECT * FROM nfl_research_grades WHERE freeze_sha=? AND game_id=? "
                    "ORDER BY revision DESC LIMIT 1",(digest,game["game_id"]))
        if old and (old["home_score"],old["away_score"]) == validated[:2]:
            continue
        revision = 1 if old is None else old["revision"]+1
        with db.tx() as c:
            c.execute("INSERT INTO nfl_research_grades VALUES(?,?,?,?,?,?,?,?,?)",
                      (digest,game["game_id"],revision,validated[0],validated[1],
                       validated[3],validated[2],stamp,json.dumps(grade,sort_keys=True)))
            if old:
                c.execute("INSERT INTO nfl_research_grade_audit(freeze_sha,game_id,prior_revision,"
                          "new_revision,old_home_score,old_away_score,new_home_score,new_away_score,"
                          "reason,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (digest,game["game_id"],old["revision"],revision,
                           old["home_score"],old["away_score"],validated[0],validated[1],
                           "provider score correction",validated[3],stamp))
        counts["corrected" if old else "graded"] += 1
    return counts


def _ci(values, seed=17):
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    samples = sorted(mean(values[rng.randrange(n)] for _ in range(n)) for _ in range(2000))
    return [round(samples[49],5),round(samples[1949],5)]


def _metrics(grades):
    n = len(grades)
    if not n:
        return {"games_graded":0,"margin_mae":None,"total_mae":None,"brier":None,
                "ats_record":{"wins":0,"losses":0,"pushes":0,"passes":0,"n_decisions":0,
                              "win_rate_ci95":None},
                "totals_record":{"wins":0,"losses":0,"pushes":0,"passes":0,"n_decisions":0,
                                 "win_rate_ci95":None}}
    output = {"games_graded":n}
    for name,key in (("margin_mae","absolute_margin_error"),
                     ("total_mae","absolute_total_error"),("brier","brier_contribution")):
        vals=[g[key] for g in grades if g[key] is not None]
        output[name]={"value":mean(vals) if vals else None,
                      "bootstrap_ci95":_ci(vals),"n":len(vals)}
    for label,key in (("ats_record","ats_result"),("totals_record","total_result")):
        outcomes=[g[key] for g in grades]
        decisions=[int(v=="win") for v in outcomes if v in ("win","loss")]
        output[label]={"wins":outcomes.count("win"),"losses":outcomes.count("loss"),
                       "pushes":outcomes.count("push"),"passes":outcomes.count("pass"),
                       "unavailable":outcomes.count("unavailable"),"n_decisions":len(decisions),
                       "win_rate":mean(decisions) if decisions else None,
                       "win_rate_ci95":_ci(decisions)}
    return output


def report(season=2026):
    rows = db.q("SELECT f.family,f.week,g.payload FROM nfl_research_grades g JOIN "
                "nfl_research_freezes f ON f.freeze_sha=g.freeze_sha WHERE f.season=? AND "
                "g.revision=(SELECT MAX(z.revision) FROM nfl_research_grades z WHERE "
                "z.freeze_sha=g.freeze_sha AND z.game_id=g.game_id)",(season,))
    freezes = db.q("SELECT family,week FROM nfl_research_freezes WHERE season=?",(season,))
    families = {}
    for f in freezes:
        families.setdefault(f["family"],[])
    for row in rows:
        families[row["family"]].append((row["week"],json.loads(row["payload"])))
    forward={}
    for family, observations in families.items():
        grades=[grade for _,grade in observations]
        summary=_metrics(grades)
        summary["weeks"]={str(w):_metrics([g for week,g in observations if week==w])
                          for w in sorted({w for w,_ in observations})}
        buckets={}
        for market in ("spread","total"):
            market_buckets={}
            for lower,upper in BUCKETS:
                if market=="spread":
                    selected=[g for g in grades if g.get("frozen_spread") is not None and
                              lower <= abs(g["predicted_margin"]+g["frozen_spread"]) < upper]
                else:
                    selected=[g for g in grades if g.get("frozen_total") is not None and
                              lower <= abs(g["predicted_total"]-g["frozen_total"]) < upper]
                market_buckets[f"{lower}-{upper if math.isfinite(upper) else 'plus'}"]=_metrics(selected)
            buckets[market]=market_buckets
        summary["edge_buckets"]=buckets
        summary["versus_closing_market"]=_market_comparison(grades)
        forward[family]=summary
    return {"retrospective_development":{"source":"reports/nfl_model_v2/research_results.json",
                                          "combined_with_forward":False},
            f"prospective_{season}":forward,
            "policy":"RESEARCH_ONLY; all live product behavior PASS"}


def _market_comparison(grades):
    """Require an observed quote in the last 15 minutes before kickoff."""
    margin_pairs=[]
    total_pairs=[]
    brier_pairs=[]
    for grade in grades:
        canonical = _game_id({"season":int(grade["game_id"][:4]),
                              "week":int(grade["game_id"][5:7])},
                             {"home":grade["home_team"],"away":grade["away_team"]})
        for event_id in (canonical,):
            quotes=db.q("SELECT * FROM odds_snapshots WHERE event_id=? AND is_demo=0",(event_id,))
            spread=closing_observation(quotes,grade["kickoff_utc"],
                                       grade.get("frozen_spread_book") or "unknown",
                                       "spread",grade["home_team"])
            total=closing_observation(quotes,grade["kickoff_utc"],
                                      grade.get("frozen_total_book") or "unknown",
                                      "total","Over")
            if spread:
                margin_pairs.append((grade["absolute_margin_error"],
                                     abs(-spread["line"]-grade["actual_margin"])))
            if total:
                total_pairs.append((grade["absolute_total_error"],
                                    abs(total["line"]-grade["actual_total"])))
            home_ml=closing_observation(quotes,grade["kickoff_utc"],
                                        grade["frozen_moneyline"].get("book") or "unknown",
                                        "moneyline",grade["home_team"])
            away_ml=closing_observation(quotes,grade["kickoff_utc"],
                                        grade["frozen_moneyline"].get("book") or "unknown",
                                        "moneyline",grade["away_team"])
            if (home_ml and away_ml and
                    abs(home_ml["fetched_at"]-away_ml["fetched_at"]) <= 60 and
                    grade["brier_contribution"] is not None):
                from .market_provenance import _implied
                ph,pa=_implied(home_ml["price"]),_implied(away_ml["price"])
                market_prob=ph/(ph+pa)
                brier_pairs.append((grade["brier_contribution"],
                                    (market_prob-float(grade["actual_margin"]>0))**2))
            if spread or total or home_ml and away_ml:
                break
    def comparison(pairs):
        differences=[model-market for model,market in pairs]
        return {"n":len(pairs),"candidate_mae":mean(x for x,_ in pairs) if pairs else None,
                "observed_close_mae":mean(x for _,x in pairs) if pairs else None,
                "candidate_minus_close_mae":mean(differences) if pairs else None,
                "difference_ci95":_ci(differences)}
    return {"basis":"same-book quote fetched within 15m before kickoff; unavailable otherwise",
            "margin":comparison(margin_pairs),"total":comparison(total_pairs),
            "brier":comparison(brier_pairs)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=("sync-all","report"))
    parser.add_argument("--season",type=int,default=2026)
    parser.add_argument("--freeze-dir",default="reports/nfl_model_v2")
    args=parser.parse_args()
    if args.command == "sync-all":
        output=sync_all(args.season,args.freeze_dir)
        print(json.dumps(output))
        if any("error" in item for item in output["sync"]):
            raise SystemExit(2)
    else:
        print(json.dumps(write_report(args.season,args.freeze_dir)))


def write_report(season=2026, freeze_dir="reports/nfl_model_v2"):
    output=report(season)
    destination=Path(freeze_dir)/f"forward_performance_{season}.json"
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(output,indent=2,sort_keys=True)+"\n")
    lines=[f"# Prospective {season} NFL research", "", "Status: RESEARCH_ONLY; live product PASS.",
           "", "These numbers exclude retrospective development games. See `research_results.json` "
           "for that separate analysis.", "", "| Family | Graded | Margin MAE | Total MAE | Brier | ATS W-L-P | Totals W-L-P |",
           "| --- | ---: | ---: | ---: | ---: | --- | --- |"]
    for family,data in output[f"prospective_{season}"].items():
        metric=lambda key: f"{data[key]['value']:.3f}" if data[key] else "—"
        record=lambda key: f"{data[key]['wins']}-{data[key]['losses']}-{data[key]['pushes']}"
        lines.append(f"| {family} | {data['games_graded']} | {metric('margin_mae')} | "
                     f"{metric('total_mae')} | {metric('brier')} | {record('ats_record')} | "
                     f"{record('totals_record')} |")
    lines.extend(["", "Detailed week, edge-bucket, closing-market comparisons, sample counts, "
                  "and bootstrap intervals are in the companion JSON file.",
                  "A close comparator requires a same-book quote observed within 15 minutes before kickoff. "
                  "Without one, that comparison is unavailable. ESPN's `close` field alone does not establish a closing timestamp.",""])
    markdown=destination.with_suffix(".md")
    markdown.write_text("\n".join(lines))
    return {"report":str(destination),"markdown":str(markdown),
            "families":list(output[f"prospective_{season}"])}


def sync_all(season=2026, freeze_dir="reports/nfl_model_v2", provider=None, now=None):
    results=[]
    for path in sorted(Path(freeze_dir).glob(f"forward_{season}_week*.json")):
        try:
            results.append(sync_freeze(path,provider=provider,now=now))
        except Exception as exc:
            results.append({"freeze":str(path),"error":f"{type(exc).__name__}: {exc}"})
    return {"sync":results,**write_report(season,freeze_dir)}


if __name__ == "__main__":
    main()
