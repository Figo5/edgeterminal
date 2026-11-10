"""Explicit, append-only release authorizations for sealed learning contexts.

Only the reviewed deployment operator registers a release after its exact Git
commit is installed under idle worker locks. Capture readers can consume that
authorization, but cannot authorize arbitrary current code or rewrite a bank.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from . import db
from .config import settings

CONTRACT = 'approved-learning-code-context-v1'
BASE_COMMIT = '4c691b64d882565c044b91d3858705cd518f7ce5'
ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {'NBA_TOTAL', 'NFL_SPREAD'}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _catalog(value):
    if not isinstance(value, dict) or not 0 < len(value) <= 128:
        raise ValueError('learning context requires a bounded code catalog')
    for name, digest in value.items():
        if (not isinstance(name, str) or not re.fullmatch(r'edgeterminal/(?:[a-z_]+/)*[a-z_]+\.py', name)
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('learning context code path/hash is invalid')
    return value


def _manifest(value):
    keys = {'schema', 'base_commit', 'target_commit', 'approved_patch_sha256', 'components'}
    if not isinstance(value, dict) or set(value) != keys or value['schema'] != CONTRACT:
        raise ValueError('learning context release contract is invalid')
    if (value['base_commit'] != BASE_COMMIT or
            not isinstance(value['target_commit'], str) or not re.fullmatch('[0-9a-f]{40}', value['target_commit']) or
            not isinstance(value['approved_patch_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['approved_patch_sha256'])):
        raise ValueError('learning context release identity is invalid')
    components = value['components']
    if not isinstance(components, dict) or not components or not set(components) <= COMPONENTS:
        raise ValueError('learning context component is invalid')
    for group in components.values():
        if not isinstance(group, dict) or set(group) != {'before', 'after'}:
            raise ValueError('learning context catalog pair is invalid')
        _catalog(group['before']); _catalog(group['after'])
        if group['before'] == group['after']:
            raise ValueError('learning context requires a real approved code change')
    return value


def _git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=10)


def _verify_release_commit(manifest):
    """Operator-only verification; no capture path executes Git or writes files."""
    if (_git('rev-parse', 'HEAD').decode().strip() != manifest['target_commit'] or
            _git('rev-parse', manifest['target_commit'] + '^').decode().strip() != BASE_COMMIT):
        raise ValueError('learning context target is not the exact approved release parent')
    patch = _git('diff', '--binary', BASE_COMMIT, manifest['target_commit'])
    if hashlib.sha256(patch).hexdigest() != manifest['approved_patch_sha256']:
        raise ValueError('learning context approved release patch changed')
    archive = {}
    for group in manifest['components'].values():
        for catalog, commit in ((group['before'], BASE_COMMIT), (group['after'], manifest['target_commit'])):
            for name, digest in catalog.items():
                body = _git('show', commit + ':' + name)
                if hashlib.sha256(body).hexdigest() != digest:
                    raise ValueError('learning context approved Git blob mismatch')
                if commit == BASE_COMMIT:
                    if name in archive and archive[name] != body:
                        raise ValueError('learning context base catalogs conflict')
                    archive[name] = body
    names={name for group in manifest['components'].values() for name in group['after']}
    if _git('diff', 'HEAD', '--name-only', '--', *sorted(names)).strip():
        raise ValueError('learning context deployed code differs from approved commit')
    return archive


def _root():
    return settings.db_path.parent / 'research/learning_code_context'


def _archive_path(relative):
    if not isinstance(relative, str) or not re.fullmatch(r'code/[0-9a-f]{64}\.py', relative):
        raise ValueError('learning context archive path is invalid')
    path = _root() / relative
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('learning context archive symlinks are forbidden')
    return path


def _read_archive(relative):
    path = _archive_path(relative)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            raise ValueError('learning context archive must be bounded regular code')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return stream.read(2_000_001)
    finally:
        os.close(fd)


def _save_archive(body):
    digest = hashlib.sha256(body).hexdigest()
    relative = 'code/' + digest + '.py'
    path = _archive_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _read_archive(relative) != body:
            raise ValueError('learning context existing code archive changed')
        return relative
    temporary = path.parent / ('.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if _read_archive(relative) != body:
                raise ValueError('learning context competing code archive changed')
        directory=os.open(str(path.parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return relative


def ensure_schema():
    with db.tx() as con:
        con.execute('CREATE TABLE IF NOT EXISTS learning_code_authorizations ('
                    'authorization_id TEXT PRIMARY KEY,registered_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        for action in ('UPDATE', 'DELETE'):
            con.execute(f'CREATE TRIGGER IF NOT EXISTS learning_code_authorizations_no_{action.lower()} '
                        f'BEFORE {action} ON learning_code_authorizations BEGIN '
                        "SELECT RAISE(ABORT,'learning code authorizations are append-only'); END")


def _read(row):
    payload = json.loads(row['payload'])
    if not isinstance(payload,dict) or set(payload)!={'manifest','archive'} or sha(payload)!=row['sha256']:
        raise ValueError('learning context authorization seal changed')
    manifest = _manifest(payload['manifest'])
    if sha(manifest) != row['authorization_id']:
        raise ValueError('learning context authorization seal changed')
    names = {name: digest for group in manifest['components'].values() for name, digest in group['before'].items()}
    if set(payload['archive']) != set(names):
        raise ValueError('learning context authorization archive catalog changed')
    for name, relative in payload['archive'].items():
        if hashlib.sha256(_read_archive(relative)).hexdigest() != names[name]:
            raise ValueError('learning context authorization archive hash changed')
    return dict(manifest, authorization_id=row['authorization_id'], archive=payload['archive'],
                registered_at=row['registered_at'], authorization_sha256=row['sha256'])


def register_release(manifest, *, now=None):
    """Guarded release operator only; not a web API or automatic fallback."""
    manifest = json.loads(encoded(_manifest(manifest)))
    stamp = time.time() if now is None else now
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        raise ValueError('learning context release time is invalid')
    bodies = _verify_release_commit(manifest)
    expected = {name: digest for group in manifest['components'].values() for name, digest in group['before'].items()}
    if set(bodies) != set(expected) or any(hashlib.sha256(body).hexdigest() != expected[name] for name, body in bodies.items()):
        raise ValueError('learning context verified base archive mismatch')
    archive = {name: _save_archive(body) for name, body in bodies.items()}
    payload = dict(manifest=manifest, archive=archive)
    ident = sha(manifest); digest = sha(payload)
    ensure_schema()
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT * FROM learning_code_authorizations WHERE authorization_id=?', (ident,)).fetchone()
        if old and (old['sha256'] != digest or old['payload'] != encoded(payload)):
            raise ValueError('learning context authorization identity conflict')
        if not old:
            con.execute('INSERT INTO learning_code_authorizations VALUES(?,?,?,?)',
                        (ident, stamp, encoded(payload), digest))
    row = db.q1('SELECT * FROM learning_code_authorizations WHERE authorization_id=?', (ident,))
    return _read(row)


def authorization(component, before, after):
    """Passive exact-context lookup. An absent authorization never blesses code."""
    if component not in COMPONENTS:
        raise ValueError('learning context component is invalid')
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_code_authorizations'"):
        return None
    for row in db.q('SELECT * FROM learning_code_authorizations ORDER BY registered_at,authorization_id'):
        auth = _read(row)
        group = auth['components'].get(component)
        if group and group['before'] == before and group['after'] == after:
            return auth
    return None
