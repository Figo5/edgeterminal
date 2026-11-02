"""The spread learner never trains an anchor on its outer validation results."""
import copy
import hashlib
import json

import pytest

from edgeterminal import db,nba_store,nba_spread_learning as learning,nba_spread_model as model
from edgeterminal import nba_spread_market as market
from edgeterminal.config import settings

START=1700000000.


@pytest.fixture
def database(tmp_path,monkeypatch):
    if db._conn is not None:db._conn.close();db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path / "spread-anchor.db"}')
    monkeypatch.setattr(learning,'_publication_time',lambda stamp:stamp,raising=False)
    db.init();nba_store.ensure_schema();learning.ensure_schema()
    yield
    db._conn.close();db._conn=None


def snapshots(n=160):
    result=[]
    for i in range(n):
        tip=START+i*86400;line=-float((i%9)-4)-.5;center=-line;edge=float((i%11)-5)
        result.append(dict(game_id=f'g{i}',home_team='BOS',away_team='NYK',tipoff_ts=tip,
            forecast_sha256='c'*64,
            as_of=tip-1800,result_observed_at=tip+10800,actual_margin=center+.5*edge+(i%3-1)*.4,
            projection=dict(home_margin=center+edge,sd_margin=12.,model_version=model.BASELINE_VERSION),
            market=dict(home_spread=line,away_spread=-line,observed_at=tip-1900,
                        source_artifact_id=i+1,source_sha256='a'*64),
            source_artifacts=[dict(id=i+1,sha256='a'*64,received_at=tip-1900)],
            label_source_artifact_id=10000+i,label_source_sha256='b'*64,
            input_manifest=dict(rows=[dict(team='BOS',game_id=f'prior{i}',core_fixture=center+edge)])))
    return result


def setup_builder(monkeypatch,rows):
    calls=[]
    monkeypatch.setattr(learning,'_snapshots',lambda now:copy.deepcopy(rows))
    monkeypatch.setattr(learning.data,'ready_at',lambda now:START-10000)
    monkeypatch.setattr(learning.data,'training_history',lambda cutoff:([],[]))
    monkeypatch.setattr(learning,'active',lambda:None)
    def fitted(history,cutoff):
        calls.append(cutoff)
        return dict(schema=1,kind='nba-spread-residual-v1',version='core-test',
            feature_names=list(model.FEATURE_NAMES),training_cutoff=cutoff,intercept=0.,
            coefficients=[0.]*len(model.FEATURE_NAMES),means=[0.]*len(model.FEATURE_NAMES),
            scales=[1.]*len(model.FEATURE_NAMES),sd_margin=12.,training={},code_sha256={})
    monkeypatch.setattr(learning,'fit_candidate',fitted)
    def predict(home,away,history,tipoff,*,as_of,schedule,artifact,market):
        core=next(row['core_fixture'] for row in history if 'core_fixture' in row)
        mean=core
        if artifact['schema']==2:mean=market_module.blend(core,-market['home_spread'],artifact['market_anchor']['weight'])
        return dict(status='ok',home_margin=mean,raw_feature_margin=core,sd_margin=artifact['sd_margin'])
    market_module=market
    monkeypatch.setattr(model,'project_game',predict)
    return calls


def test_builder_freezes_core_before_anchor_fit_and_keeps_actual_holdout_disjoint(database,monkeypatch):
    rows=snapshots();calls=setup_builder(monkeypatch,rows)
    result=learning.build_candidate(START+200*86400)
    assert result['status']=='ready'
    artifact=result['artifact'];cohorts=artifact['actual_line_cohorts']
    assert artifact['schema']==2 and artifact['kind']=='nba-spread-market-v2'
    assert len(cohorts['validation'])>=60
    index={r['game_id']:r for r in rows}
    assert calls[0]<min(index[gid]['as_of'] for gid in cohorts['fit'])
    assert artifact['training_cutoff']<min(index[gid]['as_of']for gid in cohorts['validation'])
    assert not set(cohorts['validation'])&set(artifact['market_anchor']['fit_game_ids'])
    assert not set(cohorts['validation'])&set(artifact['market_anchor']['calibration_game_ids'])
    assert result['evaluation']['benchmarks']['anchor_changed_feature_side']==0
    assert result['evaluation']['benchmarks']['market_only']['ats_wins'] is None
    assert all(r['home_spread']==index[r['game_id']]['market']['home_spread'] for r in result['evaluation']['rows_detail'])
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_model_candidates')['n']==0
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_model_events')['n']==0


