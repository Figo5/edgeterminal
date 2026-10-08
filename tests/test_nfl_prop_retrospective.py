"""Historical evidence cannot fabricate pregame timing or leak future labels."""
import copy
import hashlib
import json

import pytest

from edgeterminal import nfl_prop_retrospective as replay, nfl_prop_lab as lab, nfl_season_record as record


def quote(side='over', **kw):
    return dict(id=1 if side=='over' else 2, player_id='player', player_name='Example',
                market='receptions', side=side, sportsbook='book',line=3.5,price=-110,
                retrieved_at=80.,provider_updated_at=70.,kickoff_utc=100.,**kw)


def test_pairs_exclude_late_one_sided_duplicate_and_unpriced_quotes():
    pair=[quote(),quote('under')]
    assert len(replay.pairs(pair,100.))==2
    for field,value in [('retrieved_at',100.),('provider_updated_at',81.),('price',0),('line',float('nan'))]:
        bad=copy.deepcopy(pair);bad[0][field]=value
        assert replay.pairs(bad,100.)==[]
    assert replay.pairs(pair[:1],100.)==[]
    assert replay.pairs(pair+[pair[0]],100.)==[]


@pytest.mark.parametrize('week',[1,2,3])
def test_replay_training_stops_before_target_week(monkeypatch,week):
    hashes={'source':'hash'}
    monkeypatch.setattr(lab,'build_dataset',lambda *,cutoff: ({m:[
        {'season':2025,'week':18}, {'season':2026,'week':week-1},
        {'season':2026,'week':week}, {'season':2026,'week':week+1}] for m in lab.MARKETS}, {}, hashes))
    monkeypatch.setattr(lab,'_week3_features',lambda h,s,w: {})
    monkeypatch.setattr(lab,'_read_stats',lambda: ([],hashes))
    trained=[]
    monkeypatch.setattr(lab,'_matrix',lambda rows:(rows,[]))
    class Estimator:
        def fit(self,x,y):
            trained.extend(x);return self
    monkeypatch.setattr(lab,'_estimator',lambda family:Estimator())
    context=replay.model_context(2026,week,{m:{'selected_on_2024':'ridge'} for m in lab.MARKETS},hashes)
    assert context['cutoff']==(2026,week)
    assert trained and all((r['season'],r['week'])<(2026,week) for r in trained)


def test_changed_training_archive_is_rejected(monkeypatch):
    monkeypatch.setattr(lab,'build_dataset',lambda **kw: ({},{},{'source':'changed'}))
    with pytest.raises(ValueError,match='training files'):
        replay.model_context(2026,1,{}, {'source':'original'})


def test_missing_player_result_is_pending_and_negative_yardage_is_valid():
    row={'gsis_player_id':'player','market':'rushing_yards','line':.5,'side':'under'}
    assert replay.grade(row,{})['result']=='pending'
    assert replay.grade(row,{'player':{'rushing_yards':'-1'}})=={'result':'won','actual':-1.}
    row['market']='receptions'
    with pytest.raises(ValueError,match='stat'):
        replay.grade(row,{'player':{'receptions':'-1'}})


def test_replay_cohort_is_combined_without_replacing_archives_or_locking(monkeypatch):
    monkeypatch.setattr(record.nfl_top_five,'view',lambda:{'games':[]})
    monkeypatch.setattr(record.db,'q',lambda *a:[])
    game=dict(game_id='nfl-2026-reg-01-den-kc',week=1,basis='reconstructed_replay',
              rows=[dict(result='won',market='receptions',confidence=.8)])
    monkeypatch.setattr(replay,'load',lambda season:{'games':[game,game]})
    result=record.props()
    assert result['count']==1 and result['record']=={'won':1}
    assert result['locked']['count']==0 and result['replayed']['count']==1
    assert result['historical_count']==1 and result['diagnostics']['graded_count']==1


def test_locked_game_takes_precedence_over_replay(monkeypatch):
    game=dict(game_id='nfl-2026-reg-01-den-kc',rows=[dict(result='lost',market='receptions',confidence=.8)])
    monkeypatch.setattr(record.nfl_top_five,'view',lambda:{'games':[game]})
    monkeypatch.setattr(record.db,'q',lambda *a:[])
    monkeypatch.setattr(replay,'load',lambda season:{'games':[dict(game,week=1,basis='reconstructed_replay')]})
    result=record.props()
    assert result['count']==1 and result['locked']['count']==1 and result['replayed']['count']==0


def test_artifact_hash_and_source_tampering_are_rejected(tmp_path,monkeypatch):
    raw=b'official result source'
    sha=hashlib.sha256(raw).hexdigest()
    saved={'id':1,'line':3.5}
    body=dict(policy=replay.POLICY,season=2026,result_source_sha256=sha,result_source_url='official',
              games=[dict(basis='reconstructed_replay',selected_at=None,rows=[dict(
                  player_id=str(i),market='receptions',source_table='odds_snapshots',
                  source_sha256=replay.digest(saved),quote_id=1) for i in range(5)])])
    name=f'2026-{replay.digest(body)}.json'
    path=tmp_path/name;path.write_text(json.dumps(body))
    monkeypatch.setattr(replay,'DIRECTORY',tmp_path)
    monkeypatch.setattr(replay.db,'kv_get',lambda key:{'artifact':name})
    monkeypatch.setattr(replay.db,'q1',lambda sql,*args:dict(payload=raw,source_file='official') if 'source_artifacts' in sql else saved)
    assert replay.load()['games']
    saved['line']=4.5
    with pytest.raises(ValueError,match='quote source'):
        replay.load()
    path.write_text('{}')
    with pytest.raises(ValueError,match='integrity'):
        replay.load()
