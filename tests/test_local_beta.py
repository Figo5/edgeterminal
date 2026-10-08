"""Member isolation, hosted boundaries, immutable saves, and source explanations."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from edgeterminal import beta_access, db, members, product_insights, product_views, service_ops
from edgeterminal.api import app
from edgeterminal.config import settings
from edgeterminal.nfl_season_record import diagnostics


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "beta.db"}')
    monkeypatch.setattr(settings, 'hosted_mode', False)
    monkeypatch.setattr(settings, 'public_url', 'https://testserver')
    db.init()
    yield tmp_path
    db._conn.close()
    db._conn = None


def selection(game='nfl-2026-reg-05-tb-dal', quote=1):
    return {'game_id': game, 'canonical_game_id': game, 'freeze_sha': 'freeze', 'quote_id': quote,
            'player_id': 'p1', 'player': 'Player', 'player_name': 'Player',
            'market': 'receptions', 'side': 'over', 'stronger_side': 'over', 'sportsbook': 'draftkings',
            'line': 3.5, 'projection': 5., 'confidence': .75, 'price': -110,
            'kickoff_utc': time.time() + 3600, 'result': 'pending', 'selected_at': time.time()-20,
            'revision': None, 'graded_at': None, 'uncertainty_sigma': 2.}


def hosted(monkeypatch):
    monkeypatch.setattr(settings, 'hosted_mode', True)
    return TestClient(app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


def sign_in(client, email):
    link = members.invite(email, 30)
    token = link.split('#signin=')[1]
    response = client.post('/api/auth/signin', json={'token': token})
    assert response.status_code == 200
    return token, response


def test_local_profile_save_is_idempotent_and_cannot_fabricate_a_selection(isolated, monkeypatch):
    row = selection()
    monkeypatch.setattr(members, 'selected_rows', lambda: {1: row})
    client = TestClient(app)
    assert client.get('/api/account').json()['local']
    for _ in range(2):
        assert client.post('/api/account/props/1').status_code == 200
    assert len(client.get('/api/account/saved').json()['props']) == 1
    assert client.post('/api/account/props/999').status_code == 404
    row['result'], row['actual'], row['revision'] = 'lost', 2, 1
    assert client.get('/api/account/saved').json()['props'][0]['result'] == 'lost'
    row['line'] = 4.5
    with pytest.raises(ValueError, match='identity mismatch'):
        client.get('/api/account/saved')


def test_magic_link_is_one_use_and_session_is_revoked_on_logout(isolated, monkeypatch):
    client = hosted(monkeypatch)
    token, response = sign_in(client, 'tester@example.com')
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'secure' in cookie and 'samesite=lax' in cookie
    assert client.get('/api/account').json()['full_access']
    assert client.post('/api/auth/signin', json={'token': token}).status_code == 400
    stored = db.q1('SELECT token_hash FROM member_sessions')['token_hash']
    assert stored != client.cookies.get(members.COOKIE)
    assert client.post('/api/auth/signout').status_code == 200
    assert db.q('SELECT * FROM member_sessions') == []
    assert not client.get('/api/account').json()['signed_in']


def test_expired_token_cannot_log_in(isolated, monkeypatch):
    client = hosted(monkeypatch)
    token = members.invite('tester@example.com', 30).split('#signin=')[1]
    db.ex('UPDATE member_login_tokens SET expires_at=0')
    assert client.post('/api/auth/signin', json={'token': token}).status_code == 400


def test_two_members_cannot_read_delete_or_mark_each_others_saved_data(isolated, monkeypatch):
    monkeypatch.setattr(members, 'selected_rows', lambda: {1: selection()})
    first, second = hosted(monkeypatch), hosted(monkeypatch)
    sign_in(first, 'one@example.com')
    sign_in(second, 'two@example.com')
    first.post('/api/account/props/1')
    assert second.get('/api/account/saved').json()['props'] == []
    second.delete('/api/account/props/1')
    assert len(first.get('/api/account/saved').json()['props']) == 1
    owner = db.q1('SELECT id FROM members WHERE email=?', ('one@example.com',))['id']
    db.ex("INSERT INTO member_alerts(id,member_id,created_at,kind,game_id,message) VALUES('a',?,1,'result','game','Loss')", (owner,))
    second.post('/api/account/alerts/read')
    assert first.get('/api/account/alerts').json()['alerts'][0]['read_at'] is None
    assert second.get('/api/account/alerts').json()['alerts'] == []


@pytest.mark.parametrize('path,method', [('/api/grade', 'post'), ('/api/refresh', 'post'),
    ('/api/dashboard?refresh_analysis=true', 'get'), ('/api/event/game?refresh_odds=true', 'get'),
    ('/api/debug/research', 'get'), ('/api/props/best', 'get'), ('/api/predictions', 'get'),
    ('/api/product/nfl/refresh', 'post'), ('/api/research/refresh', 'post')])
def test_hosted_readers_cannot_reach_legacy_or_ingestion_routes(isolated, monkeypatch, path, method):
    client = hosted(monkeypatch)
    assert getattr(client, method)(path).status_code == 403


def test_csrf_and_host_checks_and_private_response_cache_headers(isolated, monkeypatch):
    client = hosted(monkeypatch)
    assert client.post('/api/auth/signout', headers={'Origin': 'https://evil.example'}).status_code == 403
    client.headers.pop('origin')
    assert client.post('/api/auth/signout').status_code == 403
    assert client.get('/api/account', headers={'Host': 'evil.example'}).status_code == 400
    response = client.get('/api/account')
    assert response.headers['cache-control'] == 'no-store'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']


def test_preview_does_not_leak_props_through_board_matchup_or_performance(isolated, monkeypatch):
    client = hosted(monkeypatch)
    first, private = 'nfl-2026-reg-05-a-b', 'nfl-2026-reg-05-c-d'
    db.kv_set('nfl_current_schedule', {'game_ids': [first, private], 'week': 5})
    monkeypatch.setattr(beta_access, 'featured_game', lambda: first)
    row = selection(private)
    game = {'id': private, 'kickoff_utc': '2099-01-01T00:00:00Z', 'top_props': [row]}
    monkeypatch.setattr(product_views, 'weekly_board', lambda _: {'games': [game]})
    monkeypatch.setattr(product_views, 'matchup', lambda _: {'game': game, 'props': {'rows': [row]},
                                                             'top_five': {'games': [{'rows': [row]}]}})
    monkeypatch.setattr(product_views, 'performance', lambda: {'season_record': {
        'props': {'games': [{'game_id': private, 'rows': [row]}]}}, 'top_five': {'secret': row}})
    assert client.get('/api/product/nfl?week=5').json()['games'][0]['top_props'] == []
    matchup = client.get('/api/product/nfl/' + private).json()
    assert matchup['props']['rows'] == [] and matchup['top_five']['games'] == []
    performance = client.get('/api/product/performance').json()
    assert performance['season_record']['props']['games'][0]['rows'] == []
    assert 'top_five' not in performance
    sign_in(client, 'member@example.com')
    assert client.get('/api/product/nfl').json()['games'][0]['top_props'][0]['quote_id'] == 1


def test_expired_membership_loses_upcoming_props_but_keeps_own_results(isolated, monkeypatch):
    client = hosted(monkeypatch)
    row = selection()
    monkeypatch.setattr(members, 'selected_rows', lambda: {1: row})
    sign_in(client, 'member@example.com')
    client.post('/api/account/props/1')
    db.ex('UPDATE members SET access_until=0')
    assert not client.get('/api/account').json()['full_access']
    assert client.post('/api/account/props/1').status_code == 403
    assert client.get('/api/account/saved').json()['props'] == []
    row['kickoff_utc'] = time.time() - 3600
    assert len(client.get('/api/account/saved').json()['props']) == 1


def test_alerts_are_idempotent_and_corrections_add_a_new_alert(isolated, monkeypatch):
    row = selection()
    monkeypatch.setattr(members, 'selected_rows', lambda: {1: row})
    monkeypatch.setattr(product_insights, 'prop', lambda r: {'latest_quote': None})
    member = members.local_owner()
    members.save_prop(member, 1)
    row.update(result='won', revision=1, actual=5, graded_at=time.time()+10)
    assert members.sync_alerts()['created'] == 1
    assert members.sync_alerts()['created'] == 0
    row.update(result='lost', revision=2, actual=2, graded_at=time.time()+20)
    assert members.sync_alerts()['created'] == 1
    assert len(db.q('SELECT * FROM member_alerts')) == 2
    original = json.loads(db.q1('SELECT payload FROM member_saved_props')['payload'])
    assert original['result'] == 'pending' and original['line'] == 3.5


def test_line_insight_uses_stored_quote_and_leaves_selection_fixed(isolated, monkeypatch):
    row = selection()
    monkeypatch.setattr(db, 'q1', lambda *a: {'id': 4, 'line': 4.5, 'price': -120,
                                            'provider_updated_at': 100., 'retrieved_at': 101.})
    output = product_insights.prop(row, now=110.)
    assert output['line'] == 3.5 and output['latest_quote']['line'] == 4.5
    assert output['latest_quote']['line_changed']
    assert output['implied_probability'] == pytest.approx(110/210)
    assert output['probability_difference'] == pytest.approx(.75 - 110/210)
    assert row.get('latest_quote') is None
    assert 'not confirmed' in output['availability_note']


def test_probability_diagnostics_exclude_pending_void_push_and_invalid(isolated):
    rows = [{'result': 'won', 'confidence': .75, 'market': 'receptions'},
            {'result': 'lost', 'confidence': .75, 'market': 'receptions'},
            *[{'result': r, 'confidence': .95} for r in ('push', 'pending', 'void')],
            {'result': 'won', 'confidence': float('nan')}]
    output = diagnostics(rows)
    assert output['graded_count'] == 2
    assert output['brier_score'] == .3125
    assert output['probability_bands'][2]['average_probability'] == .75
    assert output['probability_bands'][2]['win_rate'] == .5


def test_online_backup_is_valid_includes_wal_and_does_not_mutate_forecasts(isolated):
    directory = isolated / 'backups'
    db.kv_set('test_wal_value', {'value': 17})
    output = service_ops.backup(directory, now=1000.)
    with sqlite3.connect(output['path']) as copy:
        assert copy.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert json.loads(copy.execute("SELECT v FROM kv WHERE k='test_wal_value'").fetchone()[0])['value'] == 17
    assert output['ok']
    with pytest.raises(ValueError, match='already exists'):
        service_ops.backup(directory, now=1000.)
