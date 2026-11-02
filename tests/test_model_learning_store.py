"""Promotion is earned by sourced, paired, whole-game validation evidence."""
import copy
import hashlib
import json
import sqlite3

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal import model_learning_store as store


def evidence(offset=0.):
    rows=[]
    for game in range(30):
        for index,market in enumerate(store.MARKETS['NBA']):
            won=(game+index)%2 == 0
            actual=12. if won else 8.
            rows.append(dict(game_id=f'game-{game}',player_id=f'p-{index}',market=market,
                quote_id=game*5+index+1,line=10.5,side='over',observed_at=201.+game,
                kickoff_at=250.+game,result_observed_at=350.+game,actual=actual,
                baseline_prediction=10.,candidate_prediction=actual+offset,
                baseline_probability=.55 if won else .45,
                candidate_probability=.8 if won else .2))
    return dict(kind='actual_main_lines',validation_start=200.,validation_end=400.,
                calibration_cutoff=200.,baseline_candidate_id=None,rows=rows)


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    if db._conn is not None:
        db._conn.close();db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path / "learning.db"}')
    monkeypatch.setattr(store,'ROOT',tmp_path)
    monkeypatch.setattr(store,'DIRECTORY',tmp_path/'models')
    db.init()
    db.ex('CREATE TABLE nba_source_artifacts (id INTEGER PRIMARY KEY,kind TEXT,url TEXT,'
          'received_at REAL,http_status INTEGER,sha256 TEXT,body BLOB)')
    db.ex('CREATE TABLE nba_games (game_id TEXT PRIMARY KEY,tipoff_ts REAL,status TEXT)')
    db.ex('CREATE TABLE nba_quote_observations (quote_id INTEGER PRIMARY KEY,game_id TEXT,'
          'espn_player_id TEXT,market TEXT,line REAL,side TEXT,observed_at REAL,source_artifact_id INTEGER)')
    db.ex('CREATE TABLE nba_selection_lock_items (lock_id TEXT,rank INTEGER,quote_id INTEGER)')
    db.ex('CREATE TABLE nba_grade_revisions (id INTEGER PRIMARY KEY,lock_id TEXT,rank INTEGER,'
          'revision INTEGER,result TEXT,actual_value REAL,graded_at REAL,source_artifact_id INTEGER)')
    for i,at in ((1,90.),(2,150.),(3,201.),(4,350.)):
        body=json.dumps({'source':i}).encode();sha=hashlib.sha256(body).hexdigest()
        kind='final-boxscore' if i==4 else 'fixture'
        url=('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=123'
             if i==4 else 'https://site.api.espn.com/source')
        db.ex('INSERT INTO nba_source_artifacts VALUES(?,?,?,?,?,?,?)',
              (i,kind,url,at,200,sha,body))
    for row in evidence()['rows']:
        qid=row['quote_id'];gid=row['game_id']
        db.ex('INSERT OR IGNORE INTO nba_games VALUES(?,?,?)',(gid,row['kickoff_at'],'final'))
        market=store.NBA_MARKETS[row['market']]
        db.ex('INSERT INTO nba_quote_observations VALUES(?,?,?,?,?,?,?,?)',
              (qid,gid,row['player_id'],market,row['line'],row['side'],row['observed_at'],3))
        db.ex('INSERT INTO nba_selection_lock_items VALUES(?,?,?)',(gid,qid,qid))
        db.ex('INSERT INTO nba_grade_revisions VALUES(?,?,?,?,?,?,?,?)',
              (qid,gid,qid,1,'win' if row['actual']>row['line'] else 'loss',row['actual'],
               row['result_observed_at'],4))
    code=tmp_path/'edgeterminal/learner.py';code.parent.mkdir();code.write_text('VERSION = 1\n')
    yield tmp_path
    db._conn.close();db._conn=None


