"""Season summaries retain the model actually saved for each live decision."""
import copy
import json

import pytest

from edgeterminal import db, nba_data, nba_ops, nba_store, nfl_season_record as nfl
from edgeterminal.config import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "versions.db"}')
    monkeypatch.setattr(nfl, 'ROOT', tmp_path)
    db.init()
    nba_ops.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def test_nfl_locked_versions_use_saved_row_identity_not_current_model(monkeypatch):
    game = 'nfl-2026-reg-08-min-det'
    rows = [{'result': state, 'model_version': version} for state, version in
            [('won', 'original'), ('lost', 'original'), ('push', 'new'), ('void', 'new'), ('pending', None)]]
    locked = {'game_id': game, 'selected_at': 100., 'rows': rows}
    original = copy.deepcopy(rows)
    monkeypatch.setattr(nfl.nfl_top_five, 'view', lambda: {'games': [locked]})
    monkeypatch.setattr(settings, 'model_version', 'today-is-not-the-saved-model')
    result = nfl.props(include_replay=False)
    versions = {r['model_version']: r for r in result['model_versions']}
    assert set(versions) == {'original', 'new', None}
    assert versions['original']['record'] == {'won': 1, 'lost': 1}
    assert versions['original']['win_rate'] == .5
    assert versions['new']['record'] == {'push': 1, 'void': 1}
    assert versions['new']['win_rate'] is None
    assert versions[None]['record'] == {'pending': 1}
    assert all(v['game_count'] == 1 and v['first_saved_at'] == v['last_saved_at'] == 100. for v in versions.values())
    assert sum(v['count'] for v in versions.values()) == result['locked']['count'] == 5
    assert rows == original  # Aggregation never annotates sealed rows.


def test_nfl_archived_rankings_do_not_acquire_a_live_model_version(monkeypatch):
    game = 'nfl-2026-reg-03-atl-gb'
    row = dict(quote_id=1, canonical_game_id=game, player_id='p', player_name='Player',
               market='receptions', side='over', sportsbook='book', line=2.5,
               probability_over=.7, probability_under=.3, observed_at=10., kickoff_utc=100.)
    monkeypatch.setattr(nfl.nfl_top_five, 'view', lambda: {'games': []})
    original_q = db.q
    monkeypatch.setattr(db, 'q', lambda query, args=():
        [{'freeze_sha': 'freeze', 'frozen_at': 20.}] if 'FROM nfl_prop_research_freezes' in query else original_q(query, args))
    monkeypatch.setattr(nfl.nfl_prop_grading, '_freeze', lambda _: {'projections': [row]})
    monkeypatch.setattr(nfl.nfl_prop_grading, '_latest', lambda _: [])
    result = nfl.props(now=101., include_replay=False)
    assert result['count'] == result['archived']['count'] == 1
    assert result['model_versions'] == []
    assert result['games'][0]['basis'] == 'archived_ranking'


@pytest.mark.parametrize('stamp', [None, float('nan'), float('inf'), True])
def test_nfl_unknown_dates_stay_null_instead_of_using_today(monkeypatch, stamp):
    monkeypatch.setattr(nfl.nfl_top_five, 'view', lambda: {'games': [
        {'game_id': 'nfl-2026-reg-08-min-det', 'selected_at': stamp,
         'rows': [{'result': 'pending', 'model_version': 'old'}]}]})
    result = nfl.props(include_replay=False)['model_versions'][0]
    assert result['first_saved_at'] is None and result['last_saved_at'] is None
    json.dumps(result, allow_nan=False)


def test_nfl_games_attribute_only_the_latest_supported_pregame_forecast(monkeypatch):
    game = 'nfl-2026-reg-08-min-det'
    snapshot = dict(game=dict(id=game, home_team='Detroit Lions', away_team='Minnesota Vikings',
                              kickoff_utc='1970-01-01T00:03:20Z'),
                    projection=dict(home_win_prob=.7, margin=3., total=44.), market=[])
    saved = [dict(game_id=game, model_version=version, created_at=stamp,
                  home_win_probability=.7, projected_margin=3., projected_total=44.,
                  projected_home_points=23.5, projected_away_points=20.5,
                  payload=json.dumps(snapshot), start_time='1970-01-01T00:03:20Z')
             for version, stamp in [('late', 201.), ('selected', 150.), ('original', 50.)]]
    monkeypatch.setattr(nfl, '_final_scores', lambda _: {game: (24, 20)})
    monkeypatch.setattr(db, 'q', lambda *a: saved)
    monkeypatch.setattr(nfl, '_legacy_games', lambda *a: [])
    result = nfl.games()
    cohort, = result['model_versions']
    assert cohort['model_version'] == 'selected'
    assert cohort['first_saved_at'] == cohort['last_saved_at'] == 150.
    assert cohort['game_count'] == 1
    assert cohort['winner']['record'] == result['winner']['record'] == {'won': 1}
    assert cohort['spread']['record'] == result['spread']['record'] == {'unavailable': 1}


