"""Deterministic demo fixtures.

Lines are DERIVED from the fixture team/player ratings plus a seeded
perturbation, so most demo markets sit near fair (the terminal correctly says
PASS) and a handful carry a real, discoverable edge. This keeps the analytics
pipeline honest when no live provider is configured.

Everything produced here is tagged is_demo=True and the UI labels it DEMO DATA.
"""
from __future__ import annotations

import datetime as dt
import random
from typing import Dict, List, Optional

from .base import (GolfProvider, InjuryProvider, NewsProvider, OddsProvider,
                   SearchProvider, StatsProvider, WeatherProvider)

SEED = 20260914
BOOKS = ["fanduel", "bet365", "draftkings", "caesars", "betmgm"]

# --------------------------------------------------------------------- ratings
# ppg scored / ppg allowed, opponent adjusted; pace = plays or possessions
NFL_TEAMS = {
    "Baltimore Ravens":   dict(abbr="BAL", off=27.4, deff=19.8, pace=64.0, epa_off=0.11, epa_def=-0.06, proe=1.5),
    "Cincinnati Bengals": dict(abbr="CIN", off=25.1, deff=23.6, pace=65.5, epa_off=0.08, epa_def=0.02, proe=4.0),
    "Kansas City Chiefs": dict(abbr="KC",  off=26.0, deff=18.9, pace=62.5, epa_off=0.09, epa_def=-0.07, proe=1.0),
    "Buffalo Bills":      dict(abbr="BUF", off=27.9, deff=20.6, pace=64.5, epa_off=0.12, epa_def=-0.04, proe=0.5),
    "San Francisco 49ers":dict(abbr="SF",  off=26.8, deff=19.2, pace=63.0, epa_off=0.10, epa_def=-0.06, proe=-2.0),
    "Dallas Cowboys":     dict(abbr="DAL", off=23.6, deff=22.9, pace=63.5, epa_off=0.04, epa_def=0.01, proe=2.0),
    "Detroit Lions":      dict(abbr="DET", off=28.6, deff=22.4, pace=65.0, epa_off=0.13, epa_def=0.00, proe=-1.0),
    "Green Bay Packers":  dict(abbr="GB",  off=24.8, deff=21.0, pace=63.8, epa_off=0.06, epa_def=-0.03, proe=0.0),
    "Philadelphia Eagles":dict(abbr="PHI", off=25.9, deff=20.1, pace=62.0, epa_off=0.08, epa_def=-0.05, proe=-3.0),
    "New York Giants":    dict(abbr="NYG", off=18.4, deff=25.1, pace=62.8, epa_off=-0.08, epa_def=0.05, proe=3.0),
    "Miami Dolphins":     dict(abbr="MIA", off=23.1, deff=23.8, pace=66.0, epa_off=0.03, epa_def=0.03, proe=1.0),
    "New York Jets":      dict(abbr="NYJ", off=19.2, deff=21.4, pace=62.0, epa_off=-0.05, epa_def=-0.02, proe=1.5),
}
CFB_TEAMS = {
    "Ohio State Buckeyes":  dict(abbr="OSU", off=38.5, deff=15.2, pace=70.0, sos=0.72),
    "Michigan Wolverines":  dict(abbr="MICH", off=31.0, deff=14.8, pace=66.0, sos=0.70),
    "Georgia Bulldogs":     dict(abbr="UGA", off=36.9, deff=16.1, pace=68.0, sos=0.74),
    "Alabama Crimson Tide": dict(abbr="ALA", off=34.2, deff=18.3, pace=69.0, sos=0.73),
    "Texas Longhorns":      dict(abbr="TEX", off=35.1, deff=17.6, pace=71.0, sos=0.71),
    "Oklahoma Sooners":     dict(abbr="OU",  off=29.8, deff=22.9, pace=72.0, sos=0.69),
    "Penn State Nittany Lions": dict(abbr="PSU", off=32.6, deff=15.9, pace=67.0, sos=0.66),
    "Illinois Fighting Illini": dict(abbr="ILL", off=26.4, deff=23.1, pace=68.5, sos=0.62),
}
NBA_TEAMS = {
    "Boston Celtics":      dict(abbr="BOS", ortg=122.1, drtg=110.6, pace=98.5),
    "Denver Nuggets":      dict(abbr="DEN", ortg=118.9, drtg=113.0, pace=97.8),
    "Oklahoma City Thunder":dict(abbr="OKC", ortg=120.4, drtg=108.9, pace=100.2),
    "Minnesota Timberwolves":dict(abbr="MIN", ortg=115.3, drtg=108.4, pace=97.0),
    "New York Knicks":     dict(abbr="NYK", ortg=118.2, drtg=112.7, pace=96.4),
    "Milwaukee Bucks":     dict(abbr="MIL", ortg=117.8, drtg=114.2, pace=99.1),
    "Los Angeles Lakers":  dict(abbr="LAL", ortg=115.9, drtg=114.8, pace=99.8),
    "Phoenix Suns":        dict(abbr="PHX", ortg=117.1, drtg=115.6, pace=98.9),
    "Dallas Mavericks":    dict(abbr="DAL", ortg=117.6, drtg=113.4, pace=97.2),
    "Golden State Warriors":dict(abbr="GSW", ortg=116.4, drtg=114.1, pace=101.0),
}

