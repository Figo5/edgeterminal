"""Explain stored forecasts and expose service health without requesting feeds."""
from __future__ import annotations

import hashlib
import json
import time

from . import db
from .config import settings
from .integrity import parse_time
from .config import ROOT


def prop(row: dict, now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    result = dict(row)
    from .nfl_prop_availability import view as availability
    result['availability'] = availability(row.get('player_name') or row['player'], row['canonical_game_id'],
                                          row['kickoff_utc'], row['market'], stamp)
    point, line = row.get('projection'), row.get('line')
    reasons = []
    if point is not None and line is not None:
        difference = point - line
        reasons.append(f"Model projects {point:.1f}; the selected line is {line:g} ({abs(difference):.1f} {'above' if difference >= 0 else 'below'} the line).")
    sigma = row.get('uncertainty_sigma')
    if sigma is not None:
        reasons.append(f"The frozen model uses a residual spread of {sigma:.1f} for this stat. The percentage is a model estimate.")
    context = row.get('input_context') or {}
    if context.get('stat_last3') is not None:
        reasons.append(f"Prior three appearances averaged {context['stat_last3']:.1f} in this stat and {context['opportunities_last3']:.1f} {context['opportunity_kind']}.")
    result['explanation'] = reasons
    price = row.get('price')
    implied = (100 / (price + 100) if price > 0 else -price / (-price + 100)) if price and abs(price) >= 100 else None
    result['implied_probability'] = implied
    result['probability_difference'] = row['confidence'] - implied if implied is not None else None
    # A later line is displayed separately. It never changes the locked selection.
    latest = db.q1('SELECT id,line,price,provider_updated_at,retrieved_at FROM nfl_prop_snapshots '
                    'WHERE canonical_game_id=? AND player_id=? AND market=? AND sportsbook=? '
                    "AND side=? AND line_type='main' AND retrieved_at<=? AND retrieved_at<? "
                    'AND (provider_updated_at IS NULL OR provider_updated_at<=?) '
                    'ORDER BY retrieved_at DESC,id DESC LIMIT 1',
                    (row['canonical_game_id'], row['player_id'], row['market'], row['sportsbook'],
                     row['side'], stamp, row['kickoff_utc'], stamp))
    observed = (latest.get('provider_updated_at') or latest['retrieved_at']) if latest else None
    result['latest_quote'] = ({**latest, 'observed_at': observed,
                               'is_stale': stamp - observed > settings.stale_odds_s,
                               'line_changed': latest['line'] != line,
                               'game_started': stamp >= row['kickoff_utc']} if latest else None)
    result['quote_source'] = row.get('quote_source') or 'SportsGameOdds / ' + row['sportsbook']
    result['availability_note'] = 'Player participation is not confirmed by a prop line. Review availability before kickoff.'
    return result


def game(projection: dict | None) -> list[str]:
    if not projection:
        return ['No saved pregame projection is available.']
    notes = []
    n = projection.get('sample_size_n')
    if n is not None:
        notes.append(f'Team form includes {n:g} prior games in the saved inputs.')
    contribution = (projection.get('feature_groups') or {}).get('efficiency_points')
    if contribution is not None:
        notes.append(f'Team efficiency contributes {contribution:+.1f} points to the home margin.')
    weather = projection.get('weather_points')
    if weather:
        notes.append(f'The stored weather adjustment is {weather:+.1f} points.')
    notes.append('This forecast uses its saved pregame inputs. New observations do not rewrite the record.')
    return notes


def unresolved_failures(steps: list[dict], now: float | None = None) -> list[dict]:
    """Keep current failures; expired capture windows remain in the audit log."""
    stamp = time.time() if now is None else now
    failures = []
    for step in steps:
        if step.get('success'):
            continue
        parts = step['name'].split('_')
        resolved = False
        if step['name'] == 'near_kickoff_market' and step.get('attempted_at') is not None:
            from .nfl_market_capture import WINDOW_S
            # Every target was within this window when the attempt started.
            # After it closes, current coverage checks describe new targets.
            resolved = stamp >= step['attempted_at'] + WINDOW_S
        if len(parts) == 4 and parts[:2] == ['weekly', 'freeze'] and parts[2].isdigit() and parts[3].isdigit():
            frozen = db.q1('SELECT file_path,freeze_sha FROM nfl_research_freezes '
                           "WHERE season=? AND week=? AND family='linear'", (int(parts[2]), int(parts[3])))
            if frozen:
                path = ROOT / frozen['file_path']
                resolved = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == frozen['freeze_sha']
        if not resolved:
            failures.append(step)
    return failures


def update_checks(steps: list[dict], now: float) -> list[dict]:
    """Public, source-safe status for the latest refresh steps."""
    from .research_refresh import result_interval
    failed = {r['name'] for r in unresolved_failures(steps, now)}
    labels = {'current_game_odds': 'ESPN game quotes',
              'top_five_capture': 'Five-prop selections',
              'top_five_grading': 'Player-prop grading', 'game_grading': 'Game grading',
              'near_kickoff_market': 'Near-kickoff game quotes'}
    checks = []
    for step in steps:
        if step['name'] not in labels:
            continue
        active = step['name'] in failed
        detail = json.loads(step.get('detail') or '{}')
        error = detail.get('error') or ''
        official_review = [c for r in detail.get('result', {}).get('results', [])
                           for c in r.get('official_checks', []) if c.get('status') == 'needs_review']
        transport = any(kind in error for kind in (
            'ReadError', 'ReadTimeout', 'ConnectError', 'ConnectTimeout', 'RemoteProtocolError'))
        state = 'retry_pending' if active and transport else 'needs_review' if active else (
            'ok' if step['success'] else 'past_window')
        message = {'ok': 'Latest check succeeded.',
                   'past_window': 'The failed capture window has ended; retained in audit history.',
                   'retry_pending': 'Result source could not be read. Waiting for the next scheduled retry.',
                   'needs_review': 'Latest check failed and still needs review.'}[state]
        if active and 'provider player ID changed name' in error:
            message = 'The prop provider returned a conflicting player identity. Capture was rejected.'
        if official_review:
            active, state = True, 'needs_review'
            message = 'Official participation evidence is missing, ambiguous, or conflicting. Affected results remain pending; the worker will retry.'
        checks.append({'name': step['name'], 'label': labels[step['name']], 'state': state,
                       'message': message, 'checked_at': step.get('attempted_at'),
                       'next_retry_at': step['attempted_at'] + result_interval(step)
                       if active and step['name'] in {'top_five_grading', 'game_grading'} else None})
    return checks


def health(now: float | None = None) -> dict:
    stamp = time.time() if now is None else now
    schedule = db.kv_get('nfl_current_schedule') or {}
    checked = db.kv_time('nfl_current_schedule')
    tick = db.q1('SELECT started_at,success FROM research_refresh_ticks ORDER BY started_at DESC LIMIT 1')
    steps = db.q('SELECT name,success,detail,attempted_at FROM research_refresh_steps WHERE id IN '
                 '(SELECT MAX(id) FROM research_refresh_steps GROUP BY name) ORDER BY name')
    failures = unresolved_failures(steps, stamp)
    covered = {r['game_id'] for r in db.q('SELECT game_id FROM nfl_top_five_selections')}
    from .nfl_top_five import coverage
    prop_coverage = coverage(schedule.get('game_ids', []), stamp)
    near = []
    upcoming = []
    for game_id in schedule.get('game_ids', []):
        event = db.q1('SELECT start_time,status FROM events WHERE id=?', (game_id,))
        kickoff = parse_time(event['start_time']) if event else None
        if kickoff and stamp < kickoff and event['status'] not in {'STATUS_FINAL', 'STATUS_CANCELED', 'STATUS_POSTPONED'}:
            odds = db.q1("SELECT COUNT(DISTINCT market) n FROM odds_snapshots WHERE event_id=? "
                          "AND is_demo=0 AND market IN ('moneyline','spread','total') AND price IS NOT NULL "
                          "AND (market='moneyline' OR line IS NOT NULL) "
                          "AND id NOT IN (SELECT CAST(ref_id AS INTEGER) FROM quarantine WHERE kind='odds') "
                          "AND captured_at>=? AND captured_at<=?",
                          (game_id, stamp - settings.stale_odds_s, stamp))['n']
            item = {'game_id': game_id, 'props_ready': game_id in covered,
                    'game_quotes_ready': odds == 3}
            upcoming.append(item)
            if kickoff - stamp <= 900 and (game_id not in covered or odds != 3):
                near.append(item)
    lag = stamp - tick['started_at'] if tick else None
    warnings = []
    if lag is None or lag > 12 * 60:
        warnings.append('The background worker has not checked in recently. Updates resume when the local service runs.')
    elif tick and not tick['success']:
        warnings.append('The latest background check did not finish successfully.')
    if checked is None or stamp - checked > settings.stale_schedule_s:
        warnings.append('The saved schedule needs a refresh.')
    checks = update_checks(steps, stamp)
    for check in checks:
        if check['state'] in {'retry_pending', 'needs_review'}:
            warnings.append(f"{check['label']}: {check['message']}")
    if any(r['name'] not in {c['name'] for c in checks} for r in failures):
        warnings.append('A source check needs review in About & data.')
    if near:
        warnings.append('A game starting within 15 minutes lacks five selections or fresh game quotes.')
    if any(r['status'] in {'projection_gap', 'selection_needed', 'capture_failed', 'missing_source', 'source_paused'} for r in prop_coverage):
        warnings.append('Some player prop selections need source or model-input review. See game coverage.')
    backup = db.kv_get('last_service_backup') or {}
    if (db.kv_get('last_backup_health') or {}).get('ok') is False:
        warnings.append('The local database backup needs review.')
    if (db.kv_get('member_alerts_health') or {}).get('ok') is False:
        warnings.append('Saved-selection updates need a source-integrity review.')
    visit = db.kv_get('product_visit_refresh') or {}
    recovered = tick and tick['success'] and tick['started_at'] >= visit.get('checked_at', float('inf'))
    # Tagged attention mirrors step failures already described above. Only an
    # independent page exception warrants another warning.
    if visit.get('ok') is False and not visit.get('attention') and not recovered:
        warnings.append('The latest page-triggered update check needs review.')
    return {'state': 'attention' if warnings else 'healthy', 'warnings': warnings,
            'worker_checked_at': tick['started_at'] if tick else None,
            'schedule_checked_at': checked, 'week': schedule.get('week'),
            'upcoming_games': len(upcoming), 'games_with_five': sum(x['props_ready'] for x in upcoming),
            'near_kickoff_missing': near, 'checked_at': stamp,
            'prop_coverage': prop_coverage,
            'update_checks': checks,
            'runtime': 'hosted' if settings.hosted_mode else 'local',
            'last_backup_at': backup.get('created_at'),
            'readiness': 'RESEARCH_ONLY', 'designation': 'PASS'}
