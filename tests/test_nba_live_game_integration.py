import pytest

from edgeterminal import db, nba_ops, nba_store
from edgeterminal.config import settings
from test_nba_game_forecasts import GID, NOW, TIP, seed, save


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "nba.db"}')
    monkeypatch.setattr(nba_ops, 'today', lambda now=None: '2026-10-30')
    monkeypatch.setattr(db, 'now', lambda: NOW)
    db.init()
    nba_store.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def test_live_selector_and_season_record_count_game_once():
    legacy, projection, market, manifest, sources = seed()
    chosen = save(projection, market, manifest, sources)
    save(dict(projection, home_margin=7, margin=7, home_score=113.5, away_score=106.5,
              proj_home=113.5, proj_away=106.5), market, manifest, sources, NOW+600)
    assert nba_ops._forecast(GID, 'game')['forecast_id'] != legacy['forecast_id']
    record = nba_ops.performance()
    assert record['winners']['pending'] == 1
    assert record['spread']['pending'] == 1
    assert record['total']['pending'] == 0


def test_revision_never_borrows_legacy_supplemental_line():
    legacy, projection, market, manifest, sources = seed()
    nba_store.save_forecast('spread-line-'+GID, GID, 'legacy',
        {'kind': 'spread-line', 'market': market}, sources, now=NOW-5)
    chosen = save(projection, {}, manifest, sources)
    assert nba_ops.comparison_market(GID, chosen) == {}
    assert nba_ops.comparison_market(GID, legacy)['home_spread'] == -2.5


def test_worker_refreshes_existing_early_projection_without_changing_it(monkeypatch):
    legacy, projection, market, manifest, sources = seed()
    before = nba_store.get_forecast(legacy['forecast_id'])
    for code in ('BOS', 'NYK'):
        db.kv_set('nba_team_history_'+code, [r for r in manifest['rows'] if r['team']==code])
    db.kv_set('nba_market_'+GID, market)
    monkeypatch.setattr(nba_ops.time, 'time', lambda: NOW)
    result = nba_ops.forecasts(NOW)
    assert result['new_revisions'] == 1
    assert nba_ops._forecast(GID, 'game')['payload']['kind'] == 'game-revision'
    assert nba_store.get_forecast(legacy['forecast_id']) == before
    assert nba_ops.forecasts(NOW)['new_revisions'] == 0


def test_delayed_inference_cannot_save_after_tipoff(monkeypatch):
    from edgeterminal import nba_spread_model
    legacy, projection, market, manifest, sources = seed()
    for code in ('BOS', 'NYK'):
        db.kv_set('nba_team_history_'+code, [r for r in manifest['rows'] if r['team']==code])
    db.kv_set('nba_market_'+GID, market)
    monkeypatch.setattr(nba_ops.time, 'time', lambda: NOW)
    project = nba_spread_model.project_game
    def delayed(*a, **kw):
        out = project(*a, **kw)
        monkeypatch.setattr(nba_ops.time, 'time', lambda: TIP+1)
        return out
    monkeypatch.setattr(nba_spread_model, 'project_game', delayed)
    assert nba_ops.forecasts(NOW)['new_revisions'] == 0
    assert nba_ops._forecast(GID, 'game')['forecast_id'] == legacy['forecast_id']


def test_near_tipoff_schedule_and_game_prices_recheck_every_five_minutes():
    seed()
    assert nba_ops._schedule_cadence(NOW) == 900
    assert nba_ops._schedule_cadence(TIP-1800) == 300
