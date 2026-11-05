"""Totals evidence replay and mandatory reservation boundaries."""
import hashlib
import json
from datetime import datetime, timezone
import pytest
from edgeterminal import db, nba_store
from edgeterminal.config import settings
from edgeterminal import nba_totals_data as data
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

def test_canonical_map_replays_only_selected_scores():
    game,row=fixture(); mapped=data.prepare(game,[row],ASOF)
    assert mapped['schema']=='nba-totals-input-1' and len(mapped['rows'])==1
    assert mapped['rows'][0]['season_start_year']==2026 and mapped['feature_cutoff']==TIP-4*3600
    assert data.source_ids(mapped)==[row['source_artifact_id']]
    assert data.verify_input_map(mapped,game,ASOF)==mapped
    assert '/Users/' not in json.dumps(mapped)

@pytest.mark.parametrize('field,value',[('points',111),('opponent','CHI'),('home',False),
    ('played_at',iso(TIP-2*86400)),('game_id','401111111'),('source_sha256','0'*64)])
def test_changed_rows_fail_semantic_replay(field,value):
    game,row=fixture()
    with pytest.raises(ValueError): data.prepare(game,[dict(row,**{field:value})],ASOF)

@pytest.mark.parametrize('tail',['/teams/02/schedule?season=2027&seasontype=2',
    '/teams/2/schedule?season=2027&seasontype=2&extra=1','/teams/2/schedule?season=2027&seasontype=2&season=2027',
    '/teams/2/schedule?season=2027&seasontype=2#fragment','/teams/2/schedule?season=2027&seasontype=3'])
