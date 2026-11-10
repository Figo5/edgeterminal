"""Separate, append-only research about two archived missing-line decisions.

This module never creates a forecast, changes a live grade, or supplies learning
examples. Pair selection has no access to results. Readers never initialize or
repair data; the normal worker owns the bounded audit.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from . import db
from .integrity import parse_time

POLICY_ID = 'saved-line-gap-audit-2026-11-10-v1'
TARGETS = {'NBA': ('nba-2026-401909959',), 'NFL': ('nfl-2026-reg-09-buf-min',)}
TABLES = ('game_line_audit_bindings', 'game_line_audit_results')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Line audit requires finite evidence')
    return value


def _has_schema():
    present = {r['name'] for r in db.q("SELECT name FROM sqlite_master WHERE type='table' "
               "AND name IN ('game_line_audit_bindings','game_line_audit_results')")}
    if present and present != set(TABLES):
        raise ValueError('Historical line audit schema is incomplete')
    return present == set(TABLES)


def ensure_schema():
    with db.tx() as con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        _has_schema()  # A partial ledger is an integrity failure, not a fresh install.
        con.execute('CREATE TABLE IF NOT EXISTS game_line_audit_bindings('
                    'binding_id TEXT PRIMARY KEY,sport TEXT NOT NULL,game_id TEXT NOT NULL,'
                    'market TEXT NOT NULL,created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,'
                    'UNIQUE(sport,game_id,market))')
        con.execute('CREATE TABLE IF NOT EXISTS game_line_audit_results('
                    'id INTEGER PRIMARY KEY,binding_id TEXT NOT NULL REFERENCES game_line_audit_bindings(binding_id),'
                    'created_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,'
                    'UNIQUE(binding_id,sha256))')
        for table in TABLES:
            for action in ('UPDATE', 'DELETE'):
                con.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                            "BEGIN SELECT RAISE(ABORT,'Historical line audit is append-only'); END")


def _select_pair(rows, market, verify):
    """Select by saved time only; verify every candidate at the newest priced time."""
    selected = None
    selected_at = None
    for row in sorted(rows, key=lambda r: (r['created_at'], str(r.get('id', ''))), reverse=True):
        if selected_at is not None and row['created_at'] < selected_at:
            break
        pair = verify(row, market)
        if pair is None:
            continue
        if selected is not None and _sha(pair) != _sha(selected):
            raise ValueError('Historical line audit has ambiguous same-time pairs')
        selected, selected_at = pair, row['created_at']
    return selected


def _validate_pair(pair):
    sport, game = pair.get('sport'), pair.get('game_id')
    if sport not in TARGETS or game not in TARGETS[sport] or pair.get('market') not in ('spread', 'total'):
        raise ValueError('Unapproved historical line audit target')
    if not re.fullmatch('[0-9a-f]{64}', str(pair.get('forecast_sha256', ''))):
        raise ValueError('Historical forecast hash missing')
    if not isinstance(pair.get('forecast_id'), str) or not pair['forecast_id'] or not pair.get('model_version'):
        raise ValueError('Historical forecast identity missing')
    if not (_number(pair['observed_at']) <= _number(pair['as_of']) <=
            _number(pair['forecast_at']) < _number(pair['tipoff'])):
        raise ValueError('Historical line audit chronology conflict')
    if abs(_number(pair['line'])) > 1000 or abs(_number(pair['projection'])) > 1000:
        raise ValueError('Historical line audit value is out of bounds')
    if pair.get('side') not in ('home', 'away', 'over', 'under', 'pass'):
        raise ValueError('Historical decision side missing')
    if (pair['market'] == 'spread' and pair['side'] not in ('home', 'away', 'pass')) or \
            (pair['market'] == 'total' and pair['side'] not in ('over', 'under', 'pass')):
        raise ValueError('Historical decision side conflicts with market')
    if not isinstance(pair.get('source'), dict) or not pair['source']:
        raise ValueError('Historical line audit source missing')
    difference = pair['projection'] + pair['line'] if pair['market'] == 'spread' else pair['projection'] - pair['line']
    expected = ('pass' if sport == 'NFL' and difference == 0 else
                ('home' if difference >= 0 else 'away') if pair['market'] == 'spread' else
                'over' if difference >= 0 else 'under')
    if pair['side'] != expected:
        raise ValueError('Historical decision side does not match its saved projection')
    _encoded(pair)


def _append_binding(pair, *, now):
    _validate_pair(pair)
    if _number(now) <= pair['tipoff']:
        raise ValueError('Historical audit is not a prospective capture')
    identity = dict(policy_id=POLICY_ID, selection='latest_verified_priced_pregame_pair', pair=pair,
                    excluded_from_live_record=True, excluded_from_learning=True)
    ident = _sha(identity)
    body = dict(identity, audited_at=now)
    digest = _sha(body)
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT * FROM game_line_audit_bindings WHERE sport=? AND game_id=? AND market=?',
                          (pair['sport'], pair['game_id'], pair['market'])).fetchone()
        if old:
            if _binding(dict(old)) != pair or old['binding_id'] != ident:
                raise ValueError('Historical line audit binding changed')
            return dict(old)
        con.execute('INSERT INTO game_line_audit_bindings VALUES(?,?,?,?,?,?,?)',
                    (ident, pair['sport'], pair['game_id'], pair['market'], now, _encoded(body), digest))
        return dict(con.execute('SELECT * FROM game_line_audit_bindings WHERE binding_id=?', (ident,)).fetchone())


def _binding(row):
    body = json.loads(row['payload'])
    if (_sha(body) != row['sha256'] or
            row['binding_id'] != _sha({k:v for k,v in body.items() if k != 'audited_at'}) or
            body.get('audited_at') != row['created_at'] or
            body.get('policy_id') != POLICY_ID or not body.get('excluded_from_live_record') or
            not body.get('excluded_from_learning')):
        raise ValueError('Historical line audit binding seal conflict')
    pair = body['pair']
    _validate_pair(pair)
    if not _number(body['audited_at']) > pair['tipoff']:
        raise ValueError('Historical line audit recording clock conflict')
    if (pair['sport'], pair['game_id'], pair['market']) != (row['sport'], row['game_id'], row['market']):
        raise ValueError('Historical line audit identity conflict')
    return pair


def _append_result(binding, final, *, now):
    pair = _binding(binding)
    if not pair['tipoff'] <= _number(final['received_at']) <= _number(now):
        raise ValueError('Historical final receipt chronology conflict')
    home, away = (_number(final[k]) for k in ('home_score', 'away_score'))
    if not 0 <= home <= 1000 or not 0 <= away <= 1000 or int(home) != home or int(away) != away or not final.get('source'):
        raise ValueError('Historical final score/source invalid')
    cushion = home - away + pair['line'] if pair['market'] == 'spread' else home + away - pair['line']
    cushion *= -1 if pair['side'] in ('away', 'under') else 1
    result = ('pass' if pair['side'] == 'pass' else
              'won' if cushion > 0 else 'lost' if cushion < 0 else 'push')
    identity = dict(binding_id=binding['binding_id'], final=final, result=result,
                    excluded_from_live_record=True, excluded_from_learning=True)
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        prior = con.execute('SELECT * FROM game_line_audit_results WHERE binding_id=? ORDER BY id DESC LIMIT 1',
                            (binding['binding_id'],)).fetchone()
        if prior:
            old = json.loads(prior['payload'])
            if _sha(old) != prior['sha256'] or old.get('recorded_at') != prior['created_at']:
                raise ValueError('Historical line audit previous result seal conflict')
            if {k:v for k,v in old.items() if k != 'recorded_at'} == identity:
                return
            if final['received_at'] <= old['final']['received_at']:
                raise ValueError('Historical line audit final source is older or ambiguous')
        payload = dict(identity, recorded_at=now)
        digest = _sha(payload)
        con.execute('INSERT OR IGNORE INTO game_line_audit_results(binding_id,created_at,payload,sha256) VALUES(?,?,?,?)',
                    (binding['binding_id'], now, _encoded(payload), digest))


def _nba_pair(row, game, market):
    from . import nba_game_lines
    saved = _nba_forecast(row['id'])
    origin = nba_game_lines.verify_origin(saved, game, market)
    if not origin:
        return None
    group = origin['market']
    p = saved['payload']['projection']
    line = group['home_spread' if market == 'spread' else 'total']
    value = p['home_margin' if market == 'spread' else 'total']
    side = ('home' if value + line >= 0 else 'away') if market == 'spread' else ('over' if value >= line else 'under')
    return dict(sport='NBA', game_id=game['game_id'], market=market, forecast_id=saved['forecast_id'],
                forecast_sha256=saved['payload_sha256'], model_version=saved['model_version'],
                forecast_at=saved['created_at'], as_of=saved['payload']['as_of'], tipoff=game['tipoff_ts'],
                observed_at=group['observed_at'], home=game['home_team'], away=game['away_team'],
                line=line, projection=value, side=side, source=group)


def _nfl_pair(row, event, market):
    from . import nfl_game_lines
    origin = nfl_game_lines.verify_origin(dict(snapshot_id=row['id']), event, market)
    if not origin:
        return None
    quotes = origin['quotes']
    selected = next(q for q in quotes if q['selection'] == (event['home_team'] if market == 'spread' else 'Over'))
    p = origin['projection']
    component = p.get('spread_component') or {}
    line = selected['line']
    value = ((component['margin'] if component.get('used') else p['margin']) if market == 'spread' else p['total'])
    difference = value + line if market == 'spread' else value - line
    side = ('pass' if difference == 0 else ('home' if difference > 0 else 'away') if market == 'spread'
            else 'over' if difference > 0 else 'under')
    return dict(sport='NFL', game_id=event['id'], market=market, forecast_id=origin['snapshot_id'],
                forecast_sha256=origin['snapshot_sha256'], model_version=origin['model_version'],
                forecast_at=origin['created_at'], as_of=origin['as_of'], tipoff=parse_time(event['start_time']),
                observed_at=origin['captured_at'], home=event['home_team'], away=event['away_team'],
                line=line, projection=value, side=side,
                source=dict(quotes=quotes, book=origin['book'], captured_at=origin['captured_at']))


def _nba_final(game, now):
    from . import nba_data
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nba_game_grade_revisions'"):
        return None
    previous = 0
    latest = None
    for row in db.q('SELECT * FROM nba_game_grade_revisions WHERE game_id=? ORDER BY id', (game['game_id'],)):
        expected = hashlib.sha256((row['payload'] + ':' + str(previous)).encode()).hexdigest()
        if expected != row['sha256']:
            raise ValueError('Historical NBA final grade chain conflict')
        previous, latest = row['id'], row
    if not latest:
        return None
    source = db.q1('SELECT * FROM nba_source_artifacts WHERE id=?', (latest['source_artifact_id'],))
    if (not source or source['http_status'] != 200 or source['kind'] != 'final-boxscore' or
            source['received_at'] > now or hashlib.sha256(source['body']).hexdigest() != source['sha256'] or
            source['url'] != 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=' + game['espn_event_id']):
        raise ValueError('Historical NBA final source conflict')
    parsed = nba_data.parse_summary(json.loads(source['body']))
    actual = parsed['game']
    grade = json.loads(latest['payload'])
    if (not parsed['box_complete'] or actual['status'] != 'final' or actual['game_id'] != game['game_id'] or
            (actual['home_team'], actual['away_team']) != (game['home_team'], game['away_team']) or
            (actual['home_score'], actual['away_score']) != (grade['home_score'], grade['away_score'])):
        raise ValueError('Historical NBA final evidence conflict')
    return dict(home_score=actual['home_score'], away_score=actual['away_score'], received_at=source['received_at'],
                source=dict(id=source['id'], url=source['url'], sha256=source['sha256']))


def _nfl_final(event, now):
    from . import nfl_game_sources
    value = nfl_game_sources._known_finals(now).get(event['id'])
    if not value:
        return None
    # Freeze the exact accepted grade identity, not whichever final is latest on a later read.
    matches = [r for r in db.q('SELECT * FROM nfl_research_grades WHERE source_retrieved_at=?',
                              (value['received_at'],))
               if hashlib.sha256(r['payload'].encode()).hexdigest() == value['payload_sha256']]
    if len(matches) != 1:
        raise ValueError('Historical NFL final source identity is ambiguous')
    row = matches[0]
    return dict(home_score=value['home_score'], away_score=value['away_score'], received_at=value['received_at'],
                source=dict(payload_sha256=value['payload_sha256'],
                            freeze_sha=row['freeze_sha'], game_id=row['game_id'], revision=row['revision'],
                            url='https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'))


def _verify_final_reference(pair, final, now):
    """Re-read only the exact referenced final, never silently select a later result."""
    if not pair['tipoff'] <= _number(final['received_at']) <= _number(now):
        raise ValueError('Historical final source chronology conflict')
    ref = final['source']
    if pair['sport'] == 'NBA':
        from . import nba_data
        if isinstance(ref.get('id'), bool) or not isinstance(ref.get('id'), int):
            raise ValueError('Historical NBA final source identity conflict')
        row = db.q1('SELECT * FROM nba_source_artifacts WHERE id=?', (ref['id'],))
        url = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=' + pair['game_id'].rsplit('-', 1)[1]
        if (not row or row['http_status'] != 200 or row['kind'] != 'final-boxscore' or
                row['url'] != url or ref.get('url') != url or row['received_at'] != final['received_at'] or
                not 0 < len(row['body']) <= 16_000_000 or
                row['sha256'] != ref.get('sha256') or hashlib.sha256(row['body']).hexdigest() != ref.get('sha256')):
            raise ValueError('Historical NBA final source hash/identity conflict')
        parsed = nba_data.parse_summary(json.loads(row['body']))
        actual = parsed['game']
        if (not parsed['box_complete'] or actual['status'] != 'final' or actual['game_id'] != pair['game_id'] or
                (actual['home_team'], actual['away_team']) != (pair['home'], pair['away']) or
                parse_time(actual['tipoff_utc']) != pair['tipoff'] or
                (actual['home_score'], actual['away_score']) != (final['home_score'], final['away_score'])):
            raise ValueError('Historical NBA final source score conflict')
    else:
        from . import nfl_game_sources, nfl_identity
        row = db.q1('SELECT * FROM nfl_research_grades WHERE freeze_sha=? AND game_id=? AND revision=?',
                    (ref.get('freeze_sha'), ref.get('game_id'), ref.get('revision')))
        url = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
        if (not row or row['result_source'] != url or ref.get('url') != url or
                row['source_retrieved_at'] != final['received_at'] or
                hashlib.sha256(row['payload'].encode()).hexdigest() != ref.get('payload_sha256')):
            raise ValueError('Historical NFL final source hash/identity conflict')
        value = json.loads(row['payload'])
        season, week, away, home = nfl_game_sources._game_key(row['game_id'])
        if (f'nfl-{season}-reg-{week:02d}-{away}-{home}' != pair['game_id'] or
                value.get('freeze_sha256') != row['freeze_sha'] or value.get('game_id') != row['game_id'] or
                value.get('result_provider') != 'espn' or value.get('result_source') != url or
                value.get('result_request') != dict(season=season, season_type=2, week=week) or
                value.get('result_retrieved_at') != final['received_at'] or
                parse_time(value.get('kickoff_utc')) != pair['tipoff'] or
                (nfl_identity.team_code(value.get('home_team')), nfl_identity.team_code(value.get('away_team'))) != (home, away) or
                (nfl_identity.team_code(pair['home']), nfl_identity.team_code(pair['away'])) != (home, away) or
                (value.get('final_home_score'), value.get('final_away_score')) != (row['home_score'], row['away_score']) or
                (row['home_score'], row['away_score']) != (final['home_score'], final['away_score'])):
            raise ValueError('Historical NFL final source score/request conflict')


def _verify_bound_pair(pair):
    """Audit readers still verify the referenced original pair, never just a copy."""
    if pair['sport'] == 'NBA':
        game = db.q1('SELECT * FROM nba_games WHERE game_id=?', (pair['game_id'],))
        checked = _nba_pair({'id': pair['forecast_id']}, game, pair['market']) if game else None
    else:
        event = db.q1('SELECT * FROM events WHERE id=?', (pair['game_id'],))
        checked = _nfl_pair({'id': pair['forecast_id']}, event, pair['market']) if event else None
    if checked != pair:
        raise ValueError('Historical line audit original pair/source changed')


def _nba_forecast(ident):
    """Decode existing NBA evidence without the writer-side schema initializer."""
    row = db.q1('SELECT * FROM nba_forecasts WHERE forecast_id=?', (ident,))
    if not row:
        raise ValueError('Historical NBA forecast is missing')
    return dict(row, payload=json.loads(row['payload']), source_artifact_ids=json.loads(row['source_artifact_ids']))


def maintain(now=None):
    """Bounded worker-only audit; never fetches, forecasts, or fits a model."""
    stamp = _number(time.time() if now is None else now)
    ensure_schema()
    errors, count = [], 0
    for sport, targets in TARGETS.items():
        for game_id in targets:
            try:
                if sport == 'NBA':
                    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nba_games'"):
                        continue
                    game = db.q1('SELECT * FROM nba_games WHERE game_id=?', (game_id,))
                    if not game or stamp <= game['tipoff_ts']:
                        continue
                    rows = db.q('SELECT forecast_id AS id,created_at FROM nba_forecasts WHERE game_id=? '
                                'AND created_at<? ORDER BY created_at DESC,forecast_id DESC LIMIT 2048', (game_id, game['tipoff_ts']))
                    # Only revision-era evidence is eligible, not legacy projections.
                    rows = [r for r in rows if _nba_forecast(r['id'])['payload'].get('kind') == 'game-revision']
                    verify = lambda row, market: _nba_pair(row, game, market)
                else:
                    game = db.q1('SELECT * FROM events WHERE id=?', (game_id,))
                    if not game or stamp <= parse_time(game['start_time']):
                        continue
                    rows = db.q('SELECT id,created_at FROM nfl_game_snapshots WHERE game_id=? AND created_at<? '
                                'ORDER BY created_at DESC,id DESC LIMIT 2048', (game_id, parse_time(game['start_time'])))
                    rows = [r for r in rows if json.loads(db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',
                        (r['id'],))['payload']).get('forecast_policy_id') == 'nfl-game-source-revisions-v1']
                    verify = lambda row, market: _nfl_pair(row, game, market)
                for market in ('spread', 'total'):
                    # A recovery only documents a gap; it never supplants an already priced latest decision.
                    if not rows or verify(rows[0], market) is not None:
                        continue
                    pair = _select_pair(rows, market, verify)
                    if pair is None:
                        continue
                    binding = _append_binding(pair, now=stamp)
                    # This read occurs strictly after the binding transaction committed.
                    final = _nba_final(game, stamp) if sport == 'NBA' else _nfl_final(game, stamp)
                    if final:
                        _append_result(binding, final, now=stamp)
                    count += 1
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(dict(sport=sport, game_id=game_id, error=str(exc)[:250]))
    return dict(ok=not errors, pairs=count, errors=errors, excluded_from_live_record=True, excluded_from_learning=True)


def view(sport):
    if sport not in TARGETS:
        raise ValueError('Unknown audit sport')
    rows = []
    if _has_schema():
        for binding in db.q('SELECT * FROM game_line_audit_bindings WHERE sport=? ORDER BY game_id,market', (sport,)):
            pair = _binding(binding)
            _verify_bound_pair(pair)
            result = db.q1('SELECT * FROM game_line_audit_results WHERE binding_id=? ORDER BY id DESC LIMIT 1',
                           (binding['binding_id'],))
            state = 'pending'
            if result:
                payload = json.loads(result['payload'])
                if _sha(payload) != result['sha256'] or payload.get('recorded_at') != result['created_at'] or \
                        result['created_at'] < binding['created_at'] or payload.get('binding_id') != binding['binding_id'] or \
                        not payload.get('excluded_from_live_record') or not payload.get('excluded_from_learning'):
                    raise ValueError('Historical line audit result seal conflict')
                _verify_final_reference(pair, payload['final'], result['created_at'])
                final = payload['final']
                cushion = (final['home_score'] - final['away_score'] + pair['line'] if pair['market'] == 'spread'
                           else final['home_score'] + final['away_score'] - pair['line'])
                cushion *= -1 if pair['side'] in ('away', 'under') else 1
                expected = ('pass' if pair['side'] == 'pass' else 'won' if cushion > 0 else 'lost' if cushion < 0 else 'push')
                if payload['result'] != expected:
                    raise ValueError('Historical line audit result differs from its bound pair')
                state = payload['result']
            rows.append(dict(pair, binding_id=binding['binding_id'], audited_at=binding['created_at'], result=state))
    return dict(policy_id=POLICY_ID, sport=sport, rows=rows,
                excluded_from_live_record=True, excluded_from_learning=True,
                note='Recovered historical research only. Original live forecasts, grades and season totals are unchanged.')


def public_view(sport):
    """An auxiliary audit failure stays visible without hiding the original record.

    The strict reader and worker still raise/report the underlying integrity
    failure. This presentation boundary never returns unchecked recovery rows.
    """
    if sport not in TARGETS:
        raise ValueError('Unknown audit sport')
    try:
        return dict(view(sport), status='ok')
    except Exception as exc:
        return dict(policy_id=POLICY_ID, sport=sport, rows=[], status='needs_review',
                    error_type=type(exc).__name__, excluded_from_live_record=True, excluded_from_learning=True,
                    note='Recovered research did not pass its source integrity checks. Original live season totals remain visible.')
