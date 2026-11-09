"""Offline report parity and deterministic companion-lookup work bounds."""
import copy
import hashlib
import json
from collections import Counter

import pytest

from edgeterminal import nfl_prop_grading as grading


def _quote_and_grade(quote_id, player, side, *, line=10.0, projection=8.0,
                     actual=9.0, result="won", book="fanduel",
                     market="receiving_yards", sigma=4.0, interpretation=None):
    quote = dict(quote_id=quote_id, canonical_game_id="nfl-2026-reg-03-atl-gb",
                 player_id=player, player_name=player, side=side, line=line,
                 projection=projection, sportsbook=book, market=market,
                 uncertainty_sigma=sigma, probability_over=0.6, price=-110,
                 observed_at=10.0, kickoff_utc=100.0)
    error = projection - actual if actual is not None else None
    grade = dict(quote_id=quote_id, canonical_game_id=quote["canonical_game_id"],
                 player_id=player, frozen_side=side, frozen_line=line,
                 projection=projection, actual_stat=actual, sportsbook=book,
                 market=market, result=result, model_probability_over=0.6,
                 absolute_error=abs(error) if error is not None else None,
                 signed_error=error, result_source="fixture-official",
                 sportsbook_settlement_interpretation=interpretation or result)
    return quote, grade


def _mixed_freezes():
    first = [
        _quote_and_grade(1, "duplicate", "under", interpretation="void"),
        _quote_and_grade(2, "duplicate", "over", result="lost"),
        _quote_and_grade(3, "duplicate", "under", result="lost"),
        _quote_and_grade(4, "duplicate", "over", projection=12.0),
        _quote_and_grade(5, "missing-under", "over", result="lost"),
        _quote_and_grade(6, "missing-over", "under"),
        _quote_and_grade(7, "equal", "over", projection=10.0),
        _quote_and_grade(8, "equal", "under", projection=10.0),
        _quote_and_grade(9, "zero", "over", line=0, projection=1.0,
                         actual=0.0, result="push", market="receptions", sigma=0),
        _quote_and_grade(10, "zero", "under", line=0.0, projection=1.0,
                         actual=0.0, result="push", market="receptions", sigma=0),
        _quote_and_grade(11, "negative", "under", line=-1.5, projection=-2.0,
                         actual=-3.0, book="draftkings", sigma=None),
        _quote_and_grade(12, "negative", "over", line=-1.5, projection=-2.0,
                         actual=-3.0, result="lost", book="draftkings", sigma=None),
        _quote_and_grade(13, "fraction", "over", line=0.5, projection=0.75,
                         actual=1.0, market="rushing_attempts", sigma=2.0),
        _quote_and_grade(14, "fraction", "under", line=0.5, projection=0.75,
                         actual=1.0, result="lost", market="rushing_attempts", sigma=2.0),
        _quote_and_grade(15, "void", "over", line=1.0, projection=0.0,
                         actual=None, result="void", market="passing_yards", sigma=1.0),
        _quote_and_grade(16, "void", "under", line=1.0, projection=0.0,
                         actual=None, result="void", market="passing_yards", sigma=1.0),
    ]
    second = [
        _quote_and_grade(17, "duplicate", "under", result="lost"),
        _quote_and_grade(18, "duplicate", "over", result="lost"),
    ]
    pending, _ = _quote_and_grade(19, "pending", "over")
    pending_under, _ = _quote_and_grade(20, "pending", "under")
    old, old_grade = _quote_and_grade(21, "previous-season", "over")
    old["canonical_game_id"] = old_grade["canonical_game_id"] = "nfl-2025-reg-03-atl-gb"
    return {
        "previous": ([old], [old_grade]),
        "first": ([q for q, _ in first] + [pending, pending_under], [g for _, g in first]),
        "second": ([q for q, _ in second], [g for _, g in second]),
    }


def _report(monkeypatch, tmp_path, freezes, *, freeze_sha=None, retrospective=None):
    monkeypatch.setattr(grading, "_freeze", lambda sha: {"projections": freezes[sha][0]})
    monkeypatch.setattr(grading, "_latest", lambda sha: freezes[sha][1])

    def freeze_rows(sql):
        assert sql == "SELECT freeze_sha FROM nfl_prop_research_freezes ORDER BY frozen_at"
        return [{"freeze_sha": sha} for sha in freezes]

    monkeypatch.setattr(grading.db, "q", freeze_rows)
    path = tmp_path / "retrospective.json"
    if retrospective is not None:
        path.write_text(json.dumps(retrospective))
    return grading.forward_report(freeze_sha, calibration_path=path)


