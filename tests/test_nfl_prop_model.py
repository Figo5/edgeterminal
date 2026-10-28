"""Direct NFL model upgrades preserve time, identity and probability contracts."""
import copy
from contextlib import contextmanager
import hashlib
import json

import numpy as np
import pytest

from edgeterminal import nfl_prop_lab as lab
from edgeterminal import nfl_prop_model as model


def row(**features):
    values = {key: 0.0 for key in lab.FEATURES}
    values.update(prior_games=10, carries_last3=15, carries_last5=10,
                  rushing_yards_last3=60, rushing_yards_last5=50)
    values.update(features)
    return {"features": values, "target": 60, "season": 2024, "week": 1}


def test_opportunity_features_use_prior_volume_and_efficiency_without_mutation():
    source = row()
    before = copy.deepcopy(source)
    features = model.derived_features(source, "rushing_yards")
    assert features["usage_projection"] == 75
    assert features["opportunity_change"] == 5
    assert features["efficiency_last5"] == 5
    assert source == before


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_invalid_input_does_not_silently_become_zero(bad):
    with pytest.raises(ValueError, match="finite"):
        model.derived_features(row(carries_last3=bad), "rushing_yards")


def test_empirical_probabilities_are_asymmetric_and_account_for_integer_pushes():
    residuals = [-2.] * 100 + [0.] * 100 + [1.] * 100
    configuration = {"residuals": residuals, "conditioning": "absolute"}
    integer = model.probabilities(4, 4, configuration, "receptions")
    half = model.probabilities(4.5, 4, configuration, "receptions")
    assert integer["push"] > .3
    assert half["push"] == 0
    assert sum(integer.values()) == pytest.approx(1)
    assert sum(half.values()) == pytest.approx(1)
    assert 0 < integer["over"] < 1
    assert integer["under"] > integer["over"] - .001


def test_empirical_distribution_never_invents_negative_catches():
    configuration = {"residuals": [-10.] * 100 + [0.] * 100,
                     "conditioning": "absolute"}
    result = model.probabilities(-.5, 1, configuration, "receptions")
    # A small finite-sample tail remains, but no negative simulated catches.
    assert result["over"] > .99
    assert result["under"] < .01


@pytest.mark.parametrize("residuals", [[0.] * 99, [float("nan")] * 100])
def test_empirical_probabilities_fail_closed_on_insufficient_or_invalid_calibration(residuals):
    with pytest.raises(ValueError, match="calibration"):
        model.probabilities(4.5, 4, {"residuals": residuals,
                                 "conditioning": "absolute"}, "receptions")


def test_training_cutoff_rejects_forward_rows():
    datasets = {market: [row()] for market in lab.MARKETS}
    datasets["receptions"].append({**row(), "season": 2026, "week": 3})
    with pytest.raises(ValueError, match="cutoff"):
        model.validate_training_rows(datasets)


def test_model_identity_is_versioned_and_fingerprinted():
    assert model.VERSION != lab.VERSION
    assert model.CUTOFF == (2026, 3)
    assert len(model.PARAMETER_FINGERPRINT) == 64
    assert model.PARAMETERS["selection"] == "top-five-confidence-v1"


