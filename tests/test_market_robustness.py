import time

from edgeterminal import engine
from edgeterminal.config import settings


def test_selected_quote_staleness_is_not_hidden_by_fresh_event_quote():
    stale_at = time.time() - settings.stale_odds_s - 1
    fresh_at = time.time()
    assert engine.quote_is_stale(stale_at) is True
    assert engine.quote_is_stale(fresh_at) is False


def test_consensus_fair_never_blends_different_lines():
    rows = [
        {"event_id": "g", "market": "total", "selection": "Over", "line": 44.5, "price": -110},
        {"event_id": "g", "market": "total", "selection": "Under", "line": 44.5, "price": -110},
        {"event_id": "g", "market": "total", "selection": "Over", "line": 47.5, "price": 105},
        {"event_id": "g", "market": "total", "selection": "Under", "line": 47.5, "price": -125},
    ]
    fair_44 = engine._consensus_fair(rows, "total", "g", "Over", 44.5)
    fair_47 = engine._consensus_fair(rows, "total", "g", "Over", 47.5)
    assert fair_44 == 0.5
    assert fair_47 != fair_44


def test_consensus_fair_requires_paired_outcomes_at_same_line():
    rows = [
        {"event_id": "g", "market": "spread", "selection": "A", "line": -3.5, "price": -110},
        {"event_id": "g", "market": "spread", "selection": "B", "line": 4.5, "price": -110},
    ]
    assert engine._consensus_fair(rows, "spread", "g", "A", -3.5) is None
