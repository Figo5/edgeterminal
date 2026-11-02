"""Mac-origin challenger pointers must relocate to a trusted container directory, and only there.

Every fixture is synthetic: real SHA-256-sealed manifest/snapshot bytes, fake model bytes, and a
stubbed joblib.load / lab pipeline so shadow() never trains or calls a provider.
"""
import hashlib
import json
import multiprocessing
import os
import shutil
from pathlib import Path

import pytest

from edgeterminal import nfl_prop_challenger as model

TS = '1791894691663023000'
OTHER_TS = '1791894691663023001'
LEGACY_BASE = '/Users/giofiore/edgeterminal/data/research/prop_challenger'
LEGACY = f'{LEGACY_BASE}/{TS}'
MODEL_BYTES = b'synthetic-model-bytes-v1'
SENTINEL = b'synthetic freeze sentinel\n'
BATCH, FRESH = 'sealed-batch', 'fresh-batch'
REJECT = (ValueError, OSError)
ENTRIES = ('shadow', 'selected_rows')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.root = tmp_path / 'app'
        self.base = self.root / 'data/research/prop_challenger'
        self.artifact = self.base / TS
        self.artifact.mkdir(parents=True)
        monkeypatch.setattr(model, 'ROOT', self.root)
        monkeypatch.setattr(model, 'DIRECTORY', self.base)
        self.loads = []
        monkeypatch.setattr(model.joblib, 'load', lambda p, *a, **k: self.loads.append(p) or {})
        monkeypatch.setattr(model.lab, 'build_dataset', lambda *a, **k: ({}, [], {}))
        monkeypatch.setattr(model.lab, '_week3_features', lambda *a, **k: {})
        monkeypatch.setattr(model.lab, '_read_stats', lambda *a, **k: ([], None))
        monkeypatch.setattr(model.lab, '_pair_quotes', lambda *a, **k: ([], None))

        manifest = {'training_cutoff': [2026, 3], 'mode': 'shadow', 'models_sha256': sha(MODEL_BYTES),
                    'markets': {}, 'evaluation_2025': {}}
        self.manifest = json.dumps(manifest, sort_keys=True).encode()
        self.manifest_sha = sha(self.manifest)
        (self.artifact / 'manifest.json').write_bytes(self.manifest)
        (self.artifact / 'models.joblib').write_bytes(MODEL_BYTES)
        (self.artifact / 'freeze.sentinel').write_bytes(SENTINEL)
        row = {'game_id': 'game', 'player_id': 'p', 'market': 'receptions', 'side': 'over', 'line': 3.5,
               'quote_id': 1, 'projection': 5., 'baseline_projection': 4.,
               'probability_over': .6, 'probability_under': .4, 'confidence': .6}
        self.snapshot_path = self.artifact / f'shadow-{BATCH}.json'
        self.seal_path = self.snapshot_path.with_suffix('.sha256')
        self.write_snapshot(self.manifest_sha, row)
        self.expected = [{**row, 'selection_sha256': sha(self.snapshot_path.read_bytes()), 'selected_at': 1.}]
        self.use('legacy')

    def write_snapshot(self, manifest_sha, row):
        raw = json.dumps({'created_at': 1., 'model_manifest_sha256': manifest_sha,
                          'top_five': {'game': [row]}}, sort_keys=True).encode()
        self.snapshot_path.write_bytes(raw)
        self.seal_path.write_text(sha(raw))

    def pointer(self, path, digest=None):
        self.pointer_text(json.dumps({'path': path, 'sha256': digest or self.manifest_sha}))

    def pointer_text(self, text):
        (self.base / 'active.json').write_text(text)

    def use(self, style):
        self.pointer(LEGACY if style == 'legacy' else str(self.artifact))

    def rename_artifact(self, name):
        """Move the artifact to a different identity under the trusted base."""
        shutil.move(self.artifact, self.base / name)
        self.artifact = self.base / name
        self.snapshot_path = self.artifact / f'shadow-{BATCH}.json'
        self.seal_path = self.snapshot_path.with_suffix('.sha256')

    def tree(self):
        return {str(p.relative_to(self.tmp)): p.read_bytes() for p in sorted(self.tmp.rglob('*')) if p.is_file()}

    def call(self, entry):
        return model.shadow(FRESH, 2026, 6) if entry == 'shadow' else model.selected_rows()

    def symlink_out(self, target):
        paths = {'base': self.base, 'artifact': self.artifact, 'active': self.base / 'active.json',
                 'manifest': self.artifact / 'manifest.json', 'model': self.artifact / 'models.joblib',
                 'snapshot': self.snapshot_path, 'seal': self.seal_path,
                 'ancestor': self.root / 'data'}
        outside = self.tmp / 'outside' / target
        outside.parent.mkdir(parents=True)
        shutil.move(paths[target], outside)
        paths[target].symlink_to(outside, target_is_directory=outside.is_dir())

    def assert_rejected(self, entry):
        with pytest.raises(REJECT):
            self.call(entry)
        assert not self.loads, 'model must not be deserialized from a rejected artifact'


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# --- relocation succeeds -------------------------------------------------------------------

