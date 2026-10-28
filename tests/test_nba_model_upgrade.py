"""Opportunity forecasts and frozen, out-of-sample NBA distributions."""
import hashlib
import json
import math
from datetime import datetime, timezone

import pytest

from edgeterminal import nba_model, nba_prop_model
from edgeterminal.nba_model_fit import examples, save_frozen_artifact


def future_history():
    return [dict(player_id='role-player', game_id=f'game-{i}',
                 played_at=f'2026-10-{i+1:02d}T00:00:00Z', minutes=m,
                 points=m*.6, rebounds=m*.2, assists=m*.15, threes=2)
            for i,m in enumerate([18,19,20,18,21,20,19,20,21,22,30,32,34,33,35])]


def test_active_nba_version_and_artifact_fingerprinted():
    assert nba_model.MODEL_VERSION == 'nba-research-2.0.1'
    artifact = nba_model.PROP_PARAMETERS
    encoded = json.dumps(artifact, sort_keys=True, separators=(',', ':'))
    assert nba_model.PARAMETERS['prop_fit_sha256'] == hashlib.sha256(encoded.encode()).hexdigest()
    assert artifact['training_cutoff'] == '2026-10-01T00:00:00Z'
    assert artifact['calibration_end_exclusive'] == '2026-03-01T00:00:00Z'


def test_recent_role_minutes_forecast_and_calibrated_probabilities_used():
    rows = future_history()
    result = nba_model.project_prop('role-player', 'points', rows, '2026-10-27T00:00:00Z', line=20.5)
    selected = nba_model.PROP_PARAMETERS['markets']['points']['probability_method']
    assert result['probability_method'] == ('historical_standardized_residual_cdf' if selected=='empirical'
                                           else 'historical_validated_parametric')
    assert result['projected_minutes'] > sum(r['minutes'] for r in rows)/len(rows)
    normalized,_ = nba_model._player_rows(rows, 'role-player', 'points', result['tipoff'])
    mu,minutes = nba_prop_model.predict(normalized,result['tipoff'],'points',nba_model.PROP_PARAMETERS)
    expected = nba_prop_model.probabilities('points',mu,result['sd'],20.5,nba_model.PROP_PARAMETERS)
    assert result['raw_projection'] == pytest.approx(mu)
    assert result['projected_minutes'] == round(minutes,1)
    assert result['probability_over'] == pytest.approx(expected[0],abs=.0002)


def test_historical_report_is_frozen_and_detached():
    report = nba_model.historical_evaluation()
    assert report['holdout_start'] == '2026-03-01T00:00:00Z'
    assert report['artifact_sha256'] == nba_model.PROP_FIT_SHA256
    assert report['source_catalog']
    report['markets']['points']['baseline_mae'] = -100
    assert nba_model.historical_evaluation()['markets']['points']['baseline_mae'] > 0


def test_invalid_parameter_artifact_rejected(tmp_path):
    artifact = json.loads(json.dumps(nba_model.PROP_PARAMETERS))
    artifact['markets']['points']['standardized_residuals'][0] = float('nan')
    path = tmp_path/'invalid.json';path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        nba_prop_model.load_parameters(path)
    artifact = json.loads(json.dumps(nba_model.PROP_PARAMETERS))
    artifact['eligible_from'] = 0
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        nba_prop_model.load_parameters(path)


def test_calibrated_integer_push_and_zero_support():
    rows = future_history()
    for market in nba_model.MARKETS:
        result = nba_model.project_prop('role-player',market,rows,'2026-10-27',line=2.)
        probabilities = [result['probability_over'],result['probability_under'],result['probability_push']]
        assert all(0 <= p <= 1 for p in probabilities)
        assert sum(probabilities) == pytest.approx(1.,abs=.0002)
        assert result['probability_push'] > 0
        at_zero = nba_model.project_prop('role-player',market,rows,'2026-10-27',line=0.)
        assert at_zero['probability_under'] == 0


