"""New market artifacts are optional, receipt-bound and backward compatible."""
import copy

import pytest

from edgeterminal import nba_spread_model as model

TIP=1793314800.


def history():
    return [dict(game_id=f'{team}-{i}',team=team,opponent='CHI',played_at=TIP-(i+2)*86400,
                 points=110+i%7,opponent_points=105+i%5,home=i%2==0,source_artifact_id=i+1)
            for team in ('BOS','NYK') for i in range(20)]


def core():
    return dict(schema=1,kind='nba-spread-residual-v1',version='feature-fixture',
        feature_names=list(model.FEATURE_NAMES),training_cutoff=TIP-10*86400,intercept=0.,
        coefficients=[0.]*len(model.FEATURE_NAMES),means=[0.]*len(model.FEATURE_NAMES),
        scales=[1.]*len(model.FEATURE_NAMES),sd_margin=12.)


def anchored():
    return dict(core(),schema=2,kind='nba-spread-market-v2',version='anchor-fixture',
        core_training_cutoff=TIP-20*86400,sd_margin=8.,market_anchor=dict(
            kind='positive_convex_market_anchor',weight=.5,calibration_sd=8.,core_sd_margin=12.,
            market_basis='exact_saved_ESPN_home_spread'))


def quote(center=3.5):
    return dict(home_spread=-center,away_spread=center,observed_at=TIP-2000,
                source_artifact_id=1,source_sha256='a'*64)


def test_v1_projection_is_unchanged_and_v2_uses_only_the_supplied_contemporaneous_line(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    unanchored=model.project_game('BOS','NYK',history(),TIP,as_of=TIP-1800,artifact=core())
    out=model.project_game('BOS','NYK',history(),TIP,as_of=TIP-1800,artifact=anchored(),market=quote())
    assert out['home_margin']==pytest.approx(3.5+.5*(unanchored['home_margin']-3.5))
    assert out['raw_feature_margin']==pytest.approx(unanchored['home_margin'])
    assert out['market_anchor_applied'] and out['market_anchor_weight']==.5
    assert out['market_margin']==3.5 and out['market_observed_at']==TIP-2000
    assert out['game_component_version']=='anchor-fixture'
    assert out['spread_probability_home']+out['spread_probability_away']+out['spread_probability_push']==pytest.approx(1.)


def test_missing_market_keeps_the_retained_baseline_and_explicit_fallback(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    baseline=model.project_game('BOS','NYK',history(),TIP,as_of=TIP-1800)
    out=model.project_game('BOS','NYK',history(),TIP,as_of=TIP-1800,artifact=anchored())
    assert out['home_margin']==baseline['home_margin']
    assert out['game_component_version']==model.BASELINE_VERSION
    assert not out['features_applied'] and out['market_anchor_fallback']=='missing_supported_market'


def test_future_market_or_invalid_weight_cannot_affect_forecast(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    with pytest.raises(ValueError):
        model.project_game('BOS','NYK',history(),TIP,as_of=TIP-1800,
            artifact=anchored(),market=dict(quote(),observed_at=TIP-1700))
    bad=anchored();bad['market_anchor']['weight']=0.
    with pytest.raises(ValueError):model.validate_artifact(bad)
    bad=anchored();bad['core_training_cutoff']=TIP
    with pytest.raises(ValueError):model.validate_artifact(bad)


def test_anchoring_does_not_flip_a_tiny_negative_edge_by_rounding_to_the_market(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    before=model.project_game('BOS','NYK',history(),TIP,as_of=TIP-1800,artifact=core())
    q=quote(before['home_margin']+.01)
    config=anchored();config['market_anchor']['weight']=.25
    out=model.project_game('BOS','NYK',history(),TIP,as_of=TIP-1800,artifact=config,market=q)
    assert out['home_margin']+q['home_spread']<0
    assert out['raw_home_margin']==out['home_margin']
    assert out['unchanged_feature_side'] is True
