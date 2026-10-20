"""NBA-only SQLite tables on the shared connection (db.q / db.tx).

Created by `ensure_schema()`, never by db.py, so NFL schema is untouched.
Integrity rules are enforced twice: Python raises ValueError with a reason,
and triggers refuse the same mutation if anyone writes SQL directly.

- source artifacts, forecasts, quote observations, locks and grade revisions
  are append-only (no UPDATE / DELETE);
- forecasts need HTTP 200 sources received no later than the forecast;
- quotes need an HTTP 200 source received by observation, a finite integer
  American price and (player markets) a line >= 0, observed before tipoff;
- a lock is five props from ONE game, each matching its current quote
  exactly; one lock per game; refused at or after the stored tipoff;
- a lock is sealed by inserting its header after exactly five items, so it
  can never gain or change an item afterwards;
- a final score is never rewritten;
- grades are numbered revisions; replaying the latest grade is a no-op.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, Iterable, List, Optional

from . import db, nba_identity as ident
from .nba_data import PROPS, _digits

GAME_STATUSES = ('scheduled', 'in_progress', 'final', 'postponed', 'canceled', 'suspended', 'delayed')
GRADE_RESULTS = ('win', 'loss', 'push', 'void', 'pending')
RUN_STATUSES = ('ok', 'error', 'skipped')
PROP_MARKETS = {'points': 'player_points', 'rebounds': 'player_rebounds', 'assists': 'player_assists',
                'threes': 'player_threes', 'pra': 'player_points_rebounds_assists'}
# market -> (allowed sides, line rule). Player markets need a player and a line >= 0.
QUOTE_MARKETS = dict({m: (('over', 'under'), 'nonnegative') for m in PROP_MARKETS.values()},
                     totals=(('over', 'under'), 'nonnegative'), spreads=(('home', 'away'), 'finite'),
                     h2h=(('home', 'away'), 'none'))
MAX_LINE = 1000.0


def _immutable(table: str) -> str:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END;\n"
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} rows cannot be deleted'); END;\n")


SCHEMA = """
CREATE TABLE IF NOT EXISTS nba_source_artifacts (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, received_at REAL NOT NULL,
  http_status INTEGER NOT NULL, sha256 TEXT NOT NULL, body BLOB NOT NULL,
  UNIQUE(url, received_at, sha256));

