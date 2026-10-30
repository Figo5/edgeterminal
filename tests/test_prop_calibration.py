import copy
import math

import pytest

from edgeterminal import prop_calibration as calibration


def observations(n=80):
    return [dict(game_id='g'+str(i//4), quote_id=i+1, probability_over=.9,
                 probability_under=.1, probability_push=0., actual=12. if i%2 else 8.,
                 line=10.5, result_observed_at=100.+i) for i in range(n)]


def test_actual_line_calibration_reduces_overconfidence_without_changing_push_mass():
    config=calibration.fit_calibrator(observations(),cutoff=200.)
    assert config['method']=='shrunk_logit'
    got=calibration.apply_calibrator(dict(over=.72,under=.08,push=.2),config)
    assert got['push']==.2 and sum(got.values())==pytest.approx(1.)
    assert .4<got['over']<.72 and got['under']>0.
    assert config['fit_rows']==80 and config['fit_games']==20
    assert config['label_max_at']<200.


def test_sparse_or_single_outcome_calibration_is_exact_identity():
    probabilities=dict(over=.63,under=.27,push=.1)
    sparse=calibration.fit_calibrator(observations(8),cutoff=200.)
    assert sparse['method']=='identity'
    assert calibration.apply_calibrator(probabilities,sparse)==probabilities
    one=observations()
    for r in one:r['actual']=12.
    assert calibration.fit_calibrator(one,cutoff=200.)['method']=='identity'


@pytest.mark.parametrize('problem',['future','duplicate','nan','bad_mass'])
def test_invalid_or_postcutoff_calibration_evidence_fails_closed(problem):
    rows=observations()
    if problem=='future':rows[0]['result_observed_at']=200.
    if problem=='duplicate':rows[1]['quote_id']=rows[0]['quote_id']
    if problem=='nan':rows[0]['probability_over']=float('nan')
    if problem=='bad_mass':rows[0]['probability_push']=.2
    with pytest.raises(ValueError):calibration.fit_calibrator(rows,cutoff=200.)


def test_calibration_is_monotone_finite_and_supports_zero_event_inputs():
    config=calibration.fit_calibrator(observations(),cutoff=200.)
    output=[calibration.apply_calibrator(dict(over=p,under=1-p,push=0.),config)['over']
            for p in (0.,.1,.5,.9,1.)]
    assert output==sorted(output)
    assert all(math.isfinite(p) and 0<p<1 for p in output)
    with pytest.raises(ValueError):
        calibration.apply_calibrator(dict(over=.6,under=.4,push=0.),dict(config,slope=-1.))


def paired():
    return [dict(game_id='g'+str(i//2),quote_id=i+1,market=market,side='over',
                 line=10.5,actual=12. if i%2 else 8.,result_observed_at=100.+i,
                 baseline_prediction=10.,candidate_prediction=10.,
                 baseline_probability=.9,candidate_probability=.6)
            for market in ('points','assists') for i in range(40)]


def test_selective_market_choice_uses_only_inner_data_and_retains_regressions():
    rows=paired()
    for r in rows:
        if r['market']=='assists':r['candidate_prediction']=100.
    result=calibration.choose_market_updates(rows,('points','assists'),cutoff=200.)
    assert result['changed_markets']==['points']
    assert 'mae_regression' in result['markets']['assists']['reasons']
    assert result['selection_label_max_at']<200.
    assert set(result['selection_game_ids'])=={'g'+str(i) for i in range(20)}


def test_unchanged_and_sparse_markets_never_claim_a_selected_improvement():
    rows=paired()
    for r in rows:r['candidate_probability']=r['baseline_probability']
    result=calibration.choose_market_updates(rows,('points','assists','rebounds'),cutoff=200.)
    assert result['changed_markets']==[]
    assert 'insufficient_rows' in result['markets']['rebounds']['reasons']
    changed=copy.deepcopy(rows);changed[0]['result_observed_at']=200.
    with pytest.raises(ValueError):
        calibration.choose_market_updates(changed,('points','assists'),cutoff=200.)


def test_saved_six_decimal_mass_is_identity_without_rounding_drift():
    probabilities=dict(over=.333334,under=.333334,push=.333333)
    config=calibration.fit_calibrator([],cutoff=200.)
    assert calibration.apply_calibrator(probabilities,config)==probabilities
    config.update(method='shrunk_logit',slope=.8)
    output=calibration.apply_calibrator(probabilities,config)
    assert sum(output.values())==pytest.approx(1.) and output['push']==probabilities['push']
