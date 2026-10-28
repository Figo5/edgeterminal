"""Active model upgrades preserve records and expose version-matched evidence."""
import pytest

from edgeterminal import db, nba_ops, nba_store
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "models.db"}')
    db.init()
    nba_ops.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def test_direct_nba_capture_registers_the_active_version_before_work(isolated):
    assert db.q('SELECT * FROM nba_model_registry') == []
    assert nba_ops.capture_props(1793100000.)['status'] == 'no_targets'
    registered = db.q1('SELECT * FROM nba_model_registry')
    assert registered['version'] == nba_ops.model_info()['version']
    assert registered['fingerprint'] == nba_ops.model_info()['fingerprint']


def test_status_does_not_label_old_evaluation_as_current(isolated):
    db.kv_set('nba_evaluation', {'model': {'fingerprint': 'old'},
                               'summary': 'Old model evidence', 'markets': {'points': {'mae': 1}}})
    report = nba_ops.status()['evaluation']
    assert report.get('status') == 'pending'
    assert not report.get('markets')
    assert report.get('model') == nba_ops.model_info()


def test_worker_refreshes_evaluation_when_model_changes_without_more_players(isolated, monkeypatch):
    now = 1793100000.
    db.kv_set('nba_evaluation', {'model': {'fingerprint': 'old'}, 'player_count': 1000})
    nba_store.set_cadence('evaluation', 1800, now=now - 1)
    nba_store.record_run('evaluation', 'ok', {'message': 'old evaluation'}, now=now - 1)
    for name in ('refresh_schedule', 'histories', 'forecasts', 'capture_props', 'grade_due'):
        monkeypatch.setattr(nba_ops, name, lambda *args: {})
    calls = []
    def evaluate(stamp):
        calls.append(stamp)
        db.kv_set('nba_evaluation', {'model': nba_ops.model_info(), 'player_count': 1000})
        return {'message': 'Current evaluation'}
    monkeypatch.setattr(nba_ops, 'evaluate', evaluate)
    nba_ops.run(now)
    nba_ops.run(now + 1)
    assert calls == [now]
    assert nba_ops.status()['evaluation']['model'] == nba_ops.model_info()


def test_refresh_stops_new_challengers_and_keeps_grading_saved_history(isolated, monkeypatch, tmp_path):
    from edgeterminal import research_refresh, nfl_prop_challenger
    monkeypatch.setattr(research_refresh, '_events', lambda now: [])
    monkeypatch.setattr(research_refresh, '_due', lambda name, *args: name == 'top_five_capture')
    monkeypatch.setattr(research_refresh, '_result_due', lambda name, *args: name == 'challenger_grading')
    monkeypatch.setattr(research_refresh, '_failed_prop_pair_batches', lambda now: [])
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'plan', lambda *args, **kwargs: {'due': {}})
    monkeypatch.setattr(research_refresh.paper_research, 'needs_settlement', lambda: False)
    monkeypatch.setattr(research_refresh.paper_research, 'evaluate', lambda *args, **kwargs: {})
    monkeypatch.setattr(research_refresh.nfl_top_five, 'capture_missing',
                        lambda *args, **kwargs: {'capture': {'batch_id': 'current'}})
    db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
          ('current', 'sportsgameodds', 1000., 'sha', '{}', 1, 1, 0,
           '{"season":2026,"week":8}'))
    (tmp_path / 'active.json').write_text('{}')
    monkeypatch.setattr(nfl_prop_challenger, 'DIRECTORY', tmp_path)
    calls = []
    monkeypatch.setattr(nfl_prop_challenger, 'grade_due', lambda **kwargs: calls.append('grade') or {})
    monkeypatch.setattr(nfl_prop_challenger, 'shadow', lambda *args, **kwargs: calls.append('new') or {})
    result = research_refresh._run_locked(1000., record_tick=False)
    assert result['success']
    assert calls == ['grade']


def test_invalid_nfl_model_fails_before_inputs_or_provider_requests(isolated, monkeypatch):
    from edgeterminal import nfl_top_five as top
    db.upsert_event({'id':'nfl-2026-reg-08-tb-dal','sport':'NFL',
                     'start_time':'1970-01-02T00:00:00Z','status':'STATUS_SCHEDULED'})
    monkeypatch.setattr(settings, 'sportsgameodds_api_key', 'fixture')
    def invalid():
        raise ValueError('model artifact changed')
    monkeypatch.setattr(top.nfl_prop_model, 'load_artifact', invalid)
    monkeypatch.setattr(top, '_inputs', lambda *args: pytest.fail('inputs fetched before model verification'))
    monkeypatch.setattr(top.nfl_prop_capture_ops, 'capture',
                        lambda *args, **kwargs: pytest.fail('paid capture before model verification'))
    with pytest.raises(ValueError, match='artifact changed'):
        top.capture_missing(1000.)


def test_nba_evaluation_uses_the_frozen_upgrade_report(isolated, monkeypatch):
    monkeypatch.setattr(nba_ops, '_teams_history', lambda: [])
    monkeypatch.setattr(nba_ops.nba_model, 'historical_evaluation', lambda: {
        'holdout_start':'2026-03-01T00:00:00Z', 'holdout_end':'2026-10-01T00:00:00Z',
        'player_count':470, 'design':'Retrospective acceptance validation.',
        'markets':{'points':{'holdout_n':100, 'baseline_mae':5., 'upgraded_mae':4.8}}})
    monkeypatch.setattr(nba_ops.nba_model, 'project_prop',
                        lambda *args, **kwargs: pytest.fail('serving model re-evaluated against growing history'))
    result = nba_ops.evaluate(1793100000.)
    report = nba_ops.status()['evaluation']
    assert result['prop_predictions'] == 100
    assert report['markets']['points']['mae'] == 4.8
    assert report['markets']['points']['baseline_mae'] == 5.
    assert 'Retrospective' in report['note']
