import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from edgeterminal import db, nba_quotes, nfl_prop_capture_ops, nfl_props
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "nba.db"}')
    db.init()
    nba_quotes.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def test_nba_requests_count_toward_nfl_shared_budget(isolated):
    db.ex('INSERT INTO nba_prop_requests(request_id,attempted_at,events_returned,quota_json) VALUES(?,?,?,?)',
          ('nba', 1000, 5, '{}'))
    assert nfl_prop_capture_ops.plan([], 1001, key_configured=True)['requests_30d'] == 1


def test_nba_quota_reserves_nfl_capacity(isolated):
    for index in range(nba_quotes.NBA_MONTH_REQUESTS):
        db.ex('INSERT INTO nba_prop_requests(request_id,attempted_at,events_returned,quota_json) VALUES(?,?,?,?)',
              (str(index), 1000, 1, '{}'))
    assert nba_quotes.budget(1001)['blocked_reason'] == 'nba_monthly_budget'


def test_free_plan_local_budget_allows_daily_nba_and_keeps_nfl_reserve(isolated, monkeypatch):
    attempt = {'requests_made': 142, 'events_returned': 594, 'target_game_ids': '[]',
               'attempted_at': 1000., 'error': None, 'quota_json': '[]'}
    monkeypatch.setattr(nfl_prop_capture_ops, '_attempts', lambda now: [attempt])
    budget = nba_quotes.budget(1001.)
    assert budget['blocked_reason'] is None
    assert budget['shared_remaining'] == budget['shared_month_limit'] - 142
    assert budget['nfl_reserved_requests'] == 40
    attempt['requests_made'] = budget['shared_month_limit'] - 40
    assert nba_quotes.budget(1001.)['blocked_reason'] == 'nfl_capacity_reserved'
    attempt['requests_made'] = 142
    attempt['events_returned'] = nfl_prop_capture_ops.MAX_LOCAL_OBJECTS_30D
    assert nba_quotes.budget(1001.)['blocked_reason'] == 'local_30_day_quota_guard'


def test_nba_respects_recent_nfl_requests_without_spending_a_request(isolated):
    game, roster, _ = sample()
    stamp = 1792530000.
    for page in range(10):
        db.ex('INSERT INTO nfl_prop_api_request_attempts VALUES(?,?,?,?)',
              ('recent-nfl', page+1, stamp-10, '{}'))
    result = nba_quotes.capture([game], roster, stamp, api_key='fixture',
                               getter=lambda *a, **kw: pytest.fail('minute budget exceeded'))
    assert result['status'] == 'local_per_minute_request_guard'
    assert db.q1('SELECT COUNT(*) n FROM nba_prop_requests')['n'] == 0
    assert nba_quotes.budget(stamp)['blocked_reason'] is None


def test_nfl_pagination_respects_shared_nba_minute_budget(isolated, monkeypatch):
    stamp = 1792530000.
    monkeypatch.setattr(nfl_props.time, 'time', lambda: stamp)
    for index in range(9):
        db.ex('INSERT INTO nba_prop_requests(request_id,attempted_at) VALUES(?,?)',
              ('recent-nba-'+str(index), stamp-10))
    calls = []
    def getter(*a, **kw):
        calls.append(1)
        return 200, b'{"success":true,"data":[],"nextCursor":"second-page"}', {}
    with pytest.raises(nfl_props.ProviderError, match='local per-minute request limit'):
        nfl_props.SportsGameOddsProps(api_key='fixture', getter=getter).fetch(
            stamp, stamp+3600, batch_id='minute-nfl')
    assert calls == [1]
    assert db.q1('SELECT COUNT(*) n FROM nfl_prop_api_request_attempts')['n'] == 1


def test_minute_budget_reopens_at_the_window_boundary(isolated):
    for page in range(10):
        db.ex('INSERT INTO nfl_prop_api_request_attempts VALUES(?,?,?,?)',
              ('recent-nfl', page+1, 1000., '{}'))
    with db.tx() as conn:
        assert not nfl_prop_capture_ops.request_slot_available(conn, 1059.)
    with db.tx() as conn:
        assert nfl_prop_capture_ops.request_slot_available(conn, 1060.)


