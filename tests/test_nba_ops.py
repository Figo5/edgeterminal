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


def _grading_case(eid='401810831', status='final', props=True, roster_artifact=None):
    import copy
    from test_nba_data import SUMMARY
    raw=copy.deepcopy(SUMMARY);raw['header']['id']=eid
    final=nba_data.parse_summary(raw)['game'];tip=nba_ops.parse_time(final['tipoff_utc']);before=tip-3600
    source=nba_store.save_artifact(nba_data.fetch('https://site.api.espn.com/fixture','fixture',
        getter=lambda *a:(200,b'{}'),now=before-20))
    gid=final['game_id']
    sources=[source]+([roster_artifact] if roster_artifact is not None else [])
    nba_store.upsert_game(dict(final,status='scheduled',home_score=None,away_score=None),source,now=before)
    nba_store.save_forecast('game-'+gid,gid,'fixture',{'kind':'game',
        'projection':{'home_margin':5,'total':210},'market':{'home_spread':-2.5,'total':210.5}},sources,now=before)
    if props:
        nba_store.save_forecast('props-'+gid,gid,'fixture',{'kind':'props','rows':[]},sources,now=before)
        items=[]
        for pid,prop,line in [('3064514','points',31.5),('3064514','rebounds',7),
                              ('2968436','points',0.5),('3195','assists',0.5),('4433255','points',20.5)]:
            quote=nba_store.save_quote_observation(gid,nba_store.PROP_MARKETS[prop],pid,'over',line,-110,
                'fixture',before-10,source_artifact_id=source,now=before)
            items.append({'forecast_id':'props-'+gid,'espn_player_id':pid,'prop':prop,'side':'over',
                          'line':line,'quote_id':quote['quote_id']})
        nba_store.lock_selections('top5-'+gid,items,now=before+10)
    if status!='scheduled':
        nba_store.upsert_game(dict(final,status=status,
            home_score=final['home_score'] if status=='final' else None,
            away_score=final['away_score'] if status=='final' else None),source,now=tip+3600)
    return raw,final,source,tip


def _summary_feed(monkeypatch, summaries, calls):
    from urllib.parse import parse_qs, urlparse
    def get(url, **kwargs):
        eid=parse_qs(urlparse(url).query)['event'][0];calls.append(eid)
        return 200,json.dumps(summaries[eid]).encode(),{}
    fetch=nba_sources.fetch
    monkeypatch.setattr(nba_sources,'fetch',lambda *a,**kw:fetch(*a,getter=get,**kw))


def test_verified_final_before_four_hours_grades_game_and_all_five_props(isolated,monkeypatch):
    raw,game,_,tip=_grading_case();calls=[]
    _summary_feed(monkeypatch,{game['espn_event_id']:raw},calls)
    result=nba_ops.grade_due(tip+3*3600)
    assert result['ok'] and result['new_grades']==6
    assert [g['result'] for g in nba_store.latest_grades('top5-'+game['game_id']).values()]==['win','push','loss','void','win']
    assert json.loads(db.q1('SELECT payload FROM nba_game_grade_revisions')['payload'])['winners']=='won'
    assert nba_ops.grade_due(tip+3*3600+301)['new_grades']==0
    assert len(calls)==1


def test_fresh_summary_receipt_after_worker_start_grades_every_selection(isolated,monkeypatch):
    raw,game,_,tip=_grading_case();now=tip+3*3600;receipt=now+2
    monkeypatch.setattr(nba_sources,'get_raw_meta',lambda *a,**kw:(200,json.dumps(raw).encode(),{}))
    monkeypatch.setattr(nba_sources.time,'time',lambda:receipt)
    result=nba_ops.grade_due(now)
    assert result['ok'] and result['new_grades']==6
    grades=nba_store.latest_grades('top5-'+game['game_id'])
    assert len(grades)==5 and all(g['graded_at']==receipt for g in grades.values())
    assert db.q1('SELECT graded_at FROM nba_game_grade_revisions')['graded_at']==receipt
    assert db.kv_get('nba_grade_checked_'+game['game_id'])==receipt


@pytest.mark.parametrize('checked_status',[None,'scheduled'])
def test_saved_final_transition_bypasses_old_live_cooldown_and_cache(isolated,monkeypatch,checked_status):
    import copy
    raw,game,source,tip=_grading_case(status='scheduled');calls=[]
    live=copy.deepcopy(raw);live['header']['competitions'][0]['status']['type'].update(
        state='in',name='STATUS_IN_PROGRESS',completed=False)
    feed={game['espn_event_id']:live};_summary_feed(monkeypatch,feed,calls)
    now=tip+4*3600+10
    assert nba_ops.grade_due(now)['pending']==1
    db.kv_set('nba_grade_checked_'+game['game_id'],now)
    db.kv_set('nba_grade_checked_status_'+game['game_id'],checked_status)
    feed[game['espn_event_id']]=raw
    nba_store.upsert_game(game,source,now=now+60)
    assert nba_ops.grade_due(now+60)['new_grades']==6
    assert len(calls)==2