# mean / sd per game, plus usage context used by the prop models
NFL_PLAYERS = {
    "Joe Burrow":      dict(team="Cincinnati Bengals", pos="QB", props={"passing_yards": (268, 62), "passing_tds": (1.9, 1.1)}, usage=dict(snap_rate=0.99, games=6)),
    "Ja'Marr Chase":   dict(team="Cincinnati Bengals", pos="WR", props={"receiving_yards": (84, 34), "receptions": (6.6, 2.4)}, usage=dict(snap_rate=0.93, target_share=0.29, route_pct=0.91, games=6)),
    "Tee Higgins":     dict(team="Cincinnati Bengals", pos="WR", props={"receiving_yards": (58, 27), "receptions": (4.4, 2.0)}, usage=dict(snap_rate=0.86, target_share=0.19, route_pct=0.84, games=6)),
    "Lamar Jackson":   dict(team="Baltimore Ravens", pos="QB", props={"passing_yards": (223, 55), "rushing_yards": (56, 28)}, usage=dict(snap_rate=1.0, games=6)),
    "Derrick Henry":   dict(team="Baltimore Ravens", pos="RB", props={"rushing_yards": (98, 41), "receiving_yards": (12, 11)}, usage=dict(snap_rate=0.62, carry_share=0.68, games=6)),
    "Zay Flowers":     dict(team="Baltimore Ravens", pos="WR", props={"receiving_yards": (66, 30), "receptions": (5.1, 2.2)}, usage=dict(snap_rate=0.88, target_share=0.24, route_pct=0.86, games=6)),
    "Patrick Mahomes": dict(team="Kansas City Chiefs", pos="QB", props={"passing_yards": (256, 58)}, usage=dict(snap_rate=1.0, games=6)),
    "Josh Allen":      dict(team="Buffalo Bills", pos="QB", props={"passing_yards": (241, 60), "rushing_yards": (38, 22)}, usage=dict(snap_rate=1.0, games=6)),
    "Jahmyr Gibbs":    dict(team="Detroit Lions", pos="RB", props={"rushing_yards": (76, 33), "receiving_yards": (28, 18)}, usage=dict(snap_rate=0.55, carry_share=0.52, games=6)),
    "Amon-Ra St. Brown":dict(team="Detroit Lions", pos="WR", props={"receiving_yards": (82, 31), "receptions": (7.1, 2.3)}, usage=dict(snap_rate=0.92, target_share=0.27, route_pct=0.90, games=6)),
}
NBA_PLAYERS = {
    "Jayson Tatum":       dict(team="Boston Celtics", props={"points": (27.4, 7.9), "rebounds": (8.3, 3.1), "assists": (4.8, 2.2), "threes": (3.3, 1.8)}, usage=dict(minutes=36.2, usage_rate=0.31, games=9)),
    "Nikola Jokic":       dict(team="Denver Nuggets", props={"points": (27.1, 8.4), "rebounds": (12.3, 3.7), "assists": (9.1, 3.3)}, usage=dict(minutes=35.0, usage_rate=0.30, games=9)),
    "Shai Gilgeous-Alexander": dict(team="Oklahoma City Thunder", props={"points": (31.2, 8.1), "assists": (6.3, 2.6)}, usage=dict(minutes=34.8, usage_rate=0.34, games=9)),
    "Anthony Edwards":    dict(team="Minnesota Timberwolves", props={"points": (26.8, 8.6), "threes": (3.9, 2.0)}, usage=dict(minutes=35.4, usage_rate=0.32, games=9)),
    "Jalen Brunson":      dict(team="New York Knicks", props={"points": (26.2, 7.7), "assists": (6.9, 2.8)}, usage=dict(minutes=35.1, usage_rate=0.33, games=9)),
    "Giannis Antetokounmpo": dict(team="Milwaukee Bucks", props={"points": (30.4, 7.4), "rebounds": (11.6, 3.4)}, usage=dict(minutes=34.2, usage_rate=0.35, games=9)),
}