def test_concurrent_nba_captures_share_the_last_minute_slot(isolated):
    game, roster, event = sample()
    stamp = 1792530000.
    for page in range(9):
        db.ex('INSERT INTO nfl_prop_api_request_attempts VALUES(?,?,?,?)',
              ('recent-nfl', page+1, stamp-10, '{}'))
    calls = []
    def getter(*a, **kw):
        calls.append(1)
        return 200, json.dumps({'success':True,'data':[event]}).encode(), {}
    def capture(_):
        return nba_quotes.capture([game], roster, stamp, getter=getter, api_key='fixture')['status']
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(capture, range(2)))
    assert sorted(outcomes) == ['captured','local_per_minute_request_guard']
    assert calls == [1]
    assert db.q1('SELECT COUNT(*) n FROM nba_prop_requests')['n'] == 1


def sample():
    game = {'id': 'nba-2026-123', 'home_team': 'BOS', 'away_team': 'NYK', 'tipoff': '2026-10-20T23:00Z'}
    roster = [{'player_id': '42', 'player': 'Example Player', 'team': 'BOS'}]
    event = {'eventID': 'source-event', 'leagueID': 'NBA', 'status': {'startsAt': game['tipoff']},
      'teams': {'home': {'teamID': 'BOS_NBA', 'names': {'short': 'BOS'}},
                'away': {'teamID': 'NYK_NBA', 'names': {'short': 'NYK'}}},
      'players': {'EXAMPLE_PLAYER': {'playerID': 'EXAMPLE_PLAYER', 'name': 'Example Player', 'teamID': 'BOS_NBA'}},
      'odds': {'points-EXAMPLE_PLAYER-game-ou-over': {'statID': 'points', 'statEntityID': 'EXAMPLE_PLAYER',
        'periodID': 'game', 'betTypeID': 'ou', 'sideID': 'over',
        'byBookmaker': {'draftkings': {'available': True, 'overUnder': '20.5', 'odds': '-110'}}}}}
    return game, roster, event


def test_quote_identity_requires_same_player_team_and_pretipoff():
    game, roster, event = sample()
    quotes, warnings = nba_quotes.normalize([event], [game], roster, 1792530000.)
    assert len(quotes) == 1 and quotes[0]['player_id'] == '42' and quotes[0]['identity_verified']
    assert quotes[0]['line'] == 20.5
    event['players']['EXAMPLE_PLAYER']['name'] = 'Different Player'
    assert nba_quotes.normalize([event], [game], roster, 1792530000.)[0] == []


def identity_artifact(event, **changes):
    raw = json.dumps({'success': True, 'data': [event]}).encode()
    return dict(id=7, kind='player-quotes', url=nba_quotes.URL+'?leagueID=NBA',
                received_at=1792529000., http_status=200, body=raw,
                sha256=hashlib.sha256(raw).hexdigest(), **changes)


def test_nameless_current_player_uses_archived_exact_id_and_keeps_fresh_quote():
    game, roster, event = sample()
    prior = copy.deepcopy(event)
    prior['players']['EXAMPLE_PLAYER']['teamID'] = 'OLD_TEAM_NBA'
    prior['odds']['points-EXAMPLE_PLAYER-game-ou-over']['byBookmaker']['draftkings']['overUnder'] = '10.5'
    catalog = nba_quotes.player_identity_catalog([identity_artifact(prior)], 1792530000.)
    del event['players']['EXAMPLE_PLAYER']['name']
    assert not nba_quotes.normalize([event], [game], roster, 1792530000.)[0]
    quotes, _ = nba_quotes.normalize([event], [game], roster, 1792530000., identity_catalog=catalog)
    assert len(quotes) == 1
    assert quotes[0]['player_id'] == '42'
    assert quotes[0]['identity_source_artifact_id'] == 7
    assert quotes[0]['provider_player_id'] == 'EXAMPLE_PLAYER'
    assert quotes[0]['line'] == 20.5 and quotes[0]['observed_at'] == 1792530000.


