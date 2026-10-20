import hashlib
import json

import pytest

from edgeterminal import db, nba_sources
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "nba.db"}')
    db.init()
    yield
    db._conn.close()
    db._conn = None


def test_cached_sources_keep_original_bytes_and_do_not_refetch(isolated):
    calls = []
    raw = b'{ "events": [] }'
    def getter(url, **kwargs):
        calls.append(url)
        return 200, raw, {}
    first = nba_sources.fetch('scoreboard', {'dates': '20261020'}, ttl=60, now=1000, getter=getter)
    second = nba_sources.fetch('scoreboard', {'dates': '20261020'}, ttl=60, now=1001, getter=getter)
    assert len(calls) == 1
    assert first['sha256'] == second['sha256'] == hashlib.sha256(raw).hexdigest()
    assert first['observed_at'] == second['observed_at'] == 1000
    assert nba_sources.body(first['sha256']) == raw


def test_failed_source_is_not_replaced_with_fabricated_empty_data(isolated):
    with pytest.raises(ValueError, match='HTTP 403'):
        nba_sources.fetch('scoreboard', now=1000, getter=lambda *a, **kw: (403, b'blocked', {}))
    assert db.q1('SELECT COUNT(*) n FROM nba_http_artifacts')['n'] == 1


def test_source_routes_cannot_be_arbitrary_urls(isolated):
    with pytest.raises(ValueError):
        nba_sources.fetch('https://attacker.example/secret', getter=lambda *a, **kw: pytest.fail())


def test_malformed_200_body_does_not_poison_a_week_long_cache(isolated):
    with pytest.raises(ValueError):
        nba_sources.fetch('scoreboard',ttl=7*86400,now=1000,getter=lambda *a,**kw:(200,b'not json',{}))
    recovered=nba_sources.fetch('scoreboard',ttl=7*86400,now=1100,
        getter=lambda *a,**kw:(200,b'{"events":[]}',{}))
    assert recovered['data']=={'events':[]} and not recovered['cached']


def test_team_history_uses_requested_season_and_explicit_final_scores():
    event = {'id': '123', 'date': '2026-04-10T23:00Z', 'season': {'year': 2026},
             'seasonType': {'type': 2}, 'competitions': [{'status': {'type': {'completed': True}},
              'competitors': [{'homeAway': 'home', 'team': {'abbreviation': 'BOS'}, 'score': {'value': 100}},
                              {'homeAway': 'away', 'team': {'abbreviation': 'NY'}, 'score': {'value': 90}}]}]}
    payload = {'season': {'year': 2027}, 'requestedSeason': {'year': 2026}, 'events': [event]}
    rows = nba_sources.team_history(payload, 'source-hash')
    assert len(rows) == 2
    assert rows[0]['points'] == 100 and rows[0]['opponent_points'] == 90
    assert rows[1]['team'] == 'NYK'
    event['competitions'][0]['status']['type']['completed'] = False
    assert nba_sources.team_history(payload, 'hash') == []


def test_player_history_excludes_postseason_and_missing_or_bad_stats():
    payload = {'names': ['minutes', 'points', 'totalRebounds', 'assists', 'threePointFieldGoalsMade-threePointFieldGoalsAttempted'],
               'events': {'1': {'gameDate': '2026-04-10T23:00Z', 'leagueAbbreviation': 'NBA', 'team': {'abbreviation': 'BOS'}}},
               'seasonTypes': [{'displayName': '2025-26 Regular Season', 'categories': [
                   {'type': 'event', 'events': [{'eventId': '1', 'stats': ['20:30', '0', '2', '3', '0-2']}]}]},
                   {'displayName': '2025-26 Postseason', 'categories': [{'type': 'event', 'events': [
                       {'eventId': '1', 'stats': ['30', '50', '2', '3', '2-4']}]}]}]}
    rows = nba_sources.player_history(payload, '42', 'source')
    assert len(rows) == 1 and rows[0]['points'] == 0 and rows[0]['minutes'] == 20.5
    assert rows[0]['participation'] == 'played'
    payload['seasonTypes'][0]['categories'][0]['events'][0]['stats'][1] = '--'
    assert nba_sources.player_history(payload, '42', 'source') == []
