"""Totals evidence replay and mandatory reservation boundaries."""
import hashlib
import json
from datetime import datetime, timezone
import pytest
from edgeterminal import db, nba_store
from edgeterminal.config import settings
from edgeterminal import nba_totals_stage_data as data
TIP=datetime(2026,11,6,0,tzinfo=timezone.utc).timestamp()
ASOF=TIP-3600

def iso(stamp): return datetime.fromtimestamp(stamp,timezone.utc).isoformat()

def event(eid='401234567',at=TIP-3*86400,score=(110,100),period=None):
    status={'type':{'completed':True,'state':'post'}}
    if period is not None: status['period']=period
    return dict(id=eid,date=iso(at),season=dict(year=2027,type=2),seasonType=dict(type=2),status=status,
        competitions=[dict(date=iso(at),status=status,competitors=[
        dict(homeAway='home',team=dict(id='2',abbreviation='BOS'),score=dict(value=score[0])),
        dict(homeAway='away',team=dict(id='18',abbreviation='NY'),score=dict(value=score[1]))])])

def summary(period=None):
    ev=event(period=period)
    for c in ev['competitions'][0]['competitors']: c['score']=str(c['score']['value'])
    teams=[dict(team=dict(id=tid,abbreviation=abbr),statistics=[
        dict(name='fieldGoalsMade-fieldGoalsAttempted',displayValue='40-90'),
        dict(name='freeThrowsMade-freeThrowsAttempted',displayValue='15-20'),
        dict(name='offensiveRebounds',displayValue='10'),dict(name='totalTurnovers',displayValue='12')])
        for tid,abbr in [('2','BOS'),('18','NY')]]
    return dict(header=ev,boxscore=dict(teams=teams))

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    if db._conn is not None: db._conn.close(); db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path / "totals.db"}')
    monkeypatch.setattr(db,'now',lambda:ASOF)
    nba_store.ensure_schema()
    yield
    db._conn.close(); db._conn=None