@pytest.mark.parametrize('problem', ['hash', 'future', 'host', 'league', 'conflict', 'wrong_id'])
def test_unverified_or_conflicting_archived_identity_cannot_supply_missing_name(problem):
    game, roster, event = sample()
    prior = copy.deepcopy(event)
    source = identity_artifact(prior)
    sources = [source]
    if problem == 'hash': source['sha256'] = '0'*64
    if problem == 'future': source['received_at'] = 1792530001.
    if problem == 'host': source['url'] = 'https://example.com/v2/events'
    if problem == 'league':
        prior['leagueID'] = 'NFL'; sources = [identity_artifact(prior)]
    if problem == 'wrong_id':
        prior['players']['EXAMPLE_PLAYER']['playerID'] = 'OTHER'; sources = [identity_artifact(prior)]
    if problem == 'conflict':
        prior['players']['EXAMPLE_PLAYER']['name'] = 'Another Person'
        other = identity_artifact(prior); other['id'] = 8; sources.append(other)
    catalog = nba_quotes.player_identity_catalog(sources, 1792530000.)
    del event['players']['EXAMPLE_PLAYER']['name']
    assert not nba_quotes.normalize([event], [game], roster, 1792530000., identity_catalog=catalog)[0]


def test_identity_fallback_never_overrides_current_names_teams_or_duplicate_rosters():
    game, roster, event = sample()
    catalog = nba_quotes.player_identity_catalog([identity_artifact(event)], 1792530000.)
    event['players']['EXAMPLE_PLAYER']['name'] = 'Different Person'
    assert not nba_quotes.normalize([event], [game], roster, 1792530000., identity_catalog=catalog)[0]
    del event['players']['EXAMPLE_PLAYER']['name']
    roster.append(dict(roster[0], player_id='43'))
    assert not nba_quotes.normalize([event], [game], roster, 1792530000., identity_catalog=catalog)[0]
    roster.pop()
    event['players']['EXAMPLE_PLAYER']['teamID'] = 'OTHER_NBA'
    assert not nba_quotes.normalize([event], [game], roster, 1792530000., identity_catalog=catalog)[0]


def test_quote_replay_uses_the_saved_identity_artifact():
    from edgeterminal.nba_learning_data import _verify_quote_body
    game, roster, event = sample()
    catalog = nba_quotes.player_identity_catalog([identity_artifact(event)], 1792530000.)
    del event['players']['EXAMPLE_PLAYER']['name']
    saved = nba_quotes.normalize([event], [game], roster, 1792530000., identity_catalog=catalog)[0][0]
    quote = dict(quote_id=1, observed_at=1792530000.)
    frozen_game = dict(game, game_id=game['id'], tipoff_utc=game['tipoff'])
    _verify_quote_body(quote, saved, frozen_game, {'data': [event]}, roster, catalog)
    with pytest.raises(ValueError, match='sourced main line'):
        _verify_quote_body(quote, saved, frozen_game, {'data': [event]}, roster, {})


def test_wrong_game_time_and_duplicate_roster_identity_fail_closed():
    game, roster, event = sample()
    event['status']['startsAt'] = '2026-10-21T23:00Z'
    assert not nba_quotes.normalize([event], [game], roster, 1792530000.)[0]
    event['status']['startsAt'] = game['tipoff']
    roster.append(dict(roster[0], player_id='43'))
    assert not nba_quotes.normalize([event], [game], roster, 1792530000.)[0]


def test_provider_cooldown_is_shared_and_no_auth_in_archived_url(isolated):
    game, roster, event = sample()
    calls = []
    def getter(url, **kwargs):
        calls.append((url, kwargs))
        return 429, b'{"success":false}', {'x-ratelimit-remaining': '0'}
    result = nba_quotes.capture([game], roster, 1792530000., getter=getter, api_key='secret')
    assert result['status'] == 'source_error'
    assert nba_quotes.budget(1792530010.)['blocked_reason'] == 'provider_quota_cooldown'
    assert nfl_prop_capture_ops.plan([],1792530010.,key_configured=True)['blocked_reason'] == 'provider_quota_cooldown'
    assert 'secret' not in db.q1('SELECT request_params FROM nba_prop_requests')['request_params']
    assert 'secret' not in calls[0][0]
