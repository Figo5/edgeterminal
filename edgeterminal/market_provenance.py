"""Observed NFL quotes, source-aware close labels, and research-only consensus.

The ESPN JSON field named ``close`` is an offer field, not a timestamp. Every
live retrieval is ``current`` until a later, verified pre-kickoff observation
supports a closing comparison. Consensus is never fed into the NFL model.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import median
from typing import Dict, Iterable, Optional

from .providers.normalize import normalize_book, normalize_team

MARKETS = {"h2h": "moneyline", "moneyline": "moneyline",
           "spreads": "spread", "spread": "spread",
           "totals": "total", "total": "total"}
CLOSE_WINDOW_S = 15 * 60


def _stamp(row: Dict) -> Optional[float]:
    try:
        value = row.get("fetched_at")
        if value is None:
            value = row.get("captured_at")
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def normalize_quote(row: Dict, event: Dict, provider: str,
                    kickoff: Optional[float] = None) -> Dict:
    """Canonicalize a provider's one-sided quote without changing its price."""
    out = dict(row)
    market = MARKETS.get(str(row.get("market") or "").lower())
    if market is None:
        raise ValueError("unsupported NFL game market")
    selection = row.get("selection")
    if market in {"spread", "moneyline"}:
        selection = normalize_team(selection)
        if selection not in {event.get("home_team"), event.get("away_team")}:
            raise ValueError("selection does not match game")
    else:
        selection = str(selection or "").title()
        if selection not in {"Over", "Under"}:
            raise ValueError("invalid total side")
    try:
        price = float(row["price"])
        line = float(row["line"]) if row.get("line") is not None else None
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("invalid line or price") from exc
    if not math.isfinite(price) or (-100 < price < 100):
        raise ValueError("invalid American price")
    if market == "moneyline" and line is not None:
        raise ValueError("moneyline must not have a point line")
    if market == "spread" and (line is None or not math.isfinite(line) or abs(line) > 40):
        raise ValueError("invalid spread")
    if market == "total" and (line is None or not math.isfinite(line) or not 10 <= line <= 100):
        raise ValueError("invalid total")
    fetched = _stamp(row)
    out.update(book=normalize_book(row.get("book")),market=market,
               selection=selection,line=line,price=price,provider=provider,
               fetched_at=fetched,kickoff_utc=kickoff,
               quote_phase=("current" if fetched is not None and
                            kickoff is not None and fetched < kickoff else "unclassified"))
    return out


def closing_observation(rows: Iterable[Dict], kickoff: float, book: str,
                        market: str, selection: str, window_s: float = CLOSE_WINDOW_S) -> Optional[Dict]:
    """Return a timestamp-supported same-book close, never a provider's label.

    A quote must have been fetched within 15 minutes strictly before kickoff.
    This is an *observed* close; it does not prove execution at that price.
    """
    wanted_book = normalize_book(book)
    wanted_market = MARKETS.get(market, market)
    wanted_selection = (normalize_team(selection) if wanted_market in {"spread", "moneyline"}
                        else str(selection).title())
    eligible=[]
    for row in rows:
        stamp=_stamp(row)
        if (stamp is None or row.get("quote_phase") == "unclassified" or
                not kickoff-window_s <= stamp < kickoff or
                row.get("is_demo") or normalize_book(row.get("book")) != wanted_book or
                MARKETS.get(str(row.get("market")),row.get("market")) != wanted_market):
            continue
        side=(normalize_team(row.get("selection")) if wanted_market in {"spread","moneyline"}
              else str(row.get("selection") or "").title())
        if side==wanted_selection:
            eligible.append((stamp,row))
    if not eligible:
        return None
    stamp,chosen=max(eligible,key=lambda item:item[0])
    return dict(chosen,fetched_at=stamp,quote_phase="closing",
                closing_basis="latest observed same-book quote within 15m before kickoff")


def _implied(price: float) -> float:
    return -price/(100-price) if price < 0 else 100/(price+100)


def consensus(rows: Iterable[Dict], event: Dict, as_of: float,
              max_age_s: float = 300) -> Dict:
    """Median fresh lines across distinct books; independent of model inputs."""
    by_book=defaultdict(list)
    for raw in rows:
        try:
            row=normalize_quote(raw,event,raw.get("provider") or raw.get("source") or "unknown",
                                raw.get("kickoff_utc"))
        except ValueError:
            continue
        stamp=row["fetched_at"]
        if (stamp is None or not as_of-max_age_s <= stamp <= as_of or
                (row.get("kickoff_utc") is not None and stamp >= row["kickoff_utc"]) or
                row.get("is_demo")):
            continue
        by_book[(row["book"],row["market"],row["selection"])].append(row)
    latest={key:max(group,key=lambda row:row["fetched_at"]) for key,group in by_book.items()}
    home,away=event["home_team"],event["away_team"]
    spread,totals,ml=[],[],[]
    for book in sorted({key[0] for key in latest}):
        h=latest.get((book,"spread",home))
        a=latest.get((book,"spread",away))
        if h and a and abs(h["fetched_at"]-a["fetched_at"]) <= 1 and abs(h["line"]+a["line"]) > .001:
            h=a=None
        if h or a:
            chosen=(h if not a or h and h["fetched_at"]>=a["fetched_at"] else a)
            spread.append((book,chosen["line"] if chosen["selection"]==home else -chosen["line"]))
        over=latest.get((book,"total","Over"))
        under=latest.get((book,"total","Under"))
        if (over and under and abs(over["fetched_at"]-under["fetched_at"]) <= 1
                and abs(over["line"]-under["line"]) > .001):
            over=under=None
        if over or under:
            chosen=(over if not under or over and over["fetched_at"]>=under["fetched_at"] else under)
            totals.append((book,chosen["line"]))
        hm=latest.get((book,"moneyline",home))
        am=latest.get((book,"moneyline",away))
        if hm and am:
            ph,pa=_implied(hm["price"]),_implied(am["price"])
            ml.append((book,ph/(ph+pa)))
    output={"as_of":as_of,"max_age_s":max_age_s}
    if spread:
        output["spread"]={"home_handicap":median(x for _,x in spread),
                          "n_books":len(spread),"books":[book for book,_ in spread]}
    if totals:
        output["total"]={"line":median(x for _,x in totals),
                         "n_books":len(totals),"books":[book for book,_ in totals]}
    if ml:
        output["moneyline"]={"no_vig_home_probability":median(x for _,x in ml),
                             "n_books":len(ml),"books":[book for book,_ in ml]}
    return output
