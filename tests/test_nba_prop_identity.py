"""End-to-end identity evidence survives capture, locks, and learning replay."""
import copy
import hashlib
import json

import pytest

from edgeterminal import db, nba_data, nba_learning_data, nba_ops, nba_quotes, nba_sources, nba_store
from edgeterminal.config import settings
from edgeterminal.integrity import parse_time


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "identity.db"}')
    monkeypatch.setattr(settings, 'sportsgameodds_api_key', 'fixture')
    db.init()
    nba_ops.ensure_schema()
    nba_quotes.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def artifact(kind, url, payload, received):
    raw = json.dumps(payload, sort_keys=True).encode()
    return nba_store.save_artifact(dict(kind=kind, url=url, body=raw,
        http_status=200, received_at=received, sha256=hashlib.sha256(raw).hexdigest()))


def capture_case(monkeypatch):
    from test_nba_data import SUMMARY
    final_body = copy.deepcopy(SUMMARY)
    final_body['header']['season']['year'] = 2027
    final_body['header']['competitions'][0]['date'] = '2026-10-20T23:00Z'
    final = nba_data.parse_summary(final_body)['game']
    tip = parse_time(final['tipoff_utc'])
    now = tip - 2 * 3600
    monkeypatch.setattr(nba_ops.time, 'time', lambda: now)
    source = artifact('scoreboard', nba_sources.route('scoreboard'), {}, now - 4000)
    game = dict(final, status='scheduled', home_score=None, away_score=None)
    nba_store.upsert_game(game, source, now=now - 3000)
    nba_store.save_forecast('game-' + game['game_id'], game['game_id'], 'fixture',
        {'kind': 'game', 'projection': {'home_margin': 5., 'total': 220.}, 'market': {}},
        [source], now=now - 3000)

    players = [('3064514', 'Julius Randle', 'MIN'), ('4396971', 'Naz Reid', 'MIN'),
               ('4278073', 'Shai Gilgeous-Alexander', 'OKC'),
               ('4433255', 'Chet Holmgren', 'OKC'), ('2991350', 'Alex Caruso', 'OKC')]
    parsed_final = nba_data.parse_summary(final_body)
    actuals = {p['espn_player_id']: p['stats']['points'] for p in parsed_final['players']
               if p['status'] == 'played'}
    for code, tid in [('MIN', '16'), ('OKC', '25')]:
        payload = dict(season={'year': 2027}, team={'id': tid, 'abbreviation': code},
            athletes=[dict(id=pid, displayName=name, status={'type': 'active'}, injuries=[])
                      for pid, name, team in players if team == code])
        rid = artifact('roster', nba_sources.route(f'teams/{tid}/roster'), payload, now - 300)
        db.kv_set('nba_roster_' + code, [dict(player_id=pid, player=name, team=team,
            availability='not_listed_on_roster_injury_feed', observed_at=now - 300,
            source_artifact_id=rid) for pid, name, team in players if team == code])
    for pid, name, team in players:
        events = {}
        values = []
        for i in range(15):
            gid = str(401700000 + i)
            events[gid] = dict(gameDate=f'2026-03-{i + 1:02d}T00:00Z',
                              leagueAbbreviation='NBA', team={'abbreviation': team})
            values.append(dict(eventId=gid, stats=['30', str(actuals[pid] + 4), '5', '4', '2-4']))
        payload = dict(names=['minutes', 'points', 'totalRebounds', 'assists',
                              'threePointFieldGoalsMade-threePointFieldGoalsAttempted'],
            events=events, seasonTypes=[dict(displayName='2025-26 Regular Season',
                                            categories=[dict(type='event', events=values)])])
        hid = artifact('player-history', nba_sources.route(f'athletes/{pid}/gamelog') + '?season=2026',
                       payload, now - 300)
        raw_source = nba_store.get_artifact(hid)
        history = nba_sources.player_history(payload, pid, raw_source['sha256'])
        db.kv_set('nba_player_history_' + pid, [dict(row, player=name, source_artifact_id=hid)
                                               for row in history])

    event = dict(eventID='provider-fixture', leagueID='NBA', status={'startsAt': game['tipoff_utc']},
        teams={'home': {'teamID': 'OKC_NBA', 'names': {'short': 'OKC'}},
               'away': {'teamID': 'MIN_NBA', 'names': {'short': 'MIN'}}}, players={}, odds={})
    for index, (pid, name, team) in enumerate(players):
        ref = f'opaque-provider-{index}'
        event['players'][ref] = dict(playerID=ref, teamID=team + '_NBA', name=name)
        key = f'points-{ref}-game-ou-over'
        event['odds'][key] = dict(oddID=key, statID='points', statEntityID=ref, periodID='game',
            betTypeID='ou', sideID='over', byBookmaker={'draftkings': {
                'available': True, 'overUnder': str(actuals[pid] + .5), 'odds': '-105'}})
    old = copy.deepcopy(event)
    for odd in old['odds'].values():
        odd['byBookmaker']['draftkings'].update(overUnder='10.5', odds='+150')
    proof = artifact('player-quotes', nba_quotes.URL + '?leagueID=NBA',
                     {'success': True, 'data': [old]}, now - 3600)
    fresh = copy.deepcopy(event)
    for player in fresh['players'].values():
        del player['name']
    calls = []

    def get(url, **kwargs):
        assert url == nba_quotes.URL
        assert kwargs['params']['includeAltLines'] == 'false'
        calls.append(kwargs)
        return 200, json.dumps({'success': True, 'data': [fresh]}).encode(), {}

    monkeypatch.setattr(nba_quotes, 'get_raw_meta', get)
    return dict(game=game, final=final, final_body=final_body, now=now, tip=tip,
                proof=proof, fresh=fresh, actuals=actuals, calls=calls)