# SG per round vs field, plus fit descriptors
GOLF_FIELD = {
    "Scottie Scheffler": dict(sg_total=2.42, ott=0.92, app=1.21, arg=0.24, putt=0.05, form=0.90, course_fit=0.86, history=0.80),
    "Rory McIlroy":      dict(sg_total=1.94, ott=1.05, app=0.62, arg=0.10, putt=0.17, form=0.78, course_fit=0.81, history=0.72),
    "Xander Schauffele": dict(sg_total=1.81, ott=0.62, app=0.74, arg=0.19, putt=0.26, form=0.80, course_fit=0.77, history=0.68),
    "Collin Morikawa":   dict(sg_total=1.62, ott=0.41, app=1.02, arg=0.14, putt=0.05, form=0.72, course_fit=0.83, history=0.66),
    "Ludvig Aberg":      dict(sg_total=1.58, ott=0.88, app=0.55, arg=0.08, putt=0.07, form=0.75, course_fit=0.74, history=0.40),
    "Viktor Hovland":    dict(sg_total=1.34, ott=0.58, app=0.71, arg=-0.05, putt=0.10, form=0.62, course_fit=0.70, history=0.61),
    "Tommy Fleetwood":   dict(sg_total=1.29, ott=0.44, app=0.62, arg=0.15, putt=0.08, form=0.70, course_fit=0.72, history=0.74),
    "Patrick Cantlay":   dict(sg_total=1.26, ott=0.39, app=0.55, arg=0.12, putt=0.20, form=0.60, course_fit=0.69, history=0.70),
    "Hideki Matsuyama":  dict(sg_total=1.18, ott=0.28, app=0.79, arg=0.16, putt=-0.05, form=0.66, course_fit=0.75, history=0.63),
    "Justin Thomas":     dict(sg_total=1.12, ott=0.46, app=0.58, arg=0.06, putt=0.02, form=0.64, course_fit=0.71, history=0.69),
    "Wyndham Clark":     dict(sg_total=0.94, ott=0.61, app=0.31, arg=0.00, putt=0.02, form=0.52, course_fit=0.66, history=0.45),
    "Sungjae Im":        dict(sg_total=0.91, ott=0.22, app=0.49, arg=0.11, putt=0.09, form=0.58, course_fit=0.64, history=0.58),
    "Russell Henley":    dict(sg_total=0.88, ott=0.10, app=0.61, arg=0.09, putt=0.08, form=0.61, course_fit=0.78, history=0.71),
    "Sahith Theegala":   dict(sg_total=0.79, ott=0.31, app=0.24, arg=0.18, putt=0.06, form=0.55, course_fit=0.62, history=0.44),
    "Corey Conners":     dict(sg_total=0.76, ott=0.44, app=0.52, arg=-0.08, putt=-0.12, form=0.53, course_fit=0.68, history=0.57),
    "Tony Finau":        dict(sg_total=0.71, ott=0.42, app=0.29, arg=0.04, putt=-0.04, form=0.48, course_fit=0.60, history=0.62),
    "Max Homa":          dict(sg_total=0.62, ott=0.24, app=0.25, arg=0.09, putt=0.04, form=0.41, course_fit=0.58, history=0.55),
    "Cameron Young":     dict(sg_total=0.69, ott=0.71, app=0.14, arg=-0.05, putt=-0.11, form=0.50, course_fit=0.63, history=0.38),
    "Jordan Spieth":     dict(sg_total=0.58, ott=0.18, app=0.21, arg=0.24, putt=-0.05, form=0.44, course_fit=0.59, history=0.72),
    "Shane Lowry":       dict(sg_total=0.66, ott=0.21, app=0.38, arg=0.07, putt=0.00, form=0.51, course_fit=0.67, history=0.60),
    "Akshay Bhatia":     dict(sg_total=0.51, ott=0.28, app=0.16, arg=0.05, putt=0.02, form=0.46, course_fit=0.55, history=0.30),
    "Si Woo Kim":        dict(sg_total=0.42, ott=0.14, app=0.22, arg=0.06, putt=0.00, form=0.38, course_fit=0.52, history=0.48),
    "Keegan Bradley":    dict(sg_total=0.47, ott=0.26, app=0.27, arg=0.01, putt=-0.07, form=0.42, course_fit=0.61, history=0.53),
    "Brian Harman":      dict(sg_total=0.39, ott=0.02, app=0.24, arg=0.08, putt=0.05, form=0.36, course_fit=0.57, history=0.50),
    "J.T. Poston":       dict(sg_total=0.31, ott=0.09, app=0.14, arg=0.04, putt=0.04, form=0.33, course_fit=0.49, history=0.35),
}

