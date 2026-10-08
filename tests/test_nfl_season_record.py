"""Historical records preserve forecasts and cannot manufacture selections."""
import hashlib
import json

import pytest

from edgeterminal import db, nfl_season_record as record, nfl_top_five
from edgeterminal.config import settings

GAME = 'nfl-2026-reg-03-atl-gb'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "season.db"}')
    monkeypatch.setattr(record, "ROOT", tmp_path)
    db.init()
    db.ex("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
          ("batch", "sportsgameodds", 10., "source", "{}", 1, 1, 12, "{}"))
    db.upsert_event(dict(id=GAME, sport='NFL', home_team='Green Bay Packers',
                        away_team='Atlanta Falcons', name='ATL at GB',
                        start_time='1970-01-01T00:01:40Z', status='STATUS_FINAL'))
    yield
    db._conn.close()
    db._conn = None


def freeze(stamp=20., count=6):
    rows = [dict(quote_id=i*2+(side == 'under'), canonical_game_id=GAME,
                 player_id=f'p{i}', player_name=f'Player {i}', market='receptions',
                 side=side, sportsbook='draftkings', line=3.5,
                 probability_over=.8-i*.04, probability_under=.2+i*.04,
                 observed_at=10., kickoff_utc=100.)
            for i in range(count) for side in ('over', 'under')]
    payload = json.dumps(dict(projections=rows), sort_keys=True)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    batch = f'batch-{stamp}'
    db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
          (batch, 'sportsgameodds', 10., 'source', '{}', 1, 1, 12, '{}'))
    db.ex('INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)', (sha, batch, stamp, payload))
    return sha


def test_archive_counts_five_unique_forecasts_without_writing_ledger(isolated, monkeypatch):
    sha = freeze()
    monkeypatch.setattr(record.nfl_prop_grading, '_latest', lambda _: [
        dict(quote_id=0, research_statistical_result='won', actual_stat=5),
        dict(quote_id=2, research_statistical_result='lost', actual_stat=2),
        dict(quote_id=1, research_statistical_result='lost')])
    before = db.q('SELECT * FROM nfl_prop_research_freezes')
    result = record.props(now=101.)
    assert result['selection_count'] == 5
    assert result['record'] == dict(won=1, lost=1, pending=3)
    assert result['games'][0]['basis'] == 'archived_ranking'
    assert result['games'][0]['selected_at'] is None
    assert db.q('SELECT * FROM nfl_top_five_selections') == []
    assert db.q('SELECT * FROM nfl_prop_research_freezes') == before
    assert result['games'][0]['freeze_sha'] == sha
    assert result['locked']['count'] == 0
    assert result['locked']['diagnostics']['graded_count'] == 0
    assert result['archived']['record'] == dict(won=1, lost=1, pending=3)
    assert result['archived']['weekly'][3]['count'] == 5


def test_incomplete_archive_stays_incomplete_and_future_is_not_backfilled(isolated):
    freeze(count=2)
    assert record.props(now=90.)['selection_count'] == 0
    assert record.props(now=101.)['selection_count'] == 2


def test_postkickoff_freeze_is_excluded(isolated):
    freeze(stamp=101.)
    assert record.props(now=200.)['selection_count'] == 0


def test_locked_selection_takes_precedence_over_archive(isolated):
    sha = freeze()
    db.ex("UPDATE events SET status='STATUS_SCHEDULED'")
    nfl_top_five.lock(sha, 50.)
    result = record.props(now=200.)
    assert result['selection_count'] == 5
    assert result['historical_count'] == 0
    assert result['games'][0]['basis'] == 'locked'
    assert result['locked']['count'] == 5
    assert result['locked']['weekly'][3]['record'] == dict(pending=5)
    assert result['archived']['count'] == 0


def prediction(monkeypatch, stamp, probability=.8, margin=5., total=45.):
    monkeypatch.setattr(db, 'now', lambda: stamp)
    projection = dict(proj_home=25., proj_away=20., home_win_prob=probability, margin=margin, total=total)
    payload = dict(game=dict(id=GAME, home_team='Green Bay Packers', away_team='Atlanta Falcons',
                            kickoff_utc='1970-01-01T00:01:40Z'), projection=projection,
                   market=[dict(event_id=GAME, market='spread', selection='Green Bay Packers',
                                line=-3.5, captured_at=10., is_demo=False),
                           dict(event_id=GAME, market='total', selection='Over', line=44.5,
                                captured_at=10., is_demo=False)])
    db.save_nfl_snapshot(str(stamp), GAME, payload, 'board-model', 'f1')
    db.save_nfl_game_prediction('p'+str(stamp), GAME, str(stamp), projection, 'board-model')