def finalize(case):
    after = case['tip'] + 3 * 3600
    source = artifact('final-boxscore', nba_sources.route('summary') +
        '?event=' + case['game']['espn_event_id'], case['final_body'], after)
    nba_store.upsert_game(case['final'], source, now=after)
    lock = nba_store.get_lock('top5-' + case['game']['game_id'])
    for item in lock['items']:
        actual = case['actuals'][item['espn_player_id']]
        assert actual < item['line']
        nba_store.add_grade_revision(lock['lock_id'], item['rank'], 'loss', actual, source, now=after)
    return after


def test_nameless_fresh_capture_uses_archived_names_and_current_quote_time_and_price(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    roster = nba_ops._rosters([case['game']])
    ready = dict(case['game'], id=case['game']['game_id'], tipoff=case['game']['tipoff_utc'])
    captured = nba_quotes.capture([ready], roster, case['now'])
    assert captured['status'] == 'captured' and len(captured['quotes']) == 5
    assert len(case['calls']) == 1
    assert all(q['identity_source_artifact_id'] == case['proof'] for q in captured['quotes'])
    assert all(q['observed_at'] == case['now'] and q['price'] == -105 for q in captured['quotes'])
    assert {q['line'] for q in captured['quotes']} == {v + .5 for v in case['actuals'].values()
                                                      if v in (32, 6, 20, 21, 17)}


def test_capture_persists_identity_proof_in_rows_and_forecast_source_manifest(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    result = nba_ops._capture_props(case['now'])
    assert result['new_locks'] == 1 and result['quotes'] == 5
    assert len(case['calls']) == 1
    forecast = nba_store.get_forecast('props-' + case['game']['game_id'])
    assert case['proof'] in forecast['source_artifact_ids']
    assert len(forecast['payload']['rows']) == 5
    assert all(row['identity_source_artifact_id'] == case['proof'] for row in forecast['payload']['rows'])
    assert all(row['provider_player_id'].startswith('opaque-provider-') for row in forecast['payload']['rows'])
    quotes = db.q('SELECT * FROM nba_quote_observations')
    assert len(quotes) == 5 and all(q['observed_at'] == case['now'] and q['price'] == -105 for q in quotes)
    assert all(q['line'] == case['actuals'][q['espn_player_id']] + .5 for q in quotes)


def test_frozen_learning_replays_nameless_quote_with_declared_identity_source(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    assert nba_ops._capture_props(case['now'])['new_locks'] == 1
    after = finalize(case)
    monkeypatch.setattr(nba_quotes, 'get_raw_meta', lambda *a, **k: pytest.fail('learning fetched quotes'))
    records, context, sources = nba_learning_data.validation_records(after + 1, game_count=1)
    assert len(records) == 5 and context['game_ids'] == [case['game']['game_id']]
    assert any(s['id'] == case['proof'] and s['role'] == 'validation' for s in sources)
    assert all(r['saved']['identity_source_artifact_id'] == case['proof'] for r in records)
    assert all(r['observed_at'] == case['now'] and r['line'] == r['actual'] + .5 for r in records)
    assert len(case['calls']) == 1


@pytest.mark.parametrize('problem', ['missing', 'future'])
def test_frozen_learning_rejects_missing_or_future_identity_proof(isolated, monkeypatch, problem):
    case = capture_case(monkeypatch)
    assert nba_ops._capture_props(case['now'])['new_locks'] == 1
    after = finalize(case)
    original = db.q1

    def altered_read(sql, params=()):
        row = original(sql, params)
        if row and problem == 'missing' and sql.startswith('SELECT * FROM nba_forecasts WHERE forecast_id='):
            row = dict(row)
            row['source_artifact_ids'] = json.dumps([s for s in json.loads(row['source_artifact_ids'])
                                                    if s != case['proof']])
        if row and problem == 'future' and sql.startswith('SELECT * FROM nba_source_artifacts WHERE id=') \
                and params == (case['proof'],):
            row = dict(row, received_at=case['now'] + 1)
        return row

    monkeypatch.setattr(db, 'q1', altered_read)
    with pytest.raises(ValueError, match='frozen identity source|received after its capture'):
        nba_learning_data.validation_records(after + 1, game_count=1)