VENUES = {
    "Baltimore Ravens": ("M&T Bank Stadium, Baltimore MD", 39.278, -76.623, False),
    "Cincinnati Bengals": ("Paycor Stadium, Cincinnati OH", 39.095, -84.516, False),
    "Kansas City Chiefs": ("GEHA Field at Arrowhead, Kansas City MO", 39.049, -94.484, False),
    "Buffalo Bills": ("Highmark Stadium, Orchard Park NY", 42.774, -78.787, False),
    "Detroit Lions": ("Ford Field, Detroit MI", 42.340, -83.046, True),
    "Green Bay Packers": ("Lambeau Field, Green Bay WI", 44.501, -88.062, False),
    "Dallas Cowboys": ("AT&T Stadium, Arlington TX", 32.747, -97.093, True),
    "New York Jets": ("MetLife Stadium, East Rutherford NJ", 40.814, -74.074, False),
    "Philadelphia Eagles": ("Lincoln Financial Field, Philadelphia PA", 39.901, -75.168, False),
    "Miami Dolphins": ("Hard Rock Stadium, Miami Gardens FL", 25.958, -80.239, False),
}

NFL_SLATE = [("Cincinnati Bengals", "Baltimore Ravens", 0), ("Buffalo Bills", "Kansas City Chiefs", 0),
             ("Detroit Lions", "Green Bay Packers", 0), ("Philadelphia Eagles", "Dallas Cowboys", 1),
             ("New York Jets", "Miami Dolphins", 0), ("San Francisco 49ers", "New York Giants", 0)]
CFB_SLATE = [("Ohio State Buckeyes", "Michigan Wolverines", -1), ("Georgia Bulldogs", "Alabama Crimson Tide", -1),
             ("Texas Longhorns", "Oklahoma Sooners", -1), ("Penn State Nittany Lions", "Illinois Fighting Illini", -1)]