def _digest(report):
    return hashlib.sha256(json.dumps(report, sort_keys=True, allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize("freeze_sha,retrospective,expected_digest", [
    (None, None, "d722f2e6d679e047a0f5f90a124fc89bed4a27c0993b27f5aa3c1246b18343c6"),
    ("first", {"fixture": [1, None, "saved"]}, "6b82cfb6f3edc207d0b67122fa58dd73d11d1c0a329b2a1e97f197a09a79d63e"),
])
def test_forward_report_matches_complete_preoptimization_output(
        monkeypatch, tmp_path, freeze_sha, retrospective, expected_digest):
    freezes = _mixed_freezes()
    before = copy.deepcopy(freezes)
    report = _report(monkeypatch, tmp_path, freezes, freeze_sha=freeze_sha,
                     retrospective=retrospective)
    # Captured before the production optimization; covers every nested report value
    # and list order while the explicit assertions explain companion semantics.
    assert _digest(report) == expected_digest
    prospective = report["prospective_2026"]
    assert prospective["model_direction_by_sportsbook"] == {
        "draftkings": {"won": 1, "lost": 0, "push": 0, "void": 0, "undetermined": 0, "n": 1},
        "fanduel": {"won": 3 if freeze_sha is None else 2, "lost": 1, "push": 1,
                     "void": 1, "undetermined": 0, "n": 6 if freeze_sha is None else 5},
    }
    assert prospective["conditional_settlement_by_sportsbook"]["fanduel"]["void"] == (
        3 if freeze_sha is None else 2)
    # The existing function annotates only selected grade dictionaries with sigma.
    for sha, (quotes, grades) in freezes.items():
        assert quotes == before[sha][0]
        expected = before[sha][1]
        if sha != "previous" and (freeze_sha is None or sha == freeze_sha):
            sigmas = {q["quote_id"]: q["uncertainty_sigma"] for q in quotes}
            for grade in expected:
                grade["uncertainty_sigma"] = sigmas[grade["quote_id"]]
        assert grades == expected


class _CountedGrade(dict):
    def __init__(self, row, reads):
        super().__init__(row)
        self.reads = reads

    def __getitem__(self, key):
        if key == "canonical_game_id":
            self.reads[key] += 1
        return super().__getitem__(key)


def test_forward_report_companion_lookup_work_grows_linearly(monkeypatch, tmp_path):
    work = []
    for size in (64, 128):
        # Put under companions after all over rows so repeated scans cannot hide
        # behind early matches. Count key reads instead of machine-dependent time.
        pairs = [_quote_and_grade(2 * i + side, f"player-{i}", name,
                                  result="lost" if name == "over" else "won")
                 for side, name in enumerate(("over", "under")) for i in range(size)]
        reads = Counter()
        grades = [_CountedGrade(grade, reads) for _, grade in pairs]
        report = _report(monkeypatch, tmp_path,
                         {"fixture": ([quote for quote, _ in pairs], grades)})
        record = report["prospective_2026"]["model_direction_by_sportsbook"]["fanduel"]
        assert record["n"] == record["won"] == size
        work.append(reads["canonical_game_id"])
        assert work[-1] <= 12 * len(grades), f"{size} pairs required {work[-1]} grade-key reads"
    assert work[1] <= 2.2 * work[0], f"doubling report size grew lookup work from {work[0]} to {work[1]}"


@pytest.mark.parametrize("case,error", [
    ("duplicate-quote", "duplicate frozen prop quote identity across weeks"),
    ("inconsistent-stat", "inconsistent official stat across frozen books"),
])
def test_forward_report_retains_integrity_errors(monkeypatch, tmp_path, case, error):
    freezes = _mixed_freezes()
    if case == "duplicate-quote":
        freezes["second"][0][0]["quote_id"] = 1
    else:
        freezes["second"][1][0]["actual_stat"] = 99.0
    with pytest.raises(ValueError, match=error):
        _report(monkeypatch, tmp_path, freezes)


def test_forward_report_without_direction_candidates_keeps_empty_diagnostics(monkeypatch, tmp_path):
    quotes, grades = zip(_quote_and_grade(1, "equal", "over", projection=10.0),
                        _quote_and_grade(2, "equal", "under", projection=10.0))
    report = _report(monkeypatch, tmp_path, {"equal": (list(quotes), list(grades))})
    prospective = report["prospective_2026"]
    assert prospective["model_direction_by_sportsbook"] == {}
    assert all(record["n"] == 0 for record in prospective["model_direction_by_market"].values())