@pytest.mark.parametrize('style', ('native', 'legacy'))
@pytest.mark.parametrize('entry', ENTRIES)
def test_accepts_valid_pointer_and_preserves_every_byte(env, entry, style):
    env.use(style)
    before = env.tree()
    result = env.call(entry)
    assert env.tree() == before
    if entry == 'selected_rows':
        assert result == env.expected
    else:
        assert result == {'status': 'no_supported_pregame_quotes'}
        assert len(env.loads) == 1
        assert env.loads[0].getvalue() == MODEL_BYTES
    assert (LEGACY in (env.base / 'active.json').read_text()) == (style == 'legacy')


def test_relocated_selected_rows_identical_to_native_pointer(env):
    env.use('native')
    native = model.selected_rows()
    env.use('legacy')
    assert model.selected_rows() == native == env.expected


def test_relocated_shadow_finds_existing_snapshot_in_destination(env):
    env.use('legacy')
    before = env.tree()
    result = model.shadow(BATCH, 2026, 6)
    assert result['status'] == 'already_saved'
    assert Path(result['path']).resolve() == env.snapshot_path.resolve()
    assert env.tree() == before


@pytest.mark.parametrize('name', ('model', 'artifact', '42'))
@pytest.mark.parametrize('entry', ENTRIES)
def test_accepts_safe_local_artifact_names(env, entry, name):
    env.rename_artifact(name)
    env.pointer(str(env.artifact))
    result = env.call(entry)
    assert result == (env.expected if entry == 'selected_rows' else {'status': 'no_supported_pregame_quotes'})


# --- pointer path handling -----------------------------------------------------------------

NONCANONICAL = {
    'dotdot-in-legacy-base': lambda e: f'{LEGACY_BASE}/../prop_challenger/{TS}',
    'dotdot-escape-legacy': lambda e: f'{LEGACY_BASE}/../../../../etc/{TS}',
    'dot-segment': lambda e: f'{LEGACY_BASE}/./{TS}',
    'repeated-slash': lambda e: f'{LEGACY_BASE}//{TS}',
    'backslash': lambda e: LEGACY.replace('/', '\\'),
    'nul': lambda e: LEGACY + '\x00',
    'relative': lambda e: f'data/research/prop_challenger/{TS}',
    'nested-suffix': lambda e: LEGACY + '/nested',
    'nested-suffix-local': lambda e: f'{e.artifact}/nested',
    'dotdot-local': lambda e: f'{e.base}/../prop_challenger/{TS}',
    'dotdot-sibling-local': lambda e: f'{e.artifact}/../{TS}',
    'dot-local': lambda e: f'{e.base}/./{TS}',
    'parent-directory': lambda e: str(e.base),
}


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('path', NONCANONICAL.values(), ids=NONCANONICAL.keys())
def test_rejects_noncanonical_pointer_path(env, entry, path):
    env.pointer(path(env))
    env.assert_rejected(entry)


