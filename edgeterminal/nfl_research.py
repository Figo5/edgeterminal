"""Offline, chronological NFL model research. Never used to issue picks.

Only prior regular-season weeks enter a game's features. Closing lines and
same-game starting QB, weather, and injury fields are evaluation-only/absent.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .backtest import _grade_ats, _grade_total, _num, load_schedules
from .nfl_baselines import baselines_before
from .pricing import norm_cdf
from .providers.normalize import normalize_team

# Approximate home-city time zones. This is known before a game and is set to
# zero for neutral sites, where the actual host venue would need separate data.
PACIFIC = {normalize_team(None, team) for team in ("SEA", "SF", "LA", "LAC", "LV")}
MOUNTAIN = {normalize_team(None, team) for team in ("DEN", "ARI")}
CENTRAL = {normalize_team(None, team) for team in
           ("KC", "DAL", "HOU", "NO", "MIN", "CHI", "GB", "TEN")}


def _zone(team):
    return -8 if team in PACIFIC else -7 if team in MOUNTAIN else -6 if team in CENTRAL else -5


FEATURES = (
    "home_field", "rest_diff", "rest_sum", "elo_margin", "rolling_margin",
    "rolling_total", "off_epa_gap", "def_epa_gap", "adjusted_off_epa_gap",
    "adjusted_def_epa_gap", "off_success_gap", "def_success_gap",
    "early_epa_gap", "early_def_epa_gap", "explosive_gap", "explosive_allowed_gap",
    "sack_allowed_gap", "sack_created_gap", "hit_allowed_gap", "hit_created_gap",
    "turnover_gap", "qb_epa_gap", "qb_change_gap", "off_epa_sum", "def_epa_sum",
    "early_epa_sum", "explosive_sum", "games_min", "games_diff",
    "away_tz_shift", "away_eastbound_tz",
)
GROUPS = {
    "scores_elo_context": FEATURES[:6] + FEATURES[27:29] + FEATURES[29:],
    "epa_success_adjusted": FEATURES[6:12] + FEATURES[23:27],
    "early_explosive": FEATURES[12:16],
    "pressure_turnover": FEATURES[16:21],
    "qb_history": FEATURES[21:23],
}


def _rate(values, default=0.0):
    return sum(values) / len(values) if values else default


def _implied(price):
    if price is None or -100 < price < 100:
        return None
    return (-price) / (100-price) if price < 0 else 100 / (price+100)


def _market_probability(game):
    home, away = _implied(game["home_moneyline"]), _implied(game["away_moneyline"])
    if home is not None and away is not None:
        return home / (home+away)
    return norm_cdf(game["spread_line"]/13.2)


def _game_play_metrics(cache, seasons):
    """One compact row per team/game, derived from completed scrimmage plays."""
    result = {}
    for season in seasons:
        path = cache / f"play_by_play_{season}.csv.gz"
        groups = defaultdict(lambda: defaultdict(list))
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("season_type") != "REG" or row.get("play_type") not in {"pass", "run"}:
                    continue
                if _num(row.get("qb_spike")) == 1 or _num(row.get("qb_kneel")) == 1:
                    continue
                game_id = row.get("game_id")
                offense = normalize_team(None, row.get("posteam"))
                defense = normalize_team(None, row.get("defteam"))
                epa = _num(row.get("epa"))
                if not game_id or not offense or not defense or epa is None:
                    continue
                success = _num(row.get("success"))
                yards = _num(row.get("yards_gained"))
                play = row["play_type"]
                dropback = _num(row.get("qb_dropback")) == 1
                early = _num(row.get("down")) in {1, 2}
                explosive = yards is not None and yards >= (20 if play == "pass" else 10)
                fields = {"epa": epa, "success": success, "early_epa": epa if early else None,
                          "explosive": float(explosive),
                          "sack": _num(row.get("sack")) if dropback else None,
                          "hit": _num(row.get("qb_hit")) if dropback else None,
                          "turnover": float((_num(row.get("interception")) or 0) > 0 or
                                            (_num(row.get("fumble_lost")) or 0) > 0)}
                for key, value in fields.items():
                    if value is not None:
                        groups[(game_id, offense)]["off_" + key].append(value)
                        groups[(game_id, defense)]["def_" + key].append(value)
                qb = row.get("passer_player_id")
                if qb and dropback:
                    groups[(game_id, offense)]["qb_" + qb].append(epa)
        for key, values in groups.items():
            summary = {field: _rate(xs) for field, xs in values.items() if not field.startswith("qb_")}
            qbs = [(field[3:], xs) for field, xs in values.items() if field.startswith("qb_")]
            if qbs:
                qb, plays = max(qbs, key=lambda item: len(item[1]))
                summary.update(qb_id=qb, qb_epa=_rate(plays), qb_dropbacks=len(plays))
            result[key] = summary
    return result


def _elo_before(past, home, away, neutral):
    ratings = defaultdict(lambda: 1500.0)
    last_season = None
    for game in past:
        if game["season"] != last_season:
            if last_season is not None:
                for team in list(ratings):
                    ratings[team] = 1500 + .7 * (ratings[team] - 1500)
            last_season = game["season"]
        h, a = game["home"], game["away"]
        gap = ratings[h] - ratings[a] + (0 if game["neutral"] else 45)
        expected = 1 / (1 + 10 ** (-gap / 400))
        margin = game["actual_margin"]
        result = .5 if margin == 0 else float(margin > 0)
        delta = 18 * min(2.0, math.log1p(abs(margin)) / math.log(10)) * (result - expected)
        ratings[h] += delta
        ratings[a] -= delta
    gap = ratings[home] - ratings[away] + (0 if neutral else 45)
    return gap / 28, 1 / (1 + 10 ** (-gap / 400))


def _team_snapshot(past, team):
    recent = [g for g in past if team in (g["home"], g["away"])][-16:]
    rows = []
    for game in recent:
        metrics = game["metrics"].get(team, {})
        if metrics:
            rows.append((metrics, game["away"] if game["home"] == team else game["home"]))
    out = {field: _rate([r.get(field, 0.0) for r, _ in rows]) for field in (
        "off_epa", "def_epa", "off_success", "def_success", "off_early_epa",
        "def_early_epa", "off_explosive", "def_explosive", "off_sack", "def_sack",
        "off_hit", "def_hit", "off_turnover")}
    out["games"] = len(rows)
    out["opponents"] = [opponent for _, opponent in rows]
    qbs = [r for r, _ in rows if r.get("qb_id")]
    incumbent = qbs[-1]["qb_id"] if qbs else None
    same_qb = [r for r in qbs if r["qb_id"] == incumbent][-6:]
    dropbacks = sum(r.get("qb_dropbacks", 0) for r in same_qb)
    out["qb_epa"] = (sum(r["qb_epa"] * r.get("qb_dropbacks", 0) for r in same_qb)
                     / dropbacks if dropbacks else 0.0)
    out["qb_change"] = float(len(qbs) >= 2 and qbs[-1]["qb_id"] != qbs[-2]["qb_id"])
    return out


def _features_before(past, game, snapshots):
    home, away = game["home"], game["away"]
    h, a = snapshots[home], snapshots[away]
    league_off = _rate([v["off_epa"] for v in snapshots.values() if v["games"]])
    league_def = _rate([v["def_epa"] for v in snapshots.values() if v["games"]])

    def adjusted(team, kind, opponent_kind, center):
        row = snapshots[team]
        opponents = [snapshots[opp][opponent_kind] for opp in row["opponents"]
                     if opp in snapshots and snapshots[opp]["games"]]
        return row[kind] - (_rate(opponents, center) - center)

    elo_margin, elo_prob = _elo_before(past, home, away, game["neutral"])
    base = game["baselines"]
    hr, ar = game["home_rest"], game["away_rest"]
    values = dict(
        home_field=float(not game["neutral"]), rest_diff=(hr-ar) if hr and ar else 0,
        rest_sum=(hr+ar-14) if hr and ar else 0, elo_margin=elo_margin,
        rolling_margin=base["rolling"]["margin"], rolling_total=base["rolling"]["total"],
        off_epa_gap=h["off_epa"]-a["off_epa"], def_epa_gap=a["def_epa"]-h["def_epa"],
        adjusted_off_epa_gap=adjusted(home,"off_epa","def_epa",league_def)-adjusted(away,"off_epa","def_epa",league_def),
        adjusted_def_epa_gap=adjusted(away,"def_epa","off_epa",league_off)-adjusted(home,"def_epa","off_epa",league_off),
        off_success_gap=h["off_success"]-a["off_success"],
        def_success_gap=a["def_success"]-h["def_success"],
        early_epa_gap=h["off_early_epa"]-a["off_early_epa"],
        early_def_epa_gap=a["def_early_epa"]-h["def_early_epa"],
        explosive_gap=h["off_explosive"]-a["off_explosive"],
        explosive_allowed_gap=a["def_explosive"]-h["def_explosive"],
        sack_allowed_gap=a["off_sack"]-h["off_sack"],
        sack_created_gap=h["def_sack"]-a["def_sack"],
        hit_allowed_gap=a["off_hit"]-h["off_hit"],
        hit_created_gap=h["def_hit"]-a["def_hit"],
        turnover_gap=a["off_turnover"]-h["off_turnover"],
        qb_epa_gap=h["qb_epa"]-a["qb_epa"],
        qb_change_gap=a["qb_change"]-h["qb_change"],
        off_epa_sum=h["off_epa"]+a["off_epa"],
        def_epa_sum=h["def_epa"]+a["def_epa"],
        early_epa_sum=h["off_early_epa"]+a["off_early_epa"],
        explosive_sum=h["off_explosive"]+a["off_explosive"],
        games_min=min(h["games"],a["games"]), games_diff=h["games"]-a["games"],
        away_tz_shift=abs(_zone(home)-_zone(away)) if not game["neutral"] else 0,
        away_eastbound_tz=max(0,_zone(home)-_zone(away)) if not game["neutral"] else 0,
    )
    return values, elo_prob


def build_dataset(cache: Path, seasons=range(2019, 2026)):
    schedules = load_schedules(cache)
    metrics = _game_play_metrics(cache, seasons)
    games = []
    for row in schedules:
        season, week = _num(row.get("season")), _num(row.get("week"))
        if season not in seasons or row.get("game_type") != "REG" or week is None:
            continue
        home, away = normalize_team(None,row.get("home_team")), normalize_team(None,row.get("away_team"))
        hs, as_ = _num(row.get("home_score")), _num(row.get("away_score"))
        if not home or not away or hs is None or as_ is None:
            continue
        games.append(dict(season=int(season),week=int(week),game_id=row["game_id"],
                          home=home,away=away,actual_margin=hs-as_,actual_total=hs+as_,
                          neutral=str(row.get("location") or "").lower()=="neutral",
                          home_rest=_num(row.get("home_rest")),away_rest=_num(row.get("away_rest")),
                          spread_line=_num(row.get("spread_line")),total_line=_num(row.get("total_line")),
                          home_spread_odds=_num(row.get("home_spread_odds")),
                          away_spread_odds=_num(row.get("away_spread_odds")),
                          home_moneyline=_num(row.get("home_moneyline")),
                          away_moneyline=_num(row.get("away_moneyline")),
                          over_odds=_num(row.get("over_odds")),under_odds=_num(row.get("under_odds")),
                          metrics={home:metrics.get((row["game_id"],home),{}),
                                   away:metrics.get((row["game_id"],away),{})}))
    games.sort(key=lambda g:(g["season"],g["week"],g["game_id"]))
    rows, past = [], []
    for key in sorted({(g["season"],g["week"]) for g in games}):
        batch=[g for g in games if (g["season"],g["week"])==key]
        snapshots={team:_team_snapshot(past,team) for g in batch for team in (g["home"],g["away"])}
        opponents={opp for s in snapshots.values() for opp in s["opponents"]}
        snapshots.update({team:_team_snapshot(past,team) for team in opponents if team not in snapshots})
        for game in batch:
            game["baselines"]=baselines_before(schedules,game["season"],game["week"],game["home"],game["away"],game["neutral"])
            game["features"],game["rich_elo_prob"]=_features_before(past,game,snapshots)
            rows.append(game)
        past.extend(batch)
    return rows


def _models(train, features, family):
    x=np.asarray([[r["features"][f] for f in features] for r in train],dtype=float)
    margin=np.asarray([r["actual_margin"] for r in train])
    total=np.asarray([r["actual_total"] for r in train])
    win=(margin>0).astype(int)
    if family=="linear":
        maker=lambda:make_pipeline(StandardScaler(),Ridge(alpha=100.0))
        classifier=make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=500))
    else:
        maker=lambda:HistGradientBoostingRegressor(max_iter=60,max_leaf_nodes=7,
                  min_samples_leaf=35,l2_regularization=20,learning_rate=.05,random_state=7)
        classifier=HistGradientBoostingClassifier(max_iter=60,max_leaf_nodes=7,
                  min_samples_leaf=35,l2_regularization=20,learning_rate=.05,random_state=7)
    mm,tm=maker(),maker()
    mm.fit(x,margin);tm.fit(x,total);classifier.fit(x[margin!=0],win[margin!=0])
    return mm,tm,classifier


def walk_forward(rows, first_eval=2023, last_eval=2025, feature_names=FEATURES,
                 families=("linear", "boosted")):
    """Refit each week on earlier weeks only, with fixed hyperparameters."""
    predictions=[]
    for key in sorted({(r["season"],r["week"]) for r in rows if first_eval<=r["season"]<=last_eval}):
        train=[r for r in rows if (r["season"],r["week"])<key]
        test=[r for r in rows if (r["season"],r["week"])==key]
        if len(train)<300:
            continue
        x=np.asarray([[r["features"][f] for f in feature_names] for r in test],dtype=float)
        fit={family:_models(train,feature_names,family) for family in families}
        for i,game in enumerate(test):
            output={}
            for family,(mm,tm,clf) in fit.items():
                output[family]=dict(margin=float(mm.predict(x[i:i+1])[0]),
                                    total=float(tm.predict(x[i:i+1])[0]),
                                    prob=float(clf.predict_proba(x[i:i+1])[0,1]))
            if "linear" in output and "boosted" in output:
                output["ensemble"]={key:(output["linear"][key]+output["boosted"][key])/2
                                    for key in ("margin","total","prob")}
            base=game["baselines"]
            output["home_league"]=dict(margin=base["home_league"]["margin"],total=44.,
                                      prob=norm_cdf(base["home_league"]["margin"]/13.2))
            output["elo"]=dict(margin=base["elo"]["margin"],total=44.,prob=base["elo"]["home_win_prob"])
            output["rolling"]=dict(margin=base["rolling"]["margin"],total=base["rolling"]["total"],
                                   prob=norm_cdf(base["rolling"]["margin"]/13.2))
            output["rich_elo"]=dict(margin=game["features"]["elo_margin"],total=base["rolling"]["total"],
                                    prob=game["rich_elo_prob"])
            if game["spread_line"] is not None and game["total_line"] is not None:
                output["market_close"]=dict(margin=game["spread_line"],total=game["total_line"],
                                             prob=_market_probability(game))
            predictions.append(dict(game=game,models=output))
    return predictions


def _ci(values, seed=20260923, iterations=2000):
    if not values:return None
    rng=random.Random(seed)
    n=len(values)
    draws=sorted(sum(values[rng.randrange(n)] for _ in range(n))/n for _ in range(iterations))
    return [round(draws[49],4),round(draws[1949],4)]


def score(predictions, seasons=None):
    picked=[p for p in predictions if seasons is None or p["game"]["season"] in seasons]
    names=sorted({name for p in picked for name in p["models"]})
    report={}
    for name in names:
        available=[p for p in picked if name in p["models"]]
        margins=[abs(p["models"][name]["margin"]-p["game"]["actual_margin"]) for p in available]
        totals=[abs(p["models"][name]["total"]-p["game"]["actual_total"]) for p in available]
        briers=[(p["models"][name]["prob"]-float(p["game"]["actual_margin"]>0))**2
                for p in available if p["game"]["actual_margin"]!=0]
        def bets(kind):
            outcomes=[]
            for p in available:
                g,m=p["game"],p["models"][name]
                line=g["spread_line"] if kind=="ats" else g["total_line"]
                if line is None or name=="market_close":continue
                side=("home" if m["margin"]>line else "away") if kind=="ats" else ("over" if m["total"]>line else "under")
                if (m["margin"] if kind=="ats" else m["total"])==line:continue
                grade=_grade_ats(side,g["actual_margin"],line) if kind=="ats" else _grade_total(side,g["actual_total"],line)
                if grade!="push":outcomes.append(float(grade=="win"))
            return dict(n=len(outcomes),wins=int(sum(outcomes)),hit_rate=round(_rate(outcomes),4) if outcomes else None,
                        bootstrap_ci95=_ci(outcomes))
        report[name]=dict(n=len(available),margin_mae=round(_rate(margins),4),
                          total_mae=round(_rate(totals),4),brier=round(_rate(briers),6),
                          win_n=len(briers),ats=bets("ats"),totals=bets("totals"))
    return report


def paired_market_delta(predictions, name, metric):
    pairs=[p for p in predictions if name in p["models"] and "market_close" in p["models"]]
    if metric=="brier":
        pairs=[p for p in pairs if p["game"]["actual_margin"]!=0]
        errors=[(p["models"][name]["prob"]-float(p["game"]["actual_margin"]>0))**2-
                (p["models"]["market_close"]["prob"]-float(p["game"]["actual_margin"]>0))**2
                for p in pairs]
    else:
        actual="actual_margin" if metric=="margin" else "actual_total"
        errors=[abs(p["models"][name][metric]-p["game"][actual])-
                abs(p["models"]["market_close"][metric]-p["game"][actual]) for p in pairs]
    return {"n":len(errors),"mean_delta":round(_rate(errors),5),"bootstrap_ci95":_ci(errors)}


def edge_subsets(predictions, name="linear"):
    """Exploratory fixed bins, no selected wagering threshold."""
    result={}
    for kind in ("ats","totals"):
        buckets=defaultdict(list)
        for p in predictions:
            g=p["game"]
            if name not in p["models"]:continue
            line=g["spread_line"] if kind=="ats" else g["total_line"]
            if line is None:continue
            estimate=p["models"][name]["margin" if kind=="ats" else "total"]
            edge=estimate-line
            if edge==0:continue
            side=("home" if edge>0 else "away") if kind=="ats" else ("over" if edge>0 else "under")
            outcome=_grade_ats(side,g["actual_margin"],line) if kind=="ats" else _grade_total(side,g["actual_total"],line)
            if outcome=="push":continue
            win=float(outcome=="win")
            band="0-2" if abs(edge)<2 else ("2-4" if abs(edge)<4 else "4+")
            buckets[band].append(win)
            buckets["home_or_over" if edge>0 else "away_or_under"].append(win)
            buckets["weeks_1_4" if g["week"]<=4 else "weeks_5_plus"].append(win)
        result[kind]={key:{"n":len(values),"wins":int(sum(values)),
                          "hit_rate":round(_rate(values),4),"bootstrap_ci95":_ci(values)}
                      for key,values in sorted(buckets.items())}
    return result


def linear_importance(rows):
    """Coefficients fit through 2024; never fitted using the 2025 diagnostic."""
    train=[r for r in rows if r["season"]<=2024]
    margin,total,win=_models(train,FEATURES,"linear")
    return {name:sorted(({"feature":f,"standardized_coefficient":round(float(c),5)}
                         for f,c in zip(FEATURES,model[-1].coef_.ravel())),
                        key=lambda x:abs(x["standardized_coefficient"]),reverse=True)[:12]
            for name,model in (("margin",margin),("total",total),("win_log_odds",win))}


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--cache-dir",default="data/nflverse/pbp")
    parser.add_argument("--out",default="reports/nfl_model_v2/research_results.json")
    args=parser.parse_args()
    rows=build_dataset(Path(args.cache_dir))
    predictions=walk_forward(rows)
    dev=[p for p in predictions if p["game"]["season"] in {2023,2024}]
    full_dev=score(dev)["linear"]
    ablations={}
    for group,removed in GROUPS.items():
        reduced=tuple(f for f in FEATURES if f not in removed)
        candidate=score(walk_forward(rows,first_eval=2023,last_eval=2024,
                                     feature_names=reduced,families=("linear",)))["linear"]
        ablations[group]={"removed":list(removed),"margin_mae":candidate["margin_mae"],
                          "total_mae":candidate["total_mae"],"brier":candidate["brier"],
                          "delta_margin_mae":round(candidate["margin_mae"]-full_dev["margin_mae"],4),
                          "delta_total_mae":round(candidate["total_mae"]-full_dev["total_mae"],4),
                          "delta_brier":round(candidate["brier"]-full_dev["brier"],6)}
    report={"method":"expanding train, refit before every season/week; no same-week data",
            "features":list(FEATURES),"groups":{k:list(v) for k,v in GROUPS.items()},
            "by_season":{str(s):score(predictions,{s}) for s in (2023,2024,2025)},
            "pooled":score(predictions),"development_2023_2024":score(dev),
            "ablation_2023_2024":ablations,"importance_through_2024":linear_importance(rows),
            "paired_vs_market":{str(s):{name:{metric:paired_market_delta(
                [p for p in predictions if p["game"]["season"]==s],name,metric)
                for metric in ("margin","total","brier")}
                for name in ("linear","boosted","ensemble","rich_elo")}
                for s in (2023,2024,2025)},
            "exploratory_subsets_2023_2025":edge_subsets(predictions)}
    out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"rows":len(rows),"predictions":len(predictions),"out":str(out)}))


if __name__=="__main__":main()
