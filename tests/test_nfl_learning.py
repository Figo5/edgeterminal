"""Weekly learning uses only separated weeks and immutable quoted holdouts."""
import copy
from contextlib import contextmanager
import json

import pytest

from edgeterminal import nfl_prop_lab as lab
from edgeterminal import nfl_prop_model as original
from edgeterminal import nfl_learning as learning


def example(week, target=4.):
    features = {name: 0. for name in lab.FEATURES}
    features.update(prior_games=10., targets_last3=5., targets_last5=5.,
                    receptions_last3=4., receptions_last5=4., week=float(week))
    return {"season": 2026, "week": week, "game_id": f"2026_{week:02d}_A_B",
            "features": features, "target": target}


def test_fitting_and_calibration_are_disjoint_whole_weeks():
    datasets = {market: [example(w, float(w)) for w in range(1, 8)]
                for market in lab.MARKETS}
    fit, calibration = learning.split_rows(datasets, (2026, 5))
    assert {r["week"] for r in fit["receptions"]} == {1, 2}
    assert {r["week"] for r in calibration["receptions"]} == {3, 4}
    changed = copy.deepcopy(datasets)
    for market in lab.MARKETS:
        for row in changed[market]:
            if row["week"] >= 5:
                row["target"] = 1000000.
    assert learning.split_rows(changed, (2026, 5)) == (fit, calibration)


def test_json_ridge_matches_estimator_and_never_refits_on_calibration_labels():
    fit = [example(w, w * 2.) for w in range(1, 31)]
    config = learning.fit_market(fit, [example(w, w * 3.) for w in range(31, 71)], "receptions")
    altered = learning.fit_market(fit, [example(w, 999.) for w in range(31, 71)], "receptions")
    assert config["coefficients"] == altered["coefficients"]
    assert config["intercept"] == altered["intercept"]
    assert config["residuals"] != altered["residuals"]
    estimator = original._fit(fit, "receptions", "ridge_role", "ridge")
    expected = original._predict(estimator, [example(35)], "receptions", "ridge_role")[0]
    assert learning.predict(config, example(35)["features"], "receptions")[0] == pytest.approx(expected)


def test_empirical_probability_is_finite_and_keeps_pushes():
    config = {"coefficients": [0.] * len(original.FEATURES), "intercept": 4.,
              "residuals": [-1.] * 15 + [0.] * 15 + [1.] * 15}
    point, probability = learning.predict(config, example(1)["features"], "receptions", 4.)
    assert point == 4.
    assert probability["push"] > .3
    assert sum(probability.values()) == pytest.approx(1.)
    with pytest.raises(ValueError, match="calibration"):
        learning.predict({**config, "residuals": [0.] * 29}, example(1)["features"], "receptions", 4.)


def test_baseline_facade_does_not_fit_or_change_legacy_forecasts(monkeypatch):
    monkeypatch.setattr(learning, "_active", lambda: None)
    expected = {"projections": [{"quote_id": 1}], "readiness": "RESEARCH_ONLY"}
    monkeypatch.setattr(original, "project_current", lambda *a, **kw: expected)
    monkeypatch.setattr(learning, "fit_market", lambda *a: pytest.fail("fit during serving"))
    assert learning.project_current({}, {}, {}, "batch") is expected
    assert learning.model_info() == original.model_info()


def test_invalid_json_config_fails_closed():
    config = {"coefficients": [0.] * len(original.FEATURES), "intercept": float("nan"),
              "residuals": [0.] * 40}
    with pytest.raises(ValueError, match="finite"):
        learning.predict(config, example(1)["features"], "receptions", 4.)


def test_candidate_cannot_reuse_an_active_model_trained_inside_validation(monkeypatch):
    monkeypatch.setattr(learning, "_active", lambda: {"candidate_id": "later", "training_cutoff": 200.})
    monkeypatch.setattr(learning, "_quality_window", lambda now: ([{"game_id": "g"}],
        [(2026, 4), (2026, 5), (2026, 6)], {"g": {"season": 2026, "week": 4}}))
    monkeypatch.setattr(learning, "_kickoff", lambda game: 100.)
    monkeypatch.setattr(learning, "_validated_active", lambda: {"training_cutoff": 200., "artifact": {}})
    from edgeterminal import model_learning_store
    monkeypatch.setattr(model_learning_store, "holdout_available", lambda *a: True)
    monkeypatch.setattr(learning, "fit_market", lambda *a: pytest.fail("fit on invalid baseline window"))
    result = learning.build_candidate(now=300.)
    assert result["status"] == "waiting"
    assert "baseline" in result["reason"]


