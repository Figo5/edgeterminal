"""Totals jobs and records extend the existing live NBA flow."""
import json
import hashlib
import sys
from types import SimpleNamespace
import pytest
from edgeterminal import db, nba_ops, nba_store
from edgeterminal.config import settings
from test_nba_game_forecasts import GID, NOW, TIP, seed, save, total_market


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'totals.db'))
    monkeypatch.setattr(nba_ops, 'today', lambda now=None: '2026-10-30')
    monkeypatch.setattr(db, 'now', lambda: NOW)
    db.init()
    nba_ops.ensure_schema()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_source_cycle_queues_totals_after_grading_without_fitting(monkeypatch):
    import edgeterminal
    from edgeterminal import nba_spread_learning
    calls = []
    adapter = SimpleNamespace(refresh_inputs=lambda now: calls.append('totals') or {'ok': True})
    monkeypatch.setattr(edgeterminal, 'nba_totals_stages', adapter, raising=False)
    monkeypatch.setitem(sys.modules, 'edgeterminal.nba_totals_stages', adapter)
    monkeypatch.setattr(nba_ops, 'register_model', lambda *_: None)
    monkeypatch.setattr(nba_ops, 'model_info', lambda: {'fingerprint': 'fixture'})
    monkeypatch.setattr(db, 'kv_get', lambda *_: {'model': {'fingerprint': 'fixture'}})
    monkeypatch.setattr(nba_ops, '_schedule_cadence', lambda *_: 900)
    monkeypatch.setattr(nba_ops, '_props_cadence', lambda *_: 300)
    monkeypatch.setattr(nba_store, 'get_cadence', lambda *_: None)
    monkeypatch.setattr(nba_store, 'set_cadence', lambda *a, **k: None)
    for name in ('refresh_schedule', 'histories', 'forecasts', 'capture_props', 'grade_due'):
        monkeypatch.setattr(nba_ops, name, lambda *a, _name=name: calls.append(_name) or {'ok': True})
    monkeypatch.setattr(nba_spread_learning, 'refresh_inputs', lambda *_: {'ok': True})
    monkeypatch.setattr(nba_ops, 'step', lambda name, now, cadence, action, **kw: {'ok': True, 'result': action()})
    output = nba_ops.run(NOW)
    assert output['ok'] and 'totals_learning' in output['checks']
    assert calls.index('totals') > calls.index('grade_due')


def test_totals_version_record_counts_once_and_preserves_original(monkeypatch):
    legacy, projection, market, manifest, sources = seed()
    original = nba_store.get_forecast(legacy['forecast_id'])
    projection = dict(projection, total_component_version='totals-v2', total=225,
        home_score=115.5, away_score=109.5, proj_home=115.5, proj_away=109.5)
    sourced_total, total_source = total_market()
    chosen = save(projection, sourced_total, manifest, sources+[total_source], NOW+200)
    encoded = json.dumps({'winners': 'won', 'total': 'won',
        'forecast_id': chosen['forecast_id'], 'forecast_sha256': chosen['payload_sha256'],
        'forecast_policy_id': chosen['payload']['policy_id']},sort_keys=True)
    db.ex('INSERT INTO nba_game_grade_revisions(game_id,source_artifact_id,graded_at,payload,sha256) VALUES(?,?,?,?,?)',
        (GID, total_source, TIP + 100, encoded, hashlib.sha256((encoded+':0').encode()).hexdigest()))
    result = nba_ops.performance()
    assert result['total']['won'] == 1
    totals, = result['model_versions']['totals']
    assert totals['model_version'] == 'totals-v2'
    assert totals['count'] == totals['game_count'] == totals['record']['won'] == 1
    assert nba_store.get_forecast(legacy['forecast_id']) == original


def test_old_totals_are_legacy_without_relabeling_forecasts():
    legacy, projection, market, manifest, sources = seed()
    nba_store.save_forecast('total-line-' + GID, GID, 'legacy',
        {'kind': 'total-line', 'market': {'total': 219.5}}, sources, now=NOW - 1)
    original = nba_store.get_forecast(legacy['forecast_id'])
    result = nba_ops.performance()
    totals, = result['model_versions']['totals']
    assert totals['count'] == result['total']['pending'] == 1
    assert totals['model_version'].startswith('legacy-total:')
    assert nba_store.get_forecast(legacy['forecast_id']) == original


