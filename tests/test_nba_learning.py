"""NBA learning keeps fitting offline and uses whole-game verified holdouts."""
import copy
import hashlib
import json
import math
from datetime import datetime, timezone

import pytest

from edgeterminal import nba_learning as learning
from edgeterminal import nba_model as baseline
from edgeterminal import nba_prop_model


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


TIP = stamp("2026-10-28T23:00:00Z")


def history():
    rows = []
    for pid in ("1", "2", "3"):
        for i in range(15):
            day = f"2026-03-{i + 1:02d}T19:00:00Z" if i < 12 else f"2026-10-{i + 10:02d}T19:00:00Z"
            rows.append(dict(player_id=pid, game_id=f"g{i}", played_at=day,
                             minutes=30., points=10. if i < 12 else 30.,
                             rebounds=4., assists=3., threes=0.))
    return rows


def artifact(weight=.75):
    return dict(schema=1, kind="nba-adaptive-v1", version="nba-learning-test",
                training_cutoff=TIP - 86400,
                minutes_model=dict(intercept=30., coefficients=[0.] * len(nba_prop_model.MINUTE_FEATURES)),
                markets={m: dict(recent_weight=weight, probability_method="gamma_poisson" if m in baseline.COUNT_MARKETS else "empirical",
                                 standardized_residuals=[-1.] * 50 + [0.] * 50 + [1.] * 50,
                                 calibration_n=150) for m in baseline.MARKETS})


def active():
    a = artifact()
    return dict(candidate_id="test", version=a["version"], training_cutoff=a["training_cutoff"],
                artifact=a, artifact_sha256="a" * 64)


def quote(pid="1", market="points", side="over", line=20.5, book="one", **extra):
    return {**dict(game_id="game", player_id=pid, market=market, side=side, line=line,
                   price=-110., sportsbook=book, player=f"P{pid}", team="BOS",
                   observed_at=TIP - 300, identity_verified=True, is_main=True), **extra}


def test_baseline_fallback_does_not_fit_or_change_saved_contract(monkeypatch):
    monkeypatch.setattr(learning, "_active", lambda: None)
    monkeypatch.setattr(learning, "fit_candidate", lambda *a, **k: pytest.fail("fit during serving"))
    assert learning.project_prop("1", "points", history(), TIP, 20.5) == baseline.project_prop("1", "points", history(), TIP, 20.5)
    assert learning.select_props([quote()], history(), TIP, TIP - 100, count=1) == baseline.select_props([quote()], history(), TIP, TIP - 100, count=1)
    assert learning.model_info()["version"] == baseline.MODEL_VERSION
    assert learning.parameters() == baseline.PARAMETERS
    assert learning.historical_evaluation() == baseline.historical_evaluation()


def test_zero_event_rate_has_predictive_uncertainty_and_integer_push_mass():
    over, under, push = learning.gamma_poisson_probabilities(.5, 450., 30., .5)
    assert under == pytest.approx(math.sqrt(15 / 16))
    assert 0 < over < .05 and push == 0
    mass = learning.gamma_poisson_probabilities(10.5, 450., 30., 1.)
    assert 0 < mass[2] < 1 and sum(mass) == pytest.approx(1.)
    assert learning.gamma_poisson_probabilities(.5, 450., 30., -1) == (1., 0., 0.)


def test_season_shrinkage_changes_production_and_matches_saved_distribution(monkeypatch):
    monkeypatch.setattr(learning, "_active", active)
    out = learning.project_prop("1", "points", history(), TIP, 20.5)
    pooled = baseline.project_prop("1", "points", history(), TIP)
    assert out["raw_projection"] > pooled["raw_projection"]
    assert out["model_version"] == "nba-learning-test"
    assert out["readiness"] == "RESEARCH_ONLY" and out["designation"] == "PASS"
    assert sum(out[k] for k in ("probability_over", "probability_under", "probability_push")) == pytest.approx(1., abs=.0001)
    zero = learning.project_prop("1", "threes", history(), TIP, .5)
    assert zero["projection"] > 0 and 0 < zero["probability_over"] < .1
    poisoned = history() + [dict(history()[0], played_at=TIP, game_id="future", points=999.)]
    assert learning.project_prop("1", "points", poisoned, TIP, 20.5) == out


def test_ranker_considers_every_real_side_and_book_and_keeps_five_or_wait(monkeypatch):
    monkeypatch.setattr(learning, "_active", active)
    quotes = [quote(side="under", line=1.5), quote(side="over", line=1.5, book="two"),
              quote("2", "points"), quote("2", "assists", line=2.5),
              quote("3", "threes", side="under", line=.5), quote("3", "rebounds", line=4.5),
              quote("1", "rebounds", line=4.5, identity_verified=False)]
    out = learning.select_props(quotes, history(), TIP, TIP - 100)
    assert out["status"] == "ok" and len(out["selections"]) == 5
    chosen = next(c for c in out["candidates"] if c["player_id"] == "1" and c["market"] == "points")
    assert chosen["side"] == "over" and chosen["sportsbook"] == "two"
    assert any(e["reason"] == "identity_unverified" for e in out["excluded"])
    wait = learning.select_props(quotes[:2], history(), TIP, TIP - 100)
    assert wait["status"] == "wait" and wait["selections"] == []
    with pytest.raises(ValueError, match="before tipoff"):
        learning.select_props(quotes, history(), TIP, TIP)