FOREIGN = {
    'other-user-home': f'/Users/someone/edgeterminal/data/research/prop_challenger/{TS}',
    'tmp-prefix': f'/tmp/edgeterminal/data/research/prop_challenger/{TS}',
    'wrong-subdirectory': f'/Users/giofiore/edgeterminal/data/research/other/{TS}',
    'wrong-checkout-name': f'/Users/giofiore/other/data/research/prop_challenger/{TS}',
    'wrong-timestamp': f'{LEGACY_BASE}/{OTHER_TS}',
    'prefix-trick': f'{LEGACY_BASE}x/{TS}',
    'legacy-18-digits': f'{LEGACY_BASE}/{TS[:-1]}',
    'legacy-20-digits': f'{LEGACY_BASE}/{TS}0',
    'legacy-nondigit': f'{LEGACY_BASE}/{TS[:-1]}x',
    'legacy-unicode-digits': f'{LEGACY_BASE}/' + '١' * 19,
    'legacy-trailing-newline': f'{LEGACY}\n',
    'legacy-safe-name': f'{LEGACY_BASE}/model',
}


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('path', FOREIGN.values(), ids=FOREIGN.keys())
def test_rejects_foreign_legacy_identity_even_with_matching_manifest_digest(env, entry, path):
    env.pointer(path)
    env.assert_rejected(entry)


@pytest.mark.parametrize('entry', ENTRIES)
def test_rejects_missing_destination_when_same_basename_exists_elsewhere(env, entry):
    elsewhere = env.tmp / 'elsewhere/data/research/prop_challenger'
    elsewhere.mkdir(parents=True)
    shutil.move(env.artifact, elsewhere / TS)
    env.assert_rejected(entry)
    env.pointer(str(elsewhere / TS))
    env.assert_rejected(entry)


@pytest.mark.parametrize('entry', ENTRIES)
def test_wrong_id_never_falls_back_to_another_artifact(env, entry):
    env.pointer(f'{LEGACY_BASE}/{OTHER_TS}')
    env.assert_rejected(entry)
    env.pointer(str(env.base / OTHER_TS))
    env.assert_rejected(entry)


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('field', ('path', 'sha256'))
def test_rejects_duplicate_pointer_keys(env, entry, field):
    fields = [('path', json.dumps(str(env.artifact))), ('sha256', json.dumps(env.manifest_sha))]
    fields.append(next(item for item in fields if item[0] == field))
    env.pointer_text('{' + ', '.join(f'{json.dumps(k)}: {v}' for k, v in fields) + '}')
    env.assert_rejected(entry)


def _pointer(**fields):
    return json.dumps({'path': LEGACY, 'sha256': sha(b'x'), **fields})


MALFORMED = {
    'not-json': 'nope',
    'list': '[]',
    'missing-sha': json.dumps({'path': LEGACY}),
    'missing-path': json.dumps({'sha256': sha(b'x')}),
    'extra-field': _pointer(extra=1),
    'path-not-string': _pointer(path=7),
    'path-null': _pointer(path=None),
    'sha-not-string': _pointer(sha256=7),
    'sha-short': _pointer(sha256='ab' * 31),
    'sha-long': _pointer(sha256='ab' * 33),
    'sha-uppercase': _pointer(sha256='AB' * 32),
    'sha-nonhex': _pointer(sha256='zz' * 32),
    'sha-newline': _pointer(sha256=sha(b'x') + '\n'),
}


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('text', MALFORMED.values(), ids=MALFORMED.keys())
def test_rejects_malformed_pointer(env, entry, text):
    env.pointer_text(text)
    env.assert_rejected(entry)


