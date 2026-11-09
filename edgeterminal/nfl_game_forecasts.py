"""Source-worker NFL revisions; page reads never fetch or create predictions.

The numerical baseline is unchanged. New supported pregame inputs/quotes append
history; a separately verified spread component never changes prop game scripts.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from . import db, nfl_identity, nfl_market
from .analytics import football
from .analytics.ratings import shrink_stats
from .config import settings
from .integrity import parse_time

POLICY_ID = 'nfl-game-source-revisions-v1'
FINAL = {'STATUS_FINAL', 'FINAL', 'COMPLETED', 'POST', 'CLOSED', 'CANCELLED',
         'STATUS_CANCELED', 'STATUS_CANCELLED', 'STATUS_POSTPONED'}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _physical(now=None):
    if now is not None and (isinstance(now, bool) or not math.isfinite(now)):
        raise ValueError('NFL forecast time must be finite')
    return max(time.time(), time.time() if now is None else now)


def selected(event):
    """Last actual pregame snapshot, using identical deterministic tie breaking."""
    kickoff = parse_time(event.get('start_time'))
    if kickoff is None:
        return None
    rows = db.q('SELECT p.*,s.payload FROM nfl_game_predictions p JOIN nfl_game_snapshots s '
                'ON s.id=p.snapshot_id WHERE p.game_id=? AND p.created_at<? '
                'ORDER BY p.created_at DESC,p.id DESC', (event['id'], kickoff))
    for row in rows:
        payload = json.loads(row['payload'])
        identity = payload.get('game') or {}
        original = parse_time(identity.get('kickoff_utc')) or kickoff
        if row['created_at'] >= min(original, kickoff):
            continue
        if payload.get('forecast_policy_id') == POLICY_ID:
            if (hashlib.sha256(row['payload'].encode()).hexdigest()[:24] != row['snapshot_id']
                    or identity.get('id') != event['id']
                    or identity.get('home_team') != event['home_team']
                    or identity.get('away_team') != event['away_team']):
                raise ValueError('NFL saved forecast identity/hash conflict')
        return dict(row, payload=payload)
    return None


def _future(event, now):
    tip = parse_time(event.get('start_time'))
    return bool(tip and now < tip and str(event.get('status', '')).upper() not in FINAL)


def _stable_team(row):
    # Observation clocks alone do not change a numerical input. Raw clock and
    # provenance still remain in the full immutable snapshot.
    return {k: v for k, v in row.items() if k not in ('retrieved_at', 'fetched_at', 'updated_at')}


def _sources(event, raw, now):
    from . import nfl_game_sources
    prepared = nfl_game_sources.prepared_stats(event, now=now)
    stats = {k: dict(v) for k, v in raw.items()}
    if prepared.get('status') == 'ready':
        for team, values in prepared.get('stats', {}).items():
            if team in stats:
                stats[team].update(values)
        input_map = prepared['input_map']
    else:
        input_map = dict(policy_id=POLICY_ID, status='legacy_fallback',
                         reason=prepared.get('reason', 'verified_advanced_source_pending'))
        # Retain already saved legacy advanced context without reparsing annual
        # files in the capture process. This remains ineligible for new learning.
        previous = selected(event)
        if previous:
            advanced = {'epa_play', 'epa_dropback', 'epa_rush', 'success_rate', 'dropback_success',
                        'rush_success', 'cpoe', 'proe', 'explosive_play_rate', 'no_huddle_rate'}
            retained = False
            for team, row in (previous['payload'].get('team_form') or {}).items():
                if team in stats:
                    values = {k: v for k, v in row.items()
                              if k in advanced or k.startswith('feature_') or k == 'prior_season_features'}
                    stats[team].update(values)
                    retained = retained or bool(values)
            if retained:
                parent = previous['payload'].get('game_input_map') or {}
                input_map.update(legacy_snapshot_id=parent.get('legacy_snapshot_id', previous['snapshot_id']),
                    legacy_snapshot_sha256=parent.get('legacy_snapshot_sha256',
                        hashlib.sha256(_encoded(previous['payload']).encode()).hexdigest()))
    return stats, input_map


def _capture(snapshot_id, now):
    from . import nfl_spread_learning
    return nfl_spread_learning.capture(snapshot_id, now=now)


def maintain(now=None):
    """Called only by explicit maintenance, independently of schedule polling."""
    from . import engine, nfl_spread_learning
    stamp = _physical(now)
    games = [g for g in engine.slate('NFL', limit_events=40) if _future(g, stamp)]
    if not games:
        return {'ok': True, 'needed': 0, 'created': 0, 'missing': [], 'priced_snapshots': 0}
    if db.kv_get('nfl_game_forecast_policy') is None:
        db.kv_set('nfl_game_forecast_policy', {'policy_id': POLICY_ID, 'activated_at': stamp})
    raw, source, is_demo = engine.R.team_stats('NFL', include_advanced=False)
    if is_demo and not settings.demo_mode:
        raise ValueError('Demo NFL team statistics cannot enter production forecasts')
    created, missing, errors = 0, [], []
    for event in games:
        try:
            at = _physical(stamp)
            if not _future(event, at):
                continue
            stats, input_map = _sources(event, raw, at)
            shrunk, _ = shrink_stats(stats, 'NFL')
            home, away = event['home_team'], event['away_team']
            if home not in shrunk or away not in shrunk:
                missing.append(event['id'])
                continue
            teams = {home: shrunk[home], away: shrunk[away]}
            if any(float(row.get('retrieved_at') or 0) > at for row in teams.values()):
                raise ValueError('NFL team statistics have a future receipt')
            projection = football.project_game('NFL', home, away, shrunk,
                                               neutral=engine._neutral_site(event))
            projection['feature_groups']['scoring_margin'] = (
                projection['margin'] - projection['feature_groups'].get('efficiency_points', 0))
            quotes = nfl_market.valid_game_quotes(
                db.latest_odds(event_id=event['id'], max_age_s=settings.stale_odds_s), event)
            quotes = [q for q in quotes if (settings.demo_mode or not q.get('is_demo'))
                      and parse_time(q.get('captured_at')) is not None
                      and parse_time(q['captured_at']) <= at < parse_time(event['start_time'])
                      and (settings.demo_mode or q.get('provider') == 'espn-odds')
                      and q.get('quote_phase', 'current') == 'current']
            try:
                component = nfl_spread_learning.project_component(projection, event, quotes, input_map, at)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                # A separate component cannot remove an otherwise verified
                # baseline pick. Keep its failure visible in the worker and the
                # saved context; baseline/source failures above still refuse.
                errors.append(dict(game_id=event['id'], step='spread_component',
                                   error=str(exc), error_type=type(exc).__name__))
                component = dict(used=False, status='integrity_error',
                                 reason=str(exc)[:250], error_type=type(exc).__name__)
            if component:
                projection['spread_component'] = component
            identity = dict(id=event['id'], home_team=home, away_team=away,
                            kickoff_utc=event['start_time'], status=event.get('status'),
                            venue=event.get('venue'), meta=event.get('meta'))
            # Stable sealed source identity rather than a changing caller as-of.
            source_identity = {k: v for k, v in input_map.items()
                               if k not in ('as_of', 'checked_at', 'forecast_at', 'sha256')}
            stable_projection = dict(projection)
            if component:
                stable_projection['spread_component'] = {k: v for k, v in component.items()
                    if k not in ('forecast_input_sha256', 'as_of', 'forecast_at')}
            fingerprint = _digest(dict(game=identity, team_form={k: _stable_team(v) for k, v in teams.items()},
                input_map=source_identity, quotes=quotes, projection=stable_projection,
                formula=settings.model_version, policy=POLICY_ID))
            previous = selected(event)
            if previous and previous['payload'].get('revision_input_sha256') == fingerprint:
                _capture(previous['snapshot_id'], at)
                continue
            saved_at = _physical(at)
            payload = dict(schema_version=2, kind='automatic_pregame_revision', forecast_policy_id=POLICY_ID,
                game=identity, team_form=teams, market=quotes, injuries=None, weather=None, news=None,
                sources=[dict(kind='stats', publisher=source, is_demo=is_demo,
                              retrieved_at=max(float(r.get('retrieved_at') or 0) for r in teams.values()))],
                missing_data=dict(injuries=True, weather=True, news=True, market=not bool(quotes)),
                model_version=settings.model_version, feature_version=projection['feature_version'],
                projection=projection, game_input_map=input_map,
                neutral_site=engine._neutral_site(event), revision_input_sha256=fingerprint,
                forecast_as_of=at, created_at=saved_at)
            encoded = _encoded(payload)
            snapshot_id = hashlib.sha256(encoded.encode()).hexdigest()[:24]
            with db.tx() as con:
                # Serialize competing manual/worker writes, then re-read status,
                # kickoff, input identity and physical time inside this boundary.
                con.execute('BEGIN IMMEDIATE')
                current = con.execute('SELECT * FROM events WHERE id=?', (event['id'],)).fetchone()
                if not current or not _future(dict(current), _physical(saved_at)):
                    continue
                if (current['start_time'], current['home_team'], current['away_team']) != (
                        event['start_time'], home, away):
                    continue
                current_saved = selected(event)
                if current_saved and current_saved['payload'].get('revision_input_sha256') == fingerprint:
                    continue
                con.execute('INSERT INTO nfl_game_snapshots VALUES(?,?,?,?,?,?)',
                    (snapshot_id, event['id'], encoded, settings.model_version, projection['feature_version'], saved_at))
                con.execute('INSERT INTO nfl_game_predictions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (snapshot_id, event['id'], snapshot_id, settings.model_version, projection['feature_version'],
                     projection['proj_home'], projection['proj_away'], projection['margin'], projection['total'],
                     projection['home_win_prob'], projection['uncertainty'], saved_at))
            created += 1
            _capture(snapshot_id, _physical(saved_at))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append({'game_id': event['id'], 'error': str(exc), 'error_type': type(exc).__name__})
    return dict(ok=not missing and not errors, needed=len(games), created=created,
                missing=missing, errors=errors, priced_snapshots=0, forecast_policy_id=POLICY_ID)