def artifact(root):
    return dict(schema=1,kind='fixture',parameters={'weight':.5},
        code_sha256={'edgeterminal/learner.py':hashlib.sha256((root/'edgeterminal/learner.py').read_bytes()).hexdigest()},
        training={'fit_game_ids':['fit-game'],'calibration_game_ids':['cal-game'],
                  'fit_label_max_at':99.,'calibration_label_max_at':199.})


def sources():
    return [dict(kind='nba_source_artifact',id=i,sha256=db.q1('SELECT sha256 FROM nba_source_artifacts WHERE id=?',(i,))['sha256'],
                 received_at=at,role=role) for i,at,role in
            ((1,90.,'training'),(2,150.,'calibration'),(3,201.,'validation'),(4,350.,'validation'))]


def register(root,version='nba-adaptive-1',data=None):
    return store.register_candidate('NBA',version,100.,artifact(root),data or evidence(),sources(),now=500.)


def test_sourced_improvement_activates_once_and_is_append_only(isolated):
    c=register(isolated)
    outcome=store.activate_if_passes(c['candidate_id'],now=501.)
    assert outcome['status']=='activated'
    active=store.load_active('NBA')
    assert active['candidate_id']==c['candidate_id'] and active['artifact']['parameters']=={'weight':.5}
    assert store.activate_if_passes(c['candidate_id'],now=502.)['status']=='activated'
    assert len(store.history('NBA'))==1
    with pytest.raises(sqlite3.DatabaseError):db.ex('DELETE FROM model_learning_candidates')
    with pytest.raises(sqlite3.DatabaseError):db.ex('UPDATE model_learning_events SET reason="edited"')


def test_sparse_or_synthetic_or_regressing_evidence_never_promotes(isolated):
    for index,mutate in enumerate((
        lambda x:x.update(kind='synthetic'),
        lambda x:x.update(rows=x['rows'][:10]),
        lambda x:x['rows'][0].update(candidate_prediction=1000.),
        lambda x:x['rows'][0].update(candidate_probability=float('nan')))):
        data=evidence();mutate(data)
        if index==3:
            with pytest.raises(ValueError):register(isolated,str(index),data)
            continue
        c=register(isolated,str(index),data)
        assert store.activate_if_passes(c['candidate_id'],now=501.)['status']=='rejected'
    assert store.active('NBA') is None


@pytest.mark.parametrize('field,value',[('actual',99.),('line',9.5),('side','under'),
                                      ('kickoff_at',240.),('observed_at',199.)])
def test_caller_cannot_invent_validation_quote_or_result(isolated,field,value):
    data=evidence();data['rows'][0][field]=value
    c=register(isolated,data=data)
    result=store.activate_if_passes(c['candidate_id'],now=501.)
    assert result['status']=='rejected' and store.active('NBA') is None


def test_training_or_calibration_overlap_and_future_labels_are_rejected(isolated):
    for index,change in enumerate((
        lambda x:x['training'].update(fit_game_ids=['game-0']),
        lambda x:x['training'].update(calibration_game_ids=['game-1']),
        lambda x:x['training'].update(fit_label_max_at=101.),
        lambda x:x['training'].update(calibration_label_max_at=201.))):
        a=artifact(isolated);change(a)
        c=store.register_candidate('NBA',f'overlap-{index}',100.,a,evidence(),sources(),now=500.)
        assert store.activate_if_passes(c['candidate_id'],now=501.)['status']=='rejected'


def test_changed_artifact_or_serving_code_fails_closed(isolated):
    c=register(isolated);store.activate_if_passes(c['candidate_id'],now=501.)
    (isolated/'edgeterminal/learner.py').write_text('VERSION = 2\n')
    with pytest.raises(ValueError,match='implementation'):store.load_active('NBA')
    (isolated/'edgeterminal/learner.py').write_text('VERSION = 1\n')
    path=store.DIRECTORY/c['artifact_path'];path.chmod(0o644);path.write_text('{}')
    with pytest.raises(ValueError,match='artifact'):store.load_active('NBA')