@pytest.mark.parametrize('entry', ENTRIES)
def test_rejects_uppercase_digest_of_valid_manifest(env, entry):
    env.pointer(LEGACY, digest=env.manifest_sha.upper())
    env.assert_rejected(entry)


# --- tampered evidence ---------------------------------------------------------------------

@pytest.mark.parametrize('style', ('native', 'legacy'))
@pytest.mark.parametrize('entry', ENTRIES)
def test_rejects_changed_manifest_hash(env, entry, style):
    env.pointer(LEGACY if style == 'legacy' else str(env.artifact), digest=sha(b'other manifest'))
    env.assert_rejected(entry)


@pytest.mark.parametrize('style', ('native', 'legacy'))
@pytest.mark.parametrize('entry', ENTRIES)
def test_rejects_changed_manifest_bytes(env, entry, style):
    env.use(style)
    (env.artifact / 'manifest.json').write_bytes(env.manifest + b' ')
    env.assert_rejected(entry)


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('contract', ({'training_cutoff': [2026, 4]}, {'mode': 'live'}),
                         ids=('cutoff', 'mode'))
def test_rejects_changed_training_contract_even_when_sealed(env, entry, contract):
    env.manifest = json.dumps({**json.loads(env.manifest), **contract}, sort_keys=True).encode()
    (env.artifact / 'manifest.json').write_bytes(env.manifest)
    env.pointer(LEGACY, digest=sha(env.manifest))
    env.assert_rejected(entry)


@pytest.mark.parametrize('style', ('native', 'legacy'))
def test_shadow_rejects_changed_model_before_joblib_load(env, style):
    env.use(style)
    (env.artifact / 'models.joblib').write_bytes(MODEL_BYTES + b'x')
    env.assert_rejected('shadow')


def _flip_snapshot(env):
    env.snapshot_path.write_bytes(env.snapshot_path.read_bytes().replace(b'3.5', b'9.5'))


def _bad_seal(env):
    env.seal_path.write_text(sha(b'other snapshot'))


def _other_manifest_identity(env):
    row = {k: v for k, v in env.expected[0].items() if k not in ('selection_sha256', 'selected_at')}
    env.write_snapshot(sha(b'other manifest'), row)


@pytest.mark.parametrize('style', ('native', 'legacy'))
@pytest.mark.parametrize('tamper', (_flip_snapshot, _bad_seal, _other_manifest_identity),
                         ids=('snapshot-bytes', 'seal', 'manifest-identity'))
def test_selected_rows_rejects_changed_snapshot_or_seal(env, tamper, style):
    env.use(style)
    tamper(env)
    with pytest.raises(REJECT):
        model.selected_rows()


@pytest.mark.parametrize('tamper', (_flip_snapshot, _bad_seal, _other_manifest_identity),
                         ids=('snapshot-bytes', 'seal', 'manifest-identity'))
def test_shadow_rejects_tampered_existing_snapshot_instead_of_already_saved(env, tamper):
    tamper(env)
    with pytest.raises(REJECT):
        model.shadow(BATCH, 2026, 6)


@pytest.mark.parametrize('batch', ('../escape', 'a/b', '','x\x00y', 'a\\b', '/abs'))
def test_shadow_rejects_unsafe_batch_id_without_writing(env, batch):
    before = env.tree()
    with pytest.raises(REJECT):
        model.shadow(batch, 2026, 6)
    assert env.tree() == before


# --- symlink escapes -----------------------------------------------------------------------

SYMLINKS = [(t, e) for t in ('base', 'ancestor', 'artifact', 'active', 'manifest') for e in ENTRIES]
SYMLINKS += [('model', 'shadow'), ('snapshot', 'selected_rows'), ('seal', 'selected_rows')]


@pytest.mark.parametrize('target,entry', SYMLINKS, ids=[f'{t}-{e}' for t, e in SYMLINKS])
def test_rejects_symlink_escape(env, target, entry):
    env.use('native')
    env.symlink_out(target)
    env.assert_rejected(entry)


