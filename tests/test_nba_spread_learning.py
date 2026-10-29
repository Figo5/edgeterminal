"""Spread learning is separate, chronological and cannot fit while serving."""
import copy
import hashlib
import json

import pytest

from edgeterminal import db, nba_store, nba_spread_data as data
from edgeterminal import nba_spread_learning as learning


@pytest.fixture
def database(tmp_path,monkeypatch):
    from edgeterminal.config import settings
    if db._conn is not None:db._conn.close();db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path / "test.db"}')
    db.init(); nba_store.ensure_schema(); learning.ensure_schema()
    yield
    db._conn.close();db._conn=None


def test_promotion_requires_actual_lines_and_more_than_a_tiny_margin_gain():
    rows=[dict(game_id=f'g{i}',actual_margin=(i%7)-3,home_spread=.5,
               baseline_margin=8.,candidate_margin=7.99,
               baseline_probability=.6,candidate_probability=.6,
               forecast_at=100+i,tipoff=1000+i,result_at=2000+i)
          for i in range(60)]
    decision=learning.evaluate(rows,training_cutoff=90.)
    assert not decision['passed'] and 'margin_improvement_too_small' in decision['reasons']
    with pytest.raises(ValueError,match='line'):
        learning.evaluate([dict(r,home_spread=None) for r in rows],training_cutoff=90.)
    with pytest.raises(ValueError,match='chronolog'):
        learning.evaluate(rows,training_cutoff=105.)


def test_source_verification_rejects_wrong_owner_hash_or_late_receipt():
    body=json.dumps({'events':[]}).encode()
    source=dict(id=1,url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2026&seasontype=2',
                body=body,received_at=100.,http_status=200,sha256=hashlib.sha256(body).hexdigest())
    assert data.source_history(source,cutoff=101.)==[]
    for bad in ({'sha256':'0'*64},{'received_at':102.},{'url':source['url'].replace('/teams/2/','/teams/999/')},
                {'url':source['url'].replace('site.api.espn.com','evil.example')}):
        with pytest.raises(ValueError): data.source_history(dict(source,**bad),cutoff=101.)


def test_run_is_bounded_and_retries_waiting_on_new_evidence(database,monkeypatch):
    monkeypatch.setattr(data,'refresh',lambda *a,**k:dict(status='ok',requests=0))
    calls=[]
    monkeypatch.setattr(learning,'build_candidate',lambda now: calls.append(now) or dict(status='waiting',reason='more_future_games'))
    first=learning.run(100000.)
    assert first['status']=='waiting'
    assert learning.run(100001.)['status']=='cached' and len(calls)==1
    assert learning.status(now=100002.)['active'] is None


def test_registry_and_learning_runs_are_append_only(database):
    learning._record(100.,'waiting',{'reason':'test'})
    with pytest.raises(Exception,match='append-only'):
        db.ex('DELETE FROM nba_spread_learning_runs')


def test_gate_accepts_material_consistent_improvement_and_preserves_coverage():
    rows=[]
    for i in range(80):
        actual=10. if i%2 else -10.
        rows.append(dict(game_id=f'g{i}',actual_margin=actual,home_spread=.5,
            baseline_margin=8.,candidate_margin=actual+.3,
            baseline_probability=.6,candidate_probability=.9 if actual>0 else .1,
            forecast_at=10000+i*86400,tipoff=20000+i*86400,result_at=40000+i*86400))
    decision=learning.evaluate(rows,training_cutoff=9000.)
    assert decision['passed'] and decision['all_supported_games_predicted']
    assert decision['paired_day_intervals']['margin_mae']['high']<0
    assert decision['paired_day_intervals']['cover_brier']['high']<0
    with pytest.raises(ValueError,match='repeats'):
        learning.evaluate(rows+[rows[0]],training_cutoff=9000.)


def test_inactive_failed_candidate_cannot_be_activated_by_sql(database):
    db.ex('INSERT INTO nba_spread_model_candidates VALUES(?,?,?,?,?,?)',
          ('failed',100.,90.,'{}','x',json.dumps(dict(passed=False,rows=100,days=10))))
    with pytest.raises(Exception,match='not passed'):
        db.ex('INSERT INTO nba_spread_model_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
              ('failed',101.,'activated','{}'))


