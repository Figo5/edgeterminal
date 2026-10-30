"""Market anchoring is fitted before the untouched actual-line holdout."""
import copy
import math

import pytest

from edgeterminal import nba_spread_market as market


START=1700000000.


def records(n=160):
    rows=[]
    for i in range(n):
        tip=START+i*86400
        line=-float((i%9)-4)-.5
        center=-line
        edge=float((i%11)-5)
        rows.append(dict(game_id=f'g{i}',tipoff_ts=tip,as_of=tip-1800,
            result_observed_at=tip+10800,actual_margin=center+.5*edge+(i%3-1)*.4,
            core_margin=center+edge,market=dict(home_spread=line,away_spread=-line,
            observed_at=tip-1900,source_artifact_id=i+1,source_sha256='a'*64)))
    return rows


def test_chronological_partition_keeps_outer_holdout_and_purges_late_labels():
    rows=records()
    split=market.partition(rows,now=rows[-1]['result_observed_at']+1)
    assert split['status']=='ready'
    cohorts=split['cohorts']
    assert len(cohorts['validation'])>=60
    assert len(cohorts['fit'])>=40 and len(cohorts['development'])>=20 and len(cohorts['calibration'])>=20
    ids=[{r['game_id']for r in cohorts[k]}for k in ('fit','development','calibration','validation')]
    assert sum(map(len,ids))==len(set.union(*ids))
    for earlier,later in (('fit','development'),('development','calibration'),('calibration','validation')):
        assert max(r['result_observed_at']for r in cohorts[earlier])<min(r['as_of']for r in cohorts[later])
    late=copy.deepcopy(rows)
    late[30]['result_observed_at']=late[-1]['result_observed_at']
    again=market.partition(late,now=late[-1]['result_observed_at']+1)
    assert 'g30' not in {r['game_id']for r in again['cohorts']['fit']}


def test_partition_waits_with_only_three_games_and_never_uses_future_labels():
    rows=records(3)
    result=market.partition(rows,now=rows[-1]['result_observed_at']+1)
    assert result['status']=='waiting'
    assert market.partition(records(),now=START)['status']=='waiting'


def test_anchor_uses_fit_and_development_only_and_calibration_is_separate():
    split=market.partition(records(),now=START+200*86400)['cohorts']
    first=market.fit_anchor(split['fit'],split['development'],split['calibration'])
    assert 0<first['weight']<1
    altered=copy.deepcopy(split)
    for row in altered['calibration']:row['actual_margin']+=30*(int(row['game_id'][1:])%2)
    after=market.fit_anchor(altered['fit'],altered['development'],altered['calibration'])
    assert after['weight']==first['weight'] and after['penalty']==first['penalty']
    assert after['calibration_sd']!=first['calibration_sd']
    for row in altered['validation']:row['actual_margin']=-1000.
    unchanged=market.fit_anchor(altered['fit'],altered['development'],split['calibration'])
    assert unchanged==first


def test_convex_anchor_preserves_the_feature_model_side_without_claiming_ats_gain():
    for center,core in ((10.,10.01),(10.,9.99),(-5.,3.),(-5.,-8.)):
        for weight in (.05,.25,.5,1.):
            anchored=market.blend(core,center,weight)
            assert (anchored>center)==(core>center)
            assert min(center,core)<=anchored<=max(center,core)
    with pytest.raises(ValueError):market.blend(1.,2.,0.)


def test_quote_context_uses_original_asof_line_and_rejects_future_or_unproven_quotes():
    row=records(1)[0]
    context=market.quote_context(row['market'],row['as_of'],row['tipoff_ts'])
    assert context['margin']==-row['market']['home_spread']
    assert market.quote_context({},row['as_of'],row['tipoff_ts']) is None
    for change in ({'observed_at':row['as_of']+1},{'source_artifact_id':None},
                   {'source_sha256':'bad'},{'away_spread':99.}):
        with pytest.raises(ValueError):market.quote_context(dict(row['market'],**change),row['as_of'],row['tipoff_ts'])


def test_integer_spread_probabilities_keep_push_mass_and_conditional_cover_score():
    out=market.cover_probabilities(5.,12.,-5.)
    assert out['push']>0 and out['home']+out['away']+out['push']==pytest.approx(1.)
    assert out['conditional_home']==pytest.approx(.5)
    half=market.cover_probabilities(5.,12.,-5.5)
    assert half['push']==0 and half['conditional_home']<.5


def test_anchor_fit_refuses_overlapping_phase_labels_and_nonfinite_features():
    split=market.partition(records(),now=START+200*86400)['cohorts']
    bad=copy.deepcopy(split['fit'])
    bad[-1]['result_observed_at']=split['development'][0]['as_of']+1
    with pytest.raises(ValueError,match='chronolog'):market.fit_anchor(bad,split['development'],split['calibration'])
    bad=copy.deepcopy(split['fit']);bad[0]['core_margin']=math.nan
    with pytest.raises(ValueError):market.fit_anchor(bad,split['development'],split['calibration'])


def test_used_validation_dates_are_never_retested_but_may_become_later_training():
    rows=records(240)
    first=market.partition(rows[:160],now=START+300*86400)
    spent=first['cohorts']['validation']
    dates={market.datetime.fromtimestamp(r['tipoff_ts'],market.ET).date().isoformat() for r in spent}
    second=market.partition(rows,now=START+300*86400,
        excluded_validation_game_ids=[r['game_id']for r in spent],excluded_validation_dates=dates)
    assert second['status']=='ready'
    assert not {r['game_id']for r in spent}&{r['game_id']for r in second['cohorts']['validation']}
    assert not dates&{market.datetime.fromtimestamp(r['tipoff_ts'],market.ET).date().isoformat() for r in second['cohorts']['validation']}
    assert {r['game_id']for r in spent}&{r['game_id']for phase in ('fit','development','calibration')for r in second['cohorts'][phase]}