def test_dynamic_examples_and_windows_exclude_holdout_labels_whole_game():
    rows = history()
    cut = stamp("2026-10-23T23:00:00Z")
    examples = learning.examples(rows, cut, excluded_games={"g13"})
    assert all(not r["game_id"].endswith("-g13") and r["ts"] + baseline.FEATURE_LAG_S < cut for r in examples["points"])
    fit, dev, calibration = learning.split_windows(examples, development_games=1, calibration_games=1)
    groups = [{r["game_id"] for r in part["points"]} for part in (fit, dev, calibration)]
    assert not groups[0] & groups[1] and not groups[1] & groups[2] and not groups[0] & groups[2]
    changed = copy.deepcopy(rows)
    for row in changed:
        if learning._game_id(row["game_id"], stamp(row["played_at"])) in groups[1] | groups[2]:
            row["points"] = 100000.
    again = learning.split_windows(learning.examples(changed, cut, {"g13"}), 1, 1)
    assert again[0] == fit


def test_source_receipt_hash_and_identity_are_required_before_training():
    body = json.dumps({"data": []}).encode()
    source = dict(id=1, kind="player-history", http_status=200, received_at=10.,
                  url="https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/1/gamelog?season=2027",
                  body=body, sha256=hashlib.sha256(body).hexdigest())
    assert learning.verify_source(source, received_by=11.) == {"data": []}
    for bad in ({"received_at": 12.}, {"sha256": "0" * 64}, {"http_status": 404},
                {"url": "https://espn.com.evil.example/athletes/1/gamelog"}):
        with pytest.raises(ValueError, match="source"):
            learning.verify_source(dict(source, **bad), received_by=11.)


def test_invalid_active_parameters_fail_closed(monkeypatch):
    bad = active()
    bad["artifact"]["minutes_model"]["intercept"] = float("nan")
    monkeypatch.setattr(learning, "_active", lambda: bad)
    with pytest.raises(ValueError, match="finite"):
        learning.project_prop("1", "points", history(), TIP)


def test_calibration_outcomes_cannot_change_minutes_or_production_fit():
    datasets = {m: [] for m in baseline.MARKETS}
    for i in range(900):
        target_at = 1600000000. + i * 86400
        for market in baseline.MARKETS:
            prior = [dict(ts=target_at - (10 - j) * 86400, minutes=25. + j,
                          stat=float((i + j) % 7)) for j in range(6)]
            datasets[market].append(dict(game_id=f"game-{i}", player_id="1", ts=target_at,
                                         rows=prior, minutes=30. + i % 4, target=float(i % 7)))
    changed = copy.deepcopy(datasets)
    for market in baseline.MARKETS:
        for row in changed[market][-120:]:
            row["target"] = 10000.
    first = learning.fit_candidate(datasets, TIP)
    second = learning.fit_candidate(changed, TIP)
    assert first["minutes_model"] == second["minutes_model"]
    for market in baseline.MARKETS:
        assert first["markets"][market]["recent_weight"] == second["markets"][market]["recent_weight"]
        assert first["markets"][market]["standardized_residuals"] != second["markets"][market]["standardized_residuals"]
    training = first["training"]
    assert not set(training["fit_game_ids"]) & set(training["calibration_game_ids"])
    assert not set(training["coefficient_game_ids"]) & set(training["development_game_ids"])
    assert training["fit_label_max_at"] < training["calibration_label_max_at"] < TIP


def test_candidate_waits_when_current_baseline_would_leak_into_outer_holdout(monkeypatch):
    monkeypatch.setattr(learning, "validation_records", lambda *a: (
        [dict(observed_at=100.)], dict(game_ids=["game"]), []))
    monkeypatch.setattr(learning, "_active", lambda: dict(training_cutoff=200.))
    monkeypatch.setattr(learning, "fit_candidate", lambda *a: pytest.fail("fit with a contaminated comparator"))
    assert learning.build_candidate(now=300.)["reason"] == "active_baseline_overlaps_validation_window"


def test_historical_projection_cannot_use_newly_fitted_artifact(monkeypatch):
    monkeypatch.setattr(learning, "_active", active)
    prior_tip = TIP - 2 * 86400
    assert learning.project_prop("1", "points", history(), prior_tip, 20.5) == baseline.project_prop("1", "points", history(), prior_tip, 20.5)