def test_totals_composition_requires_source_replay_before_pure_inference(monkeypatch):
    import edgeterminal
    calls = []
    mapping = {'sha256': 'sealed-map'}
    state = {'artifact': 'frozen-artifact'}
    adapters = {
        'nba_totals_stages': SimpleNamespace(needs_inputs=lambda: True, serving_state=lambda: state),
        'nba_totals_data': SimpleNamespace(select_history=lambda *a: ['selected'],
            prepare=lambda *a: calls.append('prepare') or mapping,
            verify_input_map=lambda *a: calls.append('verify') or mapping),
        'nba_totals_model': SimpleNamespace(compose=lambda p, m, **kw:
            calls.append(('compose', kw['model_state'])) or dict(p, total=225)),
    }
    for name, adapter in adapters.items():
        monkeypatch.setattr(edgeterminal, name, adapter, raising=False)
        monkeypatch.setitem(sys.modules, 'edgeterminal.' + name, adapter)
    original = {'total': 220, 'home_margin': 3}
    result, evidence = nba_ops._totals_projection({'game_id': GID}, original, [], NOW)
    assert calls == ['prepare', 'verify', ('compose', state)]
    assert result['total'] == 225 and evidence is mapping
    assert original == {'total': 220, 'home_margin': 3}


def test_no_totals_candidate_keeps_the_incumbent_without_heavy_replay(monkeypatch):
    import edgeterminal
    adapter = SimpleNamespace(needs_inputs=lambda: False,
        serving_state=lambda: pytest.fail('no component needs replay'))
    monkeypatch.setattr(edgeterminal, 'nba_totals_stages', adapter, raising=False)
    monkeypatch.setitem(sys.modules, 'edgeterminal.nba_totals_stages', adapter)
    original = {'total': 220, 'home_margin': 3}
    result, mapping = nba_ops._totals_projection({'game_id': GID}, original, [], NOW)
    assert result is original and mapping is None


def test_refresh_composes_then_saves_before_freezing_candidate(monkeypatch):
    import edgeterminal
    from edgeterminal import nba_spread_model
    legacy, projection, market, manifest, sources = seed()
    monkeypatch.setattr(nba_ops.time, 'time', lambda: NOW)
    db.kv_set('nba_market_' + GID, market)
    raw = dict(projection, selected_rows=manifest['rows'], feature_cutoff=manifest['feature_cutoff'],
               parameter_fingerprint='fixture', training_cutoff='2026-10-01')
    monkeypatch.setattr(nba_spread_model, 'project_game', lambda *a, **k: raw.copy())
    mapping = {'source_artifacts': [{'id': sources[0]}]}
    monkeypatch.setattr(nba_ops, '_totals_projection', lambda g, p, h, t:
        (dict(p, total=225., home_score=115.5, away_score=109.5,
              proj_home=115.5, proj_away=109.5, total_component_version='totals-fixture'), mapping))
    calls = []
    adapter = SimpleNamespace(capture_forecast=lambda saved, mapped, **kw: calls.append((saved, mapped)))
    monkeypatch.setattr(edgeterminal, 'nba_totals_stages', adapter, raising=False)
    monkeypatch.setitem(sys.modules, 'edgeterminal.nba_totals_stages', adapter)
    assert nba_ops._refresh_game_projection(nba_store.get_game(GID), manifest['rows'], NOW)
    selected = nba_ops._forecast(GID, 'game')
    assert selected['payload']['projection']['total'] == 225.
    assert selected['payload']['projection']['home_margin'] == projection['home_margin']
    assert len(calls) == 1 and calls[0][0]['forecast_id'] == selected['forecast_id']
    assert calls[0][1] is mapping
    assert nba_store.get_forecast(legacy['forecast_id']) == legacy


def test_totals_integrity_failure_is_visible_and_keeps_a_supported_forecast(monkeypatch):
    legacy, projection, market, manifest, sources = seed()
    monkeypatch.setattr(nba_ops.time, 'time', lambda: NOW)
    for code in ('BOS', 'NYK'):
        db.kv_set('nba_team_history_' + code, [r for r in manifest['rows'] if r['team'] == code])
    db.kv_set('nba_market_' + GID, market)
    def invalid(*_):
        raise ValueError('untrusted detail must not be public')
    monkeypatch.setattr(nba_ops, '_totals_projection', invalid)
    result = nba_ops.forecasts(NOW)
    assert not result['ok'] and GID in result['source_errors']
    assert nba_ops._forecast(GID, 'game')['payload']['projection']['status'] == 'ok'
    failure = db.kv_get('nba_totals_projection_' + GID)
    assert failure['ok'] is False and failure['error'] == 'ValueError'
    assert 'untrusted detail' not in json.dumps(failure)


def test_nba_status_exposes_totals_job_health_separately(monkeypatch):
    import edgeterminal
    status = {'enabled': True, 'component': 'NBA_TOTAL', 'status': 'waiting',
              'message': 'Future whole-game validation is still collecting.', 'active': None}
    adapter = SimpleNamespace(status=lambda **kw: status)
    monkeypatch.setattr(edgeterminal, 'nba_totals_stages', adapter, raising=False)
    monkeypatch.setitem(sys.modules, 'edgeterminal.nba_totals_stages', adapter)
    result = nba_ops.status()
    assert result['totals_learning'] is status
    assert 'game_learning' in result and 'learning' in result
