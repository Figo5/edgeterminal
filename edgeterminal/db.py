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

CREATE TABLE IF NOT EXISTS odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT, sport TEXT, book TEXT, market TEXT, selection TEXT,
  line REAL, price REAL, captured_at REAL, source TEXT, is_demo INTEGER DEFAULT 0,
  source_url TEXT, provider_updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_odds_event ON odds_snapshots(event_id, market, selection, captured_at);
CREATE INDEX IF NOT EXISTS ix_odds_time ON odds_snapshots(captured_at);

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
  created_at REAL, trigger_note TEXT);
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
    return conn


_conn: Optional[sqlite3.Connection] = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        _conn.executescript(SCHEMA)
        _ensure_columns(_conn, "odds_snapshots", {
            "source_url": "TEXT", "provider_updated_at": "REAL"})
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


def store_odds(rows: List[Dict], source: str, is_demo: bool = False) -> int:
    """Append odds rows to history. Every successful retrieval is kept."""
    ts = now()
    payload = [(r["event_id"], r.get("sport"), r["book"], r["market"], r["selection"],
                r.get("line"), r.get("price"), ts, source, 1 if is_demo else 0,
                r.get("source_url"), r.get("provider_updated_at")) for r in rows]
    with tx() as c:
        c.executemany(
            "INSERT INTO odds_snapshots(event_id,sport,book,market,selection,line,price,"
            "captured_at,source,is_demo,source_url,provider_updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", payload)
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
            "confidence_version", "data_snapshot", "created_at", "trigger_note"]
    vals = []
    for c in cols:
        v = p.get(c)
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    ex(f"INSERT OR REPLACE INTO predictions({','.join(cols)}) "
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
