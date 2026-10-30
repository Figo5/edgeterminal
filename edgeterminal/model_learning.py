"""Recorded learning cycles run inside the shared research-refresh lock.

Reads never fit models. New fits are isolated from capture, have held-out
validation, and can only become active through the append-only registry.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import time
from zoneinfo import ZoneInfo

from . import db

ET = ZoneInfo('America/New_York')
SPORTS = ('NBA', 'NFL')
WEEK = 7 * 86400
DAY = 86400
RETRY = 300


def _registry():
    from . import model_learning_store
    return model_learning_store


def _adapter(sport):
    if sport == 'NBA':
        from . import nba_learning
        return nba_learning
    from . import nfl_learning
    return nfl_learning


def ensure_schema():
    from . import nba_store
    nba_store.ensure_schema()
    db.ex('CREATE TABLE IF NOT EXISTS model_learning_runs ('
          'id INTEGER PRIMARY KEY, sport TEXT NOT NULL CHECK(sport IN (\'NBA\',\'NFL\')),'
          'kind TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL NOT NULL,'
          'evidence_key TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        db.ex(f'CREATE TRIGGER IF NOT EXISTS model_learning_runs_no_{action.lower()} '
              f'BEFORE {action} ON model_learning_runs BEGIN '
              "SELECT RAISE(ABORT, 'learning runs are append-only'); END")


def _last(sport, kind):
    row = db.q1('SELECT * FROM model_learning_runs WHERE sport=? AND kind=? ORDER BY id DESC LIMIT 1',
                (sport, kind))
    if row:
        row['detail'] = json.loads(row['detail'])
    return row


def _record(sport, kind, now, evidence_key, detail):
    finished = max(now, time.time())
    db.ex('INSERT INTO model_learning_runs(sport,kind,started_at,finished_at,evidence_key,status,detail) '
          'VALUES(?,?,?,?,?,?,?)', (sport, kind, now, finished, evidence_key,
              detail.get('status', 'ok'), json.dumps(detail, sort_keys=True, allow_nan=False)))
    return detail


def _evidence_key(sport):
    table = 'nba_grade_revisions' if sport == 'NBA' else 'nfl_prop_grade_revisions'
    row = db.q1(f'SELECT MAX(graded_at) AS latest,COUNT(*) AS n FROM {table}')
    return json.dumps(row, sort_keys=True)


def _near_kickoff(now):
    nba = db.q1("SELECT 1 FROM nba_games WHERE status='scheduled' AND tipoff_ts>? AND tipoff_ts<=? LIMIT 1",
                (now, now + 3600))
    if nba:
        return True
    from .integrity import parse_time
    return any(0 < (parse_time(row['start_time']) or 0) - now <= 3600
               for row in db.q("SELECT start_time FROM events WHERE sport='NFL' AND status NOT IN "
                               "('STATUS_FINAL','STATUS_CANCELED','STATUS_CANCELLED')"))


def _four_am(now):
    return datetime.fromtimestamp(now, ET).replace(hour=4, minute=0, second=0, microsecond=0)


def _next_due(last, kind, now, evidence_key=None):
    if kind == 'diagnostics':
        start = _four_am(now)
        if now < start.timestamp():
            return start.timestamp()
        if not last:
            return now
        if last['status'] == 'error':
            return last['started_at'] + RETRY
        previous = datetime.fromtimestamp(last['started_at'], ET).date()
        if previous < start.date():
            return now
        return (start + timedelta(days=1)).timestamp()
    if not last:
        return now
    if last['status'] == 'error':
        return last['started_at'] + RETRY
    if last['status'] in {'waiting', 'insufficient_data', 'deferred'}:
        if evidence_key is not None and evidence_key != last['evidence_key']:
            return last['started_at'] + 3600
        return last['started_at'] + DAY
    return last['started_at'] + WEEK


def _diagnostics(sport, now):
    adapter = _adapter(sport)
    if hasattr(adapter, 'nightly_diagnostics'):
        return adapter.nightly_diagnostics(now)
    from . import nfl_top_five
    rows = [r for g in nfl_top_five.view()['games'] for r in g['rows']
            if r['result'] in {'won', 'lost'}]
    return {'status': 'ok', 'graded': len(rows),
            'win_rate': sum(r['result'] == 'won' for r in rows) / len(rows) if rows else None,
            'brier': sum((r['confidence'] - (r['result'] == 'won')) ** 2 for r in rows) / len(rows) if rows else None,
            'note': 'Descriptive saved NFL selections; no parameters fitted.'}


def _train(sport, now):
    adapter = _adapter(sport)
    builder = getattr(adapter, 'build_quality_candidate', adapter.build_candidate)
    result = builder(now)
    if result.get('status') != 'ready':
        return {'status': 'waiting', 'reason': result.get('reason', result.get('message', 'More verified data is needed.')),
                **{key: result[key] for key in ('validation_games', 'validation_rows', 'coverage') if key in result}}
    if _protocol(sport) != 'legacy-v1' and (
            not result['artifact'].get('evaluation_family') or
            not isinstance(result['artifact'].get('ranking_changed'), bool) or
            result['artifact'].get('probability_contract') != 'conditional_side_given_no_push'):
        raise ValueError('quality protocol requires a family, selection policy and decided-outcome probability contract')
    family = result['artifact'].get('evaluation_family')
    if family and not _registry().holdout_available(sport, family,
            {row['game_id'] for row in result['evaluation']['rows']}):
        return {'status': 'waiting', 'reason': 'New untouched outer validation games are required before another proposal.'}
    candidate = _registry().register_candidate(sport, result['version'], result['training_cutoff'],
        result['artifact'], result['evaluation'], result['sources'], now=now)
    decision = _registry().activate_if_passes(candidate['candidate_id'], now=now)
    return {'status': decision['status'], 'candidate_id': candidate['candidate_id'],
            'version': result['version'],
            'reason': decision.get('reason'), 'reasons': decision.get('reasons', []),
            'evaluation': {key: decision[key] for key in ('rows', 'games', 'markets', 'aggregate') if key in decision}}


def _protocol(sport):
    return getattr(_adapter(sport), 'QUALITY_PROTOCOL', 'legacy-v1')


def _due(sport, last, kind, now, evidence):
    # A new fixed protocol receives one attempt; reads still never train.
    if kind == 'retuning' and _protocol(sport) != 'legacy-v1' and (
            not last or last['detail'].get('learning_protocol') != _protocol(sport)):
        return now
    return _next_due(last, kind, now, evidence)


def _integrity(sport, now):
    """Rollback a damaged active artifact; a bad night alone cannot change it."""
    registry = _registry()
    try:
        registry.verify_active(sport, now=now)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if registry.active(sport) is None:
            raise
        result = registry.rollback(sport, 'Active artifact integrity check failed: ' + str(exc)[:180], now=now)
        registry.verify_active(sport, now=now)
        return result
    return None


def run(now=None):
    """Attempt due learning work; caller holds research_refresh.LOCK."""
    now = time.time() if now is None else now
    if isinstance(now, bool) or not math.isfinite(now):
        raise ValueError('learning time must be finite')
    ensure_schema()
    results = {}
    for sport in SPORTS:
        checks = {}
        evidence = _evidence_key(sport)
        try:
            _registry().load_active(sport)
        except (ValueError, OSError, KeyError, TypeError):
            try:
                recovery = _integrity(sport, now)
                detail = {'status': 'rolled_back', 'recovery': recovery,
                          'message': 'Restored a verified prior model after an integrity failure.'}
            except Exception as exc:
                detail = {'status': 'error', 'reason': str(exc)[:250],
                          'message': 'Active model integrity needs review; serving fails closed.'}
            checks['integrity'] = _record(sport, 'integrity', now, evidence, detail)
            if detail['status'] == 'error':
                results[sport] = checks
                continue
        else:
            prior = _last(sport, 'integrity')
            if prior and prior['status'] == 'error':
                checks['integrity'] = _record(sport, 'integrity', now, evidence,
                    {'status': 'ok', 'message': 'Active model integrity recheck succeeded.'})
        for kind in ('diagnostics', 'retuning'):
            last = _last(sport, kind)
            if now < _due(sport, last, kind, now, evidence):
                checks[kind] = {'status': 'cached'}
                continue
            if kind == 'retuning' and _near_kickoff(now):
                checks[kind] = {'status': 'deferred', 'reason': 'Upcoming kickoff takes priority; source capture and grading continue.'}
                continue
            try:
                rollback = _integrity(sport, now) if kind == 'diagnostics' else None
                if kind == 'retuning':
                    _registry().load_active(sport)
                detail = _diagnostics(sport, now) if kind == 'diagnostics' else _train(sport, now)
                if not isinstance(detail, dict):
                    raise ValueError('learning adapter returned no structured result')
                detail = dict(detail, status=detail.get('status', 'ok'))
                if rollback:
                    detail['integrity_rollback'] = rollback
                json.dumps(detail, allow_nan=False)
            except Exception as exc:
                detail = {'status': 'error', 'reason': str(exc)[:250], 'error_type': type(exc).__name__,
                          'message': 'Learning check failed; existing saved forecasts and grades remain unchanged.'}
            if kind == 'retuning':
                detail['learning_protocol'] = _protocol(sport)
            checks[kind] = _record(sport, kind, now, evidence, detail)
        results[sport] = checks
    healthy = all(check['status'] != 'error' for checks in results.values() for check in checks.values())
    # Cached failures remain visible until a real successful retry.
    healthy = healthy and all((_last(sport, kind) or {}).get('status') != 'error'
                              for sport in SPORTS for kind in ('diagnostics', 'retuning'))
    return {'ok': healthy, 'sports': results, 'designation': 'PASS', 'readiness': 'RESEARCH_ONLY',
            'message': 'Automatic learning checked due diagnostics and retuning.' if healthy else
                       'An automatic learning check needs review; the built-in worker retries.'}


def status(sport=None, *, now=None):
    """Read recorded learning state. This function cannot train or activate."""
    now = time.time() if now is None else now
    if sport is not None and sport not in SPORTS:
        raise ValueError('unsupported learning sport')
    ensure_schema()
    output = {}
    for name in (SPORTS if sport is None else (sport,)):
        current = _registry().active(name)
        metadata = {key: current[key] for key in ('candidate_id', 'version', 'training_cutoff', 'fingerprint',
                    'activated_at') if key in current} if current else None
        saved = {'enabled': True, 'sport': name, 'active': metadata,
                 'schedule': 'Nightly diagnostics after 4 AM Eastern; weekly retuning when evidence is sufficient.'}
        recovery = _last(name, 'integrity')
        if recovery:
            saved['integrity'] = {'status': recovery['status'], 'checked_at': recovery['started_at'],
                                  'message': recovery['detail']['message']}
        for kind in ('diagnostics', 'retuning'):
            last = _last(name, kind)
            saved[kind] = {'status': last['status'] if last else 'not_checked',
                           'checked_at': last['started_at'] if last else None,
                           'next_due_at': _due(name, last, kind, now, _evidence_key(name)),
                           'detail': last['detail'] if last else {}}
        output[name] = saved
    return output[sport] if sport else {'enabled': True, 'sports': output}