def test_same_version_cannot_identify_another_model(isolated):
    original=register(isolated)
    assert register(isolated)['candidate_id']==original['candidate_id']
    a=artifact(isolated);a['parameters']['weight']=.6
    with pytest.raises(ValueError,match='version'):
        store.register_candidate('NBA','nba-adaptive-1',100.,a,evidence(),sources(),now=500.)


def test_rollback_can_restore_original_baseline_without_deleting_history(isolated):
    c=register(isolated);store.activate_if_passes(c['candidate_id'],now=501.)
    result=store.rollback('NBA','sourced forward deterioration',now=502.)
    assert result['status']=='rolled_back' and store.active('NBA') is None
    assert len(store.history('NBA'))==2


def test_changed_source_or_post_cutoff_receipt_is_rejected(isolated):
    s=sources();s[0]['sha256']='0'*64
    with pytest.raises(ValueError,match='source'):
        store.register_candidate('NBA','bad-source',100.,artifact(isolated),evidence(),s,now=500.)
    s=sources();s[0]['received_at']=101.
    with pytest.raises(ValueError,match='source'):
        store.register_candidate('NBA','late-source',100.,artifact(isolated),evidence(),s,now=500.)


def test_paths_outside_owned_directories_are_rejected(isolated):
    a=artifact(isolated);a['code_sha256']={'../private.py':'a'*64}
    with pytest.raises(ValueError,match='path'):
        store.register_candidate('NBA','unsafe-path',100.,a,evidence(),sources(),now=500.)


def test_stale_baseline_comparison_cannot_replace_newer_active_model(isolated):
    first=register(isolated,'first');second=register(isolated,'second')
    store.activate_if_passes(first['candidate_id'],now=501.)
    result=store.activate_if_passes(second['candidate_id'],now=502.)
    assert result['status']=='rejected' and store.active('NBA')['candidate_id']==first['candidate_id']


def test_rollback_walks_approved_predecessors_without_reactivating_bad_model(isolated):
    first=register(isolated,'first');store.activate_if_passes(first['candidate_id'],now=501.)
    data=evidence();data['baseline_candidate_id']=first['candidate_id']
    second=register(isolated,'second',data);store.activate_if_passes(second['candidate_id'],now=502.)
    assert store.rollback('NBA','second deteriorated',now=503.)['candidate_id']==first['candidate_id']
    assert store.rollback('NBA','first source failed',now=504.)['candidate_id'] is None
    assert store.active('NBA') is None
    assert len(store.history('NBA'))==4


def test_push_only_game_cannot_leak_into_training_catalog(isolated):
    data=evidence()
    additional=copy.deepcopy(data['rows'][:5])
    for index,row in enumerate(additional):
        row.update(game_id='push-game',quote_id=1000+index,actual=10.5)
    data['rows'].extend(additional)
    a=artifact(isolated);a['training']['fit_game_ids']=['push-game']
    result=store.gate(data,100.,a,sport='NBA',now=500.)
    assert not result['passed']
    assert 'training_validation_game_overlap' in result['reasons']


def test_final_status_and_recorded_grade_must_agree_with_actual(isolated):
    db.ex('UPDATE nba_games SET status="in_progress" WHERE game_id="game-0"')
    c=register(isolated,'unfinished')
    assert store.activate_if_passes(c['candidate_id'],now=501.)['status']=='rejected'
    db.ex('UPDATE nba_games SET status="final" WHERE game_id="game-0"')
    db.ex('UPDATE nba_grade_revisions SET result="loss" WHERE id=1')
    c=register(isolated,'inconsistent')
    assert store.activate_if_passes(c['candidate_id'],now=501.)['status']=='rejected'