def test_nfl_legacy_market_versions_are_not_all_labeled_as_the_first_market(monkeypatch):
    monkeypatch.setattr(nfl, '_final_scores', lambda _: {})
    monkeypatch.setattr(db, 'q', lambda *a: [])
    monkeypatch.setattr(nfl, '_legacy_games', lambda *a: [dict(
        game_id='nfl-2026-reg-01-atl-gb', week=1, model_version='winner-old', saved_at=100.,
        results=dict(winner='won', spread='lost', total='push'),
        market_model_versions=dict(winner='winner-old', spread='spread-new', total=None),
        market_saved_at=dict(winner=50., spread=100., total=70.))])
    result = nfl.games()
    versions = {r['model_version']: r for r in result['model_versions']}
    assert set(versions) == {'winner-old', 'spread-new', None}
    assert versions['winner-old']['winner']['record'] == {'won': 1}
    assert versions['winner-old']['spread']['count'] == 0
    assert versions['spread-new']['spread']['record'] == {'lost': 1}
    assert versions['spread-new']['first_saved_at'] == 100.
    assert versions[None]['total']['record'] == {'push': 1}


def _nba_case(eid='401909090', version='props-original', component='game-original'):
    tip = 1792546200.0
    before = tip - 3600
    sid = nba_store.save_artifact(nba_data.fetch('https://site.api.espn.com/fixture', 'fixture',
        getter=lambda *a: (200, b'{}'), now=before - 20))
    gid = 'nba-2026-' + eid
    game = dict(game_id=gid, espn_event_id=eid, season_start_year=2026, season_label='2026-27',
                season_type='regular', competition='NBA', home_team='SAS', away_team='OKC',
                tipoff_utc='2026-10-21T01:30:00Z', status='scheduled', home_score=None, away_score=None)
    nba_store.upsert_game(game, sid, now=before)
    nba_store.save_forecast('game-' + gid, gid, 'wrapper', dict(kind='game',
        projection=dict(home_margin=3, total=220, game_component_version=component),
        market=dict(home_spread=-2.5, total=None)), [sid], now=before)
    nba_store.save_forecast('props-' + gid, gid, version, {'kind': 'props', 'rows': []}, [sid], now=before)
    return gid, sid, before, game


@pytest.mark.parametrize('old_version', ['props-original', ''])
def test_nba_per_item_versions_reconcile_with_the_immutable_lock(monkeypatch, old_version):
    gid, sid, before, game = _nba_case(version=old_version)
    monkeypatch.setattr(nba_ops, 'today', lambda: '2026-10-21')
    nba_store.save_forecast('props-new', gid, 'props-new', {'kind': 'props', 'rows': []}, [sid], now=before+1)
    items = []
    for i in range(5):
        quote = nba_store.save_quote_observation(gid, 'player_points', str(100+i), 'over', 10.5, -110,
            'fixture', before+5, source_artifact_id=sid, now=before+5)
        items.append(dict(forecast_id='props-'+gid if i < 2 else 'props-new', espn_player_id=str(100+i),
                          prop='points', side='over', line=10.5, quote_id=quote['quote_id']))
    nba_store.lock_selections('top5-'+gid, items, now=before+10)
    nba_store.upsert_game(dict(game, status='final', home_score=110, away_score=100), sid, now=before+5000)
    for i, state in enumerate(('win', 'loss', 'push', 'void'), 1):
        nba_store.add_grade_revision('top5-'+gid, i, state, None if state == 'void' else 10., sid, now=before+5000)
    before_rows = db.q('SELECT * FROM nba_selection_lock_items')
    result = nba_ops.performance()
    versions = {r['model_version']: r for r in result['model_versions']['props']}
    assert versions[old_version or None]['record'] == {'won': 1, 'lost': 1, 'push': 0, 'void': 0, 'pending': 0}
    assert versions[old_version or None]['win_rate'] == .5
    assert versions['props-new']['record'] == {'won': 0, 'lost': 0, 'push': 1, 'void': 1, 'pending': 1}
    assert versions['props-new']['win_rate'] is None
    assert sum(v['count'] for v in versions.values()) == sum(result['props'].values()) == 5
    assert all(v['first_saved_at'] == v['last_saved_at'] == before+10 for v in versions.values())
    assert db.q('SELECT * FROM nba_selection_lock_items') == before_rows


def test_nba_game_component_versions_remain_distinct_under_same_wrapper(monkeypatch):
    _nba_case('401909090', component='spread-old')
    _nba_case('401909091', component='spread-new')
    monkeypatch.setattr(nba_ops, 'today', lambda: '2026-10-21')
    result = nba_ops.performance()
    cohorts = result['model_versions']['games']
    assert {(r['model_version'], r['game_component_version']) for r in cohorts} == {
        ('wrapper', 'spread-old'), ('wrapper', 'spread-new')}
    assert sum(r['winner']['count'] for r in cohorts) == sum(result['winners'].values()) == 2
    assert sum(r['spread']['count'] for r in cohorts) == sum(result['spread'].values()) == 2
    assert all(r['total']['count'] == 0 for r in cohorts)


