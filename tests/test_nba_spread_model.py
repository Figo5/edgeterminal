"""Game-component changes do not silently alter the pinned prop implementation."""
import copy
import math

import pytest

from edgeterminal import nba_model as baseline
from edgeterminal import nba_spread_model as model


TIP = 1793314800.


def history():
    return [dict(game_id=f'{team}-{i}', team=team, opponent='CHI',
                 played_at=TIP-(i+2)*86400, points=110+i%7,
                 opponent_points=105+i%5, home=i%2==0, source_artifact_id=i+1)
            for team in ('BOS', 'NYK') for i in range(20)]


def test_baseline_parity_and_explicit_game_identity(monkeypatch):
    monkeypatch.setattr(model, '_active', lambda: None)
    old = baseline.project_game('BOS','NYK',history(),TIP)
    out = model.project_game('BOS','NYK',history(),TIP,as_of=TIP-100)
    for key in ('margin','proj_home','proj_away','total','home_win_probability'):
        assert out[key] == old[key]
    assert out['home_margin'] == out['margin']
    assert out['game_component_version'] == model.BASELINE_VERSION
    assert out['designation']=='PASS' and out['readiness']=='RESEARCH_ONLY'
    assert len(out['selected_rows']) == 40


def test_future_and_post_asof_rows_cannot_enter_serving(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    original=history(); asof=TIP-2*86400
    expected=model.project_game('BOS','NYK',original,TIP,as_of=asof)
    poisoned=original+[dict(original[0],game_id='future',played_at=TIP-86400,points=245)]
    assert model.project_game('BOS','NYK',poisoned,TIP,as_of=asof)==expected
    assert expected['feature_cutoff'] == asof


def test_rest_is_derived_from_explicit_complete_schedule(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    schedules={t:dict(complete=True,last_game_tipoff=TIP-86400,missing_game_ids=[])
               for t in ('BOS','NYK')}
    out=model.project_game('BOS','NYK',history(),TIP,as_of=TIP-100,schedule=schedules)
    old=baseline.project_game('BOS','NYK',history(),TIP,history_complete=False)
    assert out['rest']=={'home':'b2b','away':'b2b'}
    assert out['margin']==pytest.approx(round(old['margin']+baseline.B2B_HOME_ADJ-baseline.B2B_AWAY_ADJ,2))
    schedules['BOS']['complete']=False
    assert model.project_game('BOS','NYK',history(),TIP,as_of=TIP-100,schedule=schedules)['rest']['home']=='unknown'


def test_availability_and_possessions_are_recorded_without_unvalidated_bumps(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    rows=history(); before=model.project_game('BOS','NYK',rows,TIP,as_of=TIP-100)
    for row in rows: row['possessions']=95.
    out=model.project_game('BOS','NYK',rows,TIP,as_of=TIP-100,
                           availability=[dict(player_id='1',team='BOS',availability='out')])
    assert out['margin']==before['margin'] and not out['pace_adjusted']
    assert out['availability_context']['numeric_adjustment_applied'] is False
    assert out['coverage']['possession_rows']==40


def test_invalid_asof_or_artifact_fails_closed(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    with pytest.raises(ValueError,match='before tipoff'):
        model.project_game('BOS','NYK',history(),TIP,as_of=TIP)
    with pytest.raises(ValueError):
        model.project_game('BOS','NYK',history(),TIP,as_of=math.nan)
    with pytest.raises(ValueError):
        model.project_game('BOS','NYK',history(),TIP,as_of=TIP-100,artifact={'schema':999})


def test_feature_extraction_does_not_read_target_or_future_labels():
    rows=history(); before=model.features('BOS','NYK',rows,TIP,as_of=TIP-100)
    poison=rows+[dict(rows[0],game_id='target',played_at=TIP,points=249,opponent_points=1)]
    assert model.features('BOS','NYK',poison,TIP,as_of=TIP-100)==before


def test_fresh_receipt_after_feature_lag_can_supply_older_completed_rows(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    rows=history()
    for row in rows:row['received_at']=TIP-200
    out=model.project_game('BOS','NYK',rows,TIP,as_of=TIP-100)
    assert out['status']=='ok' and len(out['selected_rows'])==40
    for row in rows:row['received_at']=TIP-50
    assert model.project_game('BOS','NYK',rows,TIP,as_of=TIP-100)['status']=='unavailable'


def test_contradictory_historical_scores_fail_closed_before_projection(monkeypatch):
    monkeypatch.setattr(model,'_active',lambda:None)
    rows=history()
    with pytest.raises(ValueError,match='contradictory duplicate'):
        model.project_game('BOS','NYK',rows+[dict(rows[0],points=249)],TIP,as_of=TIP-100)