@pytest.mark.parametrize('target', ('snapshot', 'seal'))
def test_shadow_rejects_symlinked_existing_snapshot_or_seal(env, target):
    env.use('native')
    env.symlink_out(target)
    with pytest.raises(REJECT):
        model.shadow(BATCH, 2026, 6)


@pytest.mark.parametrize('entry', ENTRIES)
def test_dangling_active_pointer_fails_closed(env, entry):
    (env.base / 'active.json').unlink()
    (env.base / 'active.json').symlink_to(env.tmp / 'missing')
    env.assert_rejected(entry)


def test_absent_active_pointer_is_not_trained(env):
    (env.base / 'active.json').unlink()
    assert model.shadow(FRESH, 2026, 6) == {'status': 'not_trained'}
    assert model.selected_rows() == []


def test_shadow_deserializes_only_the_bytes_already_verified(env, monkeypatch):
    """A path replacement after verification must not change bytes sent to joblib."""
    loaded = []
    def load(verified):
        (env.artifact / 'models.joblib').write_bytes(b'unverified replacement')
        loaded.append(verified.read() if hasattr(verified, 'read') else Path(verified).read_bytes())
        return {}
    monkeypatch.setattr(model.joblib, 'load', load)
    assert model.shadow(FRESH, 2026, 6) == {'status': 'no_supported_pregame_quotes'}
    assert loaded == [MODEL_BYTES]


@pytest.mark.skipif(not hasattr(os, 'mkfifo') or 'fork' not in multiprocessing.get_all_start_methods(),
                    reason='Target Mac/Linux verification requires FIFO and fork support')
@pytest.mark.parametrize('entry', ENTRIES)
def test_nonregular_manifest_is_rejected_without_blocking(env, entry):
    path = env.artifact / 'manifest.json'
    path.unlink()
    os.mkfifo(path)
    def reject_fifo():
        try:
            env.call(entry)
        except REJECT:
            return
        raise AssertionError('FIFO was accepted as model evidence')
    process = multiprocessing.get_context('fork').Process(target=reject_fifo)
    process.start()
    try:
        process.join(2)
        if process.is_alive():
            pytest.fail('Opening nonregular evidence blocked instead of rejecting it')
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


@pytest.mark.parametrize('entry', (*ENTRIES, 'report'))
def test_regular_file_base_fails_closed(env, entry):
    env.base.rename(env.tmp / 'parked-base')
    env.base.write_bytes(b'not a directory')
    with pytest.raises(REJECT):
        model.report() if entry == 'report' else env.call(entry)


def test_absent_base_preserves_untrained_status(env):
    env.base.rename(env.tmp / 'parked-base')
    assert model.selected_rows() == []
    assert model.shadow(FRESH, 2026, 6) == {'status': 'not_trained'}


def _untrusted_copy(env, destination):
    shutil.copytree(env.artifact, destination)
    snapshot = destination / env.snapshot_path.name
    data = json.loads(snapshot.read_bytes())
    data['top_five']['game'][0]['player_id'] = 'untrusted-substituted-player'
    raw = json.dumps(data, sort_keys=True).encode()
    snapshot.write_bytes(raw)
    snapshot.with_suffix('.sha256').write_text(sha(raw))