def test_capture_snapshot_keeps_metadata_selection_and_points_on_one_version(monkeypatch):
    from edgeterminal import model_learning_store
    calls = []
    def loaded(sport):
        calls.append(sport)
        value = active()
        if len(calls) > 1:
            value["version"] = "later-version"
        return value
    monkeypatch.setattr(model_learning_store, "load_active", loaded)
    with learning.snapshot():
        info = learning.model_info()
        row = learning.project_prop("1", "points", history(), TIP, 20.5)
        selected = learning.select_props([quote()], history(), TIP, TIP - 100, count=1)
        assert row["model_version"] == selected["model_version"] == info["version"]
        assert selected["selections"][0]["probability"] == row["probability_over"]
        assert len(calls) == 1
    assert learning.model_info()["version"] == "later-version"


def test_version_is_idempotent_but_grade_corrections_get_a_new_identity(monkeypatch):
    record = dict(game_id="test-game", player_id="1", market="points", quote_id=1,
                  line=20.5, side="over", observed_at=TIP - 500, kickoff_at=TIP,
                  result_observed_at=TIP + 20000, actual=19., history=history())
    monkeypatch.setattr(learning, "validation_records", lambda *a: ([dict(record)], dict(game_ids=["test-game"], exclusions=[]), []))
    monkeypatch.setattr(learning, "training_history", lambda *a: ([], []))
    monkeypatch.setattr(learning, "_active", lambda: None)
    def fitted(*args):
        value = artifact()
        value["training"] = {}
        return value
    monkeypatch.setattr(learning, "fit_candidate", fitted)
    projection = dict(status="ok", raw_projection=22., probability_over=.6, probability_under=.4)
    monkeypatch.setattr(baseline, "project_prop", lambda *a, **k: dict(projection))
    monkeypatch.setattr(learning, "_project", lambda *a, **k: dict(projection))
    first = learning.build_candidate(TIP + 30000)
    assert first["version"] == learning.build_candidate(TIP + 120000)["version"]
    record["actual"] = 21.
    assert first["version"] != learning.build_candidate(TIP + 120000)["version"]


def test_void_pdf_evidence_is_checked_even_without_a_numeric_outcome():
    from edgeterminal.nba_learning_data import verify_source_bytes
    body = b"%PDF-1.7 participation report"
    row = dict(id=1, body=body, http_status=200, received_at=10.,
               url="https://statsdmz.nba.com/pdfs/20261024/20261024_MILPHI.pdf",
               sha256=hashlib.sha256(body).hexdigest())
    assert verify_source_bytes(row, 11.) == body
    with pytest.raises(ValueError, match="source"):
        verify_source_bytes({**row, "body": b"changed"}, 11.)


def test_adaptive_identity_pins_imported_uncertainty_and_frozen_baseline_manifest(tmp_path, monkeypatch):
    from edgeterminal import model_learning_store
    dependencies = ("nba_learning.py", "nba_learning_data.py", "nba_model.py", "nba_prop_model.py",
                    "pricing.py", "nba_sources.py", "nba_data.py", "nba_quotes.py", "nba_identity.py",
                    "nba_model_fit.py", "analytics/nba.py", "nba_model_manifest.py")
    for name in dependencies:
        target = tmp_path / 'edgeterminal' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((learning.ROOT / 'edgeterminal' / name).read_bytes())
    monkeypatch.setattr(learning, 'ROOT', tmp_path)
    monkeypatch.setattr(model_learning_store, 'ROOT', tmp_path)
    monkeypatch.setattr(model_learning_store, 'DIRECTORY', tmp_path / 'data/research/model_learning')
    record = dict(game_id="test-game", player_id="1", market="points", quote_id=1,
                  line=20.5, side="over", observed_at=TIP - 500, kickoff_at=TIP,
                  result_observed_at=TIP + 20000, actual=19., history=history())
    monkeypatch.setattr(learning, 'validation_records', lambda *a: (
        [record], dict(game_ids=['test-game'], exclusions=[]), []))
    monkeypatch.setattr(learning, 'training_history', lambda *a: ([], []))
    monkeypatch.setattr(learning, '_active', lambda: None)
    def fitted(*args):
        value = artifact();value['training'] = {}
        return value
    monkeypatch.setattr(learning, 'fit_candidate', fitted)
    projection = dict(status='ok', raw_projection=22., probability_over=.6, probability_under=.4)
    monkeypatch.setattr(baseline, 'project_prop', lambda *a, **k: dict(projection))
    monkeypatch.setattr(learning, '_project', lambda *a, **k: dict(projection))
    first = learning.build_candidate(TIP + 30000)
    hashes = first['artifact']['code_sha256']
    assert all('edgeterminal/' + name in hashes for name in dependencies)
    model_learning_store._check_code(first['artifact'], archive=True)
    dependency = tmp_path / 'edgeterminal/analytics/nba.py'
    dependency.write_bytes(dependency.read_bytes() + b'\n# dependency changed\n')
    assert learning.build_candidate(TIP + 30000)['version'] != first['version']
    with pytest.raises(ValueError, match='implementation changed'):
        model_learning_store._check_code(first['artifact'])