NBA_SLATE = [("Boston Celtics", "New York Knicks", 0), ("Denver Nuggets", "Minnesota Timberwolves", 0),
             ("Oklahoma City Thunder", "Dallas Mavericks", 0), ("Los Angeles Lakers", "Phoenix Suns", 0),
             ("Milwaukee Bucks", "Golden State Warriors", 1)]

HFA = {"NFL": 1.7, "CFB": 2.6, "NBA": 2.2}


def _rng(*parts) -> random.Random:
    return random.Random(hash((SEED,) + tuple(parts)) & 0xFFFFFFFF)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _kickoff(day_offset: int, hour: int) -> str:
    base = (_now() + dt.timedelta(days=max(0, day_offset))).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    if base < _now():
        base += dt.timedelta(days=1)
    return base.isoformat()


def _eid(sport: str, home: str, away: str) -> str:
    return f"demo-{sport.lower()}-{away.split()[-1].lower()}-at-{home.split()[-1].lower()}"


def _proj(sport: str, home: str, away: str):
    t = {"NFL": NFL_TEAMS, "CFB": CFB_TEAMS}[sport]
    h, a = t[home], t[away]
    hfa = HFA[sport]
    ph = (h["off"] + a["deff"]) / 2 + hfa / 2
    pa = (a["off"] + h["deff"]) / 2 - hfa / 2
    return ph, pa


def _nba_proj(home: str, away: str):
    h, a = NBA_TEAMS[home], NBA_TEAMS[away]
    pace = (h["pace"] + a["pace"]) / 2
    lg = 114.5
    ph = (h["ortg"] + a["drtg"]) / 2 / 100 * pace + HFA["NBA"] / 2
    pa = (a["ortg"] + h["drtg"]) / 2 / 100 * pace - HFA["NBA"] / 2
    return ph, pa


def _price_variants(fair_prob: float, rng: random.Random, vig: float = 0.045) -> Dict[str, int]:
    """Per-book American prices around a fair probability, with a little spread."""
    from ..pricing import prob_to_american
    out = {}
    for b in BOOKS:
        jitter = rng.uniform(-0.016, 0.016) + (0.006 if b in ("draftkings", "betmgm") else 0.0)
        p = min(0.97, max(0.03, fair_prob + vig / 2 + jitter))
        out[b] = prob_to_american(p)
    return out