def test_archived_source_survives_original_file_replacement(isolated):
    path=isolated/'data/nflverse/input.csv';path.parent.mkdir(parents=True)
    path.write_bytes(b'player,actual\np1,1\n')
    import os
    os.utime(path,(90.,90.))
    source=dict(kind='file',path='data/nflverse/input.csv',received_at=90.,role='training',
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    c=store.register_candidate('NBA','archived-file',100.,artifact(isolated),evidence(),
                               [source]+sources(),now=500.)
    path.write_bytes(b'changed upstream file')
    assert store.activate_if_passes(c['candidate_id'],now=501.)['status']=='activated'
    assert store.verify_active('NBA',now=502.)['candidate_id']==c['candidate_id']
    archived=store.DIRECTORY/'sources'/source['sha256'];archived.chmod(0o644);archived.write_bytes(b'changed')
    with pytest.raises(ValueError,match='source bytes'):store.verify_active('NBA',now=502.)


def test_development_tuning_rows_must_also_exclude_validation_games(isolated):
    a=artifact(isolated)
    a['training'].update(development_game_ids=['game-0'],development_label_max_at=99.)
    result=store.gate(evidence(),100.,a,sport='NBA',now=500.)
    assert not result['passed'] and 'training_validation_game_overlap' in result['reasons']
    a['training'].update(development_game_ids=['development-game'],development_label_max_at=101.)
    assert not store.gate(evidence(),100.,a,sport='NBA',now=500.)['passed']


def test_nonfinite_derived_metrics_are_rejected(isolated):
    data=evidence();data['rows'][0].update(actual=-1e308,baseline_prediction=1e308)
    result=store.gate(data,100.,artifact(isolated),sport='NBA',now=500.)
    assert not result['passed']
    assert any('finite' in reason for reason in result['reasons'])


def test_idempotent_activation_does_not_reenable_a_rolled_back_version(isolated):
    c=register(isolated);store.activate_if_passes(c['candidate_id'],now=501.)
    store.rollback('NBA','integrity review',now=502.)
    result=store.activate_if_passes(c['candidate_id'],now=503.)
    assert result['status']=='retained' and store.active('NBA') is None
    assert len(store.history('NBA'))==2


def test_nfl_validation_requires_archived_final_sources_and_unchanged_freeze(isolated):
    row=dict(evidence()['rows'][0],market='receptions')
    raw='{}';raw_sha=hashlib.sha256(raw.encode()).hexdigest()
    db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
          ('nfl-batch','sportsgameodds',201.,raw_sha,raw,1,1,1,'{}'))
    db.ex('INSERT INTO nfl_prop_snapshots(id,batch_id,canonical_game_id,provider_event_id,player_id,'
          'provider_player_id,player_name,team,opponent,market,line,side,price,sportsbook,retrieved_at,'
          'kickoff_utc,provider,source,line_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (1,'nfl-batch',row['game_id'],'provider-game',row['player_id'],'provider-player','Player','A','B',
           row['market'],row['line'],row['side'],-110,'book',row['observed_at'],row['kickoff_at'],
           'sportsgameodds','https://api.sportsgameodds.com/v2/events','main'))
    quote=dict(quote_id=1,canonical_game_id=row['game_id'],player_id=row['player_id'],market=row['market'],
               line=row['line'],side=row['side'],observed_at=row['observed_at'],kickoff_utc=row['kickoff_at'])
    freeze=json.dumps({'projections':[quote]},sort_keys=True,separators=(',',':'))
    freeze_sha=hashlib.sha256(freeze.encode()).hexdigest()
    db.ex('INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)',(''+freeze_sha,'nfl-batch',202.,freeze))
    source_body=b'{"final":true}';source_sha=hashlib.sha256(source_body).hexdigest()
    db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',
          (source_sha,'https://site.api.espn.com/result',350.,source_body))
    source=dict(sha256=source_sha,retrieved_at=350.)
    final=dict(game_id=row['game_id'],game_status='final',final_game_source=source,player_stat_source=source,
               players=[dict(gsis_player_id='gsis',status='active',stats={'receptions':row['actual']})])
    final_body=json.dumps(final,sort_keys=True,separators=(',',':'))
    final_sha=hashlib.sha256((freeze_sha+final_body).encode()).hexdigest()
    db.ex('INSERT INTO nfl_prop_grade_evidence VALUES(?,?,?,?,?,?,?,?)',
          (final_sha,freeze_sha,row['game_id'],'final','ESPN',350.,350.,final_body))
    payload=json.dumps(dict(actual_stat=row['actual'],research_statistical_result='won',gsis_player_id='gsis'))
    db.ex('INSERT INTO nfl_prop_grade_revisions VALUES(?,?,?,?,?,?)',(freeze_sha,1,1,final_sha,350.,payload))
    store._verify_row('NFL',row)
    with pytest.raises(ValueError,match='changed'):
        store._verify_row('NFL',{**row,'actual':99.})
    db.ex('DROP TRIGGER nfl_prop_research_freeze_no_update')
    db.ex('UPDATE nfl_prop_research_freezes SET payload="{}"')
    with pytest.raises(ValueError,match='freeze'):
        store._verify_row('NFL',row)
    db.ex('UPDATE nfl_prop_research_freezes SET payload=?',(freeze,))
    db.ex('DROP TRIGGER nfl_prop_grade_source_no_update')
    db.ex('UPDATE nfl_prop_grade_source_artifacts SET payload=?',(b'changed',))
    with pytest.raises(ValueError,match='source bytes'):
        store._verify_row('NFL',row)


