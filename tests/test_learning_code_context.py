"""A reviewed release may close old learning context, never silently reseal it."""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from edgeterminal import db
from edgeterminal.config import settings


@pytest.fixture
def owned(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'context.db'))
    db.init()
    yield tmp_path
    db._conn.close(); db._conn = None


def test_no_unreviewed_code_context_is_accepted(owned):
    from edgeterminal import learning_code_context as context
    assert context.authorization('NBA_TOTAL', {'edgeterminal/a.py': 'a' * 64},
                                 {'edgeterminal/a.py': 'b' * 64}) is None


def fixture_release(context):
    old = {'edgeterminal/a.py': hashlib.sha256(b'old\n').hexdigest()}
    new = {'edgeterminal/a.py': hashlib.sha256(b'new\n').hexdigest(),
           'edgeterminal/learning_code_context.py': hashlib.sha256(b'context\n').hexdigest()}
    return dict(schema=context.CONTRACT, base_commit=context.BASE_COMMIT,
                target_commit='1' * 40, approved_patch_sha256=hashlib.sha256(b'release patch\n').hexdigest(),
                components={'NBA_TOTAL': dict(before=old, after=new)})


def test_operator_authorization_is_exact_append_only_and_idempotent(owned, monkeypatch):
    from edgeterminal import learning_code_context as context
    manifest = fixture_release(context)
    calls = []
    def checked(value):
        calls.append(deepcopy(value))
        return {'edgeterminal/a.py': b'old\n'}
    monkeypatch.setattr(context, '_verify_release_commit', checked)
    result = context.register_release(manifest, now=100.)
    assert calls == [manifest]
    again = context.register_release(manifest, now=200.)
    assert again == result
    group = manifest['components']['NBA_TOTAL']
    auth = context.authorization('NBA_TOTAL', group['before'], group['after'])
    assert auth['authorization_id'] == result['authorization_id']
    assert auth['base_commit'] == context.BASE_COMMIT
    assert context.authorization('NBA_TOTAL', group['before'],
                                 {**group['after'], 'edgeterminal/a.py': 'f' * 64}) is None
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        db.ex('UPDATE learning_code_authorizations SET payload=?', ('{}',))
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        db.ex('DELETE FROM learning_code_authorizations')


def test_authorization_requires_verified_commit_and_refuses_mutable_old_archive(owned, monkeypatch):
    from edgeterminal import learning_code_context as context
    manifest = fixture_release(context)
    monkeypatch.setattr(context, '_verify_release_commit', lambda _: {'edgeterminal/a.py': b'old\n'})
    context.register_release(manifest, now=100.)
    group = manifest['components']['NBA_TOTAL']
    auth = context.authorization('NBA_TOTAL', group['before'], group['after'])
    path = context._archive_path(next(iter(auth['archive'].values())))
    path.write_bytes(b'tampered\n')
    with pytest.raises(ValueError, match='archive'):
        context.authorization('NBA_TOTAL', group['before'], group['after'])


@pytest.mark.parametrize('alter', ['base', 'path', 'hash', 'component', 'empty'])
def test_malformed_release_does_not_create_authorization(owned, monkeypatch, alter):
    from edgeterminal import learning_code_context as context
    manifest = fixture_release(context)
    if alter == 'base': manifest['base_commit'] = '3' * 40
    if alter == 'path': manifest['components']['NBA_TOTAL']['before'] = {'../escape.py': 'a' * 64}
    if alter == 'hash': manifest['components']['NBA_TOTAL']['after']['edgeterminal/a.py'] = 'bad'
    if alter == 'component': manifest['components'] = {'UNTRUSTED': next(iter(manifest['components'].values()))}
    if alter == 'empty': manifest['components'] = {}
    monkeypatch.setattr(context, '_verify_release_commit', lambda _: pytest.fail('invalid manifest reached git'))
    with pytest.raises(ValueError): context.register_release(manifest, now=100.)


