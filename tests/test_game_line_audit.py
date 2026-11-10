"""Recovery research cannot change the original prospective decision record."""
import copy
import hashlib
import json
import sqlite3

import pytest

from edgeterminal import db
from test_nfl_foundation import isolated_db


def pair(at=100, line=-2.5):
    return dict(sport='NBA', game_id='nba-2026-401909959', market='spread',
                forecast_id='original-'+str(at), forecast_sha256='a'*64,
                model_version='original-v1', forecast_at=at, as_of=at, tipoff=200,
                observed_at=90, home='GSW', away='NOP', line=line,
                projection=6., side='home', source={'sha256':'b'*64, 'id':1})


def test_recovery_pair_selection_ignores_results_and_never_grafts_latest_projection():
    from edgeterminal import game_line_audit as audit
    rows=[{'created_at':120,'id':'new','pair':None,'final_home_score':999},
          {'created_at':110,'id':'priced','pair':pair(110),'final_home_score':0},
          {'created_at':100,'id':'old','pair':pair(100, -10)}]
    verify=lambda row, market: row['pair']
    first=audit._select_pair(rows, 'spread', verify)
    for row in rows:row['final_home_score']=-999
    assert audit._select_pair(rows, 'spread', verify)==first==pair(110)


def test_same_time_conflicting_pairs_are_not_resolved_to_a_favorable_quote():
    from edgeterminal import game_line_audit as audit
    rows=[{'created_at':100,'id':'a','pair':pair(100)},
          {'created_at':100,'id':'b','pair':pair(100, -10)}]
    with pytest.raises(ValueError, match='ambiguous'):
        audit._select_pair(rows, 'spread', lambda row, market:row['pair'])


def test_corrupt_newer_origin_does_not_fall_back_to_an_older_pair():
    from edgeterminal import game_line_audit as audit
    def verify(row, market):
        if row['id']=='broken':raise ValueError('source hash conflict')
        return pair()
    with pytest.raises(ValueError, match='source hash'):
        audit._select_pair([{'created_at':101,'id':'broken'}, {'created_at':100,'id':'old'}],
                           'spread', verify)


def test_reading_missing_audit_is_passive(isolated_db):
    from edgeterminal import game_line_audit as audit
    before=db.q("SELECT name FROM sqlite_master WHERE type='table'")
    assert audit.view('NBA')['rows']==[]
    assert db.q("SELECT name FROM sqlite_master WHERE type='table'")==before


def test_partial_audit_schema_is_visible_and_not_silently_recreated(isolated_db):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema();db.ex('DROP TABLE game_line_audit_results')
    assert audit.public_view('NBA')['status']=='needs_review'
    with pytest.raises(ValueError,match='schema'):
        audit.ensure_schema()
    assert db.q1("SELECT name FROM sqlite_master WHERE name='game_line_audit_results'") is None


def test_first_audit_schema_initialization_is_atomic(isolated_db):
    from edgeterminal import game_line_audit as audit
    def deny_second_table(action, name, *args):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and name=='game_line_audit_results' else sqlite3.SQLITE_OK
    con=db.conn();con.set_authorizer(deny_second_table)
    try:
        with pytest.raises(sqlite3.DatabaseError):audit.ensure_schema()
    finally:
        # This bundled SQLite adapter does not clear the hook with None.
        con.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
    assert db.q("SELECT name FROM sqlite_master WHERE name LIKE 'game_line_audit_%'")==[]


def test_malformed_resealed_audit_stays_visible_without_hiding_record(isolated_db):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema()
    db.ex('INSERT INTO game_line_audit_bindings VALUES(?,?,?,?,?,?,?)',
          ('corrupt', 'NBA', 'nba-2026-401909959', 'spread', 300,
           audit._encoded([]), audit._sha([])))
    with pytest.raises(AttributeError):
        audit.view('NBA')
    result = audit.public_view('NBA')
    assert result['status'] == 'needs_review' and result['rows'] == []
    assert result['error_type'] == 'AttributeError'


def test_audit_database_failure_is_isolated_at_presentation_only(isolated_db, monkeypatch):
    from edgeterminal import game_line_audit as audit
    def unavailable(sport):
        raise sqlite3.DatabaseError('private database detail')
    monkeypatch.setattr(audit, 'view', unavailable)
    result = audit.public_view('NFL')
    assert result['status'] == 'needs_review' and result['rows'] == []
    assert 'private database detail' not in str(result)
    with pytest.raises(sqlite3.DatabaseError):
        audit.view('NFL')