def test_incomplete_final_retries_after_five_minutes(isolated,monkeypatch):
    import copy
    raw,game,_,tip=_grading_case();calls=[]
    incomplete=copy.deepcopy(raw);incomplete['boxscore']['players'][0]['statistics'][0]['totals'][1]='0'
    feed={game['espn_event_id']:incomplete};_summary_feed(monkeypatch,feed,calls)
    now=tip+3*3600
    assert not nba_ops.grade_due(now)['ok']
    assert nba_store.latest_grades('top5-'+game['game_id'])=={}
    assert nba_ops.grade_due(now+299)['new_grades']==0
    assert len(calls)==1
    feed[game['espn_event_id']]=raw
    assert nba_ops.grade_due(now+300)['new_grades']==6
    assert len(calls)==2


@pytest.mark.parametrize('nonfinal',['in_progress','suspended'])
def test_multiple_finals_grade_while_nonfinal_stays_pending(isolated,monkeypatch,nonfinal):
    summaries={};calls=[]
    for i in range(3):
        raw,game,_,tip=_grading_case(str(401810831+i),status='final' if i<2 else nonfinal)
        if i==2:
            raw['header']['competitions'][0]['status']['type'].update(
                state='in',name='STATUS_SUSPENDED' if nonfinal=='suspended' else 'STATUS_IN_PROGRESS',completed=False)
        summaries[game['espn_event_id']]=raw
    _summary_feed(monkeypatch,summaries,calls)
    result=nba_ops.grade_due(tip+5*3600)
    assert result['ok'] and result['new_grades']==12 and result['pending']==1
    assert len(calls)==3
    assert nba_store.latest_grades('top5-'+game['game_id'])=={}


def test_pending_count_includes_unchanged_results_across_finished_games(isolated,monkeypatch):
    first,game,_,tip=_grading_case()
    second,later,source,_=_grading_case('401810832',status='scheduled')
    # Keep reconciled team totals while selected identities are absent from the box.
    for raw,missing in [(first,'3064514'),(second,'4433255')]:
        for team in raw['boxscore']['players']:
            for row in team['statistics'][0]['athletes']:
                if row['athlete']['id']==missing:row['athlete']['id']='999999'
    calls=[];_summary_feed(monkeypatch,{game['espn_event_id']:first,later['espn_event_id']:second},calls)
    now=tip+3*3600
    assert nba_ops.grade_due(now)['pending']==2
    nba_store.upsert_game(later,source,now=now+300)
    updated=nba_ops.grade_due(now+300)
    assert updated['new_grades']==6 and updated['pending']==3
    repeated=nba_ops.grade_due(now+600)
    assert repeated['new_grades']==0 and repeated['pending']==3
    cached=nba_ops.grade_due(now+601)
    assert cached['new_grades']==0 and cached['pending']==3
    assert len(calls)==5
    assert db.q1('SELECT COUNT(*) n FROM nba_grade_revisions')['n']==10
    assert db.q1('SELECT COUNT(*) n FROM nba_game_grade_revisions')['n']==2


def test_pending_report_separates_completed_props_from_live_games(isolated,monkeypatch):
    final,game,_,tip=_grading_case()
    for team in final['boxscore']['players']:
        for row in team['statistics'][0]['athletes']:
            if row['athlete']['id']=='3064514':row['athlete']['id']='999999'
    live,later,_,_=_grading_case('401810832',status='in_progress')
    live['header']['competitions'][0]['status']['type'].update(
        state='in',name='STATUS_IN_PROGRESS',completed=False)
    _summary_feed(monkeypatch,{game['espn_event_id']:final,later['espn_event_id']:live},[])
    report=nba_ops.grade_due(tip+5*3600)
    assert report['pending']==3
    assert report.get('pending_props')==2 and report.get('pending_games')==1
    assert '2 completed-game props still pending; 1 games awaiting final results' in report['message']