def test_upgrade_ignores_later_and_conflicting_rows():
    rows = future_history();tip='2026-10-27T00:00:00Z'
    result = nba_model.project_prop('role-player','points',rows,tip,line=20.5)
    future = dict(rows[-1],game_id='future',played_at='2026-10-28',points=500.)
    assert nba_model.project_prop('role-player','points',rows+[future],tip,line=20.5) == result
    conflicting = dict(rows[0],points=100.)
    tainted = nba_model.project_prop('role-player','points',rows+[conflicting],tip,line=20.5)
    assert tainted['input_conflicts'] == 1 and tainted['n'] == 14


def test_historical_fit_excludes_forward_season_and_target_game():
    rows = [dict(r,played_at=r['played_at'].replace('2026-10','2025-12')) for r in future_history()]
    datasets = examples(rows + future_history())
    for market,records in datasets.items():
        assert records
        for r in records:
            assert r['ts'] < datetime(2026,10,1,tzinfo=timezone.utc).timestamp()
            assert all(p['ts'] <= r['ts'] - 14400 for p in r['rows'])
    poisoned = [dict(r,points=1000) for r in future_history()]
    assert examples(rows+poisoned) == datasets


def test_empirical_cdf_is_monotonic_and_avoids_certainty():
    residuals = [-2., -1., -.5, 0., .5, 1., 2.]
    ps = [nba_prop_model.residual_cdf(x,residuals) for x in [-100,-1.7,-1,0,.2,1.9,100]]
    assert ps == sorted(ps)
    assert 0 < ps[0] < ps[-1] < 1
    with pytest.raises(ValueError):
        nba_prop_model.residual_cdf(0,[])


def test_active_artifact_and_implementation_pins_reject_tampering(tmp_path):
    from pathlib import Path
    from edgeterminal.nba_model_manifest import ARTIFACT_SHA256,SERVING_SOURCE_SHA256
    artifact = json.loads(json.dumps(nba_model.PROP_PARAMETERS))
    artifact['minutes_model']['intercept'] += .1
    path = tmp_path/'nba_prop_parameters.json';path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError,match='artifact hash mismatch'):
        nba_prop_model.load_parameters(path,ARTIFACT_SHA256,SERVING_SOURCE_SHA256)
    path.write_text(json.dumps(nba_model.PROP_PARAMETERS))
    source_root=Path(nba_model.__file__).parent
    for name in SERVING_SOURCE_SHA256:
        (tmp_path/name).write_bytes((source_root/name).read_bytes())
    (tmp_path/'nba_prop_model.py').write_text('# changed feature math\n')
    with pytest.raises(ValueError,match='implementation changed'):
        nba_prop_model.load_parameters(path,ARTIFACT_SHA256,SERVING_SOURCE_SHA256)


def test_parametric_normal_censors_zero_without_changing_positive_lines():
    from edgeterminal.pricing import norm_cdf
    for market in ('points','pra'):
        over,under,push=nba_prop_model.parametric_probabilities(market,1.,5.5,0.)
        assert under == 0.
        assert push == pytest.approx(norm_cdf((.5-1.)/5.5))
        assert over+under+push == pytest.approx(1.)
        assert nba_prop_model.parametric_probabilities(market,1.,5.5,-.5)==(1.,0.,0.)
        positive=nba_prop_model.parametric_probabilities(market,1.,5.5,2.5)
        assert positive[0]==pytest.approx(1.-norm_cdf((2.5-1.)/5.5))
        assert nba_prop_model.parametric_probabilities(market,1.,5.5,2.2)==positive


def test_offline_fit_cannot_overwrite_existing_version(tmp_path):
    path=tmp_path/'nba_prop_parameters.json'
    original='{"version":"v2","coefficient":1}'
    save_frozen_artifact(path,original)
    assert path.read_text() == original+'\n'
    with pytest.raises(ValueError,match='artifact is frozen'):
        save_frozen_artifact(path,'{"version":"v2","coefficient":2}')
    assert path.read_text() == original+'\n'
    save_frozen_artifact(path,original)
    assert path.read_text() == original+'\n'