class DemoOdds(OddsProvider):
    name = "demo-odds"
    is_demo = True

    def list_events(self, sport: str) -> List[Dict]:
        out = []
        if sport == "NFL":
            for i, (home, away, d) in enumerate(NFL_SLATE):
                venue, lat, lon, dome = VENUES.get(home, ("TBD", 39.0, -77.0, False))
                out.append(dict(id=_eid("NFL", home, away), sport="NFL", league="NFL",
                                name=f"{away} @ {home}", home_team=home, away_team=away,
                                start_time=_kickoff(d, 17 + (i % 4)), venue=venue,
                                meta=dict(lat=lat, lon=lon, indoor=dome, demo=True)))
        elif sport == "CFB":
            for i, (home, away, d) in enumerate(CFB_SLATE):
                out.append(dict(id=_eid("CFB", home, away), sport="CFB", league="NCAAF",
                                name=f"{away} @ {home}", home_team=home, away_team=away,
                                start_time=_kickoff(max(0, d + 1), 16 + (i % 5)),
                                venue=f"{home.split()[0]} Stadium",
                                meta=dict(lat=40.0 + i, lon=-83.0 - i, indoor=False, demo=True)))
        elif sport == "NBA":
            for i, (home, away, d) in enumerate(NBA_SLATE):
                out.append(dict(id=_eid("NBA", home, away), sport="NBA", league="NBA",
                                name=f"{away} @ {home}", home_team=home, away_team=away,
                                start_time=_kickoff(d, 23 + (i % 2) - 1),
                                venue=f"{home.split()[-1]} Arena",
                                meta=dict(indoor=True, demo=True)))
        elif sport == "PGA":
            out.append(dict(id="demo-pga-fall-championship", sport="PGA", league="PGA TOUR",
                            name="EdgeTerminal Fall Championship", start_time=_kickoff(1, 12),
                            venue="Crestline National GC (par 71, 7,312y, bermuda)",
                            meta=dict(course_length=7312, grass="bermuda", greens="bermuda",
                                      round_state="pre-tournament", field=len(GOLF_FIELD), demo=True)))
        return out

    # -------------------------------------------------------------- odds rows
    def fetch_odds(self, sport: str) -> List[Dict]:
        rows: List[Dict] = []
        if sport == "PGA":
            return self._golf_odds()
        for ev in self.list_events(sport):
            home, away = ev["home_team"], ev["away_team"]
            rng = _rng(sport, ev["id"])
            if sport == "NBA":
                ph, pa = _nba_proj(home, away)
                sd_margin, sd_total = 11.6, 16.5
            else:
                ph, pa = _proj(sport, home, away)
                sd_margin = 13.2 if sport == "NFL" else 16.5
                sd_total = 10.4 if sport == "NFL" else 13.5
            margin, total = ph - pa, ph + pa
            # book line = fair line, nudged; ~1 market in 4 carries real mispricing
            nudge = rng.choice([0, 0, 0, 0.5, 1.0, -1.0, 1.5, -0.5])
            book_spread = round((-margin + nudge) * 2) / 2
            total_nudge = rng.choice([0, 0, 0, 1.0, -1.5, 2.0, -1.0])
            book_total = round((total + total_nudge) * 2) / 2
            from ..pricing import norm_cdf
            p_home_ml = 1 - norm_cdf((0 - margin) / sd_margin)
            ml_nudge = rng.uniform(-0.03, 0.03)
            for book in BOOKS:
                brng = _rng(ev["id"], book)
                sp = book_spread + brng.choice([0, 0, 0, 0.5, -0.5])
                pr = _price_variants(0.5, brng)[book]
                rows += [
                    dict(event_id=ev["id"], sport=sport, book=book, market="spread",
                         selection=home, line=sp, price=pr),
                    dict(event_id=ev["id"], sport=sport, book=book, market="spread",
                         selection=away, line=-sp, price=_price_variants(0.5, brng)[book]),
                ]
                tl = book_total + brng.choice([0, 0, 0.5, -0.5, 1.0])
                rows += [
                    dict(event_id=ev["id"], sport=sport, book=book, market="total",
                         selection="Over", line=tl, price=_price_variants(0.5, brng)[book]),
                    dict(event_id=ev["id"], sport=sport, book=book, market="total",
                         selection="Under", line=tl, price=_price_variants(0.5, brng)[book]),
                ]
                mlp = min(0.95, max(0.05, p_home_ml + ml_nudge))
                rows += [
                    dict(event_id=ev["id"], sport=sport, book=book, market="moneyline",
                         selection=home, line=None, price=_price_variants(mlp, brng)[book]),
                    dict(event_id=ev["id"], sport=sport, book=book, market="moneyline",
                         selection=away, line=None, price=_price_variants(1 - mlp, brng)[book]),
                ]
            rows += self._props(sport, ev, rng)
        return rows

    def _props(self, sport: str, ev: Dict, rng: random.Random) -> List[Dict]:
        rows: List[Dict] = []
        pool = NFL_PLAYERS if sport == "NFL" else (NBA_PLAYERS if sport == "NBA" else {})
        for pname, pdata in pool.items():
            if pdata["team"] not in (ev["home_team"], ev["away_team"]):
                continue
            for prop, (mean, sd) in pdata["props"].items():
                prng = _rng(ev["id"], pname, prop)
                shift = prng.choice([0, 0, 0, 0.035, -0.04, 0.06, -0.025]) * mean
                raw = mean + shift
                # yardage lines land on .5; low-count props step by 0.5 too
                line = (round(raw) + 0.5 if mean > 20 else round(raw * 2) / 2 + 0.5)
                for book in BOOKS:
                    brng = _rng(ev["id"], pname, prop, book)
                    bl = line + (0.5 if brng.random() < 0.18 else 0) * (1 if brng.random() < .5 else -1)
                    for side in ("Over", "Under"):
                        rows.append(dict(event_id=ev["id"], sport=sport, book=book,
                                         market="player_prop",
                                         selection=f"{pname} {side} {prop}",
                                         line=bl, price=_price_variants(0.5, brng)[book]))
        return rows

    def _golf_odds(self) -> List[Dict]:
        from ..analytics.golf import field_probabilities
        from ..pricing import prob_to_american
        ev = self.list_events("PGA")[0]
        probs = field_probabilities(GOLF_FIELD)
        rows: List[Dict] = []
        markets = [("outright", "win", 1.22), ("top5", "top5", 1.13),
                   ("top10", "top10", 1.10), ("top20", "top20", 1.08)]
        for market, key, overround in markets:
            for player, pr in probs.items():
                p = pr[key]
                prng = _rng("golf", player, market)
                for book in BOOKS:
                    brng = _rng("golf", player, market, book)
                    adj = min(0.95, max(0.002, p * overround * brng.uniform(0.86, 1.16)))
                    rows.append(dict(event_id=ev["id"], sport="PGA", book=book, market=market,
                                     selection=player, line=None, price=prob_to_american(adj)))
        # a few tournament matchups
        names = list(GOLF_FIELD)
        for i in range(0, 8, 2):
            a, b = names[i], names[i + 1]
            pa = probs[a]["skill"]; pb = probs[b]["skill"]
            p = pa / (pa + pb)
            for book in BOOKS:
                brng = _rng("golfmatch", a, b, book)
                rows.append(dict(event_id=ev["id"], sport="PGA", book=book, market="matchup",
                                 selection=f"{a} vs {b}: {a}", line=None,
                                 price=_price_variants(p, brng)[book]))
                rows.append(dict(event_id=ev["id"], sport="PGA", book=book, market="matchup",
                                 selection=f"{a} vs {b}: {b}", line=None,
                                 price=_price_variants(1 - p, brng)[book]))
        return rows