def test_nba_selected_revision_gets_its_own_version_and_timestamp(monkeypatch):
    from test_nba_game_forecasts import seed, save, NOW
    monkeypatch.setattr(db, 'now', lambda: NOW)
    legacy, projection, market, manifest, sources = seed()
    first = save(dict(projection, game_component_version='spread-first'), market, manifest, sources)
    chosen = save(dict(projection, game_component_version='spread-chosen'),
                  market, manifest, sources, NOW+600)
    monkeypatch.setattr(nba_ops, 'today', lambda: '2026-10-30')
    raw = db.q('SELECT * FROM nba_forecasts')
    result = nba_ops.performance()
    cohort, = result['model_versions']['games']
    assert cohort['model_version'] == chosen['model_version']
    assert cohort['game_component_version'] == 'spread-chosen'
    assert cohort['first_saved_at'] == cohort['last_saved_at'] == chosen['created_at']
    assert cohort['winner']['count'] == 1
    assert chosen['forecast_id'] != first['forecast_id'] != legacy['forecast_id']
    assert db.q('SELECT * FROM nba_forecasts') == raw


def test_empty_records_have_empty_cohorts_and_no_implicit_model(monkeypatch):
    monkeypatch.setattr(nba_ops, 'today', lambda: '2026-10-21')
    assert nba_ops.performance()['model_versions'] == {'props': [], 'games': [], 'totals': []}
    monkeypatch.setattr(nfl.nfl_top_five, 'view', lambda: {'games': []})
    assert nfl.props(include_replay=False)['model_versions'] == []
    assert nfl.games()['model_versions'] == []


def test_nfl_model_dates_and_order_use_only_saved_selections(monkeypatch):
    games = [{'game_id': f'nfl-2026-reg-08-test-{i}', 'selected_at': stamp,
              'rows': [{'result': state, 'model_version': version}]} for i, (stamp, version, state) in enumerate([
                  (100., 'later', 'lost'), (50., 'earlier', 'won'), (200., 'later', 'pending'),
                  (None, None, 'void')])]
    monkeypatch.setattr(nfl.nfl_top_five, 'view', lambda: {'games': games})
    versions = nfl.props(include_replay=False)['model_versions']
    assert [v['model_version'] for v in versions] == ['earlier', 'later', None]
    assert versions[1]['first_saved_at'] == 100. and versions[1]['last_saved_at'] == 200.
    assert versions[1]['game_count'] == versions[1]['count'] == 2


def test_nba_unknown_version_and_component_are_not_replaced_with_active_values(monkeypatch):
    _nba_case(version='', component=None)
    monkeypatch.setattr(nba_ops, 'today', lambda: '2026-10-21')
    # Current status/model access is unrelated to attribution of saved decisions.
    monkeypatch.setattr(nba_ops, 'model_info', lambda: pytest.fail('looked up today\'s model'))
    cohort, = nba_ops.performance()['model_versions']['games']
    assert cohort['model_version'] == 'wrapper'
    assert cohort['game_component_version'] is None


def test_nba_version_date_ranges_and_zero_line_are_retained(monkeypatch):
    gid, sid, before, _ = _nba_case('401909090', component='same')
    _nba_case('401909091', component='same')
    monkeypatch.setattr(nba_ops, 'today', lambda: '2026-10-21')
    rows = nba_ops.performance()['model_versions']['games']
    assert len(rows) == 1 and rows[0]['game_count'] == 2
    assert rows[0]['first_saved_at'] == rows[0]['last_saved_at'] == before
    # A sourced zero spread is a valid comparison line, not absent.
    original = nba_ops.comparison_market
    monkeypatch.setattr(nba_ops, 'comparison_market', lambda game, saved: dict(original(game, saved), home_spread=0.))
    result = nba_ops.performance()
    assert result['model_versions']['games'][0]['spread']['count'] == 2
    assert result['spread']['pending'] == 2


def test_nfl_view_forwards_only_the_actual_live_version_cohorts(monkeypatch):
    prop_cohorts = [{'model_version': 'saved-prop'}]
    game_cohorts = [{'model_version': 'saved-game'}]
    monkeypatch.setattr(nfl, 'props', lambda _: {'weekly': {}, 'model_versions': prop_cohorts})
    monkeypatch.setattr(nfl, 'games', lambda _: {'weekly': {}, 'model_versions': game_cohorts})
    monkeypatch.setattr(nfl, 'linear_research_record', lambda _: {})
    from edgeterminal import nfl_prop_challenger
    monkeypatch.setattr(nfl_prop_challenger, 'view', lambda: {'record': {'won': 99}})
    result = nfl.view()
    assert result['model_versions'] == {'props': prop_cohorts, 'games': game_cohorts}
    assert result['challenger']['record'] == {'won': 99}
