"""Totals evidence has its own immutable, portable, fail-closed ledger."""
import copy
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from edgeterminal import nba_totals_stage_store as store

NOW = datetime.fromisoformat('2026-11-10T12:00:00+00:00').timestamp()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "totals.db"}')
    monkeypatch.setattr(store.time, 'time', lambda: NOW)
    repo = tmp_path / 'repo'
    (repo / 'edgeterminal').mkdir(parents=True)
    (repo / 'edgeterminal/component.py').write_text('immutable code\n')
    monkeypatch.setattr(store, 'ROOT', repo)
    monkeypatch.setattr(store, 'CODE_NAMES', ('edgeterminal/component.py',))
    monkeypatch.setattr(store, 'RESEARCH_ROOT', tmp_path / 'research')
    monkeypatch.setattr(store, '_validate_artifact', lambda value: None)
    store.ensure_schema()
    yield tmp_path
    db._conn.close()
    db._conn = None


def proposal(version='candidate-a', now=NOW):
    artifact = dict(version=version, training_cutoff=now - 86400, code_sha256=store.code_catalog(), sd_total=20.)
    protocol = dict(schema=1, family='finite-totals', proposal_frequency='one_selected_candidate_per_cohort',
                    excluded_through='2026-11-04', min_games=60, min_dates=7)
    artifact.update(training_sources_sha256=store.sha([]), evaluation_protocol_sha256=store.sha(protocol))
    return store.register_candidate(artifact, protocol, [], now=now)


def freeze(candidate, gid='g1', now=NOW + 60, tipoff=None):
    tipoff = tipoff or NOW + 3600
    return store.freeze_forecast(candidate['version'], dict(game_id=gid, forecast_sha256=store.sha(gid),
        date_et=datetime.fromtimestamp(tipoff, store.ET).date().isoformat(), forecast_at=now,
        tipoff=tipoff, baseline_total=230., candidate_total=225., saved_total_line=220.5,
        baseline_probability=.7, candidate_probability=.6, input_sha256=store.sha({'game': gid})), now=now)


def reserve(candidate, games=None, now=NOW + 7200):
    games = games or [freeze(candidate)]
    universe = {}
    for game in games:
        universe.setdefault(game['date_et'], []).append(game['game_id'])
    return store.reserve(candidate['version'], games, universe, now=now)


def test_candidate_is_immutable_and_archives_exact_relative_code(isolated):
    row = proposal()
    assert row['sha256'] == store.sha(row['artifact'])
    assert row['artifact']['code_sha256'] == store.code_catalog()
    archive = row['code_archive']['edgeterminal/component.py']
    assert not Path(archive).is_absolute()
    assert store.read_archived_code(archive) == b'immutable code\n'
    assert proposal()['sha256'] == row['sha256']
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        db.ex('UPDATE nba_totals_stage_candidates SET created_at=0')
    changed = copy.deepcopy(row['artifact'])
    changed['sd_total'] = 21.
    with pytest.raises(ValueError, match='immutable'):
        store.register_candidate(changed, row['protocol'], [], now=NOW)
    (store.ROOT / 'edgeterminal/component.py').write_text('changed')
    with pytest.raises(ValueError, match='code'):
        store.candidate(row['version'], verify_code=True)


def test_code_archive_rejects_traversal_symlinks_and_collision(isolated):
    for path in ('../escape.py', '/app/file.py', 'code/../escape.py'):
        with pytest.raises(ValueError):
            store.read_archived_code(path)
    row = proposal()
    archive = row['code_archive']['edgeterminal/component.py']
    path = store.RESEARCH_ROOT / archive
    path.unlink()
    path.symlink_to(store.ROOT / 'edgeterminal/component.py')
    with pytest.raises(ValueError, match='symlink|regular'):
        store.read_archived_code(archive)
    with pytest.raises(ValueError):
        proposal('candidate-b')


def test_frozen_prediction_must_precede_tipoff_and_postdate_candidate_seal():
    row = proposal()
    frozen = freeze(row)
    assert frozen == freeze(row)
    assert frozen['candidate_sha256'] == row['sha256']
    with pytest.raises(ValueError, match='chronolog|pretip'):
        freeze(row, 'late', now=NOW + 4000, tipoff=NOW + 3600)
    with pytest.raises(ValueError, match='chronolog|seal'):
        freeze(row, 'early', now=NOW - 1)
    with pytest.raises(ValueError, match='prospective|inspected|chronolog'):
        freeze(row, 'audited', now=NOW + 60, tipoff=datetime.fromisoformat('2026-11-04T23:00:00+00:00').timestamp())


