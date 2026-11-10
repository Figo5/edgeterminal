"""Append-only NFL spread stages, prospective freezes and publication proofs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid

from . import db
from .config import settings
from .nfl_spread_model import encoded, sha, number

ROOT = Path(__file__).resolve().parents[1]
CODE_NAMES = tuple('edgeterminal/' + n for n in ('nfl_spread_model.py','nfl_spread_store.py',
    'nfl_spread_data.py','nfl_spread_learning.py','nfl_game_sources.py','analytics/football.py',
    'analytics/ratings.py','nfl_market.py','pricing.py','nfl_identity.py','nfl_game_forecasts.py',
    'learning_worker.py','learning_publication.py','db.py','config.py','integrity.py','providers/nflverse.py','providers/normalize.py','nfl_weekly_preparation.py',
    'nfl_game_lines.py','learning_code_context.py'))
TABLES = ('nfl_spread_stages','nfl_spread_observations','nfl_spread_candidates',
    'nfl_spread_freezes','nfl_spread_reservations','nfl_spread_consumed_games',
    'nfl_spread_consumed_weeks','nfl_spread_evaluations','nfl_spread_proofs','nfl_spread_integrity','nfl_spread_events','nfl_spread_runs')


def ensure_schema():
    statements = (
        'CREATE TABLE IF NOT EXISTS nfl_spread_stages(id INTEGER PRIMARY KEY,parent_id INTEGER,created_at REAL NOT NULL,state TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_observations(snapshot_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,season INTEGER NOT NULL,week INTEGER NOT NULL,created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_candidates(version TEXT PRIMARY KEY,created_at REAL NOT NULL,artifact TEXT NOT NULL,sha256 TEXT NOT NULL,code_archive TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_freezes(freeze_id TEXT PRIMARY KEY,version TEXT NOT NULL,game_id TEXT NOT NULL,season INTEGER NOT NULL,week INTEGER NOT NULL,created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(version,game_id,created_at))',
        'CREATE TABLE IF NOT EXISTS nfl_spread_reservations(reservation_id TEXT PRIMARY KEY,version TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_consumed_games(game_id TEXT PRIMARY KEY,reservation_id TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_consumed_weeks(season INTEGER NOT NULL,week INTEGER NOT NULL,reservation_id TEXT NOT NULL,PRIMARY KEY(season,week))',
        'CREATE TABLE IF NOT EXISTS nfl_spread_evaluations(reservation_id TEXT PRIMARY KEY,created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_proofs(version TEXT PRIMARY KEY,reservation_id TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_integrity(id INTEGER PRIMARY KEY,version TEXT NOT NULL,created_at REAL NOT NULL,head_sha256 TEXT NOT NULL,passed INTEGER NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_events(id INTEGER PRIMARY KEY,version TEXT,created_at REAL NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_spread_runs(id INTEGER PRIMARY KEY,created_at REAL NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL)',
    )
    with db.tx() as con:
        for sql in statements: con.execute(sql)
        con.execute("CREATE TRIGGER IF NOT EXISTS nfl_spread_activation_gate BEFORE INSERT ON nfl_spread_events "
            "WHEN NEW.kind='activated' AND NOT EXISTS(SELECT 1 FROM nfl_spread_proofs p "
            "JOIN nfl_spread_evaluations e ON e.reservation_id=p.reservation_id "
            "WHERE p.version=NEW.version AND json_extract(e.payload,'$.passed')=1 "
            "AND json_extract(e.payload,'$.games')>=60 AND json_extract(e.payload,'$.weeks')>=6) "
            "BEGIN SELECT RAISE(ABORT,'NFL spread activation requires sealed proof'); END")
        for table in TABLES:
            for action in ('UPDATE','DELETE'):
                con.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                    "BEGIN SELECT RAISE(ABORT,'NFL spread ledger is append-only'); END")


def schema_ready():
    """SELECT-only readers never initialize a missing component ledger."""
    present={r['name'] for r in db.q("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'nfl_spread_%'")}
    return set(TABLES)<=present


def _table_exists(name):
    return db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)) is not None


def latest_stage():
    if not _table_exists('nfl_spread_stages'):return None
    row = db.q1('SELECT * FROM nfl_spread_stages ORDER BY id DESC LIMIT 1')
    if row:
        state = json.loads(row['state'])
        if sha(state) != row['sha256']: raise ValueError('NFL spread stage seal mismatch')
        row['state'] = state
    return row


def verify_stage_chain():
    """Verify the entire append-only parent chain before a release transition."""
    if not _table_exists('nfl_spread_stages'):
        return None
    parent=None; latest=None
    for row in db.q('SELECT * FROM nfl_spread_stages ORDER BY id'):
        state=json.loads(row['state'])
        if row['parent_id']!=parent or sha(state)!=row['sha256']:
            raise ValueError('NFL spread stage parent chain seal mismatch')
        parent=row['id']; latest=dict(row,state=state)
    return latest


def append_stage(parent_id, state, *, now=None):
    ensure_schema(); stamp = number(time.time() if now is None else now)
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT id FROM nfl_spread_stages ORDER BY id DESC LIMIT 1').fetchone()
        if (old['id'] if old else None) != parent_id:
            raise ValueError('NFL spread stage head ownership changed')
        if state.get('stage')=='context_changed':
            boundary=con.execute('SELECT MAX(id) FROM nfl_spread_runs').fetchone()[0] or 0
            state=dict(state,context_change=dict(state['context_change'],after_retuning_run_id=boundary))
        cur = con.execute('INSERT INTO nfl_spread_stages(parent_id,created_at,state,sha256) VALUES(?,?,?,?)',
            (parent_id, stamp, encoded(state), sha(state)))
        ident = cur.lastrowid
    return dict(id=ident, parent_id=parent_id, created_at=stamp, state=state, sha256=sha(state))


def _root():
    return settings.db_path.parent / 'research/nfl_spread'


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or PurePosixPath(relative).is_absolute() or any(p in ('','.','..') for p in relative.split('/')):
        raise ValueError('NFL spread path must be relative and contained')
    root = Path(root).absolute(); path = root.joinpath(*relative.split('/'))
    for parent in reversed((path,) + tuple(path.parents)):
        if (parent.exists() or parent.is_symlink()) and parent.is_symlink():
            raise ValueError('NFL spread symlink escape is forbidden')
    return path


def read_regular(path, cap=2_000_000):
    # Every ancestor is checked by safe_path before calling this helper.
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            raise ValueError('NFL spread artifact must be a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream: return stream.read(cap+1)
    finally: os.close(fd)


def code_catalog():
    return {name:hashlib.sha256(read_regular(safe_path(ROOT,name))).hexdigest() for name in CODE_NAMES}


def archive_code(catalog):
    result = {}
    for name, digest in catalog.items():
        if not re.fullmatch('[0-9a-f]{64}',str(digest)): raise ValueError('NFL spread code seal invalid')
        raw = read_regular(safe_path(ROOT,name))
        if hashlib.sha256(raw).hexdigest() != digest: raise ValueError('NFL spread source code changed')
        relative = 'code/' + digest + '.py'; path = safe_path(_root(), relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = safe_path(_root(), 'code/.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream: stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            try: os.link(temp,path,follow_symlinks=False)
            except FileExistsError:
                if read_regular(path) != raw: raise ValueError('NFL spread immutable code collision')
        finally: temp.unlink(missing_ok=True)
        result[name] = relative
    return result


def read_code(relative, digest):
    if not re.fullmatch('[0-9a-f]{64}',str(digest)) or relative != 'code/' + digest + '.py':
        raise ValueError('NFL spread archived code identity invalid')
    raw = read_regular(safe_path(_root(),relative))
    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError('NFL spread archived code hash conflict')
    return raw


def candidate(version, *, current_code=False):
    if not _table_exists('nfl_spread_candidates'):raise ValueError('NFL spread candidate ledger missing')
    row = db.q1('SELECT * FROM nfl_spread_candidates WHERE version=?',(version,))
    if not row: raise ValueError('NFL spread candidate missing')
    artifact = json.loads(row['artifact']); archive = json.loads(row['code_archive'])
    body=dict(artifact);body.pop('version',None)
    if sha(artifact) != row['sha256'] or artifact.get('version') != version or version!='nfl-spread-'+sha(body)[:32]:
        raise ValueError('NFL spread candidate seal mismatch')
    from . import nfl_spread_model as model
    model.validate_member(artifact['member'])
    if (artifact.get('kind')!=model.KIND or artifact.get('protocol')!=model.protocol()
            or artifact.get('formula_version')!=model.FORMULA_VERSION or not isinstance(artifact.get('input_policy_id'),str)
            or not artifact['input_policy_id'] or not 1<=number(artifact.get('sd_margin'))<=100
            or not number(artifact.get('training_cutoff'))<row['created_at']
            or not isinstance(artifact.get('outer_season'),int) or not 2000<=artifact['outer_season']<=2200
            or not isinstance(artifact.get('outer_first_week'),int) or not 1<=artifact['outer_first_week']<=13
            or not re.fullmatch('[0-9a-f]{64}',str(artifact.get('stage_sha256')))):
        raise ValueError('NFL spread candidate semantic contract conflict')
    if set(archive) != set(artifact['code_sha256']): raise ValueError('NFL spread code archive coverage mismatch')
    for name,digest in artifact['code_sha256'].items(): read_code(archive[name],digest)
    if current_code and artifact['code_sha256'] != code_catalog(): raise ValueError('NFL spread current code mismatch')
    row.update(artifact=artifact, code_archive=archive)
    return row


def register(artifact, *, now):
    ensure_schema()
    from . import nfl_spread_model as model
    model.validate_member(artifact['member'])
    if artifact['member']['method']=='incumbent':raise ValueError('NFL spread retained incumbent cannot consume outer evidence')
    if artifact['protocol'] != model.protocol() or artifact['training_cutoff'] >= now:
        raise ValueError('NFL spread candidate protocol/cutoff conflict')
    body = dict(artifact); body.pop('version',None)
    version = 'nfl-spread-' + sha(body)[:32]
    if artifact.get('version',version) != version: raise ValueError('NFL spread candidate version conflict')
    artifact = dict(artifact,version=version)
    if artifact['code_sha256'] != code_catalog(): raise ValueError('NFL spread code changed before registration')
    archive = archive_code(artifact['code_sha256'])
    with db.tx() as con:
        old = con.execute('SELECT * FROM nfl_spread_candidates WHERE version=?',(version,)).fetchone()
        if old and old['artifact'] != encoded(artifact): raise ValueError('NFL spread immutable candidate collision')
        if not old:
            con.execute('INSERT INTO nfl_spread_candidates VALUES(?,?,?,?,?)',
                        (version,now,encoded(artifact),sha(artifact),encoded(archive)))
            from . import learning_worker
            learning_worker.checkpoint_candidate(con,version)
    return candidate(version)


def event(version, kind, detail, *, now):
    ensure_schema()
    db.ex('INSERT INTO nfl_spread_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
          (version,number(now),kind,encoded(detail)))


def active_event():
    if not _table_exists('nfl_spread_events'):return None
    return db.q1("SELECT * FROM nfl_spread_events WHERE kind IN ('activated','rollback') ORDER BY id DESC LIMIT 1")


def freeze(version, observation, prediction, *, now):
    """Seal the candidate and exact incumbent snapshot together before kickoff."""
    ensure_schema(); proposal=candidate(version,current_code=True); artifact=proposal['artifact']
    stamp=max(number(now),time.time())
    if (not proposal['created_at'] < observation['as_of'] <= stamp < observation['kickoff']
            or observation['formula_version']!=artifact['formula_version']
            or observation['input_policy_id']!=artifact['input_policy_id']
            or observation['incumbent_version']!=artifact['incumbent_version']):
        raise ValueError('NFL spread prospective freeze chronology/policy conflict')
    if any(k in observation for k in ('actual_margin','label_sha256','label_received_at')):
        raise ValueError('NFL spread freeze cannot contain future label evidence')
    if observation['season'] != artifact['outer_season'] or observation['week'] < artifact['outer_first_week']:
        return None
    expected=artifact['member']; from . import nfl_spread_model as model
    mean=model.predict(expected,observation)
    if prediction['margin']!=mean or prediction['home_cover_probability']!=model.probability(mean,artifact['sd_margin'],observation['home_spread']):
        raise ValueError('NFL spread candidate freeze prediction mismatch')
    payload=dict(observation,candidate_margin=mean,candidate_sd=artifact['sd_margin'],
        candidate_probability=prediction['home_cover_probability'],candidate_sha256=proposal['sha256'])
    ident=sha([version,observation['snapshot_id']])
    old=db.q1('SELECT * FROM nfl_spread_freezes WHERE freeze_id=?',(ident,))
    if old:
        if old['payload']!=encoded(payload): raise ValueError('NFL spread immutable freeze collision')
        return verify_freeze(ident)
    db.ex('INSERT INTO nfl_spread_freezes VALUES(?,?,?,?,?,?,?,?)',
        (ident,version,observation['game_id'],observation['season'],observation['week'],stamp,encoded(payload),sha(payload)))
    return verify_freeze(ident)


def verify_freeze(value):
    ident=value.get('freeze_id') if isinstance(value,dict) else value
    row=db.q1('SELECT * FROM nfl_spread_freezes WHERE freeze_id=?',(ident,))
    if not row: raise ValueError('NFL spread persisted freeze missing')
    payload=json.loads(row['payload'])
    if sha(payload)!=row['sha256']: raise ValueError('NFL spread freeze seal mismatch')
    result=dict(row,payload=payload)
    if isinstance(value,dict) and value!=result: raise ValueError('NFL spread supplied freeze mismatch')
    return result


def reservation(ident):
    if not _table_exists('nfl_spread_reservations'):raise ValueError('NFL spread reservation ledger missing')
    row=db.q1('SELECT * FROM nfl_spread_reservations WHERE reservation_id=?',(ident,))
    if not row: raise ValueError('NFL spread reservation missing')
    payload=json.loads(row['payload'])
    if sha(payload)!=row['sha256']: raise ValueError('NFL spread reservation seal mismatch')
    row['payload']=payload
    return row


def reserve(version, freezes, universe, *, now):
    """Consume all game and week identities before any caller can read labels."""
    ensure_schema()
    game_ids=sorted(f['game_id'] for f in freezes)
    weeks=sorted({(f['season'],f['week']) for f in freezes})
    expected=sorted(g for week in weeks for g in universe.get(week,[]))
    if not weeks or len(game_ids)!=len(set(game_ids)) or game_ids!=expected or any(not universe.get(w) for w in weeks):
        raise ValueError('NFL spread reservation requires whole week universe')
    if len({w[0] for w in weeks})!=1: raise ValueError('NFL spread outer weeks must belong to one season')
    verified=[verify_freeze(f) for f in freezes]
    proposal=candidate(version,current_code=True)
    payload=dict(version=version,candidate_sha256=proposal['sha256'],game_ids=game_ids,
        weeks=[list(w) for w in weeks],freezes=[dict(freeze_id=f['freeze_id'],sha256=f['sha256']) for f in verified])
    ident=sha(payload)
    with db.tx() as con:
        old=con.execute('SELECT * FROM nfl_spread_reservations WHERE version=?',(version,)).fetchone()
        if old:
            if old['payload']!=encoded(payload): raise ValueError('NFL spread immutable reservation collision')
            return reservation(old['reservation_id'])
        con.execute('INSERT INTO nfl_spread_reservations VALUES(?,?,?,?,?)',(ident,version,number(now),encoded(payload),sha(payload)))
        con.executemany('INSERT INTO nfl_spread_consumed_games VALUES(?,?)',[(g,ident) for g in game_ids])
        con.executemany('INSERT INTO nfl_spread_consumed_weeks VALUES(?,?,?)',[(s,w,ident) for s,w in weeks])
        from . import learning_worker
        learning_worker.checkpoint_candidate(con,version)
    return reservation(ident)


def record_evaluation(reserved, evaluation, state, *, now):
    reserved=reservation(reserved['reservation_id'])
    payload=dict(evaluation,state=state,reservation_sha256=reserved['sha256'])
    old=db.q1('SELECT * FROM nfl_spread_evaluations WHERE reservation_id=?',(reserved['reservation_id'],))
    if old:
        if old['payload']!=encoded(payload): raise ValueError('NFL spread evaluation already sealed')
        return old
    db.ex('INSERT INTO nfl_spread_evaluations VALUES(?,?,?,?)',
        (reserved['reservation_id'],number(now),encoded(payload),sha(payload)))
    return db.q1('SELECT * FROM nfl_spread_evaluations WHERE reservation_id=?',(reserved['reservation_id'],))