def test_rollback_skips_invalid_predecessors_and_records_why(isolated):
    first=register(isolated,'first');store.activate_if_passes(first['candidate_id'],now=501.)
    data=evidence();data['baseline_candidate_id']=first['candidate_id']
    second=register(isolated,'second',data);store.activate_if_passes(second['candidate_id'],now=502.)
    path=store.DIRECTORY/first['artifact_path'];path.chmod(0o644);path.write_text('{}')
    result=store.rollback('NBA','active integrity failure',now=503.)
    assert result['candidate_id'] is None and store.active('NBA') is None
    skipped=store.history('NBA')[0]['evidence']['skipped_invalid']
    assert skipped[0]['candidate_id']==first['candidate_id'] and skipped[0]['reason']


def test_status_reads_do_not_create_learning_tables(isolated):
    assert store.active('NBA') is None and store.history('NBA')==[]
    assert db.q1("SELECT 1 FROM sqlite_master WHERE name='model_learning_candidates'") is None


def selective_artifact(root):
    a=artifact(root)
    a.update(changed_markets=['points'],ranking_changed=False,
        market_selection={'changed_markets':['points'],'selection_game_ids':['inner-dev'],
                          'selection_quote_ids':['inner-quote'],'selection_label_max_at':198.,
                          'selection_cutoff':199.})
    return a


def selective_evidence():
    data=evidence()
    for row in data['rows']:
        if row['market']!='points':
            row['candidate_prediction']=row['baseline_prediction']
            row['candidate_probability']=row['baseline_probability']
    return data


def test_selective_update_retains_sparse_markets_exactly(isolated):
    data=selective_evidence()
    data['rows']=[r for r in data['rows'] if r['market']!='threes' or r['game_id']=='game-0']
    result=store.gate(data,100.,selective_artifact(isolated),sport='NBA',now=500.)
    assert result['passed'],result
    data['rows'][1]['candidate_probability']+=1e-13
    result=store.gate(data,100.,selective_artifact(isolated),sport='NBA',now=500.)
    assert not result['passed'] and 'retained_market_changed:rebounds' in result['reasons']


def test_market_choice_must_precede_and_exclude_outer_games(isolated):
    for change in (dict(selection_game_ids=['game-0']),dict(selection_label_max_at=199.),
                   dict(changed_markets=['points','rebounds']),dict(selection_game_ids=['cal-game'])):
        a=selective_artifact(isolated);a['market_selection'].update(change)
        result=store.gate(selective_evidence(),100.,a,sport='NBA',now=500.)
        assert not result['passed'],change


def test_selective_endpoint_is_allowed_only_for_exact_retained_market(isolated):
    data=selective_evidence()
    row=data['rows'][1];row['baseline_probability']=row['candidate_probability']=1.
    assert store.gate(data,100.,selective_artifact(isolated),sport='NBA',now=500.)['passed']
    data['rows'][0]['candidate_probability']=1.
    assert not store.gate(data,100.,selective_artifact(isolated),sport='NBA',now=500.)['passed']


