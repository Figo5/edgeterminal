"""Numerical jobs remain offline and do not run from source/page checks."""
import sys
from types import SimpleNamespace

import edgeterminal
from edgeterminal import learning_worker
from edgeterminal import db
from test_research_refresh import isolated


def test_nfl_spread_is_a_separate_offline_component(isolated, monkeypatch):
    called = []
    fake = SimpleNamespace(run_fitting=lambda: called.append('spread') or {'status': 'waiting'})
    monkeypatch.setattr(edgeterminal, 'nfl_spread_learning', fake, raising=False)
    monkeypatch.setitem(sys.modules, 'edgeterminal.nfl_spread_learning', fake)
    result = learning_worker._execute({'component': 'NFL_SPREAD', 'kind': 'retuning', 'detail': '{}'})
    assert result['status'] == 'waiting'
    assert called == ['spread']


def test_new_totals_job_routes_to_durable_stages(isolated, monkeypatch):
    called = []
    fake = SimpleNamespace(run_fitting=lambda: called.append('stages') or {'status': 'waiting'})
    monkeypatch.setattr(edgeterminal, 'nba_totals_stages', fake, raising=False)
    monkeypatch.setitem(sys.modules, 'edgeterminal.nba_totals_stages', fake)
    result = learning_worker._execute({'component': 'NBA_TOTAL', 'kind': 'retuning', 'detail': '{}'})
    assert result['status'] == 'waiting'
    assert called == ['stages']


def test_nfl_spread_candidate_checkpoint_uses_its_versioned_registry(isolated):
    version = 'nfl-spread-' + 'a' * 32
    job = learning_worker.enqueue('NFL_SPREAD', 'retuning', now=100.)
    db.ex('CREATE TABLE nfl_spread_candidates(version TEXT PRIMARY KEY)')
    db.ex('INSERT INTO nfl_spread_candidates VALUES(?)', (version,))
    db.ex("UPDATE learning_jobs SET state='running',owner='fixture' WHERE id=?", (job['id'],))
    token = learning_worker._CURRENT_JOB.set(dict(job, owner='fixture'))
    try:
        with db.tx() as con:
            learning_worker.checkpoint_candidate(con, version)
    finally:
        learning_worker._CURRENT_JOB.reset(token)
    import json
    assert json.loads(db.q1('SELECT detail FROM learning_jobs WHERE id=?', (job['id'],))['detail'])['candidate_id'] == version


def test_legacy_weekly_tick_only_dispatches_offline_work(isolated, monkeypatch):
    from edgeterminal import research_refresh
    import pytest
    calls = []
    fake = SimpleNamespace(dispatch=lambda **kw: calls.append(kw) or {'status': 'queued'})
    monkeypatch.setattr(edgeterminal, 'nfl_weekly_preparation', fake, raising=False)
    monkeypatch.setitem(sys.modules, 'edgeterminal.nfl_weekly_preparation', fake)
    monkeypatch.setattr(research_refresh.nfl_forward_weekly, 'run', lambda *a, **kw:
                        pytest.fail('weekly download/build ran under capture lock'))
    assert research_refresh._weekly(2026, 1000.)['status'] == 'queued'
    assert calls == [{'season': 2026, 'now': 1000.}]