@pytest.mark.parametrize('status',['final','canceled'])
def test_regressed_terminal_summary_is_not_counted_again_as_a_live_game(isolated,monkeypatch,status):
    raw,game,_,tip=_grading_case(status=status)
    raw['header']['competitions'][0]['status']['type'].update(
        state='in',name='STATUS_IN_PROGRESS',completed=False)
    _summary_feed(monkeypatch,{game['espn_event_id']:raw},[])
    report=nba_ops.grade_due(tip+5*3600)
    assert report['new_grades']==0 and report['pending']==5
    assert report.get('pending_props')==5 and report.get('pending_games')==0
    assert nba_store.latest_grades('top5-'+game['game_id'])=={}


def test_canceled_started_game_voids_before_four_hours(isolated,monkeypatch):
    raw,game,_,tip=_grading_case(status='canceled');calls=[]
    raw['header']['competitions'][0]['status']['type'].update(name='STATUS_CANCELED',state='pre',completed=False)
    raw['boxscore']={}
    _summary_feed(monkeypatch,{game['espn_event_id']:raw},calls)
    result=nba_ops.grade_due(tip+3600)
    assert result['ok'] and result['new_grades']==6
    assert all(g['result']=='void' for g in nba_store.latest_grades('top5-'+game['game_id']).values())


def test_canceled_unresolved_props_remain_counted_after_failed_and_cached_checks(isolated,monkeypatch):
    _,game,_,tip=_grading_case(status='canceled');calls=[]
    def unavailable(*a,**kw):
        calls.append(1)
        raise ValueError('result source unavailable')
    monkeypatch.setattr(nba_sources,'fetch',unavailable)
    failed=nba_ops.grade_due(tip+3600)
    assert failed['ok'] is False and failed['new_grades']==0 and failed['pending']==5
    cached=nba_ops.grade_due(tip+3601)
    assert cached['new_grades']==0 and cached['pending']==5
    assert calls==[1] and nba_store.latest_grades('top5-'+game['game_id'])=={}


def test_worker_checks_grading_every_five_minutes(isolated,monkeypatch):
    monkeypatch.setattr(nba_ops,'register_model',lambda *a:None)
    for name in ('refresh_schedule','histories','forecasts','capture_props','evaluate'):
        monkeypatch.setattr(nba_ops,name,lambda *a:{})
    calls=[]
    monkeypatch.setattr(nba_ops,'grade_due',lambda now:calls.append(now) or {})
    now=1792500000.
    nba_ops.run(now);nba_ops.run(now+299);nba_ops.run(now+300)
    assert calls==[now,now+300]
    assert nba_store.get_cadence('grading')['cadence_seconds']==300


def _stub_worker(monkeypatch, games=()):
    monkeypatch.setattr(nba_ops,'register_model',lambda *a:None)
    for name in ('refresh_schedule','histories','forecasts','grade_due','evaluate'):
        monkeypatch.setattr(nba_ops,name,lambda *a:{})
    monkeypatch.setattr(nba_store,'list_games',lambda day:[g for g in games if g['game_date_et']==day])
    monkeypatch.setattr(nba_store,'get_game_lock',lambda *a:None)
    monkeypatch.setattr(nba_ops,'_forecast',lambda *a:{'saved':True})
    monkeypatch.setattr(nba_sources,'fetch',lambda *a,**kw:pytest.fail('unexpected provider call'))


@pytest.mark.parametrize('status,prior_hours',[
    ('outside_capture_window',98/60),('no_targets',98/60),('captured',9)])
def test_capture_clock_opens_for_new_targets_and_new_eastern_day(isolated,monkeypatch,status,prior_hours):
    tip=nba_ops.parse_time('2026-10-23T20:00:00Z');window=tip-8*3600
    games=[{'game_id':'g1','game_date_et':'2026-10-23','tipoff_ts':tip,'status':'scheduled'}]
    _stub_worker(monkeypatch,games);calls=[]
    monkeypatch.setattr(nba_ops,'capture_props',lambda now:calls.append(now) or {'status':'captured'})
    nba_ops.step('props',window-prior_hours*3600,10800,lambda:{'status':status})
    nba_ops.run(window-300)
    assert calls==[]
    assert nba_store.get_cadence('props')['next_due_at']==window
    nba_ops.run(window-1)
    assert calls==[]
    nba_ops.run(window)
    assert calls==[window]


def test_paid_capture_keeps_retry_toward_forty_five_minutes_before_tip(isolated,monkeypatch):
    tip=nba_ops.parse_time('2026-10-23T20:00:00Z');last=tip-8*3600
    _stub_worker(monkeypatch,[{'game_id':'g1','game_date_et':'2026-10-23','tipoff_ts':tip,'status':'scheduled'}])
    calls=[]
    monkeypatch.setattr(nba_ops,'capture_props',lambda now:calls.append(now) or {'status':'captured'})
    nba_ops.step('props',last,10800,lambda:{'status':'captured'})
    nba_ops.run(last+300)
    assert calls==[]
    assert nba_store.get_cadence('props')['next_due_at']==tip-45*60
    nba_ops.run(tip-45*60)
    assert calls==[tip-45*60]