class DemoStats(StatsProvider):
    name = "demo-stats"
    is_demo = True

    def team_stats(self, sport: str) -> Dict[str, Dict]:
        src = {"NFL": NFL_TEAMS, "CFB": CFB_TEAMS, "NBA": NBA_TEAMS}.get(sport, {})
        out = {}
        for team, d in src.items():
            row = dict(d)
            row.update(team=team, games=6 if sport != "NBA" else 9, source="demo-fixture")
            if sport == "NBA":
                row.update(points_for=d["ortg"] * d["pace"] / 100,
                           points_against=d["drtg"] * d["pace"] / 100,
                           off_rating=d["ortg"], def_rating=d["drtg"])
            else:
                row.update(points_for=d["off"], points_against=d["deff"])
            out[team] = row
        return out

    def player_stats(self, sport: str, team: str) -> List[Dict]:
        pool = NFL_PLAYERS if sport == "NFL" else (NBA_PLAYERS if sport == "NBA" else {})
        return [dict(name=n, source="demo-fixture", **d) for n, d in pool.items() if d["team"] == team]


class DemoInjuries(InjuryProvider):
    name = "demo-injuries"
    is_demo = True

    DATA = {
        "NFL": [
            dict(team="Baltimore Ravens", player="Marlon Humphrey", position="CB", status="Questionable", detail="Hamstring - limited Wednesday/Thursday"),
            dict(team="Cincinnati Bengals", player="Tee Higgins", position="WR", status="Questionable", detail="Quad - trending toward playing per beat report"),
            dict(team="Kansas City Chiefs", player="Isiah Pacheco", position="RB", status="Out", detail="Fibula - on IR"),
            dict(team="New York Giants", player="Malik Nabers", position="WR", status="Doubtful", detail="Concussion protocol, no full practice"),
        ],
        "NBA": [
            dict(team="Denver Nuggets", player="Jamal Murray", position="G", status="Questionable", detail="Calf - game-time decision"),
            dict(team="Phoenix Suns", player="Bradley Beal", position="G", status="Out", detail="Hamstring"),
            dict(team="Los Angeles Lakers", player="LeBron James", position="F", status="Probable", detail="Rest management, 2nd of back-to-back"),
        ],
        "CFB": [
            dict(team="Oklahoma Sooners", player="Starting LT", position="OL", status="Questionable", detail="Ankle - limited availability reporting in CFB"),
        ],
        "PGA": [],
    }

    def injuries(self, sport: str, team: Optional[str] = None) -> List[Dict]:
        rows = [dict(r, source="demo-fixture") for r in self.DATA.get(sport, [])]
        return [r for r in rows if not team or r["team"] == team]


