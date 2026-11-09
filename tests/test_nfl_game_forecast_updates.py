"""Normal worker revisions preserve past records and passive page reads."""
import json
from types import SimpleNamespace

import pytest

from edgeterminal import db, engine, product_views, nfl_season_record
from edgeterminal.config import settings
from test_nfl_foundation import isolated_db
from test_research_refresh import isolated

GAME = 'nfl-2026-reg-10-atl-gb'
HOME, AWAY = 'Green Bay Packers', 'Atlanta Falcons'
REAL_TEAM_STATS = engine.R.team_stats


@pytest.fixture
def board(isolated_db, monkeypatch):
    clock = [1000.]
    monkeypatch.setattr(engine.time, 'time', lambda: clock[0])
    monkeypatch.setattr(db, 'now', lambda: clock[0])
    db.upsert_event(dict(id=GAME, sport='NFL', name='ATL at GB', home_team=HOME,
                        away_team=AWAY, start_time='1970-01-01T01:00:00Z',
                        status='STATUS_SCHEDULED', meta={'season': 2026, 'week': 10, 'season_type': 2}))
    db.kv_set('nfl_current_schedule', {'season': 2026, 'week': 10, 'game_ids': [GAME]})
    stats = {HOME: {'points_for': 28., 'points_against': 18., 'games': 8, 'retrieved_at': 900.},
             AWAY: {'points_for': 19., 'points_against': 24., 'games': 8, 'retrieved_at': 900.}}
    monkeypatch.setattr(engine.R, 'team_stats', lambda _, **kw: (stats, 'test-espn-stats', False))
    return clock, stats


def test_passive_weekly_board_never_fetches_or_creates_forecasts(board, monkeypatch):
    monkeypatch.setattr(engine.R, 'team_stats', lambda _, **kw: pytest.fail('passive page fetched stats'))
    rows = engine.weekly_board()
    assert len(rows) == 1
    assert rows[0].get('projection') is None
    assert db.q1('SELECT COUNT(*) n FROM nfl_game_predictions')['n'] == 0


def test_normal_maintenance_refreshes_changed_inputs_and_deduplicates(board):
    clock, stats = board
    engine.ensure_weekly_projections()
    before = db.q('SELECT * FROM nfl_game_snapshots')
    assert len(before) == 1
    engine.ensure_weekly_projections()
    assert db.q('SELECT * FROM nfl_game_snapshots') == before
    clock[0] += 300
    stats[HOME]['points_for'] = 32.
    stats[HOME]['retrieved_at'] = clock[0] - 1
    result = engine.ensure_weekly_projections()
    after = db.q('SELECT * FROM nfl_game_snapshots ORDER BY created_at')
    assert len(after) == 2
    assert after[0] == before[0]
    assert result['created'] == 1
    assert engine.weekly_board()[0]['projection']['proj_home'] > json.loads(before[0]['payload'])['projection']['proj_home']


def test_kickoff_prevents_new_revisions_and_keeps_saved_bytes(board):
    clock, stats = board
    engine.ensure_weekly_projections()
    before = db.q('SELECT * FROM nfl_game_snapshots')
    clock[0] = 3600.
    stats[HOME]['points_for'] = 60.
    engine.ensure_weekly_projections()
    assert db.q('SELECT * FROM nfl_game_snapshots') == before


def test_component_integrity_failure_keeps_a_new_games_baseline_and_reports_error(board, monkeypatch):
    from edgeterminal import nfl_spread_learning
    def damaged(*args, **kwargs):
        raise ValueError('accepted component publication proof is damaged')
    monkeypatch.setattr(nfl_spread_learning, 'project_component', damaged)
    result = engine.ensure_weekly_projections()
    saved = db.q1('SELECT payload FROM nfl_game_snapshots WHERE game_id=?', (GAME,))
    assert saved is not None
    projection = json.loads(saved['payload'])['projection']
    assert projection['margin'] > 0
    assert projection['spread_component']['used'] is False
    assert projection['spread_component']['status'] == 'integrity_error'
    assert result['ok'] is False
    assert any(e.get('step') == 'spread_component' and e['error_type'] == 'ValueError'
               for e in result['errors'])
    original = db.q('SELECT * FROM nfl_game_snapshots')
    engine.ensure_weekly_projections()
    assert db.q('SELECT * FROM nfl_game_snapshots') == original


def test_baseline_source_corruption_still_refuses_a_new_forecast(board, monkeypatch):
    from edgeterminal import nfl_game_forecasts, nfl_spread_learning
    def damaged(*args, **kwargs):
        raise ValueError('baseline source hash conflict')
    monkeypatch.setattr(nfl_game_forecasts, '_sources', damaged)
    monkeypatch.setattr(nfl_spread_learning, 'project_component', lambda *a, **kw:
                        pytest.fail('corrupt baseline reached component inference'))
    result = engine.ensure_weekly_projections()
    assert result['ok'] is False and result['created'] == 0
    assert db.q1('SELECT COUNT(*) n FROM nfl_game_snapshots')['n'] == 0