def adaptive_fixture(monkeypatch):
    config = {"coefficients": [0.] * len(original.FEATURES), "intercept": 7., "residuals": [0.] * 40}
    active = {"version": "weekly-version", "candidate_id": "candidate", "artifact_sha256": "a" * 64,
              "training_cutoff": 100., "artifact": {"schema": learning.SCHEMA,
                  "parameters": learning.PARAMETERS, "features": list(original.FEATURES),
                  "fit_boundary_week": [2026, 4], "parameter_fingerprint": "b" * 64,
                  "calibration_cutoff": 200., "validation_description": "untouched real quotes",
                  "configurations": {m: config for m in lab.MARKETS}}}
    monkeypatch.setattr(learning, "_active", lambda: active)
    quote = {"id": 1, "batch_id": "batch", "canonical_game_id": "game", "player_id": "player",
             "market": "receptions", "sportsbook": "book", "line": 4.5, "side": "over", "price": -110,
             "retrieved_at": 250., "kickoff_utc": 400., "line_type": "main", "team": "GB"}
    projection = {key: quote[key] for key in ("canonical_game_id", "player_id", "market", "sportsbook",
                                              "line", "side", "price", "kickoff_utc")}
    projection.update(quote_id=1, observed_at=250., gsis_player_id="gsis", input_context={})
    monkeypatch.setattr(original, "project_current", lambda *a, **kw: {"batch_id": "batch", "projections": [copy.deepcopy(projection)]})
    monkeypatch.setattr(lab, "_week3_features", lambda *a, **kw: {("gsis", "gb"): {
        "feature": example(9)["features"], "opp_hist": []}})
    monkeypatch.setattr(learning.time, "time", lambda: 300.)
    def query(sql, args=()):
        return {"kickoff": 400.} if "MIN(kickoff_utc)" in sql else quote
    monkeypatch.setattr(learning.db, "q1", query)
    monkeypatch.setattr(lab, "_pair_quotes", lambda batch: ([quote], {}))
    captured = []
    class Connection:
        def execute(self, sql, args):
            captured.append(args)
    @contextmanager
    def transaction():
        yield Connection()
    monkeypatch.setattr(learning.db, "tx", transaction)
    return active, captured


def test_adaptive_serving_and_freeze_preserve_actual_version_cutoffs(monkeypatch):
    active, captured = adaptive_fixture(monkeypatch)
    monkeypatch.setattr(learning, "fit_market", lambda *a: pytest.fail("fit during serving"))
    report = learning.project_current({}, {}, {}, "batch", season=2026, week=9)
    assert report["projections"][0]["projection"] == 7.
    assert report["model_metadata"]["training_cutoff"] == [2026, 4]
    assert report["model_metadata"]["training_cutoff_at"] == 100.
    learning.seal_forward(report, {"source.csv": "c" * 64}, 300.)
    saved = json.loads(captured[0][-1])
    assert saved["metadata"]["learning_candidate_id"] == active["candidate_id"]
    assert saved["metadata"]["calibration_cutoff_at"] == 200.
    assert saved["projections"][0]["training_cutoff_at"] == 100.


def test_adaptive_freeze_rejects_changed_cutoff_and_postkickoff(monkeypatch):
    adaptive_fixture(monkeypatch)
    report = learning.project_current({}, {}, {}, "batch", season=2026, week=9)
    altered = copy.deepcopy(report)
    altered["model_metadata"]["calibration_cutoff_at"] = 201.
    with pytest.raises(ValueError, match="cutoff"):
        learning.seal_forward(altered, {"source.csv": "c" * 64}, 300.)
    with pytest.raises(ValueError, match="pre-kickoff"):
        learning.seal_forward(report, {"source.csv": "c" * 64}, 400.)


def test_incomplete_current_season_training_games_fail_closed():
    schedule = {"2026_01_A_B": {"season": "2026", "week": "1"},
                "2026_02_C_D": {"season": "2026", "week": "2"}}
    body = b"game_id,season_type\n2026_01_A_B,REG\n"
    with pytest.raises(ValueError, match="game coverage"):
        learning._require_completed_history(body, (2026, 3), schedule)


def test_later_official_download_is_not_a_historical_training_receipt():
    names = ["player_id", "season", "week", "season_type", "game_id", *lab.USAGE]
    body = (",".join(names) + "\n" + ",".join(["gsis", "2026", "2", "REG", "game"] + ["1"] * len(lab.USAGE)) + "\n").encode()
    old = {"body": body, "source": {"received_at": 90.}}
    new = {"body": body, "source": {"received_at": 110.}}
    assert learning._current_snapshot({"old": old, "new": new}, 100., 2026) is old
    with pytest.raises(ValueError, match="before cutoff"):
        learning._current_snapshot({"new": new}, 100., 2026)


def test_saved_diagnostics_use_frozen_probabilities_and_verified_outcomes():
    rows = [{"actual": actual, "projection": 7., "line": 4.5,
             "side": "over", "probability_over": .8} for actual in (6., 3.)]
    result = learning._saved_metrics(rows)
    assert result["wins"] == result["losses"] == 1
    assert result["mae"] == 2.5
    assert result["brier"] == pytest.approx(.34)
    assert result["reliability"][0]["mean_probability"] == .8
    assert result["reliability"][0]["observed_win_rate"] == .5
