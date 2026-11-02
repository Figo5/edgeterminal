"""Long verification cannot own the refresh or SQLite writer locks."""
import copy
import fcntl
import hashlib
import json
import sqlite3
import pytest

from test_model_learning_store import isolated, register, evidence, artifact, sources
from edgeterminal import db, model_learning_store as store
from edgeterminal.config import settings


def test_expensive_activation_verification_does_not_hold_writer_or_refresh_lock(isolated, monkeypatch):
    candidate = register(isolated)
    original = store._verify_evaluation
    def verify(*args):
        assert not db.conn().in_transaction, 'slow verification holds SQLite write transaction'
        with sqlite3.connect(str(settings.db_path), timeout=.1) as other:
            other.execute('INSERT INTO kv(k,v,updated_at) VALUES(?,?,?)', ('parallel-grading', '{}', 501.))
        with settings.db_path.with_name(settings.db_path.name + '-research.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original(*args)
    monkeypatch.setattr(store, '_verify_evaluation', verify)
    assert store.activate_if_passes(candidate['candidate_id'], now=501.)['status'] == 'activated'
    assert db.kv_get('parallel-grading') == {}


def test_result_revision_during_verification_defers_without_activation(isolated, monkeypatch):
    model = artifact(isolated); model['evaluation_family'] = 'publication-fixture'
    candidate = store.register_candidate('NBA', 'corrected', 100., model, evidence(), sources(), now=500.)
    original = store._verify_evaluation
    def verify(*args):
        original(*args)
        db.ex('UPDATE nba_grade_revisions SET actual_value=7.,result="loss",revision=2 WHERE id=1')
    monkeypatch.setattr(store, '_verify_evaluation', verify)
    result = store.activate_if_passes(candidate['candidate_id'], now=501.)
    assert result['status'] == 'deferred'
    assert store.active('NBA') is None and store.history('NBA') == []
    assert db.q1('SELECT COUNT(*) n FROM model_learning_holdouts')['n'] == 30


def test_source_head_change_during_verification_defers(isolated, monkeypatch):
    candidate = register(isolated)
    original = store._verify_evaluation
    def verify(*args):
        original(*args)
        db.ex('UPDATE nba_source_artifacts SET sha256=? WHERE id=4', ('f'*64,))
    monkeypatch.setattr(store, '_verify_evaluation', verify)
    assert store.activate_if_passes(candidate['candidate_id'], now=501.)['status'] == 'deferred'
    assert store.active('NBA') is None


def test_active_event_change_during_verification_cannot_be_overwritten(isolated, monkeypatch):
    candidate = register(isolated, 'proposed'); other = register(isolated, 'parallel')
    original = store._verify_evaluation
    def verify(*args):
        original(*args)
        db.ex('INSERT INTO model_learning_events(sport,candidate_id,event,previous_candidate_id,'
              'recorded_at,reason,evidence) VALUES(?,?,?,?,?,?,?)',
              ('NBA', other['candidate_id'], 'activated', None, 500.5, 'parallel publication', '{}'))
    monkeypatch.setattr(store, '_verify_evaluation', verify)
    assert store.activate_if_passes(candidate['candidate_id'], now=501.)['status'] == 'deferred'
    assert store.active('NBA')['candidate_id'] == other['candidate_id']


def test_refresh_lock_contention_defers_without_waiting_or_recording_approval(isolated):
    candidate = register(isolated)
    with settings.db_path.with_name(settings.db_path.name + '-research.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = store.activate_if_passes(candidate['candidate_id'], now=501.)
    assert result['status'] == 'deferred'
    assert store.active('NBA') is None and store.history('NBA') == []


def test_rollback_verifies_predecessor_without_holding_sqlite_writer(isolated, monkeypatch):
    first = register(isolated, 'first'); store.activate_if_passes(first['candidate_id'], now=501.)
    data = evidence(); data['baseline_candidate_id'] = first['candidate_id']
    second = register(isolated, 'second', data); store.activate_if_passes(second['candidate_id'], now=502.)
    original = store._verify_candidate
    def verify(*args):
        assert not db.conn().in_transaction, 'rollback replay owns SQLite writer'
        with sqlite3.connect(str(settings.db_path), timeout=.1) as other:
            other.execute('INSERT OR REPLACE INTO kv(k,v,updated_at) VALUES(?,?,?)', ('rollback-grading', '{}', 503.))
        return original(*args)
    monkeypatch.setattr(store, '_verify_candidate', verify)
    assert store.rollback('NBA', 'verified recovery', now=503.)['candidate_id'] == first['candidate_id']


def test_policy_label_already_superseded_before_proof_is_rejected(isolated, monkeypatch):
    data = evidence(); row = data['rows'][0]
    row.update(policy_context_id='context', result_source_artifact_id=4)
    db.ex('CREATE TABLE nba_prop_policy_contexts(context_id TEXT PRIMARY KEY,game_id TEXT,as_of REAL,tipoff_ts REAL,sha256 TEXT)')
    db.ex('INSERT INTO nba_prop_policy_contexts VALUES(?,?,?,?,?)', ('context', row['game_id'], 201., 250., 'c'*64))
    url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=123'
    db.ex('UPDATE nba_source_artifacts SET kind="final-boxscore",url=? WHERE id=4', (url,))
    body=b'{"corrected":true}'; sha=hashlib.sha256(body).hexdigest()
    db.ex('INSERT INTO nba_source_artifacts VALUES(?,?,?,?,?,?,?)', (5,'final-boxscore',url,450.,200,sha,body))
    candidate = register(isolated, data=data)
    monkeypatch.setattr(store, '_verify_evaluation', lambda *args: None)
    outcome = store.activate_if_passes(candidate['candidate_id'], now=501.)
    assert outcome['status'] == 'rejected'
    assert any('superseded' in reason for reason in outcome['reasons'])
    assert store.active('NBA') is None


def test_registered_candidate_is_checkpointed_in_owning_job_transaction(isolated):
    from edgeterminal import learning_worker
    learning_worker.ensure_schema()
    job=learning_worker.enqueue('NBA','retuning',now=500.)
    db.ex("UPDATE learning_jobs SET state='running',owner='fixture-owner' WHERE id=?",(job['id'],))
    token=learning_worker._CURRENT_JOB.set({'id':job['id'],'kind':'retuning','component':'NBA','owner':'fixture-owner'})
    try:
        candidate=register(isolated)
    finally:
        learning_worker._CURRENT_JOB.reset(token)
    saved=db.q1('SELECT detail FROM learning_jobs WHERE id=?',(job['id'],))
    assert json.loads(saved['detail'])['candidate_id']==candidate['candidate_id']


def test_changed_job_ownership_rolls_back_registration_and_holdout_use(isolated):
    from edgeterminal import learning_worker
    import pytest
    learning_worker.ensure_schema()
    job=learning_worker.enqueue('NBA','retuning',now=500.)
    token=learning_worker._CURRENT_JOB.set({'id':job['id'],'kind':'retuning','component':'NBA','owner':'old-owner'})
    model=artifact(isolated);model['evaluation_family']='atomic-checkpoint'
    try:
        with pytest.raises(ValueError,match='ownership'):
            store.register_candidate('NBA','checkpoint-conflict',100.,model,evidence(),sources(),now=500.)
    finally:
        learning_worker._CURRENT_JOB.reset(token)
    assert db.q1('SELECT COUNT(*) n FROM model_learning_candidates')['n']==0
    assert db.q1('SELECT COUNT(*) n FROM model_learning_holdouts')['n']==0


def test_full_integrity_verification_defers_a_concurrent_result_update(isolated, monkeypatch):
    import pytest
    from edgeterminal.learning_publication import PublicationDeferred
    candidate=register(isolated);store.activate_if_passes(candidate['candidate_id'],now=501.)
    original=store._verify_candidate
    def verify(*args):
        original(*args)
        db.ex('UPDATE nba_grade_revisions SET actual_value=7.,result="loss",revision=2 WHERE id=1')
    monkeypatch.setattr(store,'_verify_candidate',verify)
    with pytest.raises(PublicationDeferred,match='dependencies_changed'):
        store.verify_active('NBA',now=502.)
    assert store.active('NBA')['candidate_id']==candidate['candidate_id']


def test_refresh_owned_publication_context_is_reentrant_and_releases_marker(isolated):
    from edgeterminal import learning_publication as publication
    candidate=register(isolated)
    path=settings.db_path.with_name(settings.db_path.name+'-research.lock')
    with path.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with publication.research_owned():
            with publication.publish_lock():
                with publication.publish_lock():
                    assert db.conn().in_transaction is False
        assert store.activate_if_passes(candidate['candidate_id'],now=501.)['status']=='deferred'


def test_prop_candidate_waits_if_tipoff_becomes_near_during_verification(isolated, monkeypatch):
    from edgeterminal import model_learning
    near=[False]
    monkeypatch.setattr(model_learning,'_near_kickoff',lambda now:near[0])
    candidate=register(isolated)
    original=store._verify_evaluation
    def verify(*args):
        original(*args);near[0]=True
    monkeypatch.setattr(store,'_verify_evaluation',verify)
    assert store.activate_if_passes(candidate['candidate_id'],now=501.)['status']=='deferred'
    assert store.active('NBA') is None
    near[0]=False;monkeypatch.setattr(store,'_verify_evaluation',original)
    assert store.activate_if_passes(candidate['candidate_id'],now=502.)['status']=='activated'


def test_numeric_quote_ids_in_json_cannot_bypass_result_revision_guard(isolated, monkeypatch):
    data=evidence()
    for row in data['rows']:row['quote_id']=str(row['quote_id'])
    candidate=register(isolated,data=data)
    original=store._verify_evaluation
    def verify(*args):
        original(*args)
        db.ex('UPDATE nba_grade_revisions SET actual_value=7.,result="loss",revision=2 WHERE id=1')
    monkeypatch.setattr(store,'_verify_evaluation',verify)
    assert store.activate_if_passes(candidate['candidate_id'],now=501.)['status']=='deferred'
    assert store.active('NBA') is None


@pytest.mark.parametrize('game_metadata',[False,True])
@pytest.mark.parametrize('corrected',[False,True])
def test_legacy_final_source_must_be_current_before_verification(isolated, game_metadata, corrected):
    if game_metadata:
        db.ex('ALTER TABLE nba_games ADD COLUMN espn_event_id TEXT')
        db.ex('UPDATE nba_games SET espn_event_id="123" WHERE game_id="game-0"')
    url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=123'
    db.ex('UPDATE nba_source_artifacts SET kind="final-boxscore",url=? WHERE id=4',(url,))
    candidate=register(isolated)
    original=db.q1('SELECT body FROM nba_source_artifacts WHERE id=4')['body']
    body=b'{"corrected":true}' if corrected else bytes(original)
    db.ex('INSERT INTO nba_source_artifacts VALUES(?,?,?,?,?,?,?)',
        (5,'final-boxscore',url,450.,200,hashlib.sha256(body).hexdigest(),body))
    result=store.activate_if_passes(candidate['candidate_id'],now=501.)
    if corrected:
        assert result['status']=='rejected'
        assert any('superseded' in reason for reason in result['reasons'])
        assert store.active('NBA') is None
    else:
        assert result['status']=='activated'
        assert store.active('NBA')['candidate_id']==candidate['candidate_id']


@pytest.mark.parametrize('identity',['metadata','canonical_game','url'])
def test_legacy_final_source_must_match_game_identity(isolated, identity):
    data=evidence()
    url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401854123'
    if identity=='metadata':
        db.ex('ALTER TABLE nba_games ADD COLUMN espn_event_id TEXT')
        db.ex('UPDATE nba_games SET espn_event_id="401854999" WHERE game_id="game-0"')
    elif identity=='canonical_game':
        gid='nba-2026-401854999'
        db.ex('UPDATE nba_games SET game_id=? WHERE game_id="game-0"',(gid,))
        db.ex('UPDATE nba_quote_observations SET game_id=? WHERE game_id="game-0"',(gid,))
        for row in data['rows']:
            if row['game_id']=='game-0':row['game_id']=gid
    else:
        url='https://site.api.espn.com/redirect?event=401854123'
    db.ex('UPDATE nba_source_artifacts SET kind="final-boxscore",url=? WHERE id=4',(url,))
    candidate=register(isolated,data=data)
    result=store.activate_if_passes(candidate['candidate_id'],now=501.)
    assert result['status']=='rejected'
    assert any('identity' in reason for reason in result['reasons'])
    assert store.active('NBA') is None


@pytest.mark.parametrize('url',['https://site.api.espn.com/source',
    'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=123'])
def test_legacy_grade_wrong_source_kind_cannot_bypass_final_validation(isolated,url):
    db.ex('UPDATE nba_source_artifacts SET kind="other",url=? WHERE id=4',(url,))
    candidate=register(isolated)
    result=store.activate_if_passes(candidate['candidate_id'],now=501.)
    assert result['status']=='rejected'
    assert any('identity' in reason for reason in result['reasons'])
    assert store.active('NBA') is None


@pytest.mark.parametrize('corrected',[False,True])
def test_legacy_final_receipt_change_during_proof(isolated,monkeypatch,corrected):
    url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=123'
    db.ex('UPDATE nba_source_artifacts SET kind="final-boxscore",url=? WHERE id=4',(url,))
    candidate=register(isolated)
    original=store._verify_evaluation
    def verify(*args):
        original(*args)
        source=db.q1('SELECT body FROM nba_source_artifacts WHERE id=4')
        body=b'{"corrected":true}' if corrected else bytes(source['body'])
        db.ex('INSERT INTO nba_source_artifacts VALUES(?,?,?,?,?,?,?)',
            (5,'final-boxscore',url,500.5,200,hashlib.sha256(body).hexdigest(),body))
    monkeypatch.setattr(store,'_verify_evaluation',verify)
    result=store.activate_if_passes(candidate['candidate_id'],now=501.)
    assert result['status']==('deferred' if corrected else 'activated')
    assert (store.active('NBA') is None)==corrected