def test_near_tipoff_defers_bootstrap_and_training(database,monkeypatch):
    monkeypatch.setattr(data,'refresh',lambda *a,**k:pytest.fail('bootstrap near tipoff'))
    original=db.q1
    monkeypatch.setattr(db,'q1',lambda sql,args=(): {'found':1} if 'tipoff_ts>?' in sql else original(sql,args))
    assert learning.run(100.)['status']=='deferred'


def test_invalid_active_bundle_rolls_back_to_baseline(database,monkeypatch):
    db.ex('INSERT INTO nba_spread_model_candidates VALUES(?,?,?,?,?,?)',
          ('bad',100.,90.,'{}','not-the-sha',json.dumps(dict(passed=True,rows=100,days=10))))
    db.ex('INSERT INTO nba_spread_model_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
          ('bad',101.,'activated','{}'))
    monkeypatch.setattr(data,'refresh',lambda *a,**k:pytest.fail('bootstrap before integrity recovery'))
    assert learning.run(102.)['status']=='rolled_back'
    assert learning.active() is None


def test_box_possessions_are_explicit_estimates_and_require_final():
    payload=dict(header=dict(competitions=[dict(status=dict(type=dict(completed=True)))]),
        boxscore=dict(teams=[dict(team=dict(id=ident,abbreviation=abbr),statistics=[
            dict(name='fieldGoalsMade-fieldGoalsAttempted',displayValue='40-90'),
            dict(name='freeThrowsMade-freeThrowsAttempted',displayValue='15-20'),
            dict(name='offensiveRebounds',displayValue='10'),
            dict(name='totalTurnovers',displayValue='12')]) for ident,abbr in (('2','BOS'),('18','NY'))]))
    result=data.box_features(payload)
    assert result['possessions']==pytest.approx(100.8)
    assert result['possession_method']=='symmetric_box_estimate_044'
    payload['header']['competitions'][0]['status']['type']['completed']=False
    with pytest.raises(ValueError,match='not final'):data.box_features(payload)


def test_historical_competitor_numeric_identity_cannot_be_overridden_by_abbreviation():
    event=dict(id='401234567',date='2025-11-01T00:00:00Z',competitions=[dict(
        status=dict(type=dict(completed=True)),competitors=[
        dict(homeAway='home',team=dict(id='999',abbreviation='BOS'),score=dict(value=100)),
        dict(homeAway='away',team=dict(id='18',abbreviation='NY'),score=dict(value=99))])])
    body=json.dumps(dict(events=[event])).encode()
    source=dict(id=1,url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2026&seasontype=2',
                body=body,http_status=200,received_at=1793314800.,sha256=hashlib.sha256(body).hexdigest())
    with pytest.raises(ValueError,match='conflict'):data.source_history(source,cutoff=1793314801.)


def test_calibration_labels_cannot_change_fitted_coefficients_or_selected_penalty(monkeypatch):
    rows=[]
    for i in range(1000):
        ts=1700000000.+i*86400
        values=[float(i%9),float(i%7),float(i%5),float(i%3),0.,0.]
        rows.append(dict(game_id=f'g{i}',ts=ts,values=values,baseline_margin=values[0],actual_margin=values[0]+.3*values[1]-.2*values[2]))
    monkeypatch.setattr(learning,'examples',lambda history,cutoff:copy.deepcopy(rows))
    before=learning.fit_candidate([],1800000000.)
    for row in rows[-120:]:row['actual_margin']+=20.
    after=learning.fit_candidate([],1800000000.)
    for key in ('intercept','coefficients','means','scales','development_alpha'):
        assert before[key]==after[key]
    assert before['training']['fit_game_ids']==after['training']['fit_game_ids']
    assert not set(before['training']['fit_game_ids']) & set(before['training']['calibration_game_ids'])


def _box_source():
    from test_nba_data import SUMMARY
    from edgeterminal import nba_identity
    payload=copy.deepcopy(SUMMARY)
    payload['boxscore']['teams']=[dict(team=dict(id=ident,abbreviation=abbr),statistics=[
        dict(name='fieldGoalsMade-fieldGoalsAttempted',displayValue='40-90'),
        dict(name='freeThrowsMade-freeThrowsAttempted',displayValue='15-20'),
        dict(name='offensiveRebounds',displayValue='10'),dict(name='totalTurnovers',displayValue='12')])
        for ident,abbr in (('25','OKC'),('16','MIN'))]
    raw=json.dumps(payload).encode()
    tip=nba_identity.parse_tipoff(payload['header']['competitions'][0]['date']).timestamp()
    source=dict(id=1,event_id='401810831',kind='final-boxscore',
        url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401810831',
        received_at=tip+3*3600,http_status=200,sha256=hashlib.sha256(raw).hexdigest(),body=raw)
    rows=[dict(game_id='401810831',team='OKC',opponent='MIN',points=116,opponent_points=103,
        played_at='2026-03-15T17:00Z',home=True,source_artifact_id=10)]
    return rows,source,tip


