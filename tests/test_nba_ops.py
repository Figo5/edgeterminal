import json
from datetime import datetime, timezone

import pytest

from edgeterminal import db, nba_ops, nba_store, nba_data, nba_sources
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "nba.db"}')
    db.init()
    nba_store.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def test_date_validation_and_empty_read_never_fetch(isolated, monkeypatch):
    monkeypatch.setattr(nba_sources, 'fetch', lambda *a, **kw: pytest.fail('read requested a provider'))
    assert nba_ops.board('2026-10-20')['games'] == []
    with pytest.raises(ValueError):
        nba_ops.board('2026-99-50')
    with pytest.raises(ValueError):
        nba_ops.matchup('nfl-2026-reg-07-bos-nyk')


def test_espn_market_reads_each_sides_actual_line_without_synthetic_prices():
    comp={'odds':[{'provider':{'displayName':'DraftKings'},'pointSpread':{
        'home':{'close':{'line':'+2.5','odds':'-110'}},'away':{'close':{'line':'-2.5','odds':'-110'}}},
        'total':{'over':{'close':{'line':'o220.5','odds':'-105'}}},
        'moneyline':{'home':{'close':{'odds':'+120'}}}}]}
    m=nba_ops.parse_market(comp, 1000, 'source')
    assert m['home_spread']==2.5 and m['away_spread']==-2.5
    assert m['total']==220.5 and m['under_price'] is None
    assert m['home_moneyline']==120 and m['away_moneyline'] is None


def test_ungraded_nba_record_has_explicit_empty_state(isolated):
    r=nba_ops.performance()
    assert r['props']=={'won':0,'lost':0,'push':0,'void':0,'pending':0}
    assert r['designation']=='PASS' and r['readiness']=='RESEARCH_ONLY'


def test_step_failure_records_current_status_and_respects_cadence(isolated):
    calls=[]
    def fail():
        calls.append(1)
        raise ValueError('source did not respond')
    assert not nba_ops.step('test_source',1000,60,fail)['ok']
    assert nba_ops.step('test_source',1001,60,fail)['status']=='cached'
    assert len(calls)==1
    row=nba_ops.status()['checks'][0]
    assert row['status']=='error' and row['checked_at']==1000


def test_histories_are_deduplicated_by_game_and_player():
    rows=[{'game_id':'1','team':'BOS','points':100}, {'game_id':'1','team':'BOS','points':100}]
    assert len(nba_ops.unique_history(rows, 'team'))==1
    with pytest.raises(ValueError,match='conflict'):
        nba_ops.unique_history(rows+[dict(rows[0],points=101)], 'team')


def test_history_wait_retains_five_minute_retry_on_next_tick(isolated):
    calls=[]
    nba_ops.step('props',1000,10800,lambda:{'status':'history_loading'})
    nba_store.set_cadence('props',300,now=1000)
    nba_ops.step('props',1301,10800,lambda:calls.append(1) or {'status':'captured'})
    assert calls==[1]


def test_one_failed_player_does_not_starve_history_queue(isolated,monkeypatch):
    now=1792500000.
    original=db.q
    monkeypatch.setattr(db,'q',lambda sql,params=():[{'home_team':'BOS','away_team':'MIA','tipoff_ts':now+1000}]
        if sql.startswith('SELECT * FROM nba_games WHERE tipoff_ts>') else original(sql,params))
    roster=[{'player_id':str(i),'player':str(i),'team':'BOS','availability':'unknown'} for i in range(30)]
    monkeypatch.setattr(nba_ops,'load_team_history',lambda *a:[])
    monkeypatch.setattr(nba_ops,'load_roster',lambda code,*a:roster if code=='BOS' else [])
    loaded=[]
    def load(p,*a):
        if p['player_id']=='0':raise ValueError('HTTP 404')
        loaded.append(p['player_id']);db.kv_set('nba_player_history_checked_'+p['player_id'],now)
    monkeypatch.setattr(nba_ops,'load_player_history',load)
    nba_ops.histories(now);nba_ops.histories(now+301)
    assert len(set(loaded))==29
    assert db.kv_get('nba_player_history_error_0')['checked_at']==now


def test_capture_waits_for_game_day_window_and_both_rosters(isolated,monkeypatch):
    tip=1792537200.
    g={'game_id':'nba-2026-401909088','home_team':'BOS','away_team':'NYK',
       'tipoff_ts':tip,'tipoff_utc':'2026-10-20T23:00Z','game_date_et':nba_ops.today(tip)}
    original=db.q
    monkeypatch.setattr(db,'q',lambda sql,params=():[g] if sql.startswith('SELECT * FROM nba_games WHERE tipoff_ts>') else original(sql,params))
    monkeypatch.setattr(nba_ops,'_forecast',lambda *a:{'saved':True})
    monkeypatch.setattr(nba_ops.nba_quotes,'capture',lambda *a,**kw:pytest.fail('paid request before inputs/window'))
    assert nba_ops.capture_props(tip-9*3600)['status']=='outside_capture_window'
    assert nba_ops.capture_props(tip-2*3600)['status']=='history_loading'


