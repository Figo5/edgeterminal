"""Consistent state migration preserves append-only ledgers and archive bytes."""
from pathlib import Path
import sqlite3

import pytest

from deploy import state_backup


def source(tmp_path):
    root = tmp_path / 'source'
    (root / 'data/research').mkdir(parents=True)
    (root / 'data/nflverse').mkdir()
    (root / 'reports').mkdir()
    (root / 'data/research/freeze.json').write_bytes(b'{"immutable":true}\n')
    (root / 'data/nflverse/source.csv').write_bytes(b'game,score\ng1,3\n')
    (root / 'reports/report.json').write_bytes(b'{"result":"won"}')
    con = sqlite3.connect(str(root / 'data/edgeterminal.db'))
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE ledger(id INTEGER PRIMARY KEY,payload TEXT)')
    con.execute('CREATE TRIGGER no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,"immutable"); END')
    con.execute('INSERT INTO ledger VALUES(1,"saved")')
    con.commit()
    return root, con


def test_online_backup_includes_committed_wal_and_restores_archive(tmp_path):
    root, con = source(tmp_path)
    archive = state_backup.create(root, tmp_path / 'archive')
    assert state_backup.verify(archive)['ok']
    assert not (archive / 'data/edgeterminal.db-wal').exists()
    assert not (archive / 'data/edgeterminal.db-shm').exists()
    target = tmp_path / 'restored'
    state_backup.restore(archive, target)
    restored = sqlite3.connect(str(target / 'data/edgeterminal.db'))
    assert restored.execute('SELECT payload FROM ledger').fetchone()[0] == 'saved'
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        restored.execute('DELETE FROM ledger')
    assert (target / 'data/research/freeze.json').read_bytes() == (root / 'data/research/freeze.json').read_bytes()
    assert (target / 'reports/report.json').read_bytes() == (root / 'reports/report.json').read_bytes()
    restored.close()
    con.close()


def test_tampering_prevents_restore_without_changing_destination(tmp_path):
    root, con = source(tmp_path)
    archive = state_backup.create(root, tmp_path / 'archive')
    (archive / 'data/research/freeze.json').write_text('tampered')
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='digest'):
        state_backup.restore(archive, target)
    assert not target.exists()
    con.close()


def test_restore_refuses_to_overwrite_live_state(tmp_path):
    root, con = source(tmp_path)
    archive = state_backup.create(root, tmp_path / 'archive')
    with pytest.raises(ValueError, match='empty'):
        state_backup.restore(archive, root)
    con.close()


def test_backup_rejects_symlinked_evidence(tmp_path):
    root, con = source(tmp_path)
    (root / 'data/research/escape').symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError, match='symlink'):
        state_backup.create(root, tmp_path / 'archive')
    assert not (tmp_path / 'archive').exists()
    con.close()


def test_manifest_traversal_rejected(tmp_path):
    import json
    root, con = source(tmp_path)
    archive = state_backup.create(root, tmp_path / 'archive')
    manifest = archive / 'manifest.json'
    data = json.loads(manifest.read_text())
    data['files']['../escape'] = '0' * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='path'):
        state_backup.verify(archive)
    con.close()


def test_backup_refuses_active_research_lock(tmp_path):
    import fcntl
    root, con = source(tmp_path)
    with (root / 'data/edgeterminal.db-research.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            state_backup.create(root, tmp_path / 'archive')
    con.close()


def test_backup_excludes_secrets_old_backups_and_runtime_logs(tmp_path):
    root, con = source(tmp_path)
    (root / '.env').write_text('NEVER_COPY=secret')
    (root / 'data/backups').mkdir()
    (root / 'data/backups/old.sqlite').write_bytes(b'old')
    (root / 'data/edgeterminal.log').write_text('private')
    archive = state_backup.create(root, tmp_path / 'archive')
    assert not (archive / '.env').exists()
    assert not (archive / 'data/backups').exists()
    assert not (archive / 'data/edgeterminal.log').exists()
    con.close()


def test_verify_rejects_unlisted_evidence(tmp_path):
    root, con = source(tmp_path)
    archive = state_backup.create(root, tmp_path / 'archive')
    (archive / 'data/research/unlisted').write_text('extra')
    with pytest.raises(ValueError, match='unlisted'):
        state_backup.verify(archive)
    con.close()


def test_restore_preserves_original_source_times_after_transport(tmp_path):
    import os
    root, con = source(tmp_path)
    evidence = root / 'data/research/freeze.json'
    stamp = 946684800123456789
    os.utime(evidence, ns=(stamp, stamp))
    archive = state_backup.create(root, tmp_path / 'archive')
    # An upload/download may keep bytes but replace file times.
    os.utime(archive / 'data/research/freeze.json', ns=(stamp + 1000000000, stamp + 1000000000))
    state_backup.restore(archive, tmp_path / 'restored')
    assert (tmp_path / 'restored/data/research/freeze.json').stat().st_mtime_ns == stamp
    con.close()


def test_backup_rejects_raw_nested_sqlite(tmp_path):
    root, con = source(tmp_path)
    nested = sqlite3.connect(str(root / 'data/research/nested.db'))
    nested.execute('CREATE TABLE extra(value TEXT)')
    nested.commit()
    nested.close()
    with pytest.raises(ValueError, match='Nested SQLite'):
        state_backup.create(root, tmp_path / 'archive')
    con.close()