def policy_evidence(data):
    games=[]
    for gid in sorted({r['game_id'] for r in data['rows']}):
        rows=[copy.deepcopy(r) for r in data['rows'] if r['game_id']==gid]
        games.append(dict(game_id=gid,context_id='context-'+gid,
                          universe_quote_ids=[r['quote_id'] for r in rows],baseline=rows,candidate=copy.deepcopy(rows)))
    return dict(kind='common_full_offer_universe',games=games)


def test_ranker_change_requires_complete_common_policy_evidence(isolated):
    a=artifact(isolated);a['ranking_changed']=True
    data=evidence()
    result=store.gate(data,100.,a,sport='NBA',now=500.)
    assert not result['passed'] and 'full_policy_evidence_required' in result['reasons']
    data['selection_policy']=policy_evidence(data)
    assert store.gate(data,100.,a,sport='NBA',now=500.)['passed']
    data['selection_policy']['games'][0]['candidate'].pop()
    assert not store.gate(data,100.,a,sport='NBA',now=500.)['passed']


def test_ranker_cannot_improve_by_changing_cohort_or_duplicating_a_pick(isolated):
    a=artifact(isolated);a['ranking_changed']=True
    for mutate in (lambda p:p['games'].pop(),
                   lambda p:p['games'][0]['candidate'].__setitem__(1,p['games'][0]['candidate'][0]),
                   lambda p:p['games'][0]['candidate'][0].update(quote_id=999999)):
        data=evidence();data['selection_policy']=policy_evidence(data);mutate(data['selection_policy'])
        assert not store.gate(data,100.,a,sport='NBA',now=500.)['passed']


def test_outer_games_are_consumed_once_per_family_without_breaking_idempotency(isolated):
    a=selective_artifact(isolated);data=selective_evidence()
    a['evaluation_family']='quality-v2'
    c=store.register_candidate('NBA','quality-1',100.,a,data,sources(),now=500.)
    assert store.register_candidate('NBA','quality-1',100.,a,data,sources(),now=500.)['candidate_id']==c['candidate_id']
    assert not store.holdout_available('NBA','quality-v2',[r['game_id'] for r in data['rows']])
    with pytest.raises(ValueError,match='consumed'):
        store.register_candidate('NBA','quality-2',100.,a,data,sources(),now=501.)
    with pytest.raises(sqlite3.DatabaseError):db.ex('DELETE FROM model_learning_holdouts')