def test_enrichment_uses_existing_received_boxes_without_requests(database,monkeypatch):
    rows,source,tip=_box_source()
    monkeypatch.setattr(data,'_box_sources',lambda ids,asof:[source])
    monkeypatch.setattr(data.nba_sources,'fetch',lambda *a,**k:pytest.fail('enrichment fetched source'))
    before=copy.deepcopy(rows)
    out=data.enrich_from_boxes(rows,tip+4*3600)
    assert out[0]['possessions']==pytest.approx(100.8)
    assert out[0]['possession_source_artifact_id']==1 and rows==before
    assert out[0]['possession_method']=='symmetric_box_estimate_044'


def test_enrichment_excludes_post_asof_receipts_and_rejects_wrong_source_identity(database,monkeypatch):
    rows,source,tip=_box_source()
    monkeypatch.setattr(data,'_box_sources',lambda ids,asof:[source])
    assert data.enrich_from_boxes(rows,tip+2*3600)==rows
    source['event_id']='401810832'
    with pytest.raises(ValueError,match='identity conflict'):data.enrich_from_boxes(rows,tip+4*3600)


def test_enrichment_rejects_score_conflicts_and_altered_raw_boxes(database,monkeypatch):
    rows,source,tip=_box_source()
    monkeypatch.setattr(data,'_box_sources',lambda ids,asof:[source])
    with pytest.raises(ValueError,match='score/time/team conflict'):
        data.enrich_from_boxes([dict(rows[0],points=200)],tip+4*3600)
    source['body']+=b' '
    with pytest.raises(ValueError,match='hash'):
        data.enrich_from_boxes(rows,tip+4*3600)


def test_bootstrap_completion_refreshes_waiting_message_without_one_day_delay(database,monkeypatch):
    calls=[]
    monkeypatch.setattr(data,'refresh',lambda *a,**k:dict(status='ok',requests=0,remaining_schedules=1 if not calls else 0))
    monkeypatch.setattr(learning,'build_candidate',lambda now:calls.append(now) or dict(status='waiting',
        reason='historical_seasons_bootstrapping' if len(calls)==1 else 'more_verified_future_spread_games'))
    assert learning.run(100000.)['reason']=='historical_seasons_bootstrapping'
    assert learning.run(100301.)['reason']=='more_verified_future_spread_games'
    assert len(calls)==2


def test_training_rehashes_box_evidence_even_when_enrichment_parse_is_cached(database,monkeypatch):
    from test_nba_data import SUMMARY
    rows,source,tip=_box_source()
    source['id']=nba_store.save_artifact(source)
    metadata={k:v for k,v in source.items() if k!='body'}
    monkeypatch.setattr(data,'_box_sources',lambda ids,asof:[metadata])
    payload=dict(team=dict(id='25',abbreviation='OKC'),events=[dict(id='401810831',
        date='2026-03-15T17:00Z',competitions=copy.deepcopy(SUMMARY['header']['competitions']))])
    raw=json.dumps(payload).encode()
    nba_store.save_artifact(dict(kind='team-history',
        url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/25/schedule?season=2026&seasontype=2',
        received_at=tip+4*3600,http_status=200,sha256=hashlib.sha256(raw).hexdigest(),body=raw))
    history,sources=data.training_history(tip+5*3600)
    assert history[0]['possession_source_artifact_id']==source['id']
    assert any(s['id']==source['id'] for s in sources)
    # Simulate storage damage outside normal append-only application writes.
    # The metadata and cached transform remain unchanged, so the fitting-path
    # byte recheck is what must reject this record.
    db.ex('DROP TRIGGER nba_source_artifacts_no_update')
    db.ex('UPDATE nba_source_artifacts SET body=? WHERE id=?',(b'{}',source['id']))
    with pytest.raises(ValueError,match='hash'):
        data.training_history(tip+5*3600)
