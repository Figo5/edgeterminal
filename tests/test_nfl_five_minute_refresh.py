"""Five-minute NFL grading stays independent of capture and the other model."""
import json

import pytest

from edgeterminal import db, nfl_prop_challenger, research_refresh
from edgeterminal.config import settings
from test_research_refresh import isolated


@pytest.mark.parametrize('name', ['top_five_grading', 'challenger_grading', 'game_grading'])
@pytest.mark.parametrize('success', [True, False])
def test_results_retry_at_five_minutes_for_success_and_failure(isolated, name, success):
    db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) '
          'VALUES(?,?,?,?,?)', ('prior', name, 1000., int(success),
                              json.dumps({'error': 'source identity mismatch'})))
    assert not research_refresh._result_due(name, 1299.)
    assert research_refresh._result_due(name, 1300.)


def test_original_failure_cannot_block_challenger_or_early_final_game_checks(isolated, monkeypatch):
    now=100000.
    monkeypatch.setattr(research_refresh.engine,'refresh_schedule',lambda *a:{'ok':True})
    monkeypatch.setattr(research_refresh.engine,'ensure_weekly_projections',lambda:{'ok':True})
    monkeypatch.setattr(research_refresh,'_events',lambda t:[{
        'id':'nfl-2026-reg-03-atl-gb','kickoff_utc':now-3*3600,
        'start_time':'1970-01-02T00:46:40Z','meta':{}}])
    monkeypatch.setattr(research_refresh.nfl_top_five,'capture_missing',lambda *a,**k:{})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops,'capture',
                        lambda *a,**k:pytest.fail('grading requested paid quotes'))
    original_calls=[];challenger_calls=[];game_calls=[]
    def failed_original(stamp):
        original_calls.append(stamp)
        raise ValueError('one original-model result is unavailable')
    monkeypatch.setattr(research_refresh.nfl_top_five,'grade_due',failed_original)
    nfl_prop_challenger.DIRECTORY.mkdir(parents=True)
    (nfl_prop_challenger.DIRECTORY/'active.json').write_text('{}')
    monkeypatch.setattr(nfl_prop_challenger,'grade_due',
                        lambda now=None:challenger_calls.append(now) or {})
    monkeypatch.setattr(research_refresh,'_game_grading',
                        lambda stamp:game_calls.append(stamp) or {})
    research_refresh.run(now=now)
    research_refresh.run(now=now+299)
    research_refresh.run(now=now+300)
    assert original_calls==[now,now+300]
    assert challenger_calls==[now,now+300]
    assert game_calls==[now,now+300]


def test_nfl_reports_five_minute_worker_cadence_even_when_nba_is_disabled(monkeypatch):
    monkeypatch.setattr(settings,'nba_research_enabled',False)
    assert research_refresh.interval(1000.,[])==300