def test_paid_capture_without_remaining_targets_keeps_three_hour_cadence(isolated,monkeypatch):
    now=nba_ops.parse_time('2026-10-23T12:00:00Z');_stub_worker(monkeypatch)
    calls=[]
    monkeypatch.setattr(nba_ops,'capture_props',lambda stamp:calls.append(stamp) or {'status':'no_targets'})
    nba_ops.step('props',now,10800,lambda:{'status':'captured'})
    nba_ops.run(now+10799)
    assert calls==[]
    nba_ops.run(now+10800)
    assert calls==[now+10800]


@pytest.mark.parametrize('status',['nfl_capacity_reserved','nba_daily_budget','provider_quota_cooldown',
                                   'local_per_minute_request_guard'])
def test_budget_wait_rechecks_without_a_three_hour_delay(isolated,monkeypatch,status):
    now=nba_ops.parse_time('2026-10-23T12:00:00Z');_stub_worker(monkeypatch)
    calls=[]
    monkeypatch.setattr(nba_ops,'capture_props',lambda stamp:calls.append(stamp) or {'status':status})
    nba_ops.step('props',now,10800,lambda:{'status':status})
    nba_ops.run(now+299)
    assert calls==[]
    nba_ops.run(now+300)
    assert calls==[now+300]
    assert nba_store.get_cadence('props')['next_due_at']==now+600


@pytest.mark.parametrize('name',['histories','grade_due','refresh_schedule'])
def test_worker_health_keeps_failures_until_a_real_success(isolated,monkeypatch,name):
    now=nba_ops.parse_time('2026-10-23T12:00:00Z');_stub_worker(monkeypatch)
    monkeypatch.setattr(nba_ops,'capture_props',lambda *a:{'status':'no_targets'})
    monkeypatch.setattr(nba_ops,name,lambda *a:{'ok':False,'message':'source unavailable'})
    failed=nba_ops.run(now)
    assert failed['ok'] is False and failed['needs_attention'] is True
    monkeypatch.setattr(nba_ops,name,lambda *a:{})
    cached=nba_ops.run(now+1)
    assert cached['ok'] is False and cached['needs_attention'] is True
    recovered=nba_ops.run(now+901)
    assert recovered['ok'] is True and recovered['needs_attention'] is False


def test_today_waiting_props_explain_reserved_capacity_and_automatic_recheck(isolated,monkeypatch):
    _,game,_,tip=_grading_case(status='scheduled',props=False)
    monkeypatch.setattr(nba_ops.time,'time',lambda:tip-3600)
    monkeypatch.setattr(nba_ops.nba_quotes,'budget',lambda *a:{'blocked_reason':'nfl_capacity_reserved',
        'shared_remaining':38,'nfl_reserved_requests':40})
    monkeypatch.setattr(nba_sources,'fetch',lambda *a,**kw:pytest.fail('read called a provider'))
    board=nba_ops.board(game['game_date_et'])
    assert 'NFL' in board['notice'] and '38' in board['notice'] and '40' in board['notice']
    assert 'automatically' in board['games'][0]['prop_message']
    assert 'paused' in nba_ops.matchup(game['game_id'])['game']['prop_message']
    nba_ops.step('history',tip-3600,300,lambda:{'ok':False,'message':'source unavailable'})
    assert 'source check needs attention' in nba_ops.board(game['game_date_et'])['notice']
    monkeypatch.setattr(nba_ops.time,'time',lambda:tip+3600)
    assert 'will not be backfilled' in nba_ops.board(game['game_date_et'])['games'][0]['prop_message']


@pytest.mark.parametrize('status',['scheduled','final'])
def test_budget_notice_does_not_change_locked_games(isolated,monkeypatch,status):
    _,game,_,tip=_grading_case(status=status)
    monkeypatch.setattr(nba_ops.time,'time',lambda:tip-3600 if status=='scheduled' else tip+3600)
    monkeypatch.setattr(nba_ops.nba_quotes,'budget',lambda *a:pytest.fail('locked game needs no capture budget'))
    shown=nba_ops.board(game['game_date_et'])['games'][0]
    assert shown['props_state']=='locked' and 'paused' not in shown['prop_message']