def test_recovery_ledgers_are_immutable_idempotent_and_separate(isolated_db, monkeypatch):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema()
    monkeypatch.setattr(audit, '_verify_bound_pair', lambda p: None)
    monkeypatch.setattr(audit, '_verify_final_reference', lambda *a: None)
    old_prediction_rows=db.q('SELECT * FROM nfl_game_predictions')
    binding=audit._append_binding(pair(), now=300)
    assert audit._append_binding(pair(), now=301)==binding
    audit._append_result(binding, {'home_score':138,'away_score':87,
        'received_at':250,'source':{'id':2,'sha256':'c'*64}}, now=310)
    audit._append_result(binding, {'home_score':138,'away_score':87,
        'received_at':250,'source':{'id':2,'sha256':'c'*64}}, now=311)
    view=audit.view('NBA')
    assert view['rows'][0]['result']=='won'
    assert view['excluded_from_live_record'] and view['excluded_from_learning']
    assert db.q('SELECT * FROM nfl_game_predictions')==old_prediction_rows
    assert db.q1('SELECT COUNT(*) n FROM game_line_audit_results')['n']==1
    for table in ('game_line_audit_bindings','game_line_audit_results'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.ex('DELETE FROM '+table)


@pytest.mark.parametrize('key,value', [('game_id','nba-2026-unapproved'),
    ('forecast_at',201), ('observed_at',150), ('forecast_sha256','broken'),
    ('line',float('nan')), ('market','moneyline')])
def test_recovery_refuses_unapproved_or_unsafe_evidence(isolated_db,key,value):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema();p=pair();p[key]=value
    with pytest.raises(ValueError):audit._append_binding(p,now=300)
    assert db.q1('SELECT COUNT(*) n FROM game_line_audit_bindings')['n']==0


def test_final_cannot_precede_tipoff_or_change_the_bound_side(isolated_db, monkeypatch):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema();binding=audit._append_binding(pair(),now=300)
    monkeypatch.setattr(audit, '_verify_bound_pair', lambda p: None)
    with pytest.raises(ValueError):
        audit._append_result(binding, {'home_score':138,'away_score':87,
            'received_at':199,'source':{'id':2,'sha256':'c'*64}},now=310)
    assert audit.view('NBA')['rows'][0]['result']=='pending'


def test_audit_reader_refuses_a_pair_whose_original_source_is_missing(isolated_db):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema();audit._append_binding(dict(pair(), sport='NFL', game_id='nfl-2026-reg-09-buf-min'), now=300)
    with pytest.raises(ValueError, match='original pair/source changed'):
        audit.view('NFL')


def test_audit_binding_cannot_change_side_without_changing_original_projection(isolated_db):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema()
    with pytest.raises(ValueError, match='saved projection'):
        audit._append_binding(dict(pair(), side='away'), now=300)


def test_audit_reader_rechecks_exact_final_source_after_publication(isolated_db, monkeypatch):
    from edgeterminal import game_line_audit as audit, nba_store, nba_data
    from test_nba_data import SUMMARY
    nba_store.ensure_schema();audit.ensure_schema()
    raw=copy.deepcopy(SUMMARY);raw['header']['id']='401909959'
    actual=nba_data.parse_summary(raw)['game']
    from edgeterminal.integrity import parse_time
    tip=parse_time(actual['tipoff_utc'])
    p=dict(pair(),home=actual['home_team'],away=actual['away_team'],tipoff=tip,
           observed_at=tip-200,as_of=tip-100,forecast_at=tip-100)
    monkeypatch.setattr(audit,'_verify_bound_pair',lambda p:None)
    binding=audit._append_binding(p,now=tip+150)
    body=json.dumps(raw).encode();digest=hashlib.sha256(body).hexdigest()
    source=nba_store.save_artifact({'kind':'final-boxscore','body':body,'sha256':digest,
        'url':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401909959',
        'received_at':tip+100,'http_status':200})
    audit._append_result(binding,{'home_score':actual['home_score'],'away_score':actual['away_score'],
        'received_at':tip+100,'source':{'id':source,'sha256':digest,
        'url':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401909959'}},now=tip+160)
    # Deliberate corruption belongs only to this owned test database.
    db.ex('DROP TRIGGER nba_source_artifacts_no_update')
    db.ex('UPDATE nba_source_artifacts SET body=? WHERE id=?',(b'{}',source))
    with pytest.raises(ValueError,match='final source'):
        audit.view('NBA')


def test_nfl_audit_reads_exact_referenced_final_and_rejects_later_corruption(isolated_db, monkeypatch):
    from edgeterminal import game_line_audit as audit
    audit.ensure_schema();monkeypatch.setattr(audit,'_verify_bound_pair',lambda p:None)
    p=dict(pair(),sport='NFL',game_id='nfl-2026-reg-09-buf-min',line=3.5,projection=-.69,
           home='Minnesota Vikings',away='Buffalo Bills')
    freeze='f'*64;game='2026_09_BUF_MIN'
    url='https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
    original=dict(freeze_sha256=freeze,game_id=game,final_home_score=6,final_away_score=23,
                  result_provider='espn',result_source=url,result_retrieved_at=250,
                  result_request=dict(season=2026,season_type=2,week=9),kickoff_utc=200,
                  home_team=p['home'],away_team=p['away'])
    encoded=json.dumps(original,sort_keys=True)
    db.ex('INSERT INTO nfl_research_grades VALUES(?,?,?,?,?,?,?,?,?)',
          (freeze,game,1,6,23,url,250,260,encoded))
    binding=audit._append_binding(p,now=300)
    final=dict(home_score=6,away_score=23,received_at=250,source=dict(freeze_sha=freeze,
        game_id=game,revision=1,url=url,payload_sha256=hashlib.sha256(encoded.encode()).hexdigest()))
    audit._append_result(binding,final,now=310)
    changed=dict(original,final_home_score=30,result_retrieved_at=350)
    db.ex('INSERT INTO nfl_research_grades VALUES(?,?,?,?,?,?,?,?,?)',
          (freeze,game,2,30,23,url,350,360,json.dumps(changed,sort_keys=True)))
    assert audit.view('NFL')['rows'][0]['result']=='lost'  # exact referenced final, not revision 2
    db.ex('DROP TRIGGER nfl_research_grade_no_update')
    db.ex('UPDATE nfl_research_grades SET payload=? WHERE revision=1', ('{}',))
    with pytest.raises(ValueError,match='final source'):
        audit.view('NFL')