class DemoNews(NewsProvider):
    name = "demo-news"
    is_demo = True

    ITEMS = {
        "NFL": [
            dict(title="Bengals expect Tee Higgins to play through quad soreness", publisher="Beat reporter (demo)", trust="reported", summary="Higgins logged a limited practice Friday; staff expects a normal snap share."),
            dict(title="Ravens list Humphrey questionable; secondary already thin", publisher="Team injury report (demo)", trust="official", summary="Official Friday report lists Humphrey questionable with a hamstring."),
            dict(title="Wind advisory possible for Sunday afternoon window in the Northeast", publisher="Weather desk (demo)", trust="reported", summary="Forecast models disagree on gust timing."),
        ],
        "NBA": [
            dict(title="Murray a true game-time call, Nuggets say", publisher="Team beat (demo)", trust="reported", summary="Coach declined to commit before shootaround."),
        ],
        "CFB": [
            dict(title="Oklahoma OL reshuffle expected in Red River matchup", publisher="Conference beat (demo)", trust="reported", summary="Availability reporting is thinner than NFL; treat as uncertain."),
        ],
        "PGA": [
            dict(title="Gusts of 15-22mph expected for Thursday morning wave", publisher="Tournament weather (demo)", trust="reported", summary="Afternoon wave may see calmer conditions."),
        ],
    }

    def news(self, sport: str, query: Optional[str] = None) -> List[Dict]:
        items = [dict(i, url=None, published=_now().isoformat()) for i in self.ITEMS.get(sport, [])]
        if query:
            ql = query.lower()
            hit = [i for i in items if ql.split()[0] in i["title"].lower()]
            return hit or items
        return items


class DemoWeather(WeatherProvider):
    name = "demo-weather"
    is_demo = True

    def forecast(self, lat: float, lon: float, when_iso: Optional[str] = None) -> Dict:
        rng = _rng("wx", round(lat, 2), round(lon, 2))
        return dict(temp_f=round(rng.uniform(38, 74), 1), wind_mph=round(rng.uniform(3, 19), 1),
                    wind_dir=rng.choice(["N", "NW", "W", "SW", "S"]),
                    precip_chance=round(rng.uniform(0, 45), 1),
                    conditions=rng.choice(["Clear", "Partly cloudy", "Overcast", "Light rain"]),
                    indoor=False, source="demo-fixture")


class DemoGolf(GolfProvider):
    name = "demo-golf"
    is_demo = True

    def tournament(self) -> Optional[Dict]:
        ev = DemoOdds().list_events("PGA")[0]
        return dict(ev, field=list(GOLF_FIELD), leaderboard=[])

    def field_stats(self) -> Dict[str, Dict]:
        return {k: dict(v, source="demo-fixture") for k, v in GOLF_FIELD.items()}


class DemoSearch(SearchProvider):
    name = "demo-search"
    is_demo = True

    def search(self, query: str, limit: int = 5) -> List[Dict]:
        return [dict(title=f"[demo] no live web search configured for: {query}",
                     url=None, snippet="Set SEARCH_PROVIDER and disable DEMO_MODE for live research.",
                     publisher="demo", trust="unverified")]
