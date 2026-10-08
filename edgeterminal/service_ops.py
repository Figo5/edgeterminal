"""Local monitoring and consistent SQLite backups; no data-provider requests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from . import db, product_insights
from .config import ROOT, settings


def backup(directory: Path | None = None, now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    target = directory or ROOT / 'data/backups'
    target.mkdir(parents=True, exist_ok=True)
    destination = target / ('edgeterminal-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(stamp)) + '.sqlite')
    if destination.exists():
        raise ValueError('backup already exists for this timestamp')
    temporary = destination.with_suffix('.tmp')
    try:
        # An online backup includes committed WAL data. Never copy a live .db file.
        source = sqlite3.connect(str(settings.db_path), timeout=30)
        try:
            output = sqlite3.connect(str(temporary))
            try:
                source.backup(output, pages=256)
                if output.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('backup integrity check failed')
                manifest = {'created_at': stamp, 'database': destination.name,
                            'game_freezes': [dict(zip(('freeze_sha', 'season', 'week'), row)) for row in
                                             output.execute('SELECT freeze_sha,season,week FROM nfl_research_freezes')],
                            'prop_freezes': [r[0] for r in output.execute('SELECT freeze_sha FROM nfl_prop_research_freezes')],
                            'note': 'SQLite backup only. Copy the data/research, reports, and nflverse source archives separately before migrating.'}
            finally:
                output.close()
        finally:
            source.close()
        os.chmod(temporary, 0o600)
        digest = hashlib.sha256()
        with temporary.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        manifest['sha256'] = digest.hexdigest()
        temporary.replace(destination)
        manifest_path = destination.with_suffix('.json')
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        os.chmod(manifest_path, 0o600)
        db.kv_set('last_service_backup', {'created_at': stamp, 'path': str(destination), 'sha256': manifest['sha256']})
        # Retain three daily backups; only this utility's completed backups are pruned.
        for old in sorted(target.glob('edgeterminal-*.sqlite'), reverse=True)[3:]:
            if old.with_suffix('.json').exists():
                old.unlink()
                old.with_suffix('.json').unlink()
        return {'ok': True, 'path': str(destination), 'sha256': manifest['sha256']}
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def maintain_local() -> None:
    previous = db.kv_get('last_service_backup') or {}
    attempted = db.kv_get('last_backup_attempt') or {}
    stamp = time.time()
    if stamp - previous.get('created_at', 0) < 86400 or stamp - attempted.get('created_at', 0) < 6 * 3600:
        return
    db.kv_set('last_backup_attempt', {'created_at': stamp})
    try:
        backup(now=stamp)
    except Exception:
        db.kv_set('last_backup_health', {'ok': False, 'checked_at': stamp})
    else:
        db.kv_set('last_backup_health', {'ok': True, 'checked_at': stamp})


def main():
    parser = argparse.ArgumentParser(description='Read local service health or back up the database')
    parser.add_argument('action', choices=('status', 'backup'))
    args = parser.parse_args()
    print(json.dumps(backup() if args.action == 'backup' else product_insights.health(), sort_keys=True))


if __name__ == '__main__':
    main()
