"""Prospective top-five record invariants, with no external requests."""
import hashlib
import json
import sqlite3

import pytest

from edgeterminal import db, nfl_top_five as top
from edgeterminal.config import settings

GAME = 'nfl-2026-reg-05-tb-dal'


def quotes():
    rows = []
    for i, probability in enumerate((.81, .75, .74, .72, .68, .61)):
        for side in ('over', 'under'):
            rows.append(dict(quote_id=i * 2 + (side == 'under'), canonical_game_id=GAME,
                             player_id=f'player-{i}', player_name=f'Player {i}',
                             market='receptions', line=3.5, sportsbook='draftkings',
                             side=side, probability_over=probability,
                             probability_under=1-probability, observed_at=10., kickoff_utc=100.))
    return rows


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "top-five.db"}')
    db.init()
    db.upsert_event(dict(id=GAME, sport='NFL', home_team='Dallas Cowboys',
                        away_team='Tampa Bay Buccaneers', name='TB at DAL',
                        start_time='1970-01-01T00:01:40Z', status='STATUS_SCHEDULED'))
    payload = json.dumps(dict(projections=quotes()), sort_keys=True)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
          ('batch', 'sportsgameodds', 10., 'source', '{}', 1, 1, 12, '{}'))
    db.ex('INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)', (sha, 'batch', 20., payload))
    yield sha
    db._conn.close()
    db._conn = None


def test_rank_selects_strongest_side_and_deduplicates_across_books():
    rows = quotes()
    rows += [{**rows[0], 'quote_id': 50, 'sportsbook': 'fanduel'}]
    ranked = top.rank(rows)
    assert len(ranked) == 5
    assert [r['player_id'] for r in ranked] == [f'player-{i}' for i in range(5)]
    assert all(r['side'] == 'over' for r in ranked)
    rows[0]['probability_over'], rows[0]['probability_under'] = .1, .9
    rows[1]['probability_over'], rows[1]['probability_under'] = .1, .9
    assert top.rank(rows)[0]['side'] == 'under'


def test_lock_is_pre_kickoff_immutable_and_idempotent(isolated):
    assert top.lock(isolated, 50.) == {'games_selected': 1}
    before = db.q1('SELECT * FROM nfl_top_five_selections')
    assert top.lock(isolated, 60.) == {}
    assert db.q1('SELECT * FROM nfl_top_five_selections') == before
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        db.ex('UPDATE nfl_top_five_selections SET selected_at=70')
    with pytest.raises(sqlite3.IntegrityError):
        db.ex('DELETE FROM nfl_top_five_selections')


@pytest.mark.parametrize('stamp,start,status', [
    (100., '1970-01-01T00:01:40Z', 'STATUS_SCHEDULED'),
    (50., '1970-01-01T00:00:40Z', 'STATUS_SCHEDULED'),
    (50., '1970-01-01T00:01:40Z', 'STATUS_FINAL')])
def test_lock_cannot_backfill_or_ignore_earlier_kickoff(isolated, stamp, start, status):
    db.ex('UPDATE events SET start_time=?,status=? WHERE id=?', (start, status, GAME))
    assert top.lock(isolated, stamp) == {}
    assert top.view()['selection_count'] == 0


def test_lock_cannot_predate_freeze(isolated):
    with pytest.raises(ValueError, match='predates'):
        top.lock(isolated, 15.)


def test_record_counts_only_selected_sides_using_latest_grades(isolated, monkeypatch):
    top.lock(isolated, 50.)
    monkeypatch.setattr(top.nfl_prop_grading, '_latest', lambda sha: [
        {'quote_id': 0, 'research_statistical_result': 'won', 'actual_stat': 6, 'revision': 2},
        {'quote_id': 2, 'research_statistical_result': 'lost', 'actual_stat': 2, 'revision': 1},
        {'quote_id': 4, 'research_statistical_result': 'push'},
        {'quote_id': 6, 'research_statistical_result': 'void'},
        {'quote_id': 1, 'research_statistical_result': 'lost'}])
    result = top.view()
    assert result['selection_count'] == 5
    assert result['record'] == dict(won=1, lost=1, push=1, void=1, pending=1)
    assert result['win_rate'] == .5
    assert result['games'][0]['rows'][0]['revision'] == 2
    assert result['readiness'] == 'RESEARCH_ONLY'


def test_grading_includes_older_freezes_when_new_week_is_current(isolated, monkeypatch):
    # No top-five selection is needed for the legacy immutable research record.
    calls = []
    monkeypatch.setattr(top.nfl_prop_result_ingest, 'sync',
                        lambda sha, *, now: calls.append(sha) or {'status': 'graded'})
    result = top.grade_due(100. + top.nfl_prop_grading.POSTGAME_DELAY_S)
    assert calls == [isolated]
    assert result['freezes_checked'] == 1


def test_targeted_feed_requests_real_receptions_id_and_bounded_books():
    feed = top.provider()
    assert 'receiving_receptions-PLAYER_ID-game-ou-over' in feed.odd_ids.split(',')
    assert 'receptions-PLAYER_ID-game-ou-over' not in feed.odd_ids.split(',')
    assert len(feed.odd_ids.split(',')) == 10
    assert feed.bookmaker_ids == 'draftkings,fanduel,caesars,espnbet,bovada'


def attempt(success=1, error=None):
    db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
          ('latest-batch', 'sportsgameodds', 30., 'source', '{}', 1, 1, 4, '{}'))
    db.ex('INSERT INTO nfl_prop_capture_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          ('attempt', 'latest-batch', 2026, 5, json.dumps([GAME]), 30., 31., success,
           1, 1, 1, 4, '[]', '[]', None, error))


def test_coverage_explains_partial_lines_without_fetch_or_mutation(isolated, monkeypatch):
    monkeypatch.setattr(settings, 'sportsgameodds_api_key', 'fixture-key')
    attempt()
    monkeypatch.setattr(top.nfl_prop_lab, '_pair_quotes', lambda bid: (quotes()[:4], {}))
    before = db.q('SELECT * FROM nfl_top_five_selections')
    item = top.coverage([GAME], 50.)[0]
    assert item['status'] == 'partial_lines' and item['main_line_markets'] == 2
    assert item['last_checked_at'] == 30. and item['next_capture_at'] == 330.
    assert db.q('SELECT * FROM nfl_top_five_selections') == before
    top.lock(isolated, 50.)
    assert top.coverage([GAME], 60.)[0]['status'] == 'locked'


def test_quota_and_post_kickoff_coverage_have_no_retry_promise(isolated, monkeypatch):
    monkeypatch.setattr(settings, 'sportsgameodds_api_key', 'fixture-key')
    attempt(0, 'HTTP 429')
    monkeypatch.setattr(top.nfl_prop_lab, '_pair_quotes', lambda bid: ([], {}))
    item = top.coverage([GAME], 50.)[0]
    assert item['status'] == 'source_paused' and item['next_capture_at'] is None
    assert '429' not in item['message']
    assert top.coverage([GAME], 100.)[0]['status'] == 'not_selected'


def test_next_capture_keeps_six_hour_early_week_cadence():
    event = {'start_time':'1970-01-03T00:00:00Z', 'status':'STATUS_SCHEDULED'}
    assert top.next_capture(event, {'success':1, 'attempted_at':1000.}, 1100.) == 22600.
    assert top.next_capture(event, None, 172800.) is None