def test_outer_target_poisoning_cannot_change_core_or_blend_parameters(database,monkeypatch):
    rows=snapshots();setup_builder(monkeypatch,rows)
    first=learning.build_candidate(START+200*86400)
    validation=set(first['artifact']['actual_line_cohorts']['validation'])
    for row in rows:
        if row['game_id']in validation:row['actual_margin']=-50.
    second=learning.build_candidate(START+200*86400)
    for key in ('intercept','coefficients','means','scales','market_anchor','sd_margin','core_training_cutoff'):
        assert first['artifact'][key]==second['artifact'][key]
    assert first['evaluation']['candidate_mae']!=second['evaluation']['candidate_mae']


def test_builder_waits_for_market_prefixes_instead_of_reusing_the_sixty_holdout(database,monkeypatch):
    rows=snapshots(60);calls=setup_builder(monkeypatch,rows)
    result=learning.build_candidate(START+200*86400)
    assert result['status']=='waiting' and not calls
    assert result['reason']=='more_chronological_market_anchor_games'
    assert result['coverage']['validation']==60 and result['coverage']['fit']==0


def test_prefix_source_descriptor_received_after_forecast_cannot_train_anchor(database,monkeypatch):
    rows=snapshots();setup_builder(monkeypatch,rows)
    rows[0]['source_artifacts'][0]['received_at']=rows[0]['as_of']+1
    with pytest.raises(ValueError,match='received after'):
        learning.build_candidate(START+200*86400)


def _rejected_proposal(version='proposal-one'):
    receipts=[dict(game_id=f'held{i}',game_date_et=f'2024-01-{i%28+1:02d}',
        forecast_sha256='c'*64,label_sha256='b'*64)for i in range(60)]
    return dict(status='ready',artifact=dict(kind='nba-spread-market-v2',version=version,
        training_cutoff=100.,validation_receipts=receipts),evaluation=dict(passed=False,rows=60,days=28,reasons=['no_gain']))


def test_rejection_consumes_holdout_once_and_family_rename_cannot_refund_it(database,monkeypatch):
    proposal=_rejected_proposal()
    monkeypatch.setattr(learning.data,'refresh',lambda *a,**kw:pytest.fail('learner fetched sources'))
    monkeypatch.setattr(learning,'build_candidate',lambda now:copy.deepcopy(proposal))
    assert learning.run_fitting(10000.)['status']=='rejected'
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_validation_consumption')['n']==60
    assert learning.active() is None
    with pytest.raises(Exception,match='append-only'):db.ex('DELETE FROM nba_spread_validation_consumption')
    proposal=_rejected_proposal('renamed-family-proposal')
    result=learning.run_fitting(10000.+learning.WEEK+1)
    assert result['status']=='error'
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_model_candidates')['n']==1
    assert db.q1('SELECT COUNT(*) n FROM nba_spread_model_events')['n']==1


def _history_source(received,points=100):
    event=dict(id='401700001',date='2023-11-01T00:00Z',competitions=[dict(
        status=dict(type=dict(completed=True)),competitors=[
        dict(homeAway='home',team=dict(id='2',abbreviation='BOS'),score=str(points)),
        dict(homeAway='away',team=dict(id='18',abbreviation='NY'),score='99')])])
    body=json.dumps(dict(team=dict(id='2',abbreviation='BOS'),events=[event])).encode()
    return nba_store.save_artifact(dict(kind='team-history',
        url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2024&seasontype=2',
        body=body,sha256=hashlib.sha256(body).hexdigest(),http_status=200,received_at=received))


def test_feature_replay_uses_archived_asof_view_not_latest_mutable_history(database,monkeypatch):
    older=_history_source(START-1000,100)
    newer=_history_source(START+1000,101)
    _history_source(START+3000,102)
    db.kv_set('nba_team_history_BOS',[dict(points=249)])
    replay=learning._FeatureReplay()
    first=replay.history(START);second=replay.history(START+2000)
    assert first[0]['points']==100 and first[0]['source_artifact_id']==older
    assert second[0]['points']==101 and second[0]['source_artifact_id']==newer
    replay.use(first,START);replay.use(second,START+2000)
    catalog=replay.catalog()
    assert {r['id']for r in catalog}=={older,newer}
    assert all(r['received_at']<=r['available_by']for r in catalog)


def test_feature_replay_revalidates_cached_source_bytes_before_proposal_returns(database):
    ident=_history_source(START-1000)
    replay=learning._FeatureReplay();rows=replay.history(START);replay.use(rows,START)
    db.ex('DROP TRIGGER nba_source_artifacts_no_update')
    db.ex('UPDATE nba_source_artifacts SET body=? WHERE id=?',(b'{}',ident))
    with pytest.raises(ValueError,match='hash'):replay.catalog()
