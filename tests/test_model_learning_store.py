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
        db.ex('INSERT INTO nba_source_artifacts VALUES(?,?,?,?,?,?,?)',
              (i,'fixture','https://site.api.espn.com/source',at,200,sha,body))
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