def test_game_record_uses_latest_pregame_and_its_lines_not_other_model_results(isolated, monkeypatch):
    prediction(monkeypatch, 20., probability=.2)
    prediction(monkeypatch, 30.)
    prediction(monkeypatch, 110., probability=.1)
    monkeypatch.setattr(record, '_final_scores', lambda _: {GAME: (24, 21)})
    result = record.games()
    assert len(result['rows']) == 1
    item = result['rows'][0]
    assert item['saved_at'] == 30.
    assert item['results'] == dict(winner='won', spread='lost', total='won')
    assert item['spread_line'] == -3.5
    assert result['winner']['record'] == dict(won=1)


def test_tie_and_missing_lines_are_not_losses(isolated, monkeypatch):
    prediction(monkeypatch, 20.)
    monkeypatch.setattr(record, '_final_scores', lambda _: {GAME: (21, 21)})
    assert record.games()['rows'][0]['results']['winner'] == 'push'
    prediction(monkeypatch, 30., margin=3.5, total=44.5)
    assert record.games()['rows'][0]['results'] == dict(winner='push', spread='pass', total='pass')


def test_empty_weeks_have_no_record(isolated, monkeypatch):
    monkeypatch.setattr(record, 'props', lambda _: {'weekly': {3: record.summary([{'result': 'won'}])}})
    monkeypatch.setattr(record, 'games', lambda _: {'weekly': {}})
    monkeypatch.setattr(db, 'kv_get', lambda _: {'week': 5})
    result = record.view()
    assert len(result['weekly']) == 5
    assert result['weekly'][0]['props']['count'] == 0
    assert result['weekly'][0]['props']['win_rate'] is None
    assert result['weekly'][2]['props']['record'] == dict(won=1)


def test_final_grade_identity_mismatch_is_rejected(monkeypatch):
    body = dict(freeze_sha256='freeze', game_id='2026_03_ATL_GB',
                final_home_score=24, final_away_score=21, home_team='Green Bay Packers',
                away_team='Atlanta Falcons')
    row = dict(freeze_sha='freeze', game_id='2026_03_ATL_GB', home_score=24,
               away_score=21, payload=json.dumps(body))
    monkeypatch.setattr(db, 'q', lambda *a: [row])
    assert record._final_scores(2026) == {GAME: (24, 21)}
    row['game_id'] = '2026_03_KC_GB'
    row['payload'] = json.dumps({**body, 'game_id': row['game_id']})
    with pytest.raises(ValueError, match='identity'):
        record._final_scores(2026)


def test_earliest_archive_is_used_without_cherry_picking_later_forecasts(isolated):
    first = freeze(count=2)
    freeze(stamp=50., count=6)
    result = record.props(now=200.)
    assert result['selection_count'] == 2
    assert result['games'][0]['freeze_sha'] == first


def test_legacy_markets_match_exact_schedule_without_recreating_forecasts(tmp_path, monkeypatch):
    monkeypatch.setattr(record, 'ROOT', tmp_path)
    event = dict(id='espn-1', home_team='Green Bay Packers', away_team='Atlanta Falcons',
                 start_time='1970-01-01T00:01:40Z', status='STATUS_FINAL',
                 meta=dict(season=2026, season_type=2, week=3, source='ESPN scoreboard',
                           home_score='24', away_score='21'))
    class Source:
        def list_events_for_week(self, season, season_type, week):
            assert (season, season_type, week) == (2026, 2, 3)
            return [event]
    record.capture_legacy_results(weeks=(3,), provider=Source())
    base = dict(event_id='old-id', home_team=event['home_team'], away_team=event['away_team'],
                start_time=event['start_time'], meta='{}', created_at=30., model_version='old-model',
                data_snapshot=json.dumps(dict(selected_quote_at=10.)))
    saved = [{**base, 'market': 'moneyline', 'selection': event['away_team'], 'model_prob': .2, 'line': None},
             {**base, 'market': 'spread', 'selection': event['away_team'], 'model_prob': .8, 'line': 3.5},
             {**base, 'market': 'total', 'selection': 'Under', 'model_prob': .6, 'line': 44.5}]
    # A late prediction must not replace the saved pregame market forecasts.
    saved.insert(0, {**saved[0], 'created_at': 110., 'model_prob': .9})
    monkeypatch.setattr(db, 'q', lambda *a: saved)
    rows = record._legacy_games(2026, set())
    assert len(rows) == 1
    assert rows[0]['results'] == dict(winner='won', spread='won', total='lost')
    assert rows[0]['winner_team'] == event['home_team']
    assert rows[0]['basis'] == 'legacy_market'
    assert rows[0]['projected_home_points'] is None
    assert record._legacy_games(2026, {GAME}) == []
    event['start_time'] = '1970-01-01T00:01:41Z'
    saved[1]['start_time'] = event['start_time']
    assert record._legacy_games(2026, set())[0]['results']['winner'] == 'unavailable'
    path = next((tmp_path / 'data/research/season_results').glob('*.json'))
    path.write_text('{}')
    with pytest.raises(ValueError, match='hash mismatch'):
        record._legacy_games(2026, set())
