"""Separate append-only NBA totals protocols, prospective forecasts and evidence.

Candidates are sealed before any prospective prediction. Cohorts consume games
and entire Eastern dates permanently, before the guarded reader may see labels.
Only short ledger/checkpoint writes run in SQLite transactions.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid
from zoneinfo import ZoneInfo

from . import db
from .config import settings

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = None
_ready = None
ET = ZoneInfo('America/New_York')
CODE_NAMES = tuple('edgeterminal/' + name for name in (
    'nba_totals_model.py', 'nba_totals_data.py', 'nba_totals_store.py',
    'nba_totals_learning.py', 'nba_ops.py', 'nba_model.py', 'nba_data.py', 'nba_sources.py',
    'nba_identity.py', 'nba_game_context.py', 'nba_game_forecasts.py',
    'nba_spread_model.py', 'nba_spread_data.py', 'nba_spread_market.py', 'nba_store.py',
    'learning_publication.py', 'learning_worker.py', 'db.py', 'config.py', 'pricing.py',
    'analytics/nba.py', 'nba_model_manifest.py'))
TABLES = ('nba_totals_candidates', 'nba_totals_forecast_freezes', 'nba_totals_reservations',
          'nba_totals_reserved_games', 'nba_totals_reserved_dates', 'nba_totals_evaluations',
          'nba_totals_publication_proofs', 'nba_totals_model_events', 'nba_totals_runs')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def stamp(value=None):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('NBA totals time must be finite')
    return float(value)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('NBA totals requires an exact SHA-256 seal')
    return value


def ensure_schema():
    global _ready
    connection = db.conn()
    if _ready is connection:
        return
    statements = (
        'CREATE TABLE IF NOT EXISTS nba_totals_candidates (version TEXT PRIMARY KEY,created_at REAL NOT NULL,'
        'training_cutoff REAL NOT NULL,artifact TEXT NOT NULL,sha256 TEXT NOT NULL,protocol TEXT NOT NULL,'
        'protocol_sha256 TEXT NOT NULL,sources TEXT NOT NULL,sources_sha256 TEXT NOT NULL,code_archive TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nba_totals_forecast_freezes (freeze_id TEXT PRIMARY KEY,version TEXT NOT NULL '
        'REFERENCES nba_totals_candidates(version),game_id TEXT NOT NULL,date_et TEXT NOT NULL,'
        'forecast_sha256 TEXT NOT NULL,captured_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,'
        'UNIQUE(version,forecast_sha256))',
        'CREATE TABLE IF NOT EXISTS nba_totals_reservations (reservation_id TEXT PRIMARY KEY,version TEXT NOT NULL UNIQUE '
        'REFERENCES nba_totals_candidates(version),created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nba_totals_reserved_games (game_id TEXT PRIMARY KEY,reservation_id TEXT NOT NULL '
        'REFERENCES nba_totals_reservations(reservation_id),freeze_id TEXT NOT NULL UNIQUE '
        'REFERENCES nba_totals_forecast_freezes(freeze_id),date_et TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nba_totals_reserved_dates (date_et TEXT PRIMARY KEY,reservation_id TEXT NOT NULL '
        'REFERENCES nba_totals_reservations(reservation_id))',
        'CREATE TABLE IF NOT EXISTS nba_totals_evaluations (evaluation_id TEXT PRIMARY KEY,reservation_id TEXT NOT NULL UNIQUE '
        'REFERENCES nba_totals_reservations(reservation_id),created_at REAL NOT NULL,evaluation TEXT NOT NULL,'
        'evaluation_sha256 TEXT NOT NULL,state TEXT NOT NULL,state_sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nba_totals_publication_proofs (proof_id TEXT PRIMARY KEY,version TEXT NOT NULL UNIQUE '
        'REFERENCES nba_totals_candidates(version),reservation_id TEXT NOT NULL REFERENCES nba_totals_reservations(reservation_id),'
        'evaluation_id TEXT NOT NULL REFERENCES nba_totals_evaluations(evaluation_id),created_at REAL NOT NULL,'
        'payload TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nba_totals_model_events (id INTEGER PRIMARY KEY,version TEXT '
        'REFERENCES nba_totals_candidates(version),created_at REAL NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nba_totals_runs (id INTEGER PRIMARY KEY,started_at REAL NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL)',
    )
    with db.tx() as con:
        for sql in statements:
            con.execute(sql)
        con.execute("CREATE TRIGGER IF NOT EXISTS nba_totals_activation_gate BEFORE INSERT ON nba_totals_model_events "
            "WHEN NEW.kind='activated' AND NOT EXISTS (SELECT 1 FROM nba_totals_publication_proofs p "
            "JOIN nba_totals_evaluations e ON e.evaluation_id=p.evaluation_id "
            "JOIN nba_totals_candidates c ON c.version=p.version WHERE p.version=NEW.version "
            "AND json_extract(e.evaluation,'$.passed')=1 AND json_extract(e.evaluation,'$.pairs')>=60 "
            "AND json_extract(e.evaluation,'$.days')>=7 AND c.created_at<NEW.created_at) "
            "BEGIN SELECT RAISE(ABORT,'NBA totals activation requires sealed publication proof'); END")
        for table in TABLES:
            for action in ('UPDATE', 'DELETE'):
                con.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                            "BEGIN SELECT RAISE(ABORT,'NBA totals research is append-only'); END")
    _ready = connection  # Only a successfully committed initialization is cached.


def _research():
    return Path(RESEARCH_ROOT) if RESEARCH_ROOT is not None else settings.db_path.parent / 'research/nba_totals'


def _safe_path(root, relative, *, create=False):
    relative = str(relative)
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not relative or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ValueError('NBA totals archive path must be relative and contained')
    root = Path(root).absolute()
    # Reject symlinks in every existing ancestor, including the research root.
    for parent in reversed((root,) + tuple(root.parents)):
        if parent.exists() or parent.is_symlink():
            if parent.is_symlink() or not parent.is_dir():
                raise ValueError('NBA totals archive parent is a symlink or non-directory')
    if create:
        root.mkdir(parents=True, exist_ok=True)
    path = root.joinpath(*pure.parts)
    for parent in (path,) + tuple(path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('NBA totals archive symlink is forbidden')
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('NBA totals archive is not a regular file')
    return path


def _read(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            raise ValueError('NBA totals code must be a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            return handle.read(2_000_001)
    finally:
        os.close(fd)


def code_catalog():
    return {name: hashlib.sha256(_read(_safe_path(ROOT, name))).hexdigest() for name in CODE_NAMES}


def archive_code(catalog):
    result = {}
    for name, digest in sorted(catalog.items()):
        _digest(digest)
        body = _read(_safe_path(ROOT, name))
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError('NBA totals code changed before archival')
        relative = 'code/' + digest + '.py'
        path = _safe_path(_research(), relative, create=True)
        temporary = path.with_name('.' + digest + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Same-filesystem hardlink publishes a complete, fsynced file
                # atomically and cannot replace an existing immutable SHA name.
                os.link(str(temporary), str(path), follow_symlinks=False)
            except FileExistsError:
                if _read(path) != body:
                    raise ValueError('NBA totals immutable code archive collision')
            directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        result[name] = relative
    return result


def read_archived_code(relative):
    path = _safe_path(_research(), relative)
    body = _read(path)
    if not re.fullmatch(r'code/[0-9a-f]{64}\.py', str(relative)) or hashlib.sha256(body).hexdigest() != path.stem:
        raise ValueError('NBA totals archived code seal mismatch')
    return body


def _validate_artifact(value):
    from . import nba_totals_model
    nba_totals_model.validate_artifact(value)


def _checkpoint(con, version):
    from . import learning_worker
    learning_worker.checkpoint_candidate(con, version)


def candidate(version, *, verify_code=False):
    row = db.q1('SELECT * FROM nba_totals_candidates WHERE version=?', (version,))
    if not row:
        raise ValueError('NBA totals candidate is unavailable')
    for key in ('artifact', 'protocol', 'sources'):
        value = json.loads(row[key])
        digest = row['sha256' if key == 'artifact' else key + '_sha256']
        if sha(value) != digest:
            raise ValueError('NBA totals candidate seal mismatch')
        row[key] = value
    row['code_archive'] = json.loads(row['code_archive'])
    if (row['artifact'].get('training_sources_sha256') != row['sources_sha256']
            or row['artifact'].get('evaluation_protocol_sha256') != row['protocol_sha256']):
        raise ValueError('NBA totals source/protocol envelope seal mismatch')
    if row['training_cutoff'] != row['artifact']['training_cutoff']:
        raise ValueError('NBA totals cutoff seal mismatch')
    _validate_artifact(row['artifact'])
    if verify_code:
        if row['artifact']['code_sha256'] != code_catalog():
            raise ValueError('NBA totals serving code fingerprint mismatch')
        if set(row['code_archive']) != set(row['artifact']['code_sha256']):
            raise ValueError('NBA totals code archive catalog mismatch')
        for name, relative in row['code_archive'].items():
            if hashlib.sha256(read_archived_code(relative)).hexdigest() != row['artifact']['code_sha256'][name]:
                raise ValueError('NBA totals archived serving code mismatch')
    return row


def register_candidate(artifact, protocol, sources, *, now=None):
    ensure_schema()
    now = stamp(now)
    _validate_artifact(artifact)
    version = artifact.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', version):
        raise ValueError('invalid NBA totals candidate version')
    if stamp(artifact['training_cutoff']) >= now:
        raise ValueError('NBA totals candidate must be sealed after training cutoff')
    if (artifact.get('training_sources_sha256') != sha(sources)
            or artifact.get('evaluation_protocol_sha256') != sha(protocol)):
        raise ValueError('NBA totals source/protocol envelope seal mismatch')
    catalog = code_catalog()
    if artifact.get('code_sha256') != catalog:
        raise ValueError('NBA totals candidate code seal mismatch')
    archive = archive_code(catalog)  # File I/O stays outside the writer transaction.
    digest = sha(artifact)
    # A job's entry time is not the physical seal time after fitting/archival.
    sealed_at = max(now, stamp())
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT * FROM nba_totals_candidates WHERE version=?', (version,)).fetchone()
        if old:
            if (old['sha256'], old['protocol_sha256'], old['sources_sha256']) != (digest, sha(protocol), sha(sources)):
                raise ValueError('NBA totals immutable candidate version conflict')
        else:
            con.execute('INSERT INTO nba_totals_candidates VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (version, sealed_at, artifact['training_cutoff'], encoded(artifact), digest,
                         encoded(protocol), sha(protocol), encoded(sources), sha(sources), encoded(archive)))
        _checkpoint(con, version)
    return candidate(version)


def active_event():
    return db.q1("SELECT * FROM nba_totals_model_events WHERE kind IN ('activated','rollback') ORDER BY id DESC LIMIT 1")


def pending():
    row = db.q1('SELECT c.version FROM nba_totals_candidates c WHERE NOT EXISTS '
        '(SELECT 1 FROM nba_totals_model_events e WHERE e.version=c.version AND e.kind IN '
        "('activated','rejected','abandoned')) ORDER BY c.created_at,c.version LIMIT 1")
    return candidate(row['version']) if row else None


def freeze_forecast(version, prediction, *, now=None):
    now = stamp(now)
    row = candidate(version)
    payload = dict(prediction)
    tipoff, forecast_at = stamp(payload['tipoff']), stamp(payload['forecast_at'])
    date = datetime.fromtimestamp(tipoff, ET).date().isoformat()
    if payload.get('date_et') != date or date <= row['protocol']['excluded_through']:
        raise ValueError('NBA totals validation must be prospective after inspected dates')
    if not row['training_cutoff'] < row['created_at'] <= forecast_at <= now < tipoff:
        raise ValueError('NBA totals pretip prediction chronology precedes seal or follows tipoff')
    for key in ('forecast_sha256', 'input_sha256'):
        _digest(payload.get(key))
    for key in ('baseline_total', 'candidate_total'):
        stamp(payload[key])
    if payload.get('saved_total_line') is not None:
        stamp(payload['saved_total_line'])
        for key in ('baseline_probability', 'candidate_probability'):
            if not 0 <= stamp(payload[key]) <= 1:
                raise ValueError('NBA totals probability outside zero-one')
    payload.update(candidate_version=version, candidate_sha256=row['sha256'], protocol_sha256=row['protocol_sha256'])
    digest = sha(payload)
    ident = sha([version, payload['forecast_sha256']])
    with db.tx() as con:
        old = con.execute('SELECT * FROM nba_totals_forecast_freezes WHERE freeze_id=?', (ident,)).fetchone()
        if old and old['sha256'] != digest:
            raise ValueError('NBA totals immutable forecast freeze conflict')
        if not old:
            con.execute('INSERT INTO nba_totals_forecast_freezes VALUES(?,?,?,?,?,?,?,?)',
                        (ident, version, str(payload['game_id']), date, payload['forecast_sha256'], now, encoded(payload), digest))
    return dict(payload, freeze_id=ident)


def freezes(version):
    return [dict(json.loads(r['payload']), freeze_id=r['freeze_id']) for r in db.q(
        'SELECT * FROM nba_totals_forecast_freezes WHERE version=? ORDER BY captured_at,freeze_id', (version,))]


def reserve(version, games, universe, *, now=None):
    now = stamp(now)
    row = candidate(version, verify_code=True)
    full = {str(day): sorted(str(g) for g in gids) for day, gids in sorted(universe.items())}
    wanted = {}
    verified = []
    for supplied in games:
        frozen = db.q1('SELECT * FROM nba_totals_forecast_freezes WHERE freeze_id=?', (supplied.get('freeze_id'),))
        if not frozen or frozen['version'] != version:
            raise ValueError('NBA totals requires persisted forecast freeze')
        payload = json.loads(frozen['payload'])
        if sha(payload) != frozen['sha256'] or dict(payload, freeze_id=frozen['freeze_id']) != supplied:
            raise ValueError('NBA totals forecast freeze seal mismatch')
        if payload['tipoff'] >= now:
            raise ValueError('NBA totals cannot reserve before the entire date has played')
        wanted.setdefault(payload['date_et'], []).append(str(payload['game_id']))
        verified.append(supplied)
    if not games or any(len(g) != len(set(g)) for g in wanted.values()) or {d: sorted(g) for d, g in wanted.items()} != full:
        raise ValueError('NBA totals reservation requires the whole supported date universe')
    payload = dict(candidate_version=version, candidate_sha256=row['sha256'], protocol=row['protocol'],
                   protocol_sha256=row['protocol_sha256'], game_ids=sorted(g['game_id'] for g in verified),
                   date_ids=sorted(full), universe_game_ids_by_date=full,
                   games=sorted(verified, key=lambda g: g['game_id']))
    ident = sha(payload)
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT reservation_id FROM nba_totals_reservations WHERE version=?', (version,)).fetchone()
        if old:
            if old['reservation_id'] != ident:
                raise ValueError('NBA totals candidate already owns a different immutable reservation')
        else:
            con.execute('INSERT INTO nba_totals_reservations VALUES(?,?,?,?,?)', (ident, version, now, encoded(payload), ident))
            for game in verified:
                con.execute('INSERT INTO nba_totals_reserved_games VALUES(?,?,?,?)',
                            (game['game_id'], ident, game['freeze_id'], game['date_et']))
            for day in full:
                con.execute('INSERT INTO nba_totals_reserved_dates VALUES(?,?)', (day, ident))
        _checkpoint(con, version)
    return verify_reservation(ident)


def reservation_for(version):
    row = db.q1('SELECT reservation_id FROM nba_totals_reservations WHERE version=?', (version,))
    return verify_reservation(row['reservation_id']) if row else None


def verify_reservation(reservation):
    ident = reservation.get('reservation_id') if isinstance(reservation, dict) else reservation
    row = db.q1('SELECT * FROM nba_totals_reservations WHERE reservation_id=?', (ident,))
    if not row:
        raise ValueError('NBA totals reservation is unavailable')
    value = json.loads(row['payload'])
    if sha(value) != row['sha256'] or row['sha256'] != ident:
        raise ValueError('NBA totals reservation seal mismatch')
    value.update(reservation_id=ident, created_at=row['created_at'])
    if isinstance(reservation, dict) and value != reservation:
        raise ValueError('NBA totals reservation input differs from persisted seal')
    cand = candidate(value['candidate_version'])
    if cand['sha256'] != value['candidate_sha256'] or cand['protocol_sha256'] != value['protocol_sha256']:
        raise ValueError('NBA totals reservation candidate binding mismatch')
    games = db.q('SELECT * FROM nba_totals_reserved_games WHERE reservation_id=? ORDER BY game_id', (ident,))
    dates = db.q('SELECT date_et FROM nba_totals_reserved_dates WHERE reservation_id=? ORDER BY date_et', (ident,))
    if [r['game_id'] for r in games] != value['game_ids'] or [r['date_et'] for r in dates] != value['date_ids']:
        raise ValueError('NBA totals reservation consumption mismatch')
    for game in value['games']:
        frozen = db.q1('SELECT payload,sha256 FROM nba_totals_forecast_freezes WHERE freeze_id=?', (game['freeze_id'],))
        expected = dict(game)
        expected.pop('freeze_id')
        if not frozen or frozen['sha256'] != sha(expected) or frozen['payload'] != encoded(expected):
            raise ValueError('NBA totals reserved forecast seal mismatch')
    return value


validate_reservation = verify_reservation


def record_evaluation(reservation, evaluation, state, *, now=None):
    sealed = verify_reservation(reservation)
    ident = sha([sealed['reservation_id'], evaluation, state])
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT evaluation_id FROM nba_totals_evaluations WHERE reservation_id=?',
                          (sealed['reservation_id'],)).fetchone()
        if old and old['evaluation_id'] != ident:
            raise ValueError('NBA totals cohort already evaluated under its immutable protocol')
        if not old:
            con.execute('INSERT INTO nba_totals_evaluations VALUES(?,?,?,?,?,?,?)',
                        (ident, sealed['reservation_id'], stamp(now), encoded(evaluation), sha(evaluation), encoded(state), sha(state)))
        _checkpoint(con, sealed['candidate_version'])
    return evaluation_for(sealed['candidate_version'])


def evaluation_for(version):
    row = db.q1('SELECT e.* FROM nba_totals_evaluations e JOIN nba_totals_reservations r '
                'ON r.reservation_id=e.reservation_id WHERE r.version=?', (version,))
    if row:
        for key in ('evaluation', 'state'):
            value = json.loads(row[key])
            if sha(value) != row[key + '_sha256']:
                raise ValueError('NBA totals evaluation seal mismatch')
            row[key] = value
    return row



def abandon(version, reason, *, now=None):
    """Terminal integrity failure preserves every spent game/date and receipt."""
    from . import learning_worker
    ensure_schema()
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        if not con.execute('SELECT 1 FROM nba_totals_candidates WHERE version=?', (version,)).fetchone():
            return False
        old = con.execute("SELECT kind FROM nba_totals_model_events WHERE version=? AND kind IN "
                          "('activated','rejected','abandoned') ORDER BY id DESC LIMIT 1", (version,)).fetchone()
        if old and old['kind'] != 'abandoned':
            return False
        job = learning_worker._CURRENT_JOB.get()
        if job is not None:
            if job['component'] != 'NBA_TOTAL' or job['kind'] not in ('diagnostics', 'retuning'):
                raise ValueError('only the owning totals job can abandon its checkpoint')
            prior = con.execute("SELECT detail FROM learning_jobs WHERE id=? AND owner=? AND state='running'",
                                (job['id'], job['owner'])).fetchone()
            if prior is None:
                raise ValueError('learning checkpoint ownership changed')
            if json.loads(prior['detail']).get('candidate_id') not in (None, version):
                raise ValueError('NBA totals terminal checkpoint candidate changed')
        _checkpoint(con, version)
        if not old:
            con.execute('INSERT INTO nba_totals_model_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
                        (version, max(stamp(now), stamp()), 'abandoned', encoded(dict(reason=str(reason)[:250]))))
        job = learning_worker._CURRENT_JOB.get()
        if job is not None:
            if job['component'] != 'NBA_TOTAL' or job['kind'] not in ('diagnostics', 'retuning'):
                raise ValueError('only the owning totals job can abandon its checkpoint')
            row = con.execute("SELECT detail FROM learning_jobs WHERE id=? AND owner=? AND state='running'",
                              (job['id'], job['owner'])).fetchone()
            if row is None:
                raise ValueError('learning checkpoint ownership changed')
            detail = json.loads(row['detail'])
            if detail.get('candidate_id') != version:
                raise ValueError('NBA totals terminal checkpoint candidate changed')
            detail.pop('candidate_id')
            con.execute('UPDATE learning_jobs SET detail=? WHERE id=? AND owner=?',
                        (encoded(detail), job['id'], job['owner']))
    return True

def record_run(now, status, detail):
    ensure_schema()
    result = dict(detail, status=status, ok=status != 'error')
    result.setdefault('message', str(result.get('reason', status)).replace('_', ' '))
    db.ex('INSERT INTO nba_totals_runs(started_at,status,detail) VALUES(?,?,?)', (stamp(now), status, encoded(result)))
    return result
