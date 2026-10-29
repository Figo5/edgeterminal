import json
from datetime import datetime, timezone

import pytest

from edgeterminal import db, nba_store
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "context.db"}')
    db.init()
    nba_store.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def _event(ident, when, state='post'):
    return {'id': ident, 'date': when, 'seasonType': {'type': 2},
            'competitions': [{'status': {'type': {'state': state, 'completed': state == 'post',
             'name': 'STATUS_FINAL' if state == 'post' else 'STATUS_SCHEDULED'}},
             'competitors': [{'homeAway': 'home', 'team': {'id': '2', 'abbreviation': 'BOS'}},
                             {'homeAway': 'away', 'team': {'id': '18', 'abbreviation': 'NY'}}]}]}


def test_schedule_rest_includes_intervening_scheduled_game():
    from edgeterminal import nba_game_context as context
    parsed = context.parse_schedule('BOS', {'events': [
        _event('401900001', '2026-10-27T23:00:00Z'),
        _event('401900002', '2026-10-29T23:00:00Z', 'pre'),
        _event('401900003', '2026-10-30T23:00:00Z', 'pre'),
    ]}, 7, 1793260800.)
    tip = datetime(2026, 10, 30, 23, tzinfo=timezone.utc).timestamp()
    derived = context.team_context(parsed, 'BOS', tip, [], tip-2*86400)
    assert derived['last_game_id'] == '401900002'
    assert derived['last_game_tipoff'] == tip-86400
    assert derived['source_artifact_id'] == 7


def test_schedule_identity_conflict_is_rejected():
    from edgeterminal import nba_game_context as context
    event = _event('401900001', '2026-10-27T23:00:00Z')
    event['competitions'][0]['competitors'][0]['team']['id'] = '25'
    with pytest.raises(ValueError, match='identity|conflict'):
        context.parse_schedule('BOS', {'events': [event]}, 7, 1000.)


def test_missing_completed_game_and_stale_receipt_are_visible():
    from edgeterminal import nba_game_context as context
    tip = datetime(2026, 10, 30, 23, tzinfo=timezone.utc).timestamp()
    parsed = context.parse_schedule('BOS', {'events': [
        _event('401900001', '2026-10-29T23:00:00Z')
    ]}, 7, tip-2*86400)
    derived = context.team_context(parsed, 'BOS', tip, [], tip-1800)
    assert derived['complete'] is False
    assert derived['missing_game_ids'] == ['401900001']
    assert derived['stale'] is True


def test_source_refresh_accelerates_toward_tipoff(isolated):
    from edgeterminal import nba_game_context as context
    # No games remains a cheap cached read.
    assert context.source_ttl('BOS', 1000., 'history') == 6*3600
    assert context.source_ttl('BOS', 1000., 'roster') == 3*3600
    original = db.q1
    try:
        db.q1 = lambda *a, **kw: {'tipoff_ts': 1000.+3*3600}
        assert context.source_ttl('BOS', 1000., 'history') == 900
        db.q1 = lambda *a, **kw: {'tipoff_ts': 1000.+1800}
        assert context.source_ttl('BOS', 1000., 'roster') == 300
    finally:
        db.q1 = original


def test_espn_provider_name_fallback_preserves_book():
    from edgeterminal import nba_ops
    market = nba_ops.parse_market({'odds': [{'provider': {'name': 'DraftKings'},
        'pointSpread': {'home': {'close': {'line': '-3.5', 'odds': '-110'}},
                        'away': {'close': {'line': '3.5', 'odds': '-110'}}}}]}, 1000., 'sha')
    assert market['sportsbook'] == 'DraftKings'


def test_availability_is_reparsed_from_archived_roster_not_mutable_cache(isolated):
    from edgeterminal import nba_game_context as context
    from test_nba_data import ROSTER
    import hashlib
    now = 1793260800.
    raw = json.dumps(ROSTER).encode()
    source = nba_store.save_artifact({'kind':'roster','body':raw,'sha256':hashlib.sha256(raw).hexdigest(),
        'received_at':now-10,'http_status':200,'url':'https://site.api.espn.com/roster'})
    db.kv_set('nba_roster_OKC', [{'player_id':'forged','player':'Forged','team':'OKC',
        'availability':'out','source_artifact_id':source,'observed_at':now-10}])
    rows = context.availability({'home_team':'OKC','away_team':'MIN','season_label':'2026-27'}, now)
    assert 'forged' not in {r['player_id'] for r in rows}
    assert {r['player_id'] for r in rows} == {'2991350','4684208'}