def test_quality_gate_requires_independent_date_blocks(isolated):
    a=selective_artifact(isolated);a['evaluation_family']='quality-v2'
    data=selective_evidence()
    result=store.gate(data,100.,a,sport='NBA',now=500.)
    assert not result['passed'] and 'insufficient_validation_dates' in result['reasons']
    for index,row in enumerate(data['rows']):
        offset=(index//5)*86400
        for key in ('observed_at','kickoff_at','result_observed_at'):row[key]+=offset
    data['validation_end']=3000000.
    result=store.gate(data,100.,a,sport='NBA',now=3000001.)
    assert result['passed'],result
    assert result['dates']==30 and result['date_brier_upper95']<0


def test_policy_sources_and_both_selected_collections_are_independently_checked(isolated,monkeypatch):
    from edgeterminal import nba_policy_evidence
    checks=[]
    monkeypatch.setattr(nba_policy_evidence,'verify_policy_universe',lambda group,**kwargs:checks.append(('universe',group['game_id'])),raising=False)
    monkeypatch.setattr(nba_policy_evidence,'verify_policy_row',lambda row:checks.append(('row',row['quote_id'])))
    data=evidence();data['selection_policy']=policy_evidence(data)
    for group in data['selection_policy']['games']:
        for name in ('baseline','candidate'):
            for row in group[name]:row['policy_context_id']=group['context_id']
    store._verify_evaluation('NBA',data)
    assert len([c for c in checks if c[0]=='universe'])==30
    assert len([c for c in checks if c[0]=='row'])==300
    def reject(row):
        if row['actual']==999.:raise ValueError('verified final differs')
    monkeypatch.setattr(nba_policy_evidence,'verify_policy_row',reject)
    data['selection_policy']['games'][0]['candidate'][0]['actual']=999.
    with pytest.raises(ValueError,match='verified final'):store._verify_evaluation('NBA',data)


def test_changing_family_never_refunds_spent_outer_games(isolated):
    a=artifact(isolated);a['evaluation_family']='first-family'
    store.register_candidate('NBA','family-1',100.,a,evidence(),sources(),now=500.)
    a['evaluation_family']='renamed-family'
    assert not store.holdout_available('NBA','renamed-family',['game-0'])
    with pytest.raises(ValueError,match='consumed'):
        store.register_candidate('NBA','family-2',100.,a,evidence(),sources(),now=501.)


def test_numeric_game_ids_cannot_hide_inner_outer_overlap(isolated):
    data=evidence();data['rows'][0]['game_id']=12345
    a=artifact(isolated);a['training']['fit_game_ids']=['12345']
    assert 'training_validation_game_overlap' in store.gate(data,100.,a,sport='NBA',now=500.)['reasons']


def test_development_catalog_requires_its_label_receipt_maximum(isolated):
    a=artifact(isolated);a['training']['development_game_ids']=['dev']
    assert not store.gate(evidence(),100.,a,sport='NBA',now=500.)['passed']


def test_policy_cannot_override_paired_forecasts_or_retained_market(isolated):
    data=evidence();data['selection_policy']=policy_evidence(data)
    a=artifact(isolated);a['ranking_changed']=True
    data['selection_policy']['games'][0]['candidate'][0]['candidate_probability']=.9
    assert not store.gate(data,100.,a,sport='NBA',now=500.)['passed']


def test_locked_validation_uses_frozen_schedule_after_official_start_delay(isolated,monkeypatch):
    from edgeterminal import nba_learning_data
    db.ex('CREATE TABLE nba_forecasts(forecast_id TEXT PRIMARY KEY,source_artifact_ids TEXT,created_at REAL)')
    db.ex('ALTER TABLE nba_selection_lock_items ADD COLUMN forecast_id TEXT')
    db.ex('INSERT INTO nba_forecasts VALUES(?,?,?)',('frozen','[3]',201.))
    db.ex('UPDATE nba_selection_lock_items SET forecast_id="frozen" WHERE quote_id=1')
    db.ex('UPDATE nba_games SET tipoff_ts=270. WHERE game_id="game-0"')
    monkeypatch.setattr(nba_learning_data,'capture_game',lambda forecast,current:dict(current,tipoff_ts=250.))
    store._verify_row('NBA',evidence()['rows'][0])
    with pytest.raises(ValueError,match='kickoff_at'):
        store._verify_row('NBA',dict(evidence()['rows'][0],kickoff_at=270.))


def test_full_policy_cannot_name_a_weaker_baseline_than_envelope(isolated,monkeypatch):
    import edgeterminal.nfl_learning as nfl_learning
    monkeypatch.setattr(nfl_learning,'verify_policy_universe',lambda *args,**kwargs:pytest.fail('weaker baseline reached verifier'))
    data={'rows':[], 'baseline_candidate_id':'active',
          'selection_policy':{'games':[{'baseline_candidate_id':'old'}]}}
    with pytest.raises(ValueError,match='baseline differs'):store._verify_evaluation('NFL',data)


def test_preledger_rejected_evaluation_is_not_a_fresh_quality_holdout(isolated):
    old=register(isolated,'legacy-evaluated')
    store.activate_if_passes(old['candidate_id'],now=501.)
    assert not store.holdout_available('NBA','new-protocol',['game-0'])
    a=artifact(isolated);a['evaluation_family']='new-protocol'
    with pytest.raises(ValueError,match='earlier model evaluation'):
        store.register_candidate('NBA','new-quality',100.,a,evidence(),sources(),now=502.)
