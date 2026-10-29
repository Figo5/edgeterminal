#!/usr/bin/env python3
"""Consistent, hash-checked state archives for operator migration and restore.

No provider requests or model/grade changes. SQLite's online backup includes
committed WAL data. The shared worker lock protects matching filesystem
archives. For final cross-host cutover, stop BOTH local web and worker first.
This tool never overwrites an existing archive or nonempty destination.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tempfile
import time

ARCHIVES = ('data/research', 'data/nflverse', 'reports')
DATABASE = 'data/edgeterminal.db'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _regular_files(root):
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Archive evidence must not contain a symlink')
        if path.is_file():
            yield path
        elif not path.is_dir():
            raise ValueError('Archive evidence must contain only regular files')


def _quick_check(database):
    # Archives are closed standalone snapshots; immutable mode avoids creating
    # a new SHM sidecar while verifying their fixed bytes.
    con = sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed')
    finally:
        con.close()


def create(root, destination):
    root = Path(root).resolve()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Archive destination already exists')
    for relative in ARCHIVES:
        source = root / relative
        if source.is_symlink():
            raise ValueError('Archive source must not be a symlink')
        if destination.resolve().is_relative_to(source.resolve()):
            raise ValueError('Archive destination is inside a source path')
    database = root / DATABASE
    if not database.is_file() or database.is_symlink():
        raise ValueError('A regular source database is required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.state-archive-', dir=str(destination.parent)))
    try:
        with database.with_name(database.name + '-research.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output = staging / DATABASE
            output.parent.mkdir(parents=True)
            source = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=30)
            target = sqlite3.connect(str(output))
            try:
                source.backup(target, pages=256)
                # The archive is one standalone DB, never a DB/WAL/SHM file set.
                if target.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise ValueError('Backup did not become a standalone database')
                target.commit()
            finally:
                target.close()
                source.close()
            for suffix in ('-wal', '-shm'):
                # Only this closed temporary output is touched, never source WAL.
                output.with_name(output.name + suffix).unlink(missing_ok=True)
            _quick_check(output)
            for relative in ARCHIVES:
                folder = root / relative
                (staging / relative).mkdir(parents=True, exist_ok=True)
                if not folder.exists():
                    continue
                for original in _regular_files(folder):
                    if original.name.endswith(('-wal', '-shm', '-journal', '.tmp')):
                        raise ValueError('Unfinished or nested database files require operator review')
                    with original.open('rb') as stream:
                        if stream.read(16) == b'SQLite format 3\x00':
                            raise ValueError('Nested SQLite databases need a separate online backup')
                    copied = staging / original.relative_to(root)
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, copied)
            files = {str(p.relative_to(staging)): digest(p) for p in _regular_files(staging)}
            mtimes = {str(p.relative_to(staging)): p.stat().st_mtime_ns for p in _regular_files(staging)}
            commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True, timeout=10)
            manifest = {'schema': 1, 'created_at': time.time(), 'database': DATABASE,
                        'code_commit': commit.stdout.strip() if commit.returncode == 0 else None,
                        'files': files, 'mtime_ns': mtimes,
                        'note': 'Restore with the same committed code; stop the original writer before cloud cutover.'}
            (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
            for path in _regular_files(staging):
                os.chmod(path, 0o600)
            verify(staging)
            staging.rename(destination)
        return destination
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify(archive):
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_dir():
        raise ValueError('Archive must be a regular directory')
    manifest_path = archive / 'manifest.json'
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Invalid manifest')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 1 or manifest.get('database') != DATABASE:
        raise ValueError('Unsupported archive schema')
    files = manifest.get('files')
    if not isinstance(files, dict) or DATABASE not in files:
        raise ValueError('Archive is missing its database')
    for name, expected in files.items():
        if not isinstance(name, str):
            raise ValueError('Invalid archive path')
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts
                or not path.parts or str(path) != name
                or not (name == DATABASE or any(name.startswith(p + '/') for p in ARCHIVES))):
            raise ValueError('Invalid archive path')
        target = archive / name
        current = archive
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('Invalid symlink in archive path')
        if not target.is_file() or digest(target) != expected:
            raise ValueError('Archive digest mismatch')
    mtimes = manifest.get('mtime_ns')
    if (not isinstance(mtimes, dict) or set(mtimes) != set(files)
            or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 10**21 for v in mtimes.values())):
        raise ValueError('Archive file times are invalid')
    actual = {str(p.relative_to(archive)) for p in _regular_files(archive)} - {'manifest.json'}
    if actual != set(files):
        raise ValueError('Archive contains unlisted paths')
    _quick_check(archive / DATABASE)
    return {'ok': True, 'files': len(files), 'code_commit': manifest.get('code_commit')}


def restore(archive, destination):
    verify(archive)
    archive = Path(archive).resolve()
    manifest = json.loads((archive / 'manifest.json').read_text())
    destination = Path(destination).absolute()
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise ValueError('Restore destination must be empty')
    if destination.resolve().is_relative_to(archive):
        raise ValueError('Restore destination cannot be inside the archive')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.state-restore-', dir=str(destination.parent)))
    try:
        for original in _regular_files(archive):
            copied = staging / original.relative_to(archive)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, copied)
            name = str(original.relative_to(archive))
            if name in manifest['mtime_ns']:
                # Transport may change archive filesystem times. Restore the
                # declared source times used by point-in-time source receipts.
                stamp = manifest['mtime_ns'][name]
                os.utime(copied, ns=(stamp, stamp))
            copied.chmod(0o600)
        verify(staging)
        if destination.exists():
            destination.rmdir()
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'verify', 'restore'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'verify':
            result = verify(args.path)
        elif args.action == 'create':
            result = {'ok': True, 'archive': str(create(args.root, args.path))}
        else:
            if args.destination is None:
                parser.error('restore requires --destination')
            result = {'ok': True, 'restored': str(restore(args.path, args.destination))}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