def test_reservation_atomically_consumes_full_dates_across_family_names():
    row = proposal()
    a, b = freeze(row, 'a'), freeze(row, 'b')
    with pytest.raises(ValueError, match='whole|universe'):
        store.reserve(row['version'], [a], {a['date_et']: ['a', 'b']}, now=NOW + 7200)
    assert db.q1('SELECT count(*) n FROM nba_totals_stage_reserved_games')['n'] == 0
    reservation = reserve(row, [a, b])
    verified = store.verify_reservation(reservation)
    assert verified['game_ids'] == ['a', 'b']
    assert verified['date_ids'] == [a['date_et']]
    store.record_evaluation(reservation, {'passed': False, 'reasons': ['test']}, {}, now=NOW + 7300)
    other = proposal('another-family')
    c = freeze(other, 'c')
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        reserve(other, [c])
    assert store.verify_reservation(reservation['reservation_id'])['game_ids'] == ['a', 'b']
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        db.ex('DELETE FROM nba_totals_stage_reserved_dates')


def test_checkpoint_ownership_failure_rolls_back_reservation(monkeypatch):
    from edgeterminal import learning_worker
    row = proposal()
    frozen = freeze(row)
    def fail(*args):
        raise ValueError('learning checkpoint ownership changed')
    monkeypatch.setattr(store, '_checkpoint', fail)
    with pytest.raises(ValueError, match='ownership'):
        reserve(row, [frozen])
    assert db.q1('SELECT count(*) n FROM nba_totals_stage_reservations')['n'] == 0
    assert db.q1('SELECT count(*) n FROM nba_totals_stage_reserved_dates')['n'] == 0


def test_unapproved_or_tampered_activation_is_blocked():
    row = proposal()
    with pytest.raises(sqlite3.DatabaseError, match='proof'):
        db.ex('INSERT INTO nba_totals_stage_model_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
              (row['version'], NOW + 10, 'activated', '{}'))
    assert store.active_event() is None


def test_portable_archive_identity_survives_relocation(isolated, monkeypatch):
    row = proposal()
    relocated = isolated / 'app'
    (relocated / 'edgeterminal').mkdir(parents=True)
    (relocated / 'edgeterminal/component.py').write_bytes(b'immutable code\n')
    monkeypatch.setattr(store, 'ROOT', relocated)
    assert store.candidate(row['version'], verify_code=True)['sha256'] == row['sha256']


def test_reservation_rejects_unpersisted_forged_inputs_and_repeated_dates():
    row = proposal()
    a = freeze(row)
    forged = dict(a, candidate_total=1.)
    with pytest.raises(ValueError, match='freeze|seal'):
        reserve(row, [forged])
    sealed = reserve(row, [a])
    with pytest.raises(ValueError, match='reservation'):
        store.verify_reservation(dict(sealed, game_ids=['forged']))
    assert store.verify_reservation(sealed)['candidate_version'] == row['version']


def test_candidate_envelope_cannot_disagree_with_source_or_protocol_seals():
    row = proposal()
    changed = dict(row['artifact'], version='wrong-source-envelope', training_sources_sha256='f' * 64,
                   evaluation_protocol_sha256=store.sha(row['protocol']))
    with pytest.raises(ValueError, match='source|protocol'):
        store.register_candidate(changed, row['protocol'], [], now=NOW)
    changed.update(version='wrong-protocol-envelope', training_sources_sha256=store.sha([]),
                   evaluation_protocol_sha256='f' * 64)
    with pytest.raises(ValueError, match='source|protocol'):
        store.register_candidate(changed, row['protocol'], [], now=NOW)


def test_archive_publish_is_atomic_when_write_or_fsync_is_interrupted(monkeypatch):
    original = store.os.fsync
    calls = []
    def interrupted(fd):
        calls.append(fd)
        raise OSError('simulated interruption before archive publication')
    monkeypatch.setattr(store.os, 'fsync', interrupted)
    with pytest.raises(OSError, match='interruption'):
        proposal()
    assert list((store.RESEARCH_ROOT / 'code').glob('*.py')) == []
    assert db.q1('SELECT count(*) n FROM nba_totals_stage_candidates')['n'] == 0
    monkeypatch.setattr(store.os, 'fsync', original)
    assert proposal()['version'] == 'candidate-a'


def test_candidate_physical_seal_is_after_fitting_and_archiving(monkeypatch):
    clock = [NOW]
    original = store.archive_code
    def archive(catalog):
        result = original(catalog)
        clock[0] = NOW + 600
        return result
    monkeypatch.setattr(store, 'archive_code', archive)
    monkeypatch.setattr(store.time, 'time', lambda: clock[0])
    row = proposal(now=NOW)
    assert row['created_at'] == NOW + 600
    with pytest.raises(ValueError, match='chronolog|seal'):
        freeze(row, 'during-fit', now=NOW+60)