def test_release_commit_verifier_requires_exact_head_parent_and_blob_hashes(owned, monkeypatch):
    from edgeterminal import learning_code_context as context
    manifest = fixture_release(context)
    def git(*args):
        if args == ('rev-parse', 'HEAD'): return (manifest['target_commit'] + '\n').encode()
        if args == ('rev-parse', manifest['target_commit'] + '^'): return (context.BASE_COMMIT + '\n').encode()
        if args == ('diff', '--binary', context.BASE_COMMIT, manifest['target_commit']): return b'release patch\n'
        if args[:4] == ('diff', 'HEAD', '--name-only', '--'): return b''
        if args[0] == 'show':
            commit, name = args[1].split(':', 1)
            if commit == context.BASE_COMMIT: return b'old\n'
            return b'context\n' if name.endswith('learning_code_context.py') else b'new\n'
        raise AssertionError(args)
    monkeypatch.setattr(context, '_git', git)
    assert context._verify_release_commit(manifest) == {'edgeterminal/a.py': b'old\n'}
    altered = deepcopy(manifest)
    altered['components']['NBA_TOTAL']['after']['edgeterminal/a.py'] = 'a' * 64
    with pytest.raises(ValueError, match='blob'):
        context._verify_release_commit(altered)


@pytest.mark.parametrize('changed', ['head', 'parent', 'patch', 'working_tree'])
def test_operator_cannot_authorize_a_different_release(owned, monkeypatch, changed):
    from edgeterminal import learning_code_context as context
    manifest = fixture_release(context)
    def git(*args):
        if args == ('rev-parse', 'HEAD'):
            return (('3' * 40 if changed == 'head' else manifest['target_commit']) + '\n').encode()
        if args == ('rev-parse', manifest['target_commit'] + '^'):
            return (('3' * 40 if changed == 'parent' else context.BASE_COMMIT) + '\n').encode()
        if args[:2] == ('diff', '--binary'): return b'wrong\n' if changed == 'patch' else b'release patch\n'
        if args[:4] == ('diff', 'HEAD', '--name-only', '--'): return b'edgeterminal/a.py\n' if changed == 'working_tree' else b''
        if args[0] == 'show':
            commit, name = args[1].split(':', 1)
            return b'old\n' if commit == context.BASE_COMMIT else b'context\n' if name.endswith('learning_code_context.py') else b'new\n'
        raise AssertionError(args)
    monkeypatch.setattr(context, '_git', git)
    with pytest.raises(ValueError): context.register_release(manifest, now=100.)
    assert not db.q1("SELECT 1 FROM sqlite_master WHERE name='learning_code_authorizations'")


def test_archive_paths_are_contained_and_symlinks_cannot_substitute_code(owned, monkeypatch):
    from edgeterminal import learning_code_context as context
    with pytest.raises(ValueError, match='path'):
        context._archive_path('../outside.py')
    manifest = fixture_release(context)
    monkeypatch.setattr(context, '_verify_release_commit', lambda _: {'edgeterminal/a.py': b'old\n'})
    auth = context.register_release(manifest, now=100.)
    relative = next(iter(auth['archive'].values()))
    path = context._archive_path(relative)
    other = owned / 'other.py'; other.write_bytes(b'old\n')
    path.unlink(); path.symlink_to(other)
    group = manifest['components']['NBA_TOTAL']
    with pytest.raises(ValueError, match='symlink'):
        context.authorization('NBA_TOTAL', group['before'], group['after'])


def test_authorization_read_is_passive_and_schema_creation_is_operator_only(owned):
    from edgeterminal import learning_code_context as context
    before = db.conn().total_changes
    assert context.authorization('NFL_SPREAD', {}, {}) is None
    assert db.conn().total_changes == before
    assert not db.q1("SELECT 1 FROM sqlite_master WHERE name='learning_code_authorizations'")
