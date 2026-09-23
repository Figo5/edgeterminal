"""NFL game-market quote validation and one spread orientation convention."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from .pricing import american_to_prob
from .providers.normalize import normalize_team


def nflverse_to_home_spread(spread_line: float) -> float:
    """Convert nflverse's positive-home-favorite line to book home handicap."""
    return -float(spread_line)


def home_spread(row: Dict, event: Dict) -> Optional[float]:
    """Negative means the home side lays points (HOME -3.5)."""
    if row.get("market") != "spread" or row.get("line") is None:
        return None
    side = normalize_team(row.get("selection"))
    home, away = event.get("home_team"), event.get("away_team")
    if side not in (home, away):
        return None
    try:
        value = float(row["line"])
    except (TypeError, ValueError):
        return None
    return value if side == home else -value


def valid_game_quotes(rows: Iterable[Dict], event: Dict) -> List[Dict]:
    """Reject malformed and contradictory book markets without inventing sides.

    Provider odds fields are normalized before the model sees them. A single
    sided quote may be retained, but a book with contradictory paired points
    is dropped for that market until a fresh coherent observation arrives.
    """
    groups = defaultdict(list)
    for original in rows:
        row = dict(original)
        market, side = row.get("market"), row.get("selection")
        if market not in {"spread", "total", "moneyline"}:
            continue
        if market in {"spread", "moneyline"}:
            side = normalize_team(side)
            if side not in (event.get("home_team"), event.get("away_team")):
                continue
            row["selection"] = side
        if market == "total" and side not in {"Over", "Under"}:
            continue
        try:
            price = float(row["price"])
            line = float(row["line"]) if row.get("line") is not None else None
        except (TypeError, ValueError):
            continue
        if -100 < price < 100 or price == 0:
            continue
        if market == "moneyline" and line is not None:
            continue
        if market == "spread" and (line is None or abs(line) > 40):
            continue
        if market == "total" and (line is None or line < 10 or line > 100):
            continue
        row["price"], row["line"] = price, line
        groups[(row.get("event_id"), row.get("book"), market)].append(row)
    accepted: List[Dict] = []
    for (_game_id, _book, market), group in groups.items():
        if market == "spread":
            orientations = {round(home_spread(r, event), 3) for r in group}
            if len(orientations) > 1:
                continue
        elif market == "total":
            points = {round(r["line"], 3) for r in group}
            if len(points) > 1:
                continue
        accepted.extend(group)
    return accepted


def board_rows(rows: Iterable[Dict], event: Dict) -> List[Dict]:
    """One row per (market, selection): the best price ON the consensus line.

    Prices are only comparable at the same point, so an alternate spread or
    total posted by one book is not presented as a competing best price on the
    board's line. The consensus line is the most commonly quoted point; books
    that disagree are excluded from the row, never blended into it.
    """
    groups = defaultdict(list)
    for original in rows:
        row = dict(original)
        if row.get("price") is None:
            continue
        try:
            float(row["price"])
        except (TypeError, ValueError):
            continue
        groups[(row.get("market"), row.get("selection"))].append(row)

    out: List[Dict] = []
    for (market, selection), group in groups.items():
        if market == "moneyline":
            line: Optional[float] = None
        else:
            counts: Dict = defaultdict(int)
            for r in group:
                counts[r.get("line")] += 1
            # Most common point; ties resolve to the lower line deterministically.
            line = sorted(counts, key=lambda l: (-counts[l], l or 0.0))[0]
        at_line = [r for r in group if bool(r.get("line") is not None) == (line is not None)
                   and (r.get("line") == line)]
        if not at_line:
            continue
        best = min(at_line, key=lambda r: american_to_prob(r["price"]))
        out.append(dict(
            market=market, selection=selection,
            book=best["book"], price=best["price"], line=line,
            captured_at=best.get("captured_at"), source=best.get("source"),
            provider_updated_at=best.get("provider_updated_at"),
            books_quoted=len({r["book"] for r in at_line}),
            line_rows=len(at_line), lines_seen=len({r.get("line") for r in group})))
    return out