def test_one_bad_final_does_not_block_other_game_grades(isolated,monkeypatch):
    import copy
    from test_nba_data import SUMMARY
    tip=nba_ops.parse_time('2026-03-15T17:00Z')
    summaries={}
    for index in range(2):
        eid=str(401810831+index)
        raw=copy.deepcopy(SUMMARY)
        raw['header']['id']=eid
        raw['header']['competitions'][0]['date']='2026-03-15T17:00Z' if index else '2026-03-14T17:00Z'
        final=nba_data.parse_summary(raw)['game'];summaries[eid]=raw
        before=tip-2*86400
        source=nba_store.save_artifact(nba_data.fetch('https://site.api.espn.com/test','fixture',
            getter=lambda *a:(200,b'{}'),now=before))
        scheduled=dict(final,status='scheduled',home_score=None,away_score=None)
        nba_store.upsert_game(scheduled,source,now=before)
        nba_store.save_forecast('game-'+scheduled['game_id'],scheduled['game_id'],'test',
            {'kind':'game','projection':{'home_margin':5,'total':210},'market':{'home_spread':-2.5,'total':210.5}},
            [source],now=before)
    def fetch(path,params,**kwargs):
        if params['event']=='401810832':raise ValueError('bad latest game')
        return {'data':summaries[params['event']],'observed_at':tip+15000,'url':'https://site.api.espn.com/test','sha256':'test'}
    after=tip+18000
    good_source=nba_store.save_artifact(nba_data.fetch('https://site.api.espn.com/result','fixture',
        getter=lambda *a:(200,b'{}'),now=tip+15000))
    monkeypatch.setattr(nba_sources,'fetch',fetch)
    monkeypatch.setattr(nba_ops,'artifact',lambda *a:good_source)
    result=nba_ops.grade_due(after)
    assert result['new_grades']==1 and len(result['source_errors'])==1
    assert db.q1('SELECT COUNT(*) n FROM nba_game_grade_revisions')['n']==1
    assert db.kv_get('nba_grade_checked_nba-2025-401810832')==after


def test_automatic_boxscore_grading_preserves_zero_dnp_push_and_missing(isolated,monkeypatch):
    from test_nba_data import SUMMARY
    final=nba_data.parse_summary(SUMMARY)['game'];gid=final['game_id']
    tip=nba_ops.parse_time(final['tipoff_utc']);before=tip-3600;after=tip+18000
    prior=nba_store.save_artifact(nba_data.fetch('https://site.api.espn.com/fixture','fixture',
        getter=lambda *a:(200,b'{}'),now=before-20))
    nba_store.upsert_game(dict(final,status='scheduled',home_score=None,away_score=None),prior,now=before)
    nba_store.save_forecast('props-'+gid,gid,'fixture',{'kind':'props','rows':[]},[prior],now=before)
    selections=[]
    for pid,prop,line in [('3064514','points',31.5),('3064514','rebounds',7),
                          ('2968436','points',0.5),('3195','assists',0.5),('999999','points',1.5)]:
        quote=nba_store.save_quote_observation(gid,nba_store.PROP_MARKETS[prop],pid,'over',line,-110,
            'fixture',before-10,source_artifact_id=prior,now=before)
        selections.append({'forecast_id':'props-'+gid,'espn_player_id':pid,'prop':prop,'side':'over',
                           'line':line,'quote_id':quote['quote_id']})
    lock_id='top5-'+gid;nba_store.lock_selections(lock_id,selections,now=before+10)
    raw=json.dumps(SUMMARY).encode()
    result_source=nba_store.save_artifact(nba_data.fetch('https://site.api.espn.com/summary','summary',
        getter=lambda *a:(200,raw),now=after-100))
    monkeypatch.setattr(nba_sources,'fetch',lambda *a,**kw:{'data':SUMMARY,'observed_at':after-100})
    monkeypatch.setattr(nba_ops,'artifact',lambda *a:result_source)
    result=nba_ops.grade_due(after)
    assert result['ok'] and result['new_grades']==5
    grades=nba_store.latest_grades(lock_id)
    assert [grades[i]['result'] for i in range(1,6)]==['win','push','loss','void','pending']
    assert grades[3]['actual_value']==0
    assert grades[4]['actual_value'] is None and grades[5]['actual_value'] is None
    # Repeated worker checks cannot add another grade for the same result.
    assert nba_ops.grade_due(after+3601)['new_grades']==0
