"""SQLite persistence. Odds snapshots, predictions, results, research sources.

Deliberately plain sqlite3 -- one file, no ORM, no migration framework.
Schema changes are additive via `_ensure_columns`.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sports (
  key TEXT PRIMARY KEY, label TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY, sport TEXT, name TEXT, abbrev TEXT, location TEXT,
  meta TEXT, updated_at REAL);

CREATE TABLE IF NOT EXISTS players (
  id TEXT PRIMARY KEY, sport TEXT, team_id TEXT, name TEXT, position TEXT,
  meta TEXT, updated_at REAL);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, sport TEXT, league TEXT, name TEXT,
  home_team TEXT, away_team TEXT, start_time TEXT, venue TEXT,
  status TEXT, meta TEXT, updated_at REAL);

CREATE TABLE IF NOT EXISTS nfl_provider_games (
  provider TEXT NOT NULL, provider_game_id TEXT NOT NULL,
  game_id TEXT NOT NULL, linked_at REAL NOT NULL,
  PRIMARY KEY(provider, provider_game_id));
CREATE INDEX IF NOT EXISTS ix_nfl_provider_game ON nfl_provider_games(game_id);

CREATE TABLE IF NOT EXISTS nfl_game_snapshots (
  id TEXT PRIMARY KEY, game_id TEXT NOT NULL, payload TEXT NOT NULL,
  model_version TEXT NOT NULL, feature_version TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_nfl_snapshot_game ON nfl_game_snapshots(game_id, created_at);
CREATE TRIGGER IF NOT EXISTS nfl_snapshot_no_update BEFORE UPDATE ON nfl_game_snapshots
BEGIN SELECT RAISE(ABORT, 'NFL snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_snapshot_no_delete BEFORE DELETE ON nfl_game_snapshots
BEGIN SELECT RAISE(ABORT, 'NFL snapshots cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_game_predictions (
  id TEXT PRIMARY KEY, game_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  model_version TEXT NOT NULL, feature_version TEXT NOT NULL,
  projected_home_points REAL, projected_away_points REAL,
  projected_margin REAL, projected_total REAL, home_win_probability REAL,
  uncertainty REAL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_nfl_game_prediction ON nfl_game_predictions(game_id, created_at);

CREATE TABLE IF NOT EXISTS nfl_shadow_entries (
  game_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  model_version TEXT NOT NULL, feature_version TEXT NOT NULL,
  season INTEGER NOT NULL, season_type INTEGER NOT NULL, week INTEGER NOT NULL,
  home_team TEXT NOT NULL, away_team TEXT NOT NULL, kickoff_utc REAL NOT NULL,
  frozen_at REAL NOT NULL, designation TEXT NOT NULL,
  projected_home_points REAL NOT NULL, projected_away_points REAL NOT NULL,
  projected_margin REAL NOT NULL, projected_total REAL NOT NULL,
  home_win_probability REAL NOT NULL,
  home_spread_line REAL, home_spread_book TEXT, home_spread_price REAL,
  home_spread_captured_at REAL,
  total_line REAL, total_book TEXT, over_price REAL, total_captured_at REAL);
CREATE INDEX IF NOT EXISTS ix_nfl_shadow_week ON nfl_shadow_entries(season, season_type, week);
CREATE TRIGGER IF NOT EXISTS nfl_shadow_entry_no_update BEFORE UPDATE ON nfl_shadow_entries
BEGIN SELECT RAISE(ABORT, 'shadow entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_shadow_entry_no_delete BEFORE DELETE ON nfl_shadow_entries
BEGIN SELECT RAISE(ABORT, 'shadow entries cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_shadow_grades (
  game_id TEXT NOT NULL, revision INTEGER NOT NULL,
  home_score REAL NOT NULL, away_score REAL NOT NULL,
  su_result TEXT, ats_result TEXT, total_result TEXT,
  score_mae REAL NOT NULL, score_rmse REAL NOT NULL,
  margin_error REAL NOT NULL, total_error REAL NOT NULL,
  brier REAL, closing_home_spread REAL, closing_total REAL,
  spread_clv_points REAL, total_clv_points REAL,
  result_source TEXT NOT NULL, source_retrieved_at REAL NOT NULL,
  graded_at REAL NOT NULL, PRIMARY KEY(game_id, revision));
CREATE TRIGGER IF NOT EXISTS nfl_shadow_grade_no_update BEFORE UPDATE ON nfl_shadow_grades
BEGIN SELECT RAISE(ABORT, 'shadow grades are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_shadow_grade_no_delete BEFORE DELETE ON nfl_shadow_grades
BEGIN SELECT RAISE(ABORT, 'shadow grades cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_shadow_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL,
  prior_revision INTEGER NOT NULL, new_revision INTEGER NOT NULL,
  old_home_score REAL NOT NULL, old_away_score REAL NOT NULL,
  new_home_score REAL NOT NULL, new_away_score REAL NOT NULL,
  reason TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS nfl_shadow_audit_no_update BEFORE UPDATE ON nfl_shadow_audit
BEGIN SELECT RAISE(ABORT, 'shadow audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_shadow_audit_no_delete BEFORE DELETE ON nfl_shadow_audit
BEGIN SELECT RAISE(ABORT, 'shadow audit cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_research_freezes (
  freeze_sha TEXT PRIMARY KEY, season INTEGER NOT NULL, week INTEGER NOT NULL,
  family TEXT NOT NULL, file_path TEXT NOT NULL, frozen_at REAL NOT NULL,
  model_version TEXT NOT NULL, model_hash TEXT NOT NULL,
  feature_version TEXT NOT NULL, feature_hash TEXT NOT NULL,
  training_games INTEGER NOT NULL, registered_at REAL NOT NULL,
  UNIQUE(season, week, family));
CREATE TRIGGER IF NOT EXISTS nfl_research_freeze_no_update BEFORE UPDATE ON nfl_research_freezes
BEGIN SELECT RAISE(ABORT, 'research freezes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_freeze_no_delete BEFORE DELETE ON nfl_research_freezes
BEGIN SELECT RAISE(ABORT, 'research freezes cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_research_market_freezes (
  freeze_sha TEXT NOT NULL, game_id TEXT NOT NULL,
  book TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
  line REAL, price REAL, provider TEXT NOT NULL, fetched_at REAL NOT NULL,
  kickoff_utc REAL NOT NULL, quote_phase TEXT NOT NULL,
  source_snapshot_id TEXT, source_quote_id INTEGER,
  PRIMARY KEY(freeze_sha, game_id, book, market, selection));
CREATE TRIGGER IF NOT EXISTS nfl_research_market_no_update BEFORE UPDATE ON nfl_research_market_freezes
BEGIN SELECT RAISE(ABORT, 'research market freezes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_market_no_delete BEFORE DELETE ON nfl_research_market_freezes
BEGIN SELECT RAISE(ABORT, 'research market freezes cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_market_no_late_insert BEFORE INSERT ON nfl_research_market_freezes
WHEN EXISTS(SELECT 1 FROM nfl_research_freezes WHERE freeze_sha=NEW.freeze_sha)
BEGIN SELECT RAISE(ABORT, 'research market registration is closed'); END;

CREATE TABLE IF NOT EXISTS nfl_research_market_attestations (
  freeze_sha TEXT NOT NULL, game_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL, recorded_at REAL NOT NULL,
  PRIMARY KEY(freeze_sha,game_id,snapshot_id));
CREATE TRIGGER IF NOT EXISTS nfl_research_attestation_no_update BEFORE UPDATE ON nfl_research_market_attestations
BEGIN SELECT RAISE(ABORT, 'research market attestations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_attestation_no_delete BEFORE DELETE ON nfl_research_market_attestations
BEGIN SELECT RAISE(ABORT, 'research market attestations cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_attestation_no_late_insert BEFORE INSERT ON nfl_research_market_attestations
WHEN EXISTS(SELECT 1 FROM nfl_research_freezes WHERE freeze_sha=NEW.freeze_sha)
BEGIN SELECT RAISE(ABORT, 'research market attestation registration is closed'); END;

CREATE TABLE IF NOT EXISTS nfl_research_grades (
  freeze_sha TEXT NOT NULL, game_id TEXT NOT NULL, revision INTEGER NOT NULL,
  home_score INTEGER NOT NULL, away_score INTEGER NOT NULL,
  result_source TEXT NOT NULL, source_retrieved_at REAL NOT NULL,
  graded_at REAL NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(freeze_sha, game_id, revision));
CREATE TRIGGER IF NOT EXISTS nfl_research_grade_no_update BEFORE UPDATE ON nfl_research_grades
BEGIN SELECT RAISE(ABORT, 'research grades are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_grade_no_delete BEFORE DELETE ON nfl_research_grades
BEGIN SELECT RAISE(ABORT, 'research grades cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_research_grade_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, freeze_sha TEXT NOT NULL,
  game_id TEXT NOT NULL, prior_revision INTEGER NOT NULL,
  new_revision INTEGER NOT NULL, old_home_score INTEGER NOT NULL,
  old_away_score INTEGER NOT NULL, new_home_score INTEGER NOT NULL,
  new_away_score INTEGER NOT NULL, reason TEXT NOT NULL,
  source TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS nfl_research_audit_no_update BEFORE UPDATE ON nfl_research_grade_audit
BEGIN SELECT RAISE(ABORT, 'research audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_audit_no_delete BEFORE DELETE ON nfl_research_grade_audit
BEGIN SELECT RAISE(ABORT, 'research audit cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_research_context_observations (
  observation_sha TEXT PRIMARY KEY, game_id TEXT NOT NULL,
  kind TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL,
  source_url TEXT NOT NULL, source_published_at REAL,
  fetched_at REAL NOT NULL, valid_for_utc REAL,
  kickoff_utc REAL NOT NULL, provenance_level TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_nfl_context_game ON nfl_research_context_observations(game_id,kind,fetched_at);
CREATE TRIGGER IF NOT EXISTS nfl_research_context_no_update BEFORE UPDATE ON nfl_research_context_observations
BEGIN SELECT RAISE(ABORT, 'research context is immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_research_context_no_delete BEFORE DELETE ON nfl_research_context_observations
BEGIN SELECT RAISE(ABORT, 'research context cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_capture_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL,
  kickoff_utc REAL NOT NULL, attempted_at REAL NOT NULL,
  provider TEXT NOT NULL, outcome TEXT NOT NULL,
  rows_stored INTEGER NOT NULL, error_type TEXT);
CREATE INDEX IF NOT EXISTS ix_nfl_capture_attempt_game ON nfl_capture_attempts(game_id,attempted_at);
CREATE TRIGGER IF NOT EXISTS nfl_capture_attempt_no_update BEFORE UPDATE ON nfl_capture_attempts
BEGIN SELECT RAISE(ABORT, 'capture attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_capture_attempt_no_delete BEFORE DELETE ON nfl_capture_attempts
BEGIN SELECT RAISE(ABORT, 'capture attempts cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS nfl_research_model_versions (
  version_id TEXT PRIMARY KEY, model_name TEXT NOT NULL, version TEXT NOT NULL,
  code_commit TEXT NOT NULL, training_cutoff REAL NOT NULL,
  feature_hash TEXT NOT NULL, model_hash TEXT NOT NULL,
  first_eligible_season INTEGER NOT NULL, first_eligible_week INTEGER NOT NULL,
  registered_at REAL NOT NULL, UNIQUE(model_name,version));
CREATE TRIGGER IF NOT EXISTS nfl_model_version_no_update BEFORE UPDATE ON nfl_research_model_versions
BEGIN SELECT RAISE(ABORT, 'model versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_model_version_no_delete BEFORE DELETE ON nfl_research_model_versions
BEGIN SELECT RAISE(ABORT, 'model versions cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS nfl_research_model_version_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('research','retired','promoted')),
  last_eligible_season INTEGER, last_eligible_week INTEGER,
  recorded_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS nfl_model_event_no_update BEFORE UPDATE ON nfl_research_model_version_events
BEGIN SELECT RAISE(ABORT, 'model version events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_model_event_no_delete BEFORE DELETE ON nfl_research_model_version_events
BEGIN SELECT RAISE(ABORT, 'model version events cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS nfl_research_model_freeze_links (
  freeze_sha TEXT PRIMARY KEY, version_id TEXT NOT NULL, linked_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS nfl_model_link_no_update BEFORE UPDATE ON nfl_research_model_freeze_links
BEGIN SELECT RAISE(ABORT, 'model freeze links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nfl_model_link_no_delete BEFORE DELETE ON nfl_research_model_freeze_links
BEGIN SELECT RAISE(ABORT, 'model freeze links cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT, sport TEXT, book TEXT, market TEXT, selection TEXT,
  line REAL, price REAL, captured_at REAL, source TEXT, is_demo INTEGER DEFAULT 0,
  source_url TEXT, provider_updated_at REAL,
  provider TEXT, fetched_at REAL, kickoff_utc REAL, quote_phase TEXT);
CREATE INDEX IF NOT EXISTS ix_odds_event ON odds_snapshots(event_id, market, selection, captured_at);
CREATE INDEX IF NOT EXISTS ix_odds_time ON odds_snapshots(captured_at);
CREATE TRIGGER IF NOT EXISTS odds_snapshot_no_update BEFORE UPDATE ON odds_snapshots
BEGIN SELECT RAISE(ABORT, 'odds snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS odds_snapshot_no_delete BEFORE DELETE ON odds_snapshots
BEGIN SELECT RAISE(ABORT, 'odds snapshots cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS closing_lines (
  event_id TEXT, market TEXT, selection TEXT, book TEXT,
  line REAL, price REAL, captured_at REAL,
  PRIMARY KEY (event_id, market, selection, book));

CREATE TABLE IF NOT EXISTS predictions (
  id TEXT PRIMARY KEY,
  event_id TEXT, sport TEXT, market TEXT, selection TEXT, side TEXT,
  line REAL, book TEXT, price REAL,
  model_prob REAL, implied_prob REAL, fair_price REAL, edge_pct REAL, ev_per_unit REAL,
  confidence REAL, confidence_bucket TEXT, units REAL, state TEXT,
  data_quality TEXT, breakeven_price REAL,
  reasoning TEXT, risks TEXT, components TEXT, signals TEXT, sources TEXT,
  model_version TEXT, confidence_version TEXT, data_snapshot TEXT,
  created_at REAL, trigger_note TEXT, snapshot_id TEXT);
CREATE INDEX IF NOT EXISTS ix_pred_event ON predictions(event_id);
CREATE INDEX IF NOT EXISTS ix_pred_created ON predictions(created_at);

CREATE TABLE IF NOT EXISTS results (
  prediction_id TEXT PRIMARY KEY, event_id TEXT,
  outcome TEXT, won INTEGER, push INTEGER, actual_value REAL,
  closing_price REAL, closing_line REAL, clv_pct REAL,
  graded_at REAL, notes TEXT);

CREATE TABLE IF NOT EXISTS research_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT, kind TEXT, title TEXT, url TEXT, publisher TEXT,
  retrieved_at REAL, summary TEXT, trust TEXT);

CREATE TABLE IF NOT EXISTS news_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sport TEXT, event_id TEXT, payload TEXT, retrieved_at REAL);

CREATE TABLE IF NOT EXISTS analyses (
  id TEXT PRIMARY KEY, event_id TEXT, sport TEXT, payload TEXT,
  model_version TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS calibration (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT, bucket TEXT, n INTEGER, hit_rate REAL, expected REAL,
  delta REAL, model_version TEXT, computed_at REAL);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at REAL);

-- Records proven or suspected to be wrong (wrong event, wrong matchup, wrong
-- player). They are kept, never rewritten, and excluded from analysis reads and
-- from evaluation.
CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, ref_id TEXT NOT NULL, event_id TEXT,
  reason TEXT, detail TEXT, source TEXT, created_at REAL,
  UNIQUE(kind, ref_id));
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    return conn


_conn: Optional[sqlite3.Connection] = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        _conn.executescript(SCHEMA)
        _ensure_columns(_conn, "odds_snapshots", {
            "source_url": "TEXT", "provider_updated_at": "REAL",
            "provider": "TEXT", "fetched_at": "REAL", "kickoff_utc": "REAL",
            "quote_phase": "TEXT"})
        _ensure_columns(_conn, "predictions", {"snapshot_id": "TEXT"})
        _conn.commit()
    return _conn


def _ensure_columns(c: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
    for name, kind in columns.items():
        if name not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def q(sql: str, params: Iterable = ()) -> List[Dict[str, Any]]:
    cur = conn().execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def q1(sql: str, params: Iterable = ()) -> Optional[Dict[str, Any]]:
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql: str, params: Iterable = ()) -> None:
    with tx() as c:
        c.execute(sql, tuple(params))


def now() -> float:
    return time.time()


# ------------------------------------------------------------------ kv helpers
def kv_set(k: str, v: Any) -> None:
    ex("INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
       "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
       (k, json.dumps(v), now()))


def kv_get(k: str, default: Any = None) -> Any:
    row = q1("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(row["v"]) if row else default


def kv_time(k: str) -> Optional[float]:
    row = q1("SELECT updated_at FROM kv WHERE k=?", (k,))
    return row["updated_at"] if row else None


# ------------------------------------------------------------------ writers
def upsert_event(e: Dict) -> None:
    ex("""INSERT INTO events(id,sport,league,name,home_team,away_team,start_time,venue,status,meta,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET sport=excluded.sport, name=excluded.name,
            home_team=excluded.home_team, away_team=excluded.away_team,
            start_time=excluded.start_time, venue=excluded.venue,
            status=excluded.status, meta=excluded.meta, updated_at=excluded.updated_at""",
       (e["id"], e.get("sport"), e.get("league"), e.get("name"), e.get("home_team"),
        e.get("away_team"), e.get("start_time"), e.get("venue"), e.get("status", "scheduled"),
        json.dumps(e.get("meta") or {}), now()))


def link_nfl_provider_game(provider: str, provider_game_id: str, game_id: str) -> None:
    """A provider ID can never silently switch to another internal game."""
    old = q1("SELECT game_id FROM nfl_provider_games WHERE provider=? AND provider_game_id=?",
             (provider, str(provider_game_id)))
    if old and old["game_id"] != game_id:
        raise ValueError(f"provider event changed identity: {provider}:{provider_game_id}")
    ex("INSERT OR IGNORE INTO nfl_provider_games(provider,provider_game_id,game_id,linked_at) "
       "VALUES(?,?,?,?)", (provider, str(provider_game_id), game_id, now()))


def mapped_nfl_game(provider: str, provider_game_id: str) -> Optional[str]:
    row = q1("SELECT game_id FROM nfl_provider_games WHERE provider=? AND provider_game_id=?",
             (provider, str(provider_game_id)))
    return row["game_id"] if row else None


def save_nfl_snapshot(snapshot_id: str, game_id: str, payload: Dict,
                      model_version: str, feature_version: str) -> None:
    """Frozen JSON: duplicate IDs are accepted only for byte-identical content."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    old = q1("SELECT payload FROM nfl_game_snapshots WHERE id=?", (snapshot_id,))
    if old:
        if old["payload"] != encoded:
            raise ValueError("snapshot ID already exists with different contents")
        return
    ex("INSERT INTO nfl_game_snapshots(id,game_id,payload,model_version,feature_version,created_at) "
       "VALUES(?,?,?,?,?,?)", (snapshot_id, game_id, encoded, model_version, feature_version, now()))


def save_nfl_game_prediction(prediction_id: str, game_id: str, snapshot_id: str,
                             projection: Dict, model_version: str) -> None:
    ex("INSERT INTO nfl_game_predictions(id,game_id,snapshot_id,model_version,feature_version,"
       "projected_home_points,projected_away_points,projected_margin,projected_total,"
       "home_win_probability,uncertainty,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
       (prediction_id, game_id, snapshot_id, model_version,
        projection.get("feature_version") or "unknown", projection.get("proj_home"),
        projection.get("proj_away"), projection.get("margin"), projection.get("total"),
        projection.get("home_win_prob"), projection.get("uncertainty"), now()))


def store_odds(rows: List[Dict], source: str, is_demo: bool = False) -> int:
    """Append odds rows to history. Every successful retrieval is kept."""
    from .integrity import parse_time
    from .providers.normalize import normalize_book

    ts = now()
    event_ids = {r["event_id"] for r in rows}
    kickoffs = {event_id: parse_time((q1("SELECT start_time FROM events WHERE id=?",
                                        (event_id,)) or {}).get("start_time"))
                for event_id in event_ids}
    markets = {"h2h": "moneyline", "spreads": "spread", "totals": "total"}
    payload = []
    for r in rows:
        fetched = r.get("fetched_at")
        if fetched is None:
            fetched = r.get("captured_at")
        try:
            fetched = float(fetched) if fetched is not None else ts
        except (TypeError, ValueError):
            fetched = ts
        kickoff = kickoffs[r["event_id"]]
        phase = "current" if kickoff is not None and fetched <= ts+1 and fetched < kickoff else "unclassified"
        payload.append((r["event_id"],r.get("sport"),normalize_book(r["book"]),
                        markets.get(r["market"],r["market"]),r["selection"],
                        r.get("line"),r.get("price"),fetched,source,1 if is_demo else 0,
                        r.get("source_url"),r.get("provider_updated_at"),source,
                        fetched,kickoff,phase))
    with tx() as c:
        c.executemany(
            "INSERT INTO odds_snapshots(event_id,sport,book,market,selection,line,price,"
            "captured_at,source,is_demo,source_url,provider_updated_at,provider,fetched_at,"
            "kickoff_utc,quote_phase) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
    return len(payload)


def latest_odds(event_id: Optional[str] = None, sport: Optional[str] = None,
                max_age_s: Optional[float] = None,
                exclude_quarantined: bool = True) -> List[Dict]:
    """Most recent snapshot row per (event, book, market, selection).

    Quarantined rows (wrong event, wrong matchup, wrong player) are dropped by
    default so no analysis can price them.
    """
    where, params = [], []
    if event_id:
        where.append("o.event_id = ?")
        params.append(event_id)
    if sport:
        where.append("o.sport = ?")
        params.append(sport)
    if max_age_s:
        where.append("o.captured_at >= ?")
        params.append(now() - max_age_s)
    if exclude_quarantined:
        where.append("o.id NOT IN (SELECT CAST(ref_id AS INTEGER) FROM quarantine "
                     "WHERE kind = 'odds')")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return q(f"""
        SELECT o.* FROM odds_snapshots o
        JOIN (SELECT event_id, book, market, selection, MAX(captured_at) AS mx
              FROM odds_snapshots GROUP BY event_id, book, market, selection) m
          ON o.event_id=m.event_id AND o.book=m.book AND o.market=m.market
         AND o.selection=m.selection AND o.captured_at=m.mx
        {clause} ORDER BY o.event_id, o.market, o.selection""", params)


# ------------------------------------------------------------------ quarantine
def quarantine(kind: str, ref_id: str, event_id: Optional[str] = None,
               reason: str = "", detail: Optional[Any] = None,
               source: str = "integrity") -> None:
    """Record a suspect or wrong record. Idempotent; never rewrites the original."""
    ex("INSERT OR IGNORE INTO quarantine(kind,ref_id,event_id,reason,detail,source,created_at)"
       " VALUES(?,?,?,?,?,?,?)",
       (kind, str(ref_id), event_id, reason,
        json.dumps(detail or {}, default=str), source, now()))


def quarantined(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    if kind:
        return q("SELECT * FROM quarantine WHERE kind=? ORDER BY created_at DESC", (kind,))
    return q("SELECT * FROM quarantine ORDER BY created_at DESC")


def quarantined_ids(kind: str) -> set:
    return {str(row["ref_id"]) for row in q("SELECT ref_id FROM quarantine WHERE kind=?", (kind,))}


def is_quarantined(kind: str, ref_id: str) -> bool:
    return bool(q1("SELECT 1 AS x FROM quarantine WHERE kind=? AND ref_id=?",
                   (kind, str(ref_id))))


def odds_history(event_id: str, market: str, selection: str,
                 book: Optional[str] = None) -> List[Dict]:
    sql = ("SELECT * FROM odds_snapshots WHERE event_id=? AND market=? AND selection=?")
    params = [event_id, market, selection]
    if book:
        sql += " AND book=?"
        params.append(book)
    return q(sql + " ORDER BY captured_at ASC", params)


def save_prediction(p: Dict) -> None:
    cols = ["id", "event_id", "sport", "market", "selection", "side", "line", "book", "price",
            "model_prob", "implied_prob", "fair_price", "edge_pct", "ev_per_unit", "confidence",
            "confidence_bucket", "units", "state", "data_quality", "breakeven_price",
            "reasoning", "risks", "components", "signals", "sources", "model_version",
            "confidence_version", "data_snapshot", "created_at", "trigger_note", "snapshot_id"]
    vals = []
    for c in cols:
        v = p.get(c)
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    # A repeated ID is a data-integrity error. Issued predictions are never
    # overwritten or silently discarded.
    ex(f"INSERT INTO predictions({','.join(cols)}) "
       f"VALUES({','.join('?' * len(cols))})", vals)


def add_sources(event_id: str, sources: List[Dict]) -> None:
    if not sources:
        return
    with tx() as c:
        c.executemany(
            "INSERT INTO research_sources(event_id,kind,title,url,publisher,retrieved_at,summary,trust)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(event_id, s.get("kind", "web"), s.get("title"), s.get("url"), s.get("publisher"),
              s.get("retrieved_at", now()), s.get("summary"), s.get("trust", "unverified"))
             for s in sources])


def init() -> None:
    conn()