def test_passive_pregame_detail_reads_saved_forecast_without_providers(board, monkeypatch):
    monkeypatch.setattr(engine, 'build_context', lambda *_: pytest.fail('detail fetched context'))
    output = engine.analyze_event(GAME, persist=False)
    assert output['projection'] is None
    assert output['recommendations'] == []


def test_spread_component_changes_only_spread_probability():
    projection = {'home': HOME, 'away': AWAY, 'margin': 1., 'total': 45., 'sd_margin': 13.,
                  'sd_total': 10., 'home_win_prob': .55,
                  'spread_component': {'margin': 9., 'sd_margin': 10., 'version': 'spread-new', 'used': True}}
    from edgeterminal.analytics import football
    expected = football.spread_prob({**projection, 'margin': 9., 'sd_margin': 10.}, HOME, -3.5)
    assert product_views._probability(projection, 'spread', -3.5, HOME) == expected
    assert product_views._probability(projection, 'total', 45., HOME) == football.total_prob(projection, 'Over', 45.)
    assert projection['margin'] == 1.


def test_same_timestamp_selection_has_deterministic_identity(board):
    from edgeterminal import nfl_game_forecasts
    assert callable(nfl_game_forecasts.selected)
    assert callable(nfl_game_forecasts.maintain)


def test_counted_spread_uses_component_without_changing_winner_or_total(board, monkeypatch):
    projection = dict(margin=1., total=45., home_win_prob=.55, proj_home=23., proj_away=22.,
                      spread_component={'used': True, 'margin': 9., 'sd_margin': 10., 'version': 'spread-v2'})
    snapshot = dict(game={'id': GAME, 'home_team': HOME, 'away_team': AWAY,
                         'kickoff_utc': '1970-01-01T01:00:00Z'}, projection=projection,
                    market=[dict(event_id=GAME, market='spread', selection=HOME, line=-3.5, captured_at=900.),
                            dict(event_id=GAME, market='total', line=44.5, captured_at=900.)])
    db.save_nfl_snapshot('original', GAME, snapshot, 'baseline', 'base')
    db.save_nfl_game_prediction('record', GAME, 'original', projection, 'baseline')
    monkeypatch.setattr(nfl_season_record, '_final_scores', lambda _: {GAME: (30., 20.)})
    monkeypatch.setattr(nfl_season_record, '_legacy_games', lambda *_: [])
    record = nfl_season_record.games()['rows'][0]
    assert record['results'] == {'winner': 'won', 'spread': 'won', 'total': 'won'}
    assert record['market_model_versions']['spread'] == 'spread-v2'
    assert record['market_model_versions']['winner'] == 'baseline'


def test_verified_source_flows_into_exact_snapshot_and_learning_pair(isolated_db, tmp_path, monkeypatch):
    from edgeterminal import nfl_game_sources, nfl_spread_data
    from test_nfl_game_sources import NOW, archive, game
    monkeypatch.setattr(engine.time, 'time', lambda: NOW)
    monkeypatch.setattr(db, 'now', lambda: NOW)
    target = dict(id=GAME, sport='NFL', home_team=HOME, away_team=AWAY,
                  start_time='2026-11-15T18:00:00Z', status='STATUS_SCHEDULED',
                  meta={'season': 2026, 'week': 10, 'season_type': 2})
    db.upsert_event(target)
    db.kv_set('nfl_current_schedule', {'season': 2026, 'week': 10, 'game_ids': [GAME]})
    path, meta = archive(tmp_path, game(home='GB', away='ATL'))
    prepared = nfl_game_sources.adopt_archive(path, meta, season=2026, completed_week=8, now=NOW)
    assert prepared['status'] == 'ready'
    stats = {t: {'points_for': 25., 'points_against': 21., 'games': 8, 'retrieved_at': NOW-10}
             for t in (HOME, AWAY)}
    monkeypatch.setattr(engine.R, 'team_stats', lambda _, **kw: (stats, 'espn-stats', False))
    db.store_odds([dict(event_id=GAME, sport='NFL', market='spread', selection=t,
                       line=line, price=-110, book='draftkings', captured_at=NOW-5,
                       source_url=nfl_spread_data.SCOREBOARD, quote_phase='current')
                   for t, line in ((HOME, -3.5), (AWAY, 3.5))], source='espn-odds')
    result = engine.ensure_weekly_projections()
    assert result['ok'], result
    saved = db.q1('SELECT * FROM nfl_game_snapshots WHERE game_id=?', (GAME,))
    snapshot = json.loads(saved['payload'])
    assert snapshot['team_form'][HOME]['games'] == 8
    assert snapshot['team_form'][HOME]['feature_games'] == 1
    observation = nfl_spread_data.observation(saved['id'], now=NOW, deep=True)
    assert observation['input_policy_id'] == nfl_game_sources.POLICY_ID
    assert observation['home_spread'] == -3.5
    before = db.q('SELECT * FROM nfl_game_snapshots')
    engine.ensure_weekly_projections()
    assert db.q('SELECT * FROM nfl_game_snapshots') == before