@pytest.mark.parametrize('target', ('artifact', 'base'))
@pytest.mark.parametrize('entry', ('selected_rows', 'existing-shadow'))
def test_parent_swap_cannot_replace_verified_artifact_bytes(env, monkeypatch, target, entry):
    """Substitute a same-manifest directory after trust checks, before manifest IO."""
    env.use('native')
    outside = env.tmp / 'outside-substitute'
    if target == 'artifact':
        _untrusted_copy(env, outside)
        path = env.artifact
    else:
        shutil.copytree(env.base, outside)
        untrusted = outside / TS
        shutil.rmtree(untrusted)
        _untrusted_copy(env, untrusted)
        path = env.base
    parked = env.tmp / 'parked-original'
    original_open = os.open
    switched = []
    observed_snapshots = []
    original_loads = model._loads
    def parse(raw):
        parsed = original_loads(raw)
        if isinstance(parsed, dict) and 'top_five' in parsed:
            observed_snapshots.append(parsed)
        return parsed
    def open_file(name, flags, *args, **kwargs):
        if os.fspath(name).endswith('manifest.json') and not switched:
            path.rename(parked)
            path.symlink_to(outside, target_is_directory=True)
            switched.append(True)
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(model.os, 'open', open_file)
    monkeypatch.setattr(model, '_loads', parse)
    try:
        result = model.selected_rows() if entry == 'selected_rows' else model.shadow(BATCH, 2026, 6)
    except REJECT:
        pass  # Failing closed is valid; reading a substituted artifact is not.
    else:
        if entry == 'selected_rows':
            assert result == env.expected
        else:
            assert result['status'] == 'already_saved'
        assert observed_snapshots
        assert all(s['top_five']['game'][0]['player_id'] == 'p' for s in observed_snapshots)
    assert switched, 'The race injection must run after artifact acquisition'


def test_selected_rows_cannot_silently_enumerate_a_swapped_dangling_parent(env, monkeypatch):
    env.use('native')
    inode = env.artifact.stat().st_ino
    original_scandir, original_listdir = os.scandir, os.listdir
    switched = []
    def should_swap(path):
        if switched:
            return False
        return os.fstat(path).st_ino == inode if isinstance(path, int) else Path(path) == env.artifact
    def swap():
        env.artifact.rename(env.tmp / 'parked-enumeration')
        env.artifact.symlink_to(env.tmp / 'does-not-exist', target_is_directory=True)
        switched.append(True)
    def scandir(path):
        if should_swap(path):
            swap()
        return original_scandir(path)
    def listdir(path):
        if should_swap(path):
            swap()
        return original_listdir(path)
    monkeypatch.setattr(model.os, 'scandir', scandir)
    monkeypatch.setattr(model.os, 'listdir', listdir)
    try:
        result = model.selected_rows()
    except REJECT:
        pass
    else:
        assert result == env.expected, 'A vanished path must not silently erase sealed selections'
    assert switched


def _prepare_fresh_shadow(env, monkeypatch):
    from edgeterminal import nfl_prop_availability
    manifest = json.loads(env.manifest)
    manifest['markets'] = {'receptions': {'usage_weight': 0., 'residuals': [0.] * 100}}
    manifest['evaluation_2025'] = {'receptions': {'offline_gate_passed': True}}
    env.manifest = json.dumps(manifest, sort_keys=True).encode()
    env.manifest_sha = sha(env.manifest)
    (env.artifact / 'manifest.json').write_bytes(env.manifest)
    env.use('native')
    features = {'targets_last3': 4., 'receptions_last5': 3., 'targets_last5': 4.}
    monkeypatch.setattr(model.joblib, 'load', lambda _: {'receptions': None})
    monkeypatch.setattr(model, 'predict', lambda *args: [3.])
    monkeypatch.setattr(model.lab, '_week3_features', lambda *args: {
        ('gsis-player', 'min'): {'player': {'position': 'WR'}, 'feature': features, 'opp_hist': []}})
    monkeypatch.setattr(model.lab, '_read_stats', lambda *args: ([{
        'season': '2026', 'week': '5', 'player_display_name': 'Player',
        'team': 'MIN', 'player_id': 'gsis-player'}], {}))
    monkeypatch.setattr(model.lab, '_eligible', lambda *args: True)
    monkeypatch.setattr(nfl_prop_availability, 'view', lambda *args: {'blocked': False})
    monkeypatch.setattr(model.lab, '_pair_quotes', lambda *args: ([{
        'id': 1, 'canonical_game_id': 'nfl-2026-reg-06-min-det', 'player_id': 'p', 'player_name': 'Player',
        'team': 'Minnesota Vikings', 'market': 'receptions', 'line': 2.5, 'side': 'over',
        'price': -110, 'sportsbook': 'fixture', 'retrieved_at': 10., 'kickoff_utc': 100.}], {}))