def test_loader_checks_models_bytes_before_deserialization(tmp_path, monkeypatch):
    manifest = {"version": model.VERSION, "training_cutoff": list(model.CUTOFF),
                "parameter_fingerprint": model.PARAMETER_FINGERPRINT,
                "models_sha256": "0" * 64}
    body = json.dumps(manifest).encode()
    (tmp_path / "manifest.json").write_bytes(body)
    (tmp_path / "models.joblib").write_bytes(b"not a trusted model")
    monkeypatch.setattr(model, "MANIFEST_SHA256", hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(model, "DIRECTORY", tmp_path)
    monkeypatch.setattr(model.joblib, "load", lambda p: pytest.fail("unverified model loaded"))
    with pytest.raises(ValueError, match="model.*changed"):
        model.load()


def test_loader_rejects_edited_manifest(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(model, "DIRECTORY", tmp_path)
    with pytest.raises(ValueError, match="manifest"):
        model.load()


def test_loader_rejects_changed_feature_implementation_before_deserialization(tmp_path, monkeypatch):
    serialized = b"verified-model-bytes"
    feature_hashes = model._feature_code_hashes()
    manifest = {"version": model.VERSION, "training_cutoff": list(model.CUTOFF),
                "parameter_fingerprint": model.PARAMETER_FINGERPRINT,
                "models_sha256": hashlib.sha256(serialized).hexdigest(),
                "feature_code_sha256": feature_hashes}
    body = json.dumps(manifest).encode()
    (tmp_path / "manifest.json").write_bytes(body)
    (tmp_path / "models.joblib").write_bytes(serialized)
    monkeypatch.setattr(model, "MANIFEST_SHA256", hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(model, "DIRECTORY", tmp_path)
    monkeypatch.setattr(model, "_feature_code_hashes",
                        lambda: {**feature_hashes, "lab._feature": "0" * 64})
    monkeypatch.setattr(model.joblib, "load", lambda p: pytest.fail("changed feature implementation loaded"))
    with pytest.raises(ValueError, match="feature implementation"):
        model.load()


def serving_fixture(monkeypatch, *, observed=100., provider_updated=None, names=None):
    config = {"family": "baseline", "residuals": [-1.] * 100 + [0.] * 100 + [1.] * 100,
              "conditioning": "absolute", "probability_method": "empirical"}
    manifest = {"configurations": {m: config for m in lab.MARKETS}}
    monkeypatch.setattr(model, "load", lambda: (manifest, {m: None for m in lab.MARKETS}))
    quote = {"id": 1, "retrieved_at": observed, "kickoff_utc": 200.,
             "provider_updated_at": provider_updated, "player_name": "Test Player",
             "player_id": "sgo:test", "team": "Green Bay Packers", "market": "receptions",
             "canonical_game_id": "nfl-2026-reg-09-gb-atl", "sportsbook": "book",
             "line": 3., "side": "over", "price": -110}
    monkeypatch.setattr(lab, "_pair_quotes", lambda batch: ([quote], {}))
    names = names or ["gsis-1"]
    monkeypatch.setattr(lab, "_read_stats", lambda stats: ([{
        "season": "2026", "week": "8", "player_id": name,
        "player_display_name": "Test Player", "team": "GB"} for name in names], {}))
    features = row(receptions_last3=3, targets_last3=5)["features"]
    monkeypatch.setattr(lab, "_week3_features", lambda *a: {("gsis-1", "gb"): {
        "feature": features, "player": [{"targets": 5}], "opp_hist": []}})
    monkeypatch.setattr(model.time, "time", lambda: 150.)
    return quote


def test_current_forecast_uses_frozen_model_and_retains_push_and_provenance(monkeypatch):
    serving_fixture(monkeypatch)
    result = model.project_current({}, {}, {}, "batch", season=2026, week=9)
    assert result["identity_matched"] == 1
    forecast = result["projections"][0]
    assert forecast["probability_push"] > .3
    assert forecast["model_version"].startswith(model.VERSION)
    assert forecast["parameter_fingerprint"] == model.PARAMETER_FINGERPRINT
    assert result["model_metadata"]["training_cutoff"] == [2026, 3]
    assert result["readiness"] == "RESEARCH_ONLY" and result["designation"] == "PASS"


@pytest.mark.parametrize("changed", [
    {"observed": 151.}, {"observed": -3600.}, {"provider_updated": 151.},
    {"names": ["gsis-1", "gsis-2"]},
])
def test_serving_rejects_future_stale_or_ambiguous_inputs(monkeypatch, changed):
    serving_fixture(monkeypatch, **changed)
    assert model.project_current({}, {}, {}, "batch", season=2026, week=9)["projections"] == []


def freeze_fixture(monkeypatch):
    quote = {"id": 1, "batch_id": "batch", "line_type": "main",
             "canonical_game_id": "nfl-2026-reg-09-gb-atl", "player_id": "player",
             "market": "receptions", "sportsbook": "book", "line": 4.5,
             "side": "over", "price": -110, "retrieved_at": 100., "kickoff_utc": 200.}
    record = {"quote_id": 1, "observed_at": 100., "model_version": model.VERSION+":median_role",
              "parameter_fingerprint": "a" * 64, "model_manifest_sha256": "b" * 64,
              **{key: quote[key] for key in ("canonical_game_id", "player_id", "market",
                  "sportsbook", "line", "side", "price", "kickoff_utc")}}
    metadata = {"model_version": model.VERSION, "feature_version": "opportunity-v2",
                "feature_names": ["prior_games"], "parameter_fingerprint": "a" * 64,
                "model_manifest_sha256": "b" * 64, "training_cutoff": [2026, 3],
                "selection_basis": "2024 development", "validation_holdout": "2025 acceptance"}
    monkeypatch.setattr(lab, "_pair_quotes", lambda batch: ([quote], {}))
    monkeypatch.setattr(lab.db, "q1", lambda query, args: {"kickoff": 200.} if "MIN(" in query else quote)
    class Connection:
        inserted = None
        def execute(self, query, args):
            if query.startswith("INSERT"):
                self.inserted = args
            return self
        def fetchone(self):
            return None
    connection = Connection()
    @contextmanager
    def transaction():
        yield connection
    monkeypatch.setattr(lab.db, "tx", transaction)
    return {"batch_id": "batch", "projections": [record], "model_metadata": metadata}, connection


def test_new_freeze_metadata_is_preserved_and_hashed(monkeypatch):
    report, connection = freeze_fixture(monkeypatch)
    result = lab.seal_forward(report, {"source": "hash"}, 150.)
    body = connection.inserted[3]
    assert hashlib.sha256(body.encode()).hexdigest() == result["freeze_sha"]
    saved = json.loads(body)["metadata"]
    assert saved["model_version"] == model.VERSION
    assert saved["model_manifest_sha256"] == "b" * 64
    assert saved["input_sha256"] == {"source": "hash"}


@pytest.mark.parametrize("tamper", ["source_batch_id", "fingerprint"])
def test_freeze_rejects_metadata_override_or_row_mismatch(monkeypatch, tamper):
    report, connection = freeze_fixture(monkeypatch)
    if tamper == "source_batch_id":
        report["model_metadata"]["source_batch_id"] = "another-batch"
    else:
        report["projections"][0]["parameter_fingerprint"] = "c" * 64
    with pytest.raises(ValueError, match="metadata"):
        lab.seal_forward(report, {"source": "hash"}, 150.)
    assert connection.inserted is None


def test_legacy_freeze_keeps_its_original_metadata_contract(monkeypatch):
    report, connection = freeze_fixture(monkeypatch)
    report.pop("model_metadata")
    lab.seal_forward(report, {"source": "hash"}, 150.)
    assert json.loads(connection.inserted[3])["metadata"] == {
        "model_version": lab.VERSION, "feature_version": lab.FEATURE_VERSION,
        "feature_names": list(lab.FEATURES), "input_sha256": {"source": "hash"},
        "selection_basis": "2024 development MAE", "validation_holdout": "2025",
        "source_batch_id": "batch", "created_at": 150.}