def _own_job(version, owner='owner', persisted_owner=None, other_detail=None):
    from edgeterminal import learning_worker
    learning_worker.ensure_schema()
    job = learning_worker.enqueue('NBA_TOTAL', 'diagnostics', now=NOW)
    detail = dict(other_detail or {}, stage_candidate_id=version)
    db.ex("UPDATE learning_jobs SET state='running',owner=?,started_at=?,detail=? WHERE id=?",
          (persisted_owner or owner, NOW, store.encoded(detail), job['id']))
    return dict(job, owner=owner, started_at=NOW)


def test_abandon_clears_only_owned_candidate_checkpoint_and_retry_does_not_reuse_it():
    from edgeterminal import learning_worker
    row = proposal()
    sealed = reserve(row)
    job = _own_job(row['version'], other_detail={'preserve': 'value'})
    token = learning_worker._CURRENT_JOB.set(job)
    try:
        assert store.abandon(row['version'], 'permanent source correction', now=NOW+8000)
    finally:
        learning_worker._CURRENT_JOB.reset(token)
    import json
    detail = json.loads(db.q1('SELECT detail FROM learning_jobs WHERE id=?', (job['id'],))['detail'])
    assert detail == {'preserve': 'value'}
    learning_worker._finish(job, job['owner'], 'failed', {'status': 'error'}, .1, .1)
    retry = learning_worker.enqueue('NBA_TOTAL', 'diagnostics', now=NOW+9000)
    assert 'stage_candidate_id' not in json.loads(retry['detail'])
    assert store.pending() is None
    assert store.verify_reservation(sealed)['game_ids'] == sealed['game_ids']


def test_abandon_ownership_change_rolls_back_event_and_checkpoint():
    from edgeterminal import learning_worker
    row = proposal()
    sealed = reserve(row)
    job = _own_job(row['version'], persisted_owner='new-owner')
    token = learning_worker._CURRENT_JOB.set(job)
    try:
        with pytest.raises(ValueError, match='ownership'):
            store.abandon(row['version'], 'invalid cohort', now=NOW+8000)
    finally:
        learning_worker._CURRENT_JOB.reset(token)
    assert db.q1("SELECT 1 FROM nba_totals_stage_model_events WHERE kind='abandoned'") is None
    assert store.pending()['version'] == row['version']
    assert store.verify_reservation(sealed)['game_ids'] == sealed['game_ids']


def test_abandon_cannot_overwrite_a_different_owned_checkpoint():
    from edgeterminal import learning_worker
    row = proposal()
    other = proposal('other-candidate')
    job = _own_job(other['version'])
    token = learning_worker._CURRENT_JOB.set(job)
    try:
        with pytest.raises(ValueError, match='candidate.*changed'):
            store.abandon(row['version'], 'invalid cohort', now=NOW+8000)
    finally:
        learning_worker._CURRENT_JOB.reset(token)


def test_warm_capture_guard_uses_only_selects_without_schema_or_writer_work():
    from edgeterminal import nba_totals_stage_outer
    store.ensure_schema()
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        assert not nba_totals_stage_outer.needs_inputs()
        store.ensure_schema()
    finally:
        db.conn().set_trace_callback(None)
    assert statements
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements), statements


def test_outer_dates_consumed_under_v1_cannot_be_reused_by_new_contract():
    row=proposal(); frozen=freeze(row)
    db.ex('CREATE TABLE nba_totals_reserved_dates(date_et TEXT PRIMARY KEY,reservation_id TEXT)')
    db.ex('CREATE TABLE nba_totals_reserved_games(game_id TEXT PRIMARY KEY,reservation_id TEXT)')
    db.ex('INSERT INTO nba_totals_reserved_dates VALUES(?,?)',(frozen['date_et'],'legacy'))
    with pytest.raises(ValueError,match='legacy contract'):
        reserve(row,[frozen])


@pytest.mark.parametrize('order',[('legacy','staged'),('staged','legacy')])
def test_legacy_and_staged_activation_guards_coexist_in_both_schema_orders(isolated,monkeypatch,order):
    from edgeterminal import nba_totals_store as legacy
    db._conn.close();db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{isolated / ("-".join(order)+".db")}')
    for name in order:
        (legacy if name=='legacy' else store).ensure_schema()
    for table in ('nba_totals_model_events','nba_totals_stage_model_events'):
        with pytest.raises(sqlite3.DatabaseError,match='publication proof'):
            db.ex(f'INSERT INTO {table}(version,created_at,kind,detail) VALUES(?,?,?,?)',
                  (None,NOW,'activated','{}'))
    assert db.q1("SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name='nba_totals_activation_gate'")['tbl_name']=='nba_totals_model_events'
    assert db.q1("SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name='nba_totals_stage_activation_gate'")['tbl_name']=='nba_totals_stage_model_events'