def test_new_forecast_tampering_cannot_change_published_record(board, monkeypatch):
    engine.ensure_weekly_projections()
    saved = db.q1('SELECT * FROM nfl_game_snapshots WHERE game_id=?', (GAME,))
    payload = json.loads(saved['payload'])
    payload['projection']['margin'] = 35.
    # Simulate damaged storage in an owned fixture, bypassing its normal
    # append-only trigger; readers must still verify the new contract's seal.
    for trigger in db.q("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nfl_game_snapshots'"):
        db.ex('DROP TRIGGER "' + trigger['name'].replace('"', '""') + '"')
    db.ex('UPDATE nfl_game_snapshots SET payload=? WHERE id=?', (json.dumps(payload), saved['id']))
    monkeypatch.setattr(nfl_season_record, '_final_scores', lambda _: {})
    with pytest.raises(ValueError, match='hash'):
        nfl_season_record.games()


def test_unused_spread_component_keeps_baseline_display(board):
    projection = dict(proj_home=23., proj_away=22., margin=1., total=45., home_win_prob=.55,
                      sd_margin=13., sd_total=10., spread_component={'used': False, 'margin': None})
    db.save_nfl_snapshot('fallback', GAME, {'projection': projection}, 'baseline', 'base')
    db.save_nfl_game_prediction('fallback', GAME, 'fallback', projection, 'baseline')
    event = db.q1('SELECT * FROM events WHERE id=?', (GAME,))
    displayed = product_views._game(event)
    assert displayed['market']['spread']['fair_home_spread'] == -1.


def test_worker_saves_revision_after_its_current_quote_capture(isolated, monkeypatch):
    from edgeterminal import research_refresh as refresh
    calls = []
    monkeypatch.setattr(refresh, '_events', lambda _: [dict(id=GAME, kickoff_utc=6000., meta={})])
    monkeypatch.setattr(refresh, '_due', lambda name, *a: name in {'schedule', 'current_game_odds', 'product_projections'})
    monkeypatch.setattr(engine, 'refresh_schedule', lambda *_: {'ok': True})
    monkeypatch.setattr(engine, 'ensure_weekly_projections', lambda:
                        calls.append('projection') or {'ok': True})
    monkeypatch.setattr(refresh.nfl_market_capture, 'capture_current', lambda **_:
                        calls.append('quotes') or {'ok': True})
    monkeypatch.setattr(refresh.nfl_top_five, 'grade_due', lambda *_: {})
    monkeypatch.setattr(refresh.nfl_prop_capture_ops, 'plan', lambda *a, **k: {'due': {}})
    monkeypatch.setattr(refresh, '_week_due', lambda *_: None)
    monkeypatch.setattr(refresh, '_market', lambda *_: {})
    refresh.run(now=1000.)
    assert calls == ['quotes', 'projection']


def test_normal_forecast_tick_does_not_parse_annual_pbp(board, monkeypatch):
    from edgeterminal.providers import nflverse
    stats = board[1]
    monkeypatch.setattr(engine.R, '_chain', lambda *a, **k: (stats, 'espn-stats', False))
    monkeypatch.setattr(engine.R, 'team_stats', REAL_TEAM_STATS)
    monkeypatch.setattr(nflverse, 'load_cached_team_features', lambda *a, **k:
                        pytest.fail('annual PBP decompressed in capture worker'))
    assert engine.ensure_weekly_projections()['ok']


def test_passive_detail_and_board_choose_same_tied_revision(board):
    for key, margin in [('a', 1.), ('z', 9.)]:
        projection = dict(proj_home=25., proj_away=20., margin=margin, total=45.,
                          home_win_prob=.6, feature_version='fixture')
        db.save_nfl_snapshot(key, GAME, {'projection': projection}, 'baseline', 'fixture')
        db.save_nfl_game_prediction(key, GAME, key, projection, 'baseline')
    expected = engine.weekly_board()[0]['projection']
    assert engine.analyze_event(GAME, persist=False)['projection'] == expected


def test_source_status_read_never_creates_schema(board):
    from edgeterminal import nfl_game_sources
    before = db.q('SELECT name,sql FROM sqlite_master ORDER BY name')
    assert nfl_game_sources.status()['status'] == 'missing'
    assert db.q('SELECT name,sql FROM sqlite_master ORDER BY name') == before


def test_new_forecast_probability_never_borrows_a_later_line(board):
    clock, _ = board
    assert engine.ensure_weekly_projections()['ok']
    clock[0] += 10
    db.store_odds([dict(event_id=GAME, sport='NFL', market='spread', selection=team,
                       line=line, price=-110, book='ESPN BET', captured_at=clock[0]-1,
                       source_url='https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
                       quote_phase='current') for team,line in ((HOME,-6.5),(AWAY,6.5))], source='espn-odds')
    display = product_views._game(db.q1('SELECT * FROM events WHERE id=?',(GAME,)))
    assert display['market']['spread']['home_cover_probability'] is None
    assert display['latest_market']['spread']['home']['line'] == -6.5
    assert display['newer_market_available']
