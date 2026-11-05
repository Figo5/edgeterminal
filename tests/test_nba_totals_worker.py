"""Totals jobs use the isolated worker and never the player-prop registry."""
import sys
import json
from types import SimpleNamespace

import pytest

from edgeterminal import db, learning_worker as worker
from edgeterminal.config import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'jobs.db'))
    db.init()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_totals_jobs_deduplicate_without_running_any_fit(monkeypatch):
    monkeypatch.setattr(worker, '_execute', lambda *_: pytest.fail('enqueue must not fit'))
    job = worker.enqueue('NBA_TOTAL', 'retuning', now=100.)
    assert worker.enqueue('NBA_TOTAL', 'retuning', now=101.)['id'] == job['id']
    assert worker.enqueue('NBA_TOTAL', 'diagnostics', now=101.)['id'] != job['id']
    assert db.q1('SELECT COUNT(*) n FROM learning_jobs')['n'] == 2


def test_nba_status_includes_total_failures_without_affecting_nfl(monkeypatch):
    worker.enqueue('NBA_TOTAL', 'diagnostics', now=100.)
    def fail(*_):
        raise ValueError('sensitive internal detail')
    monkeypatch.setattr(worker, '_execute', fail)
    assert worker.run_once(now=101.)['status'] == 'failed'
    assert worker.status('NBA', now=102.)['needs_attention']
    assert any(j['component'] == 'NBA_TOTAL' for j in worker.status('NBA', now=102.)['jobs'])
    assert not worker.status('NFL', now=102.)['needs_attention']
    worker.enqueue('NBA_TOTAL', 'diagnostics', now=103.)
    assert worker.status('NBA', now=103.)['needs_attention']
    monkeypatch.setattr(worker, '_execute', lambda *_: {'status': 'ok'})
    assert worker.run_once(now=104.)['status'] == 'completed'
    assert not worker.status('NBA', now=105.)['needs_attention']


def test_totals_dispatch_does_not_use_the_prop_adapter(monkeypatch):
    import edgeterminal
    calls = []
    adapter = SimpleNamespace(run_fitting=lambda: calls.append('fit') or {'status': 'waiting'},
        run_diagnostics=lambda: calls.append('diagnostics') or {'status': 'ok'},
        resume_publication=lambda checkpoint: calls.append(checkpoint) or {'status': 'deferred'})
    monkeypatch.setitem(sys.modules, 'edgeterminal.nba_totals_learning', adapter)
    monkeypatch.setattr(edgeterminal, 'nba_totals_learning', adapter, raising=False)
    monkeypatch.setattr('edgeterminal.model_learning.run_kind',
                        lambda *_: pytest.fail('totals cannot use the prop registry'))
    job = dict(component='NBA_TOTAL', kind='retuning', detail='{}')
    assert worker._execute(job)['status'] == 'waiting'
    assert worker._execute(dict(job, kind='diagnostics'))['status'] == 'ok'
    assert worker._execute(dict(job, detail='{"candidate_id":"sealed-fixture"}'))['status'] == 'deferred'
    assert calls == ['fit', 'diagnostics', 'sealed-fixture']


def test_totals_does_not_expand_the_closed_action_catalog():
    with pytest.raises(ValueError, match='unsupported'):
        worker.enqueue('NBA_TOTAL', 'shell', now=100.)


def test_totals_diagnostics_checkpoint_owns_its_candidate():
    job = worker.enqueue('NBA_TOTAL', 'diagnostics', now=100.)
    db.ex('CREATE TABLE nba_totals_candidates(version TEXT PRIMARY KEY)')
    db.ex('INSERT INTO nba_totals_candidates VALUES(?)', ('totals-fixture',))
    db.ex("UPDATE learning_jobs SET state='running',owner='fixture' WHERE id=?", (job['id'],))
    token = worker._CURRENT_JOB.set(dict(job, owner='fixture'))
    try:
        with db.tx() as con:
            worker.checkpoint_candidate(con, 'totals-fixture')
    finally:
        worker._CURRENT_JOB.reset(token)
    detail = json.loads(db.q1('SELECT detail FROM learning_jobs WHERE id=?', (job['id'],))['detail'])
    assert detail['candidate_id'] == 'totals-fixture'
