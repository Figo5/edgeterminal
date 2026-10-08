"""Refresh due research on a page visit, with one shared five-minute throttle."""
from __future__ import annotations

import json
import logging
import time

from . import db, research_refresh
from .config import settings

INTERVAL = 300
KEY = 'product_visit_refresh'
log = logging.getLogger(__name__)


def _with_attention(public: dict, steps: dict | None = None) -> dict:
    """Name an unresolved snapshot failure without exposing provider errors."""
    from .product_insights import unresolved_failures
    latest = db.q('SELECT name,success FROM research_refresh_steps WHERE id IN '
                  '(SELECT MAX(id) FROM research_refresh_steps GROUP BY name)')
    names = {row['name'] for row in unresolved_failures(latest)}
    names.update(name for name, step in (steps or {}).items() if step.get('ok') is False)
    previous_attention = public.pop('attention', None)
    if names:
        public['ok'] = False
        public['attention'] = ('weekly_forecast' if all(
            name.startswith('weekly_freeze_') for name in names) else 'updates')
        if public['status'] != 'busy':
            public['status'] = 'needs_attention'
    elif previous_attention:
        # A later successful step or verified freeze resolves a cached warning.
        public['ok'] = True
    return public


def check(now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    if settings.odds_provider != 'espn' or settings.demo_mode:
        raise ValueError('Page updates require ESPN and real research data')
    # Claim before fetching so concurrent tabs/users cannot multiply requests.
    # The ordinary worker's file lock and provider quota guards still apply.
    with db.tx() as conn:
        row = conn.execute('SELECT v FROM kv WHERE k=?', (KEY,)).fetchone()
        previous = json.loads(row['v']) if row else {}
        def cached(public):
            # Health reads this same cache. Persist a resolved warning even when
            # a recent worker tick means this visit needs no provider requests.
            # A concurrent visit must leave the active refresh claim intact.
            if previous.get('status') != 'running' and public != previous:
                conn.execute('INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) '
                             'ON CONFLICT(k) DO UPDATE SET v=excluded.v,updated_at=excluded.updated_at',
                             (KEY, json.dumps(public), stamp))
            return public
        age = stamp - previous.get('started_at', 0)
        if previous and 0 <= age < INTERVAL:
            return cached(_with_attention({**previous,
                'status': 'busy' if previous.get('status') == 'running' else 'cached',
                'did_work': False}))
        tick = conn.execute('SELECT started_at,success FROM research_refresh_ticks '
                            'ORDER BY started_at DESC LIMIT 1').fetchone()
        if tick and 0 <= stamp - tick['started_at'] < INTERVAL:
            return cached(_with_attention({'ok': bool(tick['success']), 'status': 'cached',
                                          'did_work': False, 'checked_at': tick['started_at']}))
        claim = {'ok': True, 'status': 'running', 'started_at': stamp, 'did_work': False}
        conn.execute('INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) '
                     'ON CONFLICT(k) DO UPDATE SET v=excluded.v,updated_at=excluded.updated_at',
                     (KEY, json.dumps(claim), stamp))
    try:
        result = research_refresh.run(record_tick=True)
        if result.get('status') != 'already_running':
            from .service_ops import maintain_local
            maintain_local()
        busy = result.get('status') == 'already_running'
        successful = result.get('success') is not False
        public = {'ok': successful, 'status': 'busy' if busy else 'checked' if successful else 'needs_attention',
                  'did_work': bool(result.get('steps')), 'checked_at': time.time(), 'started_at': stamp}
        public = _with_attention(public, result.get('steps'))
    except Exception as exc:
        log.warning('page research check failed: %s', type(exc).__name__)
        public = {'ok': False, 'status': 'needs_attention', 'did_work': False,
                  'checked_at': time.time(), 'started_at': stamp}
    db.kv_set(KEY, public)
    return public