CREATE TABLE IF NOT EXISTS nba_games (
  game_id TEXT PRIMARY KEY, espn_event_id TEXT NOT NULL UNIQUE,
  season_start_year INTEGER NOT NULL, season_label TEXT NOT NULL, season_type TEXT NOT NULL,
  competition TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
  tipoff_utc TEXT NOT NULL, tipoff_ts REAL NOT NULL, game_date_et TEXT NOT NULL,
  status TEXT NOT NULL, home_score INTEGER, away_score INTEGER,
  source_artifact_id INTEGER NOT NULL REFERENCES nba_source_artifacts(id), updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_nba_games_date ON nba_games(game_date_et, tipoff_ts);
CREATE TRIGGER IF NOT EXISTS nba_games_identity_fixed BEFORE UPDATE ON nba_games
WHEN NEW.game_id IS NOT OLD.game_id OR NEW.espn_event_id IS NOT OLD.espn_event_id
  OR NEW.home_team IS NOT OLD.home_team OR NEW.away_team IS NOT OLD.away_team
  OR NEW.season_start_year IS NOT OLD.season_start_year
BEGIN SELECT RAISE(ABORT, 'NBA game identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS nba_games_final_fixed BEFORE UPDATE ON nba_games
WHEN OLD.status = 'final' AND (NEW.status IS NOT 'final' OR NEW.home_score IS NOT OLD.home_score
  OR NEW.away_score IS NOT OLD.away_score)
BEGIN SELECT RAISE(ABORT, 'NBA final result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS nba_games_no_delete BEFORE DELETE ON nba_games
BEGIN SELECT RAISE(ABORT, 'NBA games cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nba_forecasts (
  forecast_id TEXT PRIMARY KEY, game_id TEXT NOT NULL REFERENCES nba_games(game_id),
  model_version TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL, source_artifact_ids TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS nba_forecasts_pretipoff BEFORE INSERT ON nba_forecasts
WHEN NOT EXISTS (SELECT 1 FROM nba_games WHERE game_id = NEW.game_id AND status = 'scheduled'
                 AND tipoff_ts > NEW.created_at)
  OR json_array_length(NEW.source_artifact_ids) = 0
  OR EXISTS (SELECT 1 FROM json_each(NEW.source_artifact_ids) j
             LEFT JOIN nba_source_artifacts a ON a.id = j.value
             WHERE a.id IS NULL OR a.http_status != 200 OR a.received_at > NEW.created_at)
BEGIN SELECT RAISE(ABORT, 'forecast needs successful sources received before it, saved before tipoff'); END;

CREATE TABLE IF NOT EXISTS nba_quote_observations (
  quote_id INTEGER PRIMARY KEY, game_id TEXT NOT NULL REFERENCES nba_games(game_id),
  market TEXT NOT NULL, espn_player_id TEXT, side TEXT NOT NULL,
  line REAL CHECK(line IS NULL OR line BETWEEN -1000 AND 1000),
  price REAL NOT NULL CHECK(price BETWEEN -100000 AND 100000 AND abs(price) >= 100
                            AND price = CAST(price AS INTEGER)),
  source TEXT NOT NULL, observed_at REAL NOT NULL,
  source_artifact_id INTEGER NOT NULL REFERENCES nba_source_artifacts(id), recorded_at REAL NOT NULL,
  CHECK(substr(market, 1, 7) != 'player_' OR (espn_player_id IS NOT NULL AND line IS NOT NULL
        AND line >= 0 AND side IN ('over','under'))));
CREATE TRIGGER IF NOT EXISTS nba_quotes_sourced_pretipoff BEFORE INSERT ON nba_quote_observations
WHEN NOT EXISTS (SELECT 1 FROM nba_source_artifacts WHERE id = NEW.source_artifact_id
                 AND http_status = 200 AND received_at <= NEW.observed_at)
  OR NOT EXISTS (SELECT 1 FROM nba_games WHERE game_id = NEW.game_id AND tipoff_ts > NEW.observed_at)
BEGIN SELECT RAISE(ABORT, 'quote needs a successful source received by observation, before tipoff'); END;

CREATE TABLE IF NOT EXISTS nba_selection_lock_items (
  lock_id TEXT NOT NULL REFERENCES nba_selection_locks(lock_id) DEFERRABLE INITIALLY DEFERRED,
  rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 5),
  forecast_id TEXT NOT NULL REFERENCES nba_forecasts(forecast_id),
  game_id TEXT NOT NULL REFERENCES nba_games(game_id), espn_player_id TEXT NOT NULL,
  prop TEXT NOT NULL CHECK(prop IN ('points','rebounds','assists','threes','pra')),
  side TEXT NOT NULL CHECK(side IN ('over','under')),
  line REAL NOT NULL CHECK(line BETWEEN 0 AND 1000),
  quote_id INTEGER NOT NULL REFERENCES nba_quote_observations(quote_id),
  PRIMARY KEY(lock_id, rank), UNIQUE(lock_id, espn_player_id, prop));
CREATE TRIGGER IF NOT EXISTS nba_lock_items_sealed BEFORE INSERT ON nba_selection_lock_items
WHEN EXISTS (SELECT 1 FROM nba_selection_locks WHERE lock_id = NEW.lock_id)
BEGIN SELECT RAISE(ABORT, 'selection lock is sealed'); END;
CREATE TRIGGER IF NOT EXISTS nba_lock_items_quote_match BEFORE INSERT ON nba_selection_lock_items
WHEN NOT EXISTS (SELECT 1 FROM nba_quote_observations q JOIN nba_forecasts f ON f.forecast_id = NEW.forecast_id
                 WHERE q.quote_id = NEW.quote_id AND q.game_id = NEW.game_id AND f.game_id = NEW.game_id
                   AND q.espn_player_id = NEW.espn_player_id AND q.side = NEW.side AND q.line = NEW.line
                   AND q.market = CASE NEW.prop WHEN 'points' THEN 'player_points'
                     WHEN 'rebounds' THEN 'player_rebounds' WHEN 'assists' THEN 'player_assists'
                     WHEN 'threes' THEN 'player_threes' WHEN 'pra' THEN 'player_points_rebounds_assists' END)
BEGIN SELECT RAISE(ABORT, 'lock item must match its quote exactly'); END;

CREATE TABLE IF NOT EXISTS nba_selection_locks (
  lock_id TEXT PRIMARY KEY, game_id TEXT NOT NULL UNIQUE REFERENCES nba_games(game_id),
  locked_at REAL NOT NULL, sha256 TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS nba_locks_five_pretipoff BEFORE INSERT ON nba_selection_locks
WHEN (SELECT COUNT(*) FROM nba_selection_lock_items WHERE lock_id = NEW.lock_id) != 5
  OR EXISTS (SELECT 1 FROM nba_selection_lock_items i
             JOIN nba_games g ON g.game_id = i.game_id
             JOIN nba_quote_observations q ON q.quote_id = i.quote_id
             JOIN nba_forecasts f ON f.forecast_id = i.forecast_id
             WHERE i.lock_id = NEW.lock_id AND (i.game_id != NEW.game_id OR g.tipoff_ts <= NEW.locked_at
               OR g.status != 'scheduled' OR q.observed_at > NEW.locked_at OR f.created_at > NEW.locked_at))
BEGIN SELECT RAISE(ABORT, 'lock needs five sourced pretipoff selections from one game'); END;

CREATE TABLE IF NOT EXISTS nba_grade_revisions (
  id INTEGER PRIMARY KEY, lock_id TEXT NOT NULL, rank INTEGER NOT NULL, revision INTEGER NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('win','loss','push','void','pending')), actual_value REAL,
  source_artifact_id INTEGER NOT NULL REFERENCES nba_source_artifacts(id),
  detail TEXT NOT NULL, graded_at REAL NOT NULL,
  UNIQUE(lock_id, rank, revision),
  FOREIGN KEY(lock_id, rank) REFERENCES nba_selection_lock_items(lock_id, rank));

CREATE TABLE IF NOT EXISTS nba_cadences (
  name TEXT PRIMARY KEY, cadence_seconds REAL NOT NULL, last_run_at REAL, last_status TEXT,
  last_detail TEXT, next_due_at REAL NOT NULL, updated_at REAL NOT NULL);
""" + ''.join(_immutable(t) for t in ('nba_source_artifacts', 'nba_forecasts', 'nba_quote_observations',
                                       'nba_selection_lock_items', 'nba_selection_locks',
                                       'nba_grade_revisions'))

_ready = None


def ensure_schema() -> None:
    global _ready
    if _ready is db.conn():
        return
    with db.tx() as c:
        c.executescript(SCHEMA)
        if 'game_id' not in {r[1] for r in c.execute('PRAGMA table_info(nba_selection_locks)')}:
            raise RuntimeError('nba_selection_locks predates the one-lock-per-game contract; '
                               'recreate the research database')
    _ready = db.conn()


def _now(now: Optional[float]) -> float:
    return db.now() if now is None else float(now)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _american(price) -> bool:
    return _finite(price) and price == int(price) and 100 <= abs(price) <= 100000


def _require_artifact(artifact_id, received_by: Optional[float] = None) -> Dict:
    """A stored HTTP 200 artifact, optionally received no later than `received_by`."""
    row = None
    if isinstance(artifact_id, int) and not isinstance(artifact_id, bool):
        row = db.q1('SELECT id, http_status, received_at FROM nba_source_artifacts WHERE id=?', (artifact_id,))
    if not row:
        raise ValueError(f'unknown source artifact: {artifact_id!r}')
    if row['http_status'] != 200:
        raise ValueError(f"source artifact {artifact_id} was HTTP {row['http_status']}")
    if received_by is not None and row['received_at'] > received_by:
        raise ValueError(f'source artifact {artifact_id} was received after {received_by}')
    return row


# ---------------------------------------------------------------- artifacts
def save_artifact(artifact: Dict) -> int:
    """Archive one `nba_data.fetch` result; the same receipt returns the same id."""
    ensure_schema()
    body = artifact['body']
    if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != artifact['sha256']:
        raise ValueError('artifact body does not match its sha256')
    key = (artifact['url'], float(artifact['received_at']), artifact['sha256'])
    with db.tx() as c:
        c.execute('INSERT OR IGNORE INTO nba_source_artifacts(kind,url,received_at,http_status,sha256,body) '
                  'VALUES(?,?,?,?,?,?)', (artifact['kind'], *key[:2], int(artifact['http_status']), key[2], body))
    return db.q1('SELECT id FROM nba_source_artifacts WHERE url=? AND received_at=? AND sha256=?', key)['id']


def get_artifact(artifact_id: int) -> Optional[Dict]:
    ensure_schema()
    return db.q1('SELECT * FROM nba_source_artifacts WHERE id=?', (artifact_id,))


# ---------------------------------------------------------------- games
_GAME_FIELDS = ('game_id', 'espn_event_id', 'season_start_year', 'season_label', 'season_type', 'competition',
                'home_team', 'away_team', 'tipoff_utc', 'tipoff_ts', 'game_date_et', 'status',
                'home_score', 'away_score', 'source_artifact_id')


def upsert_game(game: Dict, source_artifact_id: int, now: Optional[float] = None) -> Dict:
    """Insert or refresh a parsed game. Identity and final results never change."""
    ensure_schema()
    _require_artifact(source_artifact_id)
    eid = str(game['espn_event_id'])
    if game['game_id'] != ident.game_id(int(game['season_start_year']) + 1, eid):
        raise ValueError('game_id does not match season and ESPN event id')
    if game['home_team'] not in ident.TEAMS or game['away_team'] not in ident.TEAMS \
            or game['home_team'] == game['away_team']:
        raise ValueError('invalid teams')
    if game['status'] not in GAME_STATUSES:
        raise ValueError(f"invalid status {game['status']!r}")
    scores = (game.get('home_score'), game.get('away_score'))
    if game['status'] == 'final':
        if not all(isinstance(s, int) and not isinstance(s, bool) and 0 <= s <= 250 for s in scores) \
                or scores[0] == scores[1]:
            raise ValueError('final game needs two distinct non-negative integer scores')
    elif scores != (None, None):
        raise ValueError('only a final game carries scores')
    tip = ident.parse_tipoff(game['tipoff_utc'])
    if game.get('game_date_et') not in (None, ident.eastern_date(tip)):
        raise ValueError('game_date_et does not match tipoff')
    row = dict({k: game.get(k) for k in _GAME_FIELDS}, espn_event_id=eid, tipoff_utc=ident.iso_utc(tip),
               tipoff_ts=tip.timestamp(), game_date_et=ident.eastern_date(tip),
               source_artifact_id=source_artifact_id)
    with db.tx() as c:
        old = c.execute('SELECT * FROM nba_games WHERE game_id=? OR espn_event_id=?',
                        (row['game_id'], eid)).fetchall()
        if old:
            old = dict(old[0])
            for key in ('game_id', 'espn_event_id', 'home_team', 'away_team', 'season_start_year'):
                if old[key] != row[key]:
                    raise ValueError(f'NBA game identity conflict on {key}: {old[key]!r} != {row[key]!r}')
            if old['status'] == 'final':
                if (row['status'], row['home_score'], row['away_score']) != \
                        ('final', old['home_score'], old['away_score']):
                    raise ValueError('final result already recorded and cannot change')
                return get_game(row['game_id'])
        c.execute(f"INSERT INTO nba_games({','.join(_GAME_FIELDS)},updated_at) VALUES({','.join('?' * 16)}) "
                  "ON CONFLICT(game_id) DO UPDATE SET " +
                  ','.join(f'{k}=excluded.{k}' for k in _GAME_FIELDS[3:] + ('updated_at',)),
                  [row[k] for k in _GAME_FIELDS] + [_now(now)])
    return get_game(row['game_id'])


def get_game(game_id: str) -> Optional[Dict]:
    ensure_schema()
    return db.q1('SELECT * FROM nba_games WHERE game_id=?', (game_id,))


def list_games(game_date_et: str) -> List[Dict]:
    ensure_schema()
    return db.q('SELECT * FROM nba_games WHERE game_date_et=? ORDER BY tipoff_ts, game_id', (game_date_et,))


# ---------------------------------------------------------------- forecasts / quotes
def _forecast(row: Optional[Dict]) -> Optional[Dict]:
    if row:
        row = dict(row, payload=json.loads(row['payload']), source_artifact_ids=json.loads(row['source_artifact_ids']))
    return row


def save_forecast(forecast_id: str, game_id: str, model_version: str, payload: Dict,
                  source_artifact_ids: Iterable[int], now: Optional[float] = None) -> Dict:
    """Immutable pretipoff forecast. Lines are not required; quotes are recorded separately."""
    ensure_schema()
    sources = list(source_artifact_ids)
    if not sources:
        raise ValueError('forecast must reference at least one source artifact')
    encoded = _json(payload)
    sha = hashlib.sha256(encoded.encode()).hexdigest()
    old = get_forecast(forecast_id)
    if old:
        if (old['game_id'], old['model_version'], old['payload_sha256'], old['source_artifact_ids']) != \
                (game_id, model_version, sha, sources):
            raise ValueError(f'forecast {forecast_id} already exists with different contents')
        return old
    game, at = get_game(game_id), _now(now)
    if not game:
        raise ValueError(f'unknown game {game_id}')
    if game['status'] != 'scheduled' or at >= game['tipoff_ts']:
        raise ValueError('forecast must be saved before tipoff of a scheduled game')
    for a in sources:
        _require_artifact(a, received_by=at)
    db.ex('INSERT INTO nba_forecasts(forecast_id,game_id,model_version,created_at,payload,payload_sha256,'
          'source_artifact_ids) VALUES(?,?,?,?,?,?,?)',
          (forecast_id, game_id, model_version, at, encoded, sha, _json(sources)))
    return get_forecast(forecast_id)


def get_forecast(forecast_id: str) -> Optional[Dict]:
    ensure_schema()
    return _forecast(db.q1('SELECT * FROM nba_forecasts WHERE forecast_id=?', (forecast_id,)))


def list_forecasts(game_id: str) -> List[Dict]:
    ensure_schema()
    return [_forecast(r) for r in db.q('SELECT * FROM nba_forecasts WHERE game_id=? ORDER BY created_at',
                                       (game_id,))]


def save_quote_observation(game_id: str, market: str, espn_player_id: Optional[str], side: str,
                           line: Optional[float], price: float, source: str, observed_at: float,
                           source_artifact_id: Optional[int] = None, now: Optional[float] = None) -> Dict:
    """Append one sourced quote observed before tipoff. It never alters an earlier forecast.

    `source_artifact_id` is required (keyword kept for compatibility): an HTTP 200
    artifact received no later than `observed_at`. Price is a finite integer American price.
    """
    ensure_schema()
    game = get_game(game_id)
    if not game:
        raise ValueError(f'unknown game {game_id}')
    if market not in QUOTE_MARKETS:
        raise ValueError(f'unknown market {market!r}')
    sides, rule = QUOTE_MARKETS[market]
    if side not in sides:
        raise ValueError(f'invalid side {side!r} for {market}')
    if market in PROP_MARKETS.values():
        espn_player_id = _digits(espn_player_id)
    elif espn_player_id is not None:
        raise ValueError(f'{market} is a game market and takes no player')
    if rule == 'none':
        if line is not None:
            raise ValueError(f'{market} takes no line')
    elif not _finite(line) or abs(line) > MAX_LINE or (rule == 'nonnegative' and line < 0):
        raise ValueError(f'invalid line {line!r} for {market}')
    if not _american(price):
        raise ValueError(f'invalid American price {price!r}')
    if not _finite(observed_at):
        raise ValueError('observed_at must be a finite timestamp')
    if observed_at >= game['tipoff_ts']:
        raise ValueError('quote observed at or after tipoff')
    _require_artifact(source_artifact_id, received_by=observed_at)
    with db.tx() as c:
        cur = c.execute('INSERT INTO nba_quote_observations(game_id,market,espn_player_id,side,line,price,source,'
                        'observed_at,source_artifact_id,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (game_id, market, espn_player_id, side, None if line is None else float(line),
                         float(price), source, float(observed_at), source_artifact_id, _now(now)))
    return db.q1('SELECT * FROM nba_quote_observations WHERE quote_id=?', (cur.lastrowid,))


def list_quote_observations(game_id: str) -> List[Dict]:
    ensure_schema()
    return db.q('SELECT * FROM nba_quote_observations WHERE game_id=? ORDER BY observed_at, quote_id', (game_id,))


# ---------------------------------------------------------------- five-prop locks
_ITEM_FIELDS = ('rank', 'forecast_id', 'game_id', 'espn_player_id', 'prop', 'side', 'line', 'quote_id')


def lock_selections(lock_id: str, selections: List[Dict], now: Optional[float] = None) -> Dict:
    """Seal exactly five ranked prop selections for ONE game, before its stored tipoff.

    Each selection: {forecast_id, espn_player_id, prop, side, line, quote_id}; list
    order is rank 1..5. `line` must equal the quote's line, and the quote must be the
    latest observation of that game/player/market/side at lock time, from an HTTP 200
    source received <= observed_at <= lock time. One lock per game, whatever its ID.
    Replaying the identical lock returns it unchanged.
    """
    ensure_schema()
    at = _now(now)
    if len(selections) != 5:
        raise ValueError('a selection lock holds exactly five props')
    items = []
    for rank, s in enumerate(selections, 1):
        if s.get('prop') not in PROPS or s.get('side') not in ('over', 'under'):
            raise ValueError(f'invalid prop/side at rank {rank}')
        forecast = get_forecast(s.get('forecast_id'))
        if not forecast:
            raise ValueError(f"unknown forecast {s.get('forecast_id')!r}")
        line, quote_id = s.get('line'), s.get('quote_id')
        if not _finite(line) or not 0 <= line <= MAX_LINE:
            raise ValueError(f'rank {rank} needs a finite non-negative line, got {line!r}')
        if not isinstance(quote_id, int) or isinstance(quote_id, bool):
            raise ValueError(f'rank {rank} needs a sourced quote_id')
        items.append({'rank': rank, 'forecast_id': forecast['forecast_id'], 'game_id': forecast['game_id'],
                      'espn_player_id': _digits(s.get('espn_player_id')), 'prop': s['prop'],
                      'side': s['side'], 'line': float(line), 'quote_id': quote_id})
    if len({(i['espn_player_id'], i['prop']) for i in items}) != 5:
        raise ValueError('duplicate player prop in lock')
    games = {i['game_id'] for i in items}
    if len(games) != 1:
        raise ValueError('all five selections must come from one game')
    game_id = games.pop()
    sha = hashlib.sha256(_json(items).encode()).hexdigest()
    old = get_lock(lock_id)
    if old:
        if old['sha256'] != sha:
            raise ValueError(f'lock {lock_id} already sealed with different selections')
        return old
    if get_game_lock(game_id):
        raise ValueError(f'{game_id} already has a selection lock')
    game = get_game(game_id)
    if game['status'] != 'scheduled' or at >= game['tipoff_ts']:
        raise ValueError(f'{game_id} has tipped off; lock refused')
    for i in items:
        if get_forecast(i['forecast_id'])['created_at'] > at:
            raise ValueError('forecast is newer than the lock time')
        q = db.q1('SELECT * FROM nba_quote_observations WHERE quote_id=?', (i['quote_id'],))
        if not q or (q['game_id'], q['espn_player_id'], q['market'], q['side'], q['line']) != \
                (game_id, i['espn_player_id'], PROP_MARKETS[i['prop']], i['side'], i['line']):
            raise ValueError(f"quote {i['quote_id']} does not match rank {i['rank']} game/player/market/side/line")
        if not _american(q['price']) or q['observed_at'] > at:
            raise ValueError(f"quote {i['quote_id']} is unpriced or observed after the lock")
        _require_artifact(q['source_artifact_id'], received_by=q['observed_at'])
        latest = db.q1('SELECT quote_id FROM nba_quote_observations WHERE game_id=? AND espn_player_id=? '
                       'AND market=? AND side=? AND observed_at<=? ORDER BY observed_at DESC, quote_id DESC LIMIT 1',
                       (game_id, i['espn_player_id'], q['market'], i['side'], at))
        if latest['quote_id'] != q['quote_id']:
            raise ValueError(f"quote {i['quote_id']} is not the current line at lock time")
    with db.tx() as c:
        c.executemany(f"INSERT INTO nba_selection_lock_items(lock_id,{','.join(_ITEM_FIELDS)}) "
                      f"VALUES(?,{','.join('?' * len(_ITEM_FIELDS))})",
                      [[lock_id] + [i[k] for k in _ITEM_FIELDS] for i in items])
        c.execute('INSERT INTO nba_selection_locks(lock_id,game_id,locked_at,sha256) VALUES(?,?,?,?)',
                  (lock_id, game_id, at, sha))
    return get_lock(lock_id)


def get_lock(lock_id: str) -> Optional[Dict]:
    ensure_schema()
    head = db.q1('SELECT * FROM nba_selection_locks WHERE lock_id=?', (lock_id,))
    if not head:
        return None
    cols = ','.join(f'i.{k}' for k in _ITEM_FIELDS)
    items = db.q(f"SELECT {cols}, q.price, q.observed_at AS quote_observed_at, "
                 "q.source_artifact_id AS quote_source_artifact_id FROM nba_selection_lock_items i "
                 "JOIN nba_quote_observations q ON q.quote_id = i.quote_id WHERE i.lock_id=? ORDER BY i.rank",
                 (lock_id,))
    return dict(head, items=items)


def get_game_lock(game_id: str) -> Optional[Dict]:
    ensure_schema()
    row = db.q1('SELECT lock_id FROM nba_selection_locks WHERE game_id=?', (game_id,))
    return get_lock(row['lock_id']) if row else None


# ---------------------------------------------------------------- grades
def _grade(row: Dict) -> Dict:
    return dict({k: row[k] for k in ('lock_id', 'rank', 'revision', 'result', 'actual_value',
                                     'source_artifact_id', 'graded_at')}, detail=json.loads(row['detail']))


def add_grade_revision(lock_id: str, rank: int, result: str, actual_value: Optional[float],
                       source_artifact_id: int, detail: Optional[Dict] = None,
                       now: Optional[float] = None) -> Dict:
    """Append a statistical grade for one locked prop; identical to latest -> returns latest."""
    ensure_schema()
    if result not in GRADE_RESULTS:
        raise ValueError(f'invalid grade result {result!r}')
    at = _now(now)
    _require_artifact(source_artifact_id, received_by=at)
    item = db.q1('SELECT game_id FROM nba_selection_lock_items WHERE lock_id=? AND rank=?', (lock_id, rank))
    if not item or not get_lock(lock_id):
        raise ValueError(f'no sealed selection {lock_id}#{rank}')
    game_status=get_game(item['game_id'])['status']
    if result != 'pending' and game_status != 'final' and not (result=='void' and game_status=='canceled'):
        raise ValueError('only a final game can be graded')
    if result in ('win', 'loss', 'push'):
        if not _finite(actual_value) or actual_value < 0:
            raise ValueError('win/loss/push needs the observed statistic')
    elif actual_value is not None:
        raise ValueError(f'{result} grade carries no statistic')
    encoded = _json(detail or {})
    with db.tx() as c:
        last = c.execute('SELECT * FROM nba_grade_revisions WHERE lock_id=? AND rank=? ORDER BY revision DESC '
                         'LIMIT 1', (lock_id, rank)).fetchone()
        if last and (last['result'], last['actual_value'], last['source_artifact_id'], last['detail']) == \
                (result, None if actual_value is None else float(actual_value), source_artifact_id, encoded):
            return _grade(dict(last))
        revision = (last['revision'] if last else 0) + 1
        c.execute('INSERT INTO nba_grade_revisions(lock_id,rank,revision,result,actual_value,source_artifact_id,'
                  'detail,graded_at) VALUES(?,?,?,?,?,?,?,?)',
                  (lock_id, rank, revision, result, actual_value, source_artifact_id, encoded, at))
    return list_grade_revisions(lock_id, rank)[-1]


def list_grade_revisions(lock_id: str, rank: Optional[int] = None) -> List[Dict]:
    ensure_schema()
    sql, params = 'SELECT * FROM nba_grade_revisions WHERE lock_id=?', [lock_id]
    if rank is not None:
        sql, params = sql + ' AND rank=?', params + [rank]
    return [_grade(r) for r in db.q(sql + ' ORDER BY rank, revision', params)]


def latest_grades(lock_id: str) -> Dict[int, Dict]:
    return {g['rank']: g for g in list_grade_revisions(lock_id)}


# ---------------------------------------------------------------- cadences
def _cadence(row: Optional[Dict]) -> Optional[Dict]:
    if not row:
        return None
    return {'name': row['name'], 'cadence_seconds': row['cadence_seconds'], 'last_run_at': row['last_run_at'],
            'last_status': row['last_status'],
            'last_detail': json.loads(row['last_detail']) if row['last_detail'] else None,
            'next_due_at': row['next_due_at']}


def set_cadence(name: str, cadence_seconds: float, now: Optional[float] = None) -> Dict:
    ensure_schema()
    if not cadence_seconds or cadence_seconds <= 0:
        raise ValueError('cadence must be positive')
    at = _now(now)
    old = db.q1('SELECT last_run_at FROM nba_cadences WHERE name=?', (name,))
    base = old['last_run_at'] if old and old['last_run_at'] is not None else at
    db.ex('INSERT INTO nba_cadences(name,cadence_seconds,next_due_at,updated_at) VALUES(?,?,?,?) '
          'ON CONFLICT(name) DO UPDATE SET cadence_seconds=excluded.cadence_seconds, '
          'next_due_at=excluded.next_due_at, updated_at=excluded.updated_at',
          (name, float(cadence_seconds), base + cadence_seconds, at))
    return get_cadence(name)


def record_run(name: str, status: str, detail: Optional[Dict] = None, now: Optional[float] = None) -> Dict:
    ensure_schema()
    if status not in RUN_STATUSES:
        raise ValueError(f'invalid run status {status!r}')
    row = db.q1('SELECT cadence_seconds FROM nba_cadences WHERE name=?', (name,))
    if not row:
        raise ValueError(f'unknown cadence {name!r}')
    at = _now(now)
    db.ex('UPDATE nba_cadences SET last_run_at=?, last_status=?, last_detail=?, next_due_at=?, updated_at=? '
          'WHERE name=?', (at, status, _json(detail or {}), at + row['cadence_seconds'], at, name))
    return get_cadence(name)


def get_cadence(name: str) -> Optional[Dict]:
    ensure_schema()
    return _cadence(db.q1('SELECT * FROM nba_cadences WHERE name=?', (name,)))


def list_cadences() -> List[Dict]:
    ensure_schema()
    return [_cadence(r) for r in db.q('SELECT * FROM nba_cadences ORDER BY name')]