def archive(payload,kind='team-history',at=ASOF-10,url=None):
    raw=json.dumps(payload).encode()
    return nba_store.save_artifact(dict(kind=kind,body=raw,sha256=hashlib.sha256(raw).hexdigest(),
        http_status=200,received_at=at,url=url or 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2027&seasontype=2'))

def fixture():
    sid=archive(dict(team=dict(id='2',abbreviation='BOS'),events=[event()]))
    row=dict(team='BOS',opponent='NYK',game_id='401234567',played_at=iso(TIP-3*86400),
        points=110,opponent_points=100,home=True,source_artifact_id=sid)
    return dict(game_id='nba-2026-401999999',home_team='BOS',away_team='NYK',tipoff_ts=TIP),row

def test_unreserved_labels_fail_before_result_queries(monkeypatch):
    calls=[]
    monkeypatch.setattr(db,'q',lambda *args,**kw:calls.append(args[0]) or pytest.fail('queried before seal'))
    monkeypatch.setattr(data,'_verified_reservation',lambda value:(_ for _ in ()).throw(ValueError('unsealed')))
    with pytest.raises(ValueError,match='unsealed'): data.validation_labels(dict(game_ids=['x']),ASOF)
    assert calls==[]

def seed_saved(total_line=220.,mapped_asof=None,model_base=None):
    game,row=fixture()
    payload=dict(game_id=game['game_id'],espn_event_id='401999999',season_start_year=2026,
        season_label='2026-27',season_type='regular',competition='NBA',home_team='BOS',away_team='NYK',
        tipoff_utc=iso(TIP),status='scheduled',home_score=None,away_score=None)
    nba_store.upsert_game(payload,row['source_artifact_id'],now=ASOF-20)
    schedule=event(eid='401999999',at=TIP)
    schedule['status']=dict(type=dict(completed=False,state='pre'))
    schedule['competitions'][0]['status']=schedule['status']
    schedule['competitions'][0]['odds']=[dict(total=dict(over=dict(close=dict(line=total_line))))]
    quote=archive(dict(events=[schedule]),kind='scoreboard',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261105&limit=100')
    market=dict(total=total_line,source_artifact_id=quote,source_sha256=nba_store.get_artifact(quote)['sha256'],observed_at=ASOF-10)
    projection=dict(status='ok',total=222.,tipoff=TIP,sd_total=20.)
    if model_base is not None: projection.update(total_base_total=model_base,total_base_sd_total=16.5)
    if mapped_asof is not None: projection['total_input_map']=data.prepare(game,[row],mapped_asof)
    forecast=nba_store.save_forecast('totals-forecast',game['game_id'],'baseline',
        dict(kind='game',as_of=ASOF,projection=projection,
            market=market,input_manifest=dict(rows=[row])),[row['source_artifact_id'],quote],now=ASOF)
    return game,row,forecast

def test_unlabeled_total_only_universe_includes_missing_line_and_never_reads_results(monkeypatch):
    game,row,saved=seed_saved(total_line=None)
    original_q,original_q1=db.q,db.q1
    def guard(sql,args=()):
        lowered=sql.lower()
        assert 'grade_revisions' not in lowered and 'home_score' not in lowered and 'away_score' not in lowered
        assert not ('select * from nba_games' in lowered)
        return original_q(sql,args)
    def guard1(sql,args=()):
        lowered=sql.lower()
        assert 'grade_revisions' not in lowered and 'home_score' not in lowered and 'away_score' not in lowered
        assert not ('select * from nba_games' in lowered)
        return original_q1(sql,args)
    monkeypatch.setattr(db,'q',guard); monkeypatch.setattr(db,'q1',guard1)
    inputs=data.validation_inputs(ASOF)
    assert len(inputs)==1 and inputs[0]['saved_total_line'] is None
    assert inputs[0]['forecast_sha256']==saved['payload_sha256']
    # Unlike the v1 reconstruction adapter, missing original maps remain
    # unsupported inner evidence, without opening later feature sources.
    assert inputs[0]['input_map'] is None
    assert 'actual_total' not in inputs[0]

def test_future_date_floor_skips_old_bad_inputs_before_any_replay(monkeypatch):
    seed_saved()
    monkeypatch.setattr(data,'_selected_input',lambda *a:pytest.fail('old date source replay'))
    assert data.forecast_inputs(ASOF,start_date='2026-11-06')==[]

def test_unlabeled_date_floor_keeps_the_entire_included_date():
    seed_saved()
    assert len(data.forecast_inputs(ASOF,start_date='2026-11-05'))==1
    with pytest.raises(ValueError): data.forecast_inputs(ASOF,start_date='2026-11-05 OR 1=1')

def test_total_only_line_gets_conditional_no_push_probability():
    seed_saved(total_line=222.)
    inputs=data.validation_inputs(ASOF)
    assert inputs[0]['baseline_probability']==pytest.approx(.5)
    assert inputs[0]['market'].get('home_spread') is None

def seed_final(monkeypatch):
    game,row,saved=seed_saved()
    payload=summary(4); ev=payload['header']; ev['id']='401999999'; ev['date']=iso(TIP)
    ev['competitions'][0]['date']=iso(TIP)
    keys=['minutes','points','rebounds','assists','threePointFieldGoalsMade-threePointFieldGoalsAttempted']
    payload['boxscore']['players']=[]
    for idx,(tid,abbr,points) in enumerate([('2','BOS',110),('18','NY',100)]):
        payload['boxscore']['players'].append(dict(team=dict(id=tid,abbreviation=abbr),statistics=[dict(keys=keys,
            athletes=[dict(athlete=dict(id=str(idx+1)),didNotPlay=False,stats=['48',str(points),'10','10','0-0'])],
            totals=['240',str(points),'10','10','0-0'])]))
    final=archive(payload,kind='final-boxscore',at=TIP+4*3600,url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401999999')
    db.ex('CREATE TABLE nba_game_grade_revisions(id INTEGER PRIMARY KEY,game_id TEXT,source_artifact_id INTEGER,graded_at REAL,payload TEXT,sha256 TEXT)')
    label=json.dumps(dict(forecast_id=saved['forecast_id'],forecast_sha256=saved['payload_sha256'],home_score=110,away_score=100,total='lost'))
    digest=hashlib.sha256((label+':0').encode()).hexdigest()
    db.ex('INSERT INTO nba_game_grade_revisions VALUES(?,?,?,?,?,?)',(1,game['game_id'],final,TIP+4*3600,label,digest))
    reservation=dict(reservation_id='sealed-test',game_ids=[game['game_id']],date_ids=['2026-11-05'])
    monkeypatch.setattr(data,'_verified_reservation',lambda _:reservation)
    return reservation,game,saved

def test_guarded_totals_labels_need_no_spread_and_replay_final_source(monkeypatch):
    reservation,game,saved=seed_final(monkeypatch)
    result=data.validation_labels(reservation,TIP+5*3600)
    assert result['complete_dates']==['2026-11-05']
    assert result['rows'][0]['actual_total']==210
    assert result['universe_game_ids_by_date']=={'2026-11-05':[game['game_id']]}
    assert result['state'][game['game_id']]['forecast_sha256']==saved['payload_sha256']

def test_existing_new_final_correction_blocks_stale_graded_label(monkeypatch):
    reservation,game,saved=seed_final(monkeypatch)
    original=db.q1('SELECT a.body FROM nba_source_artifacts a JOIN nba_game_grade_revisions g '
                    'ON g.source_artifact_id=a.id WHERE g.game_id=?',(game['game_id'],))
    corrected=json.loads(original['body'])
    corrected['header']['competitions'][0]['competitors'][0]['score']='111'
    stats=corrected['boxscore']['players'][0]['statistics'][0]
    stats['athletes'][0]['stats'][1]='111'; stats['totals'][1]='111'
    from edgeterminal import nba_data
    assert nba_data.parse_summary(corrected)['box_complete']
    archive(corrected,kind='final-boxscore',at=TIP+4*3600+10,
        url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401999999')
    with pytest.raises(data.ResultPending,match='latest|correction|source head'):
        data.validation_labels(reservation,TIP+5*3600)

@pytest.mark.parametrize('conflict',['event','tipoff','incomplete'])
def test_latest_final_identity_conflict_is_not_a_pending_grade(monkeypatch,conflict):
    from edgeterminal import nba_data
    reservation,game,saved=seed_final(monkeypatch)
    source=db.q1('SELECT source_artifact_id FROM nba_game_grade_revisions')
    wrong=json.loads(nba_store.get_artifact(source['source_artifact_id'])['body'])
    if conflict=='event':
        wrong['header']['id']='401999998'
    elif conflict=='tipoff':
        wrong['header']['date']=iso(TIP+60)
        wrong['header']['competitions'][0]['date']=iso(TIP+60)
    else:
        wrong['boxscore']['players']=[]
    assert nba_data.parse_summary(wrong)['box_complete']==(conflict!='incomplete')
    archive(wrong,kind='final-boxscore',at=TIP+5*3600,
        url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401999999')
    with pytest.raises(ValueError) as failure:
        data.validation_labels(reservation,TIP+6*3600)
    assert not isinstance(failure.value,data.ResultPending)

def test_equivalent_refetch_does_not_change_publication_evidence(monkeypatch):
    reservation,game,saved=seed_final(monkeypatch)
    before=data.validation_state([game['game_id']],TIP+5*3600)
    row=db.q1('SELECT a.body FROM nba_source_artifacts a JOIN nba_game_grade_revisions g '
               'ON g.source_artifact_id=a.id WHERE g.game_id=?',(game['game_id'],))
    archive(json.loads(row['body']),kind='final-boxscore',at=TIP+4*3600+10,
        url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401999999')
    assert data.validation_state([game['game_id']],TIP+5*3600)==before
    assert data.validation_labels(reservation,TIP+5*3600)['rows'][0]['actual_total']==210

def test_pending_whole_date_and_corrupt_grade_chain(monkeypatch):
    reservation,game,saved=seed_final(monkeypatch)
    pending=data.validation_labels(reservation,TIP+3*3600)
    assert pending['rows']==[] and pending['complete_dates']==[]
    db.ex('UPDATE nba_game_grade_revisions SET sha256=? WHERE id=1',('0'*64,))
    with pytest.raises(ValueError,match='chain'): data.validation_labels(reservation,TIP+5*3600)

def test_quote_line_mismatch_and_unsupported_label_universe_fail(monkeypatch):
    game,row,saved=seed_saved()
    market=dict(saved['payload']['market'],total=221.)
    with pytest.raises(ValueError): data._verify_total_market(market,game,ASOF,saved['source_artifact_ids'])
    monkeypatch.setattr(data,'_verified_reservation',lambda _:dict(game_ids=['other'],date_ids=['2026-11-05']))
    with pytest.raises(ValueError,match='omits'): data.validation_labels('sealed',TIP+5*3600)

def test_totals_map_clock_precedes_later_snapshot_clock():
    seed_saved(mapped_asof=ASOF-5)
    row=data.forecast_inputs(ASOF)[0]
    assert row['forecast_at']==ASOF-5 and row['snapshot_as_of']==ASOF

def test_model_base_is_separate_from_served_incumbent_comparator():
    seed_saved(model_base=210.)
    row=data.forecast_inputs(ASOF)[0]
    assert row['baseline_total']==222.
    assert row['model_base_total']==210. and row['model_base_sd_total']==16.5

def test_future_map_clock_cannot_bypass_saved_snapshot():
    seed_saved(mapped_asof=ASOF+1)
    with pytest.raises(ValueError,match='later than saved'): data.forecast_inputs(ASOF)

def test_reserved_forecast_head_mismatch_fails_before_grade_queries(monkeypatch):
    reservation,game,saved=seed_final(monkeypatch)
    reservation['games']=[dict(game_id=game['game_id'],forecast_sha256='0'*64)]
    original=db.q
    def guarded(sql,args=()):
        if 'SELECT * FROM nba_game_grade_revisions' in sql: pytest.fail('read labels from another selected forecast')
        return original(sql,args)
    monkeypatch.setattr(db,'q',guarded)
    with pytest.raises(ValueError,match='reserved forecast'): data.validation_labels(reservation,TIP+5*3600)

def test_map_sources_must_also_be_in_the_common_saved_source_list(monkeypatch):
    game,row,saved=seed_saved(mapped_asof=ASOF-5)
    original=nba_store.get_forecast
    def missing(ident):
        result=original(ident)
        if result: result=dict(result,source_artifact_ids=[i for i in result['source_artifact_ids'] if i!=row['source_artifact_id']])
        return result
    monkeypatch.setattr(nba_store,'get_forecast',missing)
    with pytest.raises(ValueError,match='common saved'): data.forecast_inputs(ASOF)

