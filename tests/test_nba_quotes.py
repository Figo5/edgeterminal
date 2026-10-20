import json

import pytest

from edgeterminal import db, nba_quotes, nfl_prop_capture_ops
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