def test_exact_route_allowlist(tail):
    game,row=fixture(); bad=archive(dict(events=[event()]),url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba'+tail)
    with pytest.raises(ValueError): data.prepare(game,[dict(row,source_artifact_id=bad)],ASOF)

def test_wrong_numeric_team_late_receipt_target_and_conflicting_duplicates_fail():
    game,row=fixture(); payload=dict(events=[event()])
    payload['events'][0]['competitions'][0]['competitors'][0]['team']['id']='999'
    bad=archive(payload)
    with pytest.raises(ValueError): data.prepare(game,[dict(row,source_artifact_id=bad)],ASOF)
    future=archive(dict(events=[event()]),at=ASOF+1)
    with pytest.raises(ValueError): data.prepare(game,[dict(row,source_artifact_id=future)],ASOF)
    with pytest.raises(ValueError): data.prepare(dict(game,game_id='nba-2026-401234567'),[row],ASOF)
    second=archive(dict(events=[event(score=(111,100))]),at=ASOF-1)
    with pytest.raises(ValueError): data.prepare(game,[row,dict(row,points=111,source_artifact_id=second)],ASOF)

@pytest.mark.parametrize('period,duration',[(4,48),(5,53),(7,63)])
def test_overtime_pace_and_full_game_efficiency(period,duration):
    game,row=fixture(); bid=archive(summary(period),kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
    mapped=data.prepare(game,[dict(row,possession_source_artifact_id=bid)],ASOF); saved=mapped['rows'][0]
    assert saved['duration_minutes']==duration and saved['raw_possessions']==pytest.approx(100.8)
    assert saved['pace48']==pytest.approx(100.8*48/duration)
    assert saved['offensive_efficiency']==pytest.approx(110/100.8)
    assert saved['duration_source_artifact_id']==bid
    assert data.verify_input_map(mapped,game,ASOF)==mapped

def test_unknown_duration_retains_raw_possessions_without_pace():
    game,row=fixture(); bid=archive(summary(),kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
    saved=data.prepare(game,[dict(row,possession_source_artifact_id=bid)],ASOF)['rows'][0]
    assert saved['raw_possessions']==pytest.approx(100.8)
    assert 'duration_minutes' not in saved and 'pace48' not in saved
    assert data.prepare(game,[row],ASOF)['rows'][0]['points']==110

def test_conflicting_duration_or_box_identity_fails():
    game,row=fixture()
    for payload in [summary(3),summary(5),summary(4)]:
        if payload['header']['status']['period']==5:
            for c in payload['header']['competitions'][0]['competitors']:
                c['linescores']=[dict(value=n) for n in [20,30,30,30]]
        elif payload['header']['status']['period']==4: payload['boxscore']['teams'][0]['team']['id']='999'
        bid=archive(payload,kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
        with pytest.raises(ValueError): data.prepare(game,[dict(row,possession_source_artifact_id=bid)],ASOF)

def test_rehashing_tampered_input_does_not_bypass_sources():
    game,row=fixture(); mapped=data.prepare(game,[row],ASOF); mapped['rows'][0]['points']+=1
    mapped['sha256']=data.map_hash(mapped)
    with pytest.raises(ValueError): data.verify_input_map(mapped,game,ASOF)
    mapped=data.prepare(game,[row],ASOF); mapped['source_artifacts'][0]['url']+='&extra=1'; mapped['sha256']=data.map_hash(mapped)
    with pytest.raises(ValueError): data.verify_input_map(mapped,game,ASOF)

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


def test_publication_state_binds_entire_supported_date_membership():
    game,row,saved=seed_saved(total_line=None)
    before=data.validation_state([game['game_id']],ASOF)
    other=dict(nba_store.get_game(game['game_id']),game_id='nba-2026-401999998',espn_event_id='401999998')
    nba_store.upsert_game(other,row['source_artifact_id'],now=ASOF)
    nba_store.save_forecast('other-supported',other['game_id'],'baseline',
        dict(kind='game',projection=dict(status='ok',total=220.)),[row['source_artifact_id']],now=ASOF)
    assert before!=data.validation_state([game['game_id']],ASOF)


def test_publication_state_binds_schedule_identity():
    game,row,saved=seed_saved(total_line=None)
    before=data.validation_state([game['game_id']],ASOF)
    changed=dict(nba_store.get_game(game['game_id']),tipoff_utc=iso(TIP+1800))
    nba_store.upsert_game(changed,row['source_artifact_id'],now=ASOF)
    assert before!=data.validation_state([game['game_id']],ASOF)
    assert nba_store.get_forecast(saved['forecast_id'])['payload_sha256']==saved['payload_sha256']


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
    assert inputs[0]['input_map']['rows'][0]['points']==110
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


def test_historical_examples_are_explicit_reconstruction_with_actual_receipts():
    events=[event(eid=str(401234500+i),at=TIP-(i+3)*86400) for i in range(14)]
    archive(dict(team=dict(id='2',abbreviation='BOS'),events=events))
    archive(dict(team=dict(id='18',abbreviation='NY'),events=events),url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/18/schedule?season=2027&seasontype=2')
    examples,sources=data.training_examples(ASOF)
    assert examples and len(sources)==2
    first=examples[0]
    assert first['reconstructed'] and first['reconstruction_cutoff']==ASOF
    assert first['availability']=='reconstructed_at_cutoff'
    assert first['label_received_at']>first['tipoff']
    assert all(r['played_at']<first['tipoff']-4*3600 for r in first['rows'])
    assert all(r['received_at']==ASOF-10 for r in first['rows'])
    assert first['baseline_total']==206.8 and first['actual_total']==210


def test_future_score_row_and_unbound_advanced_values_fail():
    game,row=fixture()
    with pytest.raises(ValueError): data.prepare(game,[dict(row,home=1)],ASOF)
    with pytest.raises(ValueError): data.prepare(game,[dict(row,raw_possessions=100.)],ASOF)
    with pytest.raises(ValueError): data.prepare(game,[row],TIP)

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


def test_selection_is_bounded_and_opponent_rows_precede_each_historical_cutoff(monkeypatch):
    game,row=fixture(); rows=[]
    for team,opponent in [('BOS','CHI'),('NYK','CHI'),('CHI','BOS')]:
        for i in range(100):
            rows.append(dict(row,team=team,opponent=opponent,game_id=str(402000000+i),played_at=TIP-(i+2)*86400))
    monkeypatch.setattr(data,'_source',lambda *a,**k:pytest.fail('selection read archive'))
    chosen=data.select_history(game,rows,ASOF)
    targets=[r for r in chosen if r['team'] in ('BOS','NYK')]
    assert len(targets)==40 and len(chosen)<=840
    oldest_target=min(r['played_at'] for r in targets)
    assert min(r['played_at'] for r in chosen if r['team']=='CHI')<oldest_target
    assert all(r['played_at']<TIP-4*3600 for r in chosen)


def test_cached_parse_never_bypasses_fresh_body_hash(monkeypatch):
    game,row=fixture(); data.prepare(game,[row],ASOF)
    original=db.q1
    def corrupt(sql,args=()):
        result=original(sql,args)
        if result and 'SELECT * FROM nba_source_artifacts' in sql:
            return dict(result,body=result['body']+b' ')
        return result
    monkeypatch.setattr(db,'q1',corrupt)
    with pytest.raises(ValueError,match='hash'): data.prepare(game,[row],ASOF)


def test_input_map_replays_after_owned_database_relocation(tmp_path,monkeypatch):
    import sqlite3
    game,row=fixture(); mapped=data.prepare(game,[row],ASOF)
    target=tmp_path/'app'/'research.sqlite'; target.parent.mkdir()
    relocated=sqlite3.connect(target); db.conn().backup(relocated); relocated.close()
    db._conn.close(); db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{target}')
    assert data.verify_input_map(mapped,game,ASOF)==mapped
    bad=dict(mapped,path='../../outside.json'); bad['sha256']=data.map_hash(bad)
    with pytest.raises(ValueError): data.verify_input_map(bad,game,ASOF)


def test_early_ungraded_raw_final_correction_changes_publication_heads(monkeypatch):
    reservation,game,saved=seed_final(monkeypatch)
    before=data.validation_state([game['game_id']],TIP+5*3600)
    final=summary(4)
    archive(final,kind='final-boxscore',at=TIP+5*3600+1,url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401999999')
    after=data.validation_state([game['game_id']],TIP+5*3600)
    assert before[game['game_id']]['grade']==after[game['game_id']]['grade']
    assert before!=after

def test_identical_schedule_duplicates_collapse_and_conflicting_correction_fails():
    game,row=fixture(); same=archive(dict(events=[event(),event()]),at=ASOF-9)
    assert len(data.prepare(game,[dict(row,source_artifact_id=same)],ASOF)['rows'])==1
    conflict=archive(dict(events=[event(),event(score=(111,100))]),at=ASOF-8)
    with pytest.raises(ValueError,match='correction'): data.prepare(game,[dict(row,source_artifact_id=conflict)],ASOF)


def test_line_scores_alone_can_prove_regulation_duration():
    game,row=fixture(); payload=summary()
    for c in payload['header']['competitions'][0]['competitors']:
        c['linescores']=[dict(value=int(c['score'])/4) for _ in range(4)]
    # Away 100/4 and home 110/4 must be integer final period scores.
    payload['header']['competitions'][0]['competitors'][0]['linescores']=[dict(value=n) for n in [20,30,30,30]]
    bid=archive(payload,kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
    assert data.prepare(game,[dict(row,possession_source_artifact_id=bid)],ASOF)['rows'][0]['duration_minutes']==48


@pytest.mark.parametrize('bad',[
    'http://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2027&seasontype=2',
    'https://site.api.espn.com:443/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2027&seasontype=2',
    'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=%32%30%32%37&seasontype=2',
    'https://evil.example/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2027&seasontype=2'])
def test_source_scheme_authority_and_encoded_query_are_rejected(bad):
    game,row=fixture(); sid=archive(dict(events=[event()]),url=bad)
    with pytest.raises(ValueError): data.prepare(game,[dict(row,source_artifact_id=sid)],ASOF)


def test_model_used_row_map_discards_irrelevant_optional_opponent_rows():
    game,row=fixture(); other=archive(dict(events=[event()]),url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/18/schedule?season=2027&seasontype=2')
    away=dict(row,team='NYK',opponent='BOS',points=100,opponent_points=110,home=False,source_artifact_id=other)
    mapped=data.prepare(game,[row,away,row],ASOF)
    assert len(mapped['rows'])==2 and len(mapped['source_artifacts'])==2
    assert mapped['selection_method']=='target20_opponent_prior_scoring20_v1'


def test_reserved_forecast_head_mismatch_fails_before_grade_queries(monkeypatch):
    reservation,game,saved=seed_final(monkeypatch)
    reservation['games']=[dict(game_id=game['game_id'],forecast_sha256='0'*64)]
    original=db.q
    def guarded(sql,args=()):
        if 'SELECT * FROM nba_game_grade_revisions' in sql: pytest.fail('read labels from another selected forecast')
        return original(sql,args)
    monkeypatch.setattr(db,'q',guarded)
    with pytest.raises(ValueError,match='reserved forecast'): data.validation_labels(reservation,TIP+5*3600)

def test_historical_box_replay_keeps_raw_duration_and_detects_corrections():
    events=[event(eid=str(401234500+i),at=TIP-(i+3)*86400) for i in range(14)]
    archive(dict(team=dict(id='2',abbreviation='BOS'),events=events))
    archive(dict(team=dict(id='18',abbreviation='NY'),events=events),url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/18/schedule?season=2027&seasontype=2')
    payload=summary(5); payload['header']['id']='401234513'; payload['header']['date']=iso(TIP-16*86400)
    payload['header']['competitions'][0]['date']=iso(TIP-16*86400)
    boxurl='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234513'
    bid=archive(payload,kind='final-boxscore',url=boxurl)
    examples,sources=data.training_examples(ASOF)
    known=[row for example in examples for row in example['rows'] if row.get('raw_possessions')]
    assert known and all(row['duration_minutes']==53 and row['raw_possessions']==pytest.approx(100.8) for row in known)
    assert bid in {s['id'] for s in sources}
    payload['header']['status']['period']=4
    payload['header']['competitions'][0]['status']['period']=4
    archive(payload,kind='final-boxscore',url=boxurl,at=ASOF-1)
    with pytest.raises(ValueError,match='correction'): data.training_examples(ASOF)


def test_missing_historical_advanced_evidence_preserves_example_coverage():
    events=[event(eid=str(401234500+i),at=TIP-(i+3)*86400) for i in range(14)]
    archive(dict(events=events))
    archive(dict(events=events),url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/18/schedule?season=2027&seasontype=2')
    before,_=data.training_examples(ASOF)
    payload=summary(4); payload['boxscore']['teams']=[]
    archive(payload,kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234500')
    after,_=data.training_examples(ASOF)
    assert before==after and before


def test_duplicate_json_keys_are_ambiguous_even_with_matching_hash():
    raw=b'{"events":[],"events":[]}'
    sid=nba_store.save_artifact(dict(kind='team-history',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/2/schedule?season=2027&seasontype=2',
        body=raw,sha256=hashlib.sha256(raw).hexdigest(),http_status=200,received_at=ASOF))
    with pytest.raises(ValueError): data.source_history(sid,ASOF)

def test_map_sources_must_also_be_in_the_common_saved_source_list(monkeypatch):
    game,row,saved=seed_saved(mapped_asof=ASOF-5)
    original=nba_store.get_forecast
    def missing(ident):
        result=original(ident)
        if result: result=dict(result,source_artifact_ids=[i for i in result['source_artifact_ids'] if i!=row['source_artifact_id']])
        return result
    monkeypatch.setattr(nba_store,'get_forecast',missing)
    with pytest.raises(ValueError,match='common saved'): data.forecast_inputs(ASOF)

def test_source_role_mismatch_is_not_missing_optional_evidence():
    game,row=fixture()
    bad=archive(dict(events=[event()]),kind='scoreboard')
    with pytest.raises(ValueError,match='kind'): data.prepare(game,[dict(row,source_artifact_id=bad)],ASOF)
    bad=archive(summary(4),kind='team-history',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
    with pytest.raises(ValueError,match='kind'): data.prepare(game,[dict(row,possession_source_artifact_id=bad)],ASOF)


def _display_linescore_summary(periods=4, *, include_value=False):
    """Actual ESPN summary shape: linescores carry canonical displayValue strings."""
    payload=summary()
    scores=([20,30,30,30],[20,30,25,25]) if periods==4 else ([20,30,30,20,10],[20,20,20,30,10])
    for competitor,values in zip(payload['header']['competitions'][0]['competitors'],scores):
        competitor['linescores']=[dict(displayValue=str(value)) for value in values]
        if include_value:
            for line,value in zip(competitor['linescores'],values): line['value']=float(value)
    return payload


@pytest.mark.parametrize('periods,include_value,duration',[(4,False,48),(5,False,53),(5,True,53)])
def test_espn_display_value_linescores_prove_duration_without_status_period(periods,include_value,duration):
    game,row=fixture(); payload=_display_linescore_summary(periods,include_value=include_value)
    bid=archive(payload,kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
    mapped=data.prepare(game,[dict(row,possession_source_artifact_id=bid)],ASOF)
    saved=mapped['rows'][0]
    assert saved['duration_minutes']==duration and saved['overtime_periods']==periods-4
    assert saved['pace48']==pytest.approx(saved['raw_possessions']*48/duration)
    assert data.verify_input_map(mapped,game,ASOF)==mapped


@pytest.mark.parametrize('bad',['20.0','020','+20',' 20','20 ',None,True,'--','-20'])
def test_espn_display_value_linescores_reject_noncanonical_integer_text(bad):
    game,row=fixture(); payload=_display_linescore_summary()
    payload['header']['competitions'][0]['competitors'][0]['linescores'][0]['displayValue']=bad
    bid=archive(payload,kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
    with pytest.raises(ValueError): data.prepare(game,[dict(row,possession_source_artifact_id=bid)],ASOF)


def test_espn_display_value_and_numeric_value_must_agree():
    game,row=fixture(); payload=_display_linescore_summary(include_value=True)
    payload['header']['competitions'][0]['competitors'][0]['linescores'][0]['displayValue']='21'
    bid=archive(payload,kind='final-boxscore',url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401234567')
    with pytest.raises(ValueError,match='conflict'): data.prepare(game,[dict(row,possession_source_artifact_id=bid)],ASOF)