def test_fresh_shadow_fixture_reaches_verified_output_without_parent_swap(env, monkeypatch):
    _prepare_fresh_shadow(env, monkeypatch)
    monkeypatch.setattr(model.time, 'time', lambda: 20.)
    assert model.shadow(FRESH, 2026, 6)['status'] == 'saved'
    raw = (env.artifact / f'shadow-{FRESH}.json').read_bytes()
    assert (env.artifact / f'shadow-{FRESH}.sha256').read_text() == sha(raw)
    assert json.loads(raw)['top_five']['nfl-2026-reg-06-min-det'][0]['player_id'] == 'p'


@pytest.mark.parametrize('target', ('artifact', 'base'))
def test_shadow_fresh_output_and_seal_cannot_follow_substituted_parent(env, monkeypatch, target):
    _prepare_fresh_shadow(env, monkeypatch)
    outside = env.tmp / 'outside-output-substitute'
    source = env.artifact if target == 'artifact' else env.base
    shutil.copytree(source, outside)
    parked = env.tmp / 'parked-output-original'
    switched = []
    def now():
        if not switched:
            source.rename(parked)
            source.symlink_to(outside, target_is_directory=True)
            switched.append(True)
        return 20.
    monkeypatch.setattr(model.time, 'time', now)
    try:
        result = model.shadow(FRESH, 2026, 6)
    except REJECT:
        pass
    else:
        assert result['status'] == 'saved'
        trusted = parked if target == 'artifact' else parked / TS
        assert (trusted / f'shadow-{FRESH}.json').is_file()
        assert (trusted / f'shadow-{FRESH}.sha256').is_file()
    escaped = outside if target == 'artifact' else outside / TS
    assert switched
    assert not (escaped / f'shadow-{FRESH}.json').exists()
    assert not (escaped / f'shadow-{FRESH}.sha256').exists()


@pytest.mark.parametrize('entry', ('selected_rows', 'existing-shadow', 'fresh-shadow', 'missing-pointer',
                                  'changed-manifest', 'bad-pointer'))
def test_evidence_descriptors_close_on_every_exit(env, monkeypatch, entry):
    env.use('native')
    if entry == 'changed-manifest':
        (env.artifact / 'manifest.json').write_bytes(b'changed')
    elif entry == 'bad-pointer':
        env.pointer_text('{}')
    elif entry == 'missing-pointer':
        (env.base / 'active.json').unlink()
    opened = []
    original_open = os.open
    def open_file(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(model.os, 'open', open_file)
    try:
        if entry == 'existing-shadow':
            model.shadow(BATCH, 2026, 6)
        elif entry == 'fresh-shadow':
            model.shadow(FRESH, 2026, 6)
        else:
            model.selected_rows()
    except REJECT:
        assert entry in {'changed-manifest', 'bad-pointer'}
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_snapshot_timestamp_ties_preserve_original_directory_order(env, monkeypatch):
    first = env.snapshot_path.name
    other = env.artifact / 'shadow-a-tied.json'
    data = json.loads(env.snapshot_path.read_bytes())
    data['top_five']['game'][0]['player_id'] = 'later-listed-player'
    raw = json.dumps(data, sort_keys=True).encode()
    other.write_bytes(raw)
    other.with_suffix('.sha256').write_text(sha(raw))
    original_listdir = os.listdir
    inode = env.artifact.stat().st_ino
    def listdir(path='.'):
        if isinstance(path, int) and os.fstat(path).st_ino == inode:
            return [first, other.name]
        return original_listdir(path)
    monkeypatch.setattr(model.os, 'listdir', listdir)
    assert model.selected_rows() == env.expected
