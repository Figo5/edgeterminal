import math

import pytest

from edgeterminal.confidence import ConfidenceInputs, score
from edgeterminal.parlay import combined_probability, evaluate
from edgeterminal.pricing import (
    UNIT_LADDER,
    american_to_prob,
    clv_pct,
    edge_pct,
    line_move,
    prob_to_american,
    recommend_units,
)
from edgeterminal.providers.theoddsapi import TheOddsApi


def test_american_odds_to_implied_probability():
    assert american_to_prob(-110) == pytest.approx(0.5238095)
    assert american_to_prob(150) == pytest.approx(0.4)
    with pytest.raises(ValueError):
        american_to_prob(0)


def test_probability_to_fair_american_odds():
    assert prob_to_american(0.5) == -100
    assert prob_to_american(0.6) == -150
    assert prob_to_american(0.4) == 150


def test_edge_is_model_probability_less_market_implied_probability():
    assert edge_pct(0.60, -110) == pytest.approx(7.6190476)


def test_units_are_supported_and_capped_at_five():
    for confidence in (0, 64, 65, 80, 100, 500):
        units = recommend_units(confidence, 50, "moneyline", 1, 1)
        assert units == 0 or units in UNIT_LADDER
        assert units <= 5
    assert recommend_units(64, 10, "spread", 1, 1) == 0
    assert recommend_units(90, -1, "spread", 1, 1) == 0


def test_confidence_is_bounded_and_quality_capped():
    high = score(ConfidenceInputs(edge_pct=8, model_agreement=1, data_quality=1,
                                  sample_size_n=100, books_quoting=10,
                                  availability_certainty=1, projection_cv=0.1,
                                  context_certainty=1))
    poor = score(ConfidenceInputs(edge_pct=8, model_agreement=1, data_quality=0.1,
                                  sample_size_n=100, books_quoting=10,
                                  availability_certainty=1, projection_cv=0.1,
                                  context_certainty=1))
    assert 0 <= high.score <= 100
    assert poor.score <= 70


def test_line_movement_and_clv():
    assert line_move(-3.5, -2.0) == {"delta": 1.5, "direction": "up", "magnitude": 1.5}
    assert line_move(None, -2.0)["direction"] == "unknown"
    assert clv_pct(120, 100) > 0


def test_provider_normalization_flattens_book_markets():
    provider = object.__new__(TheOddsApi)
    event = {
        "id": "evt-1",
        "bookmakers": [{
            "key": "fanduel",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "A", "price": -120},
                    {"name": "B", "price": 105},
                ]},
                {"key": "spreads", "outcomes": [
                    {"name": "A", "price": -110, "point": -2.5},
                ]},
            ],
        }],
    }
    rows = provider._flatten(event, "NFL")
    assert rows == [
        {"event_id": "evt-1", "sport": "NFL", "book": "fanduel", "market": "moneyline", "selection": "A", "line": None, "price": -120},
        {"event_id": "evt-1", "sport": "NFL", "book": "fanduel", "market": "moneyline", "selection": "B", "line": None, "price": 105},
        {"event_id": "evt-1", "sport": "NFL", "book": "fanduel", "market": "spread", "selection": "A", "line": -2.5, "price": -110},
    ]


def test_parlay_probability_is_explicitly_correlation_adjusted():
    legs = [
        {"event_id": "g1", "event_name": "A @ B", "sport": "NFL", "market": "total", "selection": "Over", "line": 47.5, "book": "fanduel", "price": -110, "model_prob": 0.56, "confidence": 82, "edge_pct": 3.6, "state": "OFFICIAL PICK"},
        {"event_id": "g1", "event_name": "A @ B", "sport": "NFL", "market": "player_prop", "selection": "Player Over receiving_yards", "side": "Over", "line": 72.5, "book": "bet365", "price": -105, "model_prob": 0.57, "confidence": 81, "edge_pct": 5.8, "state": "OFFICIAL PICK"},
    ]
    independent, adjusted, notes = combined_probability(legs)
    assert independent == pytest.approx(0.3192)
    assert adjusted != pytest.approx(independent)
    result = evaluate(legs)
    assert result["correlation_adjusted_prob"] == round(adjusted, 4)
    assert result["confidence"] <= min(leg["confidence"] for leg in legs)
