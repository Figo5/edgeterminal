"""The normal NBA tick schedules learning without performing a fit."""
import pytest

from edgeterminal import nba_ops, nba_spread_learning
from test_update_health import isolated


def test_cached_official_gamebook_automatically_voids_an_absent_selected_player(isolated, monkeypatch):
    from edgeterminal import nba_store
    from test_nba_ops import _summary_feed
    from test_nba_participation import case
    raw, game, args = case()
    athletes = raw['boxscore']['players'][0]['statistics'][0]['athletes']
    raw['boxscore']['players'][0]['statistics'][0]['athletes'] = [
        p for p in athletes if p['athlete']['id'] != '2968436']
    _summary_feed(monkeypatch, {game['espn_event_id']: raw}, [])
    result = nba_ops.grade_due(args['now'])
    grade = nba_store.latest_grades('top5-' + game['game_id'])[3]
    assert result['ok'] and result['pending_props'] == 0
    assert grade['result'] == 'void' and grade['actual_value'] is None
    assert grade['source_artifact_id'] == args['source_artifact_id']
    assert grade['detail']['source'] == 'NBA official machine-verified participation'
    assert grade['detail']['proof']['verification_method'] == 'automatic_gamebook_v1'
    assert nba_ops.grade_due(args['now'] + 301)['new_grades'] == 0


def test_participation_source_error_does_not_block_other_numeric_grades(isolated, monkeypatch):
    from edgeterminal import nba_participation_sources, nba_store
    from test_nba_ops import _summary_feed
    from test_nba_participation import case
    raw, game, args = case()
    athletes = raw['boxscore']['players'][0]['statistics'][0]['athletes']
    raw['boxscore']['players'][0]['statistics'][0]['athletes'] = [
        p for p in athletes if p['athlete']['id'] != '2968436']
    _summary_feed(monkeypatch, {game['espn_event_id']: raw}, [])
    monkeypatch.setattr(nba_participation_sources, 'collect_missing', lambda *a, **kw:
                        {'ok': False, 'status': 'pending', 'requests': 1, 'checked_at': args['now'],
                         'errors': ['Official participation source unavailable']})
    result = nba_ops.grade_due(args['now'])
    grades = nba_store.latest_grades('top5-' + game['game_id'])
    assert not result['ok'] and result['source_errors'] == [game['game_id']]
    assert grades[1]['result'] == 'win' and grades[3]['result'] == 'pending'
    assert len(grades) == 5


def test_participation_budget_remains_bounded_if_a_lookup_raises(isolated, monkeypatch):
    from edgeterminal import nba_participation_sources
    from test_nba_ops import _grading_case, _summary_feed
    summaries = {}
    for index in range(5):
        raw, game, _, tip = _grading_case(str(401810831 + index))
        summaries[game['espn_event_id']] = raw
    _summary_feed(monkeypatch, summaries, [])
    allowances = []
    def failed(*args, **kwargs):
        allowances.append(kwargs['max_requests'])
        raise ValueError('lookup interrupted after using its allowance')
    monkeypatch.setattr(nba_participation_sources, 'collect_missing', failed)
    result = nba_ops.grade_due(tip + 5 * 3600)
    assert not result['ok']
    assert sum(allowances) <= nba_participation_sources.MAX_REQUESTS_PER_RUN
    assert result['new_grades'] == 30  # Five game records and 25 unchanged-rule prop grades.


def test_nba_refresh_uses_bounded_inputs_not_inline_spread_fitting(monkeypatch):
    order = []
    monkeypatch.setattr(nba_ops, 'ensure_schema', lambda: None)
    monkeypatch.setattr(nba_ops, 'register_model', lambda now: None)
    monkeypatch.setattr(nba_ops, 'model_info', lambda: {'fingerprint': 'fixed'})
    monkeypatch.setattr(nba_ops.db, 'kv_get', lambda key: {'model': {'fingerprint': 'fixed'}})
    monkeypatch.setattr(nba_ops.nba_store, 'get_cadence', lambda name: None)
    monkeypatch.setattr(nba_ops, '_schedule_cadence', lambda now: 300)
    monkeypatch.setattr(nba_ops, '_props_cadence', lambda *args: 300)
    monkeypatch.setattr(nba_ops, 'step', lambda name, now, seconds, action, **kw:
                        {'ok': True, 'result': action()})
    for name in ('refresh_schedule', 'histories', 'forecasts', 'grade_due'):
        monkeypatch.setattr(nba_ops, name, lambda now, name=name:
                            order.append(name) or {'message': 'checked'})
    monkeypatch.setattr(nba_ops, 'capture_props', lambda now: {'status': 'waiting'})
    monkeypatch.setattr(nba_ops.nba_store, 'set_cadence', lambda *a, **kw: None)
    monkeypatch.setattr(nba_spread_learning, 'run', lambda *a, **kw:
                        pytest.fail('ordinary NBA refresh entered the inline fitting path'))
    monkeypatch.setattr(nba_spread_learning, 'refresh_inputs', lambda now:
                        order.append('bounded_spread_inputs') or {'ok': True, 'status': 'queued'},
                        raising=False)
    result = nba_ops.run(1000.)
    assert result['ok']
    assert order == ['refresh_schedule', 'histories', 'forecasts', 'grade_due', 'bounded_spread_inputs']
    assert result['checks']['spread_learning']['result']['status'] == 'queued'
