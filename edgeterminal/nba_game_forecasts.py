"""Append-only, future-only publication policy for live NBA game forecasts.

The first forecast remains immutable. Games enrolled before their tipoff may
receive sourced revisions; the latest valid pretipoff revision is also the
scored forecast. Selection never inspects a final score or chooses a revision
after observing an outcome. Reads do not enroll games or activate the policy.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

from . import db, nba_data, nba_game_context, nba_identity, nba_model, nba_sources, nba_store
from .integrity import parse_time

POLICY_ID = 'nba-game-refresh-1.0.0'
POLICY = {
    'policy_id': POLICY_ID,
    'selection': 'latest_valid_revision_saved_before_tipoff',
    'cadence_seconds': 300,
    'feature_lag_seconds': 4 * 3600,
    'fallback': 'original_supported_pregame_forecast',
    'historical_records': 'preserve_original_forecasts_and_grades',
    'designation': 'PASS',
    'readiness': 'RESEARCH_ONLY',
}
_ready = None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stamp(value):
    if not _finite(value):
        raise ValueError('forecast timestamp must be finite')
    return float(value)


def _exists(name):
    return db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def ensure_schema():
    global _ready
    nba_store.ensure_schema()
    if _ready is db.conn():
        return
    with db.tx() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS nba_game_forecast_policies (
          policy_id TEXT PRIMARY KEY, activated_at REAL NOT NULL,
          payload TEXT NOT NULL, sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS nba_game_forecast_enrollments (
          game_id TEXT PRIMARY KEY REFERENCES nba_games(game_id),
          policy_id TEXT NOT NULL REFERENCES nba_game_forecast_policies(policy_id),
          enrolled_at REAL NOT NULL, tipoff_ts REAL NOT NULL,
          original_forecast_id TEXT REFERENCES nba_forecasts(forecast_id));
        CREATE TRIGGER IF NOT EXISTS nba_game_forecast_enrollment_pretip
        BEFORE INSERT ON nba_game_forecast_enrollments
        WHEN NOT EXISTS (SELECT 1 FROM nba_games g WHERE g.game_id=NEW.game_id
              AND g.status='scheduled' AND g.tipoff_ts>NEW.enrolled_at AND g.tipoff_ts=NEW.tipoff_ts)
          OR NOT EXISTS (SELECT 1 FROM nba_game_forecast_policies p WHERE p.policy_id=NEW.policy_id
              AND p.activated_at<=NEW.enrolled_at)
          OR (NEW.original_forecast_id IS NOT NULL AND NOT EXISTS
              (SELECT 1 FROM nba_forecasts f WHERE f.forecast_id=NEW.original_forecast_id
                AND f.game_id=NEW.game_id AND f.created_at<=NEW.enrolled_at
                AND json_extract(f.payload,'$.kind')='game'))
        BEGIN SELECT RAISE(ABORT, 'game refresh enrollment must precede scheduled tipoff'); END;
        ''')
        for table in ('nba_game_forecast_policies', 'nba_game_forecast_enrollments'):
            con.executescript(nba_store._immutable(table))
    _ready = db.conn()


def policy():
    """Return the verified stored policy without creating tables or rows."""
    if not _exists('nba_game_forecast_policies'):
        return None
    row = db.q1('SELECT * FROM nba_game_forecast_policies WHERE policy_id=?', (POLICY_ID,))
    if not row:
        return None
    if _sha(row['payload']) != row['sha256'] or json.loads(row['payload']) != POLICY:
        raise ValueError('NBA game publication policy integrity mismatch')
    return dict(row, payload=json.loads(row['payload']))


def activate(now=None):
    """Record the release policy once; changing it requires a new policy ID."""
    ensure_schema()
    at = _stamp(db.now() if now is None else now)
    old = policy()
    if old:
        return old
    encoded = _json(POLICY)
    db.ex('INSERT INTO nba_game_forecast_policies VALUES(?,?,?,?)',
          (POLICY_ID, max(at, _stamp(db.now())), encoded, _sha(encoded)))
    return policy()


def _legacy(game_id):
    rows = nba_store.list_forecasts(game_id)
    return next((r for r in rows if r['payload'].get('kind') == 'game'), None)


def enroll(game_id, now=None):
    """Enroll a scheduled future game, including one with an early forecast."""
    at = _stamp(db.now() if now is None else now)
    active = activate(at)
    game = nba_store.get_game(game_id)
    if not game or game['status'] != 'scheduled' or game['tipoff_ts'] <= at:
        raise ValueError('only a scheduled game before tipoff can be enrolled')
    old = db.q1('SELECT * FROM nba_game_forecast_enrollments WHERE game_id=?', (game_id,))
    if old:
        return old
    legacy = _legacy(game_id)
    with db.tx() as con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT status,tipoff_ts FROM nba_games WHERE game_id=?', (game_id,)).fetchone()
        written_at = max(at, _stamp(db.now()))
        if not current or current['status'] != 'scheduled' or written_at >= current['tipoff_ts']:
            raise ValueError('game enrollment cannot be written at or after tipoff')
        if written_at < active['activated_at']:
            raise ValueError('game enrollment cannot precede policy activation')
        con.execute('INSERT OR IGNORE INTO nba_game_forecast_enrollments VALUES(?,?,?,?,?)',
                    (game_id, POLICY_ID, written_at, current['tipoff_ts'], legacy['forecast_id'] if legacy else None))
    return db.q1('SELECT * FROM nba_game_forecast_enrollments WHERE game_id=?', (game_id,))


def _sources(source_ids, as_of):
    if not isinstance(source_ids, (list, tuple, set)) or not source_ids:
        raise ValueError('revision needs successful source artifacts')
    result = {}
    for ident in source_ids:
        if not isinstance(ident, int) or isinstance(ident, bool):
            raise ValueError('invalid source artifact ID')
        row = nba_store.get_artifact(ident)
        if not row or row['http_status'] != 200:
            raise ValueError('revision needs successful source artifacts')
        if not _finite(row['received_at']) or row['received_at'] > as_of:
            raise ValueError('source artifact was received after forecast as-of')
        if _sha(row['body']) != row['sha256']:
            raise ValueError('source body hash mismatch')
        result[ident] = dict(row, data=json.loads(row['body']))
    return result


def _event_id(value):
    return str(value).rsplit('-', 1)[-1]


def _source_rows(source):
    data = source['data']
    if isinstance(data.get('events'), list):
        for event in data['events']:
            for comp in event.get('competitions') or []:
                for competitor in comp.get('competitors') or []:
                    team = competitor.get('team') or {}
                    if team.get('id') is not None:
                        nba_identity.team_code(team)
        return nba_sources.team_history(data, source['sha256'])
    if data.get('header'):
        parsed = nba_data.parse_summary(data)
        game = parsed['game']
        if game['status'] != 'final' or not parsed['box_complete']:
            return []
        return [{'team': game[side + '_team'], 'opponent': game[other + '_team'],
                 'game_id': game['espn_event_id'], 'played_at': game['tipoff_utc'],
                 'points': game[side + '_score'], 'opponent_points': game[other + '_score'],
                 'home': side == 'home'} for side, other in (('home', 'away'), ('away', 'home'))]
    return []


def _possessions(source, game_id):
    """Bind the declared symmetric box-score estimate to its archived inputs."""
    data = source['data']
    parsed = nba_data.parse_summary(data)
    if parsed['game']['status'] != 'final' or not parsed['box_complete'] \
            or parsed['game']['espn_event_id'] != _event_id(game_id):
        raise ValueError('possessions require the matching verified final boxscore')
    estimates = []
    for team in data.get('boxscore', {}).get('teams', []):
        stats = {s['name']: s.get('displayValue') for s in team.get('statistics', [])}
        def number(name):
            value = nba_sources.number(stats.get(name))
            if value is None or value < 0:
                raise ValueError('possession source lacks required boxscore statistics')
            return value
        def attempted(name):
            value = stats.get(name)
            if not isinstance(value, str) or len(value.split('-')) != 2:
                raise ValueError('possession source lacks attempted-shot statistics')
            made, result = (nba_sources.number(x) for x in value.split('-'))
            if made is None or result is None or not 0 <= made <= result:
                raise ValueError('invalid attempted-shot statistics')
            return result
        estimates.append(attempted('fieldGoalsMade-fieldGoalsAttempted')
                         + .44 * attempted('freeThrowsMade-freeThrowsAttempted')
                         - number('offensiveRebounds')
                         + number('totalTurnovers' if 'totalTurnovers' in stats else 'turnovers'))
    if len(estimates) != 2:
        raise ValueError('possessions require two team boxscores')
    estimate = sum(estimates) / 2
    if not 30 <= estimate <= 200:
        raise ValueError('implausible boxscore possession estimate')
    return estimate


def _validate_manifest(manifest, sources, game, as_of):
    if not isinstance(manifest, dict) or not isinstance(manifest.get('rows'), list):
        raise ValueError('revision needs an exact input row manifest')
    cutoff = manifest.get('feature_cutoff')
    if not _finite(cutoff) or cutoff > as_of or cutoff > game['tipoff_ts'] - POLICY['feature_lag_seconds']:
        raise ValueError('invalid feature cutoff')
    seen, counts, parsed = set(), Counter(), {}
    for row in manifest['rows']:
        if not isinstance(row, dict) or row.get('team') not in nba_identity.TEAMS:
            raise ValueError('input row has an unknown NBA team identity')
        gid, team = _event_id(row.get('game_id')), row['team']
        key = (team, gid)
        if key in seen:
            raise ValueError('duplicate selected input row')
        seen.add(key)
        if not all(_finite(row.get(k)) and row[k] > 0 for k in ('points', 'opponent_points')):
            raise ValueError('input row scores must be finite positive numbers')
        when = parse_time(row.get('played_at'))
        if when is None or when > cutoff or gid == game['espn_event_id']:
            raise ValueError('input row exceeds the feature cutoff or is the target game')
        ident = row.get('source_artifact_id')
        if ident not in sources:
            raise ValueError('input row source is missing from revision sources')
        if ident not in parsed:
            parsed[ident] = _source_rows(sources[ident])
        matches = [r for r in parsed[ident] if str(r['game_id']) == gid and r['team'] == team
                   and parse_time(r['played_at']) == when and r['points'] == row.get('points')
                   and r['opponent_points'] == row.get('opponent_points')
                   and ('opponent' not in row or row['opponent'] == r['opponent'])
                   and ('home' not in row or row['home'] == r['home'])]
        if len(matches) != 1:
            raise ValueError('input row score/time/identity does not match its archived source')
        if row.get('possessions') is not None:
            possession_source = row.get('possession_source_artifact_id')
            if possession_source not in sources or row.get('possession_method') != 'symmetric_box_estimate_044':
                raise ValueError('possessions need a matching source and declared estimation method')
            expected = _possessions(sources[possession_source], gid)
            if not _finite(row['possessions']) or not math.isclose(row['possessions'], expected, abs_tol=1e-6):
                raise ValueError('possessions do not match the archived boxscore')
        counts[team] += 1
    if any(counts[game[side + '_team']] < nba_model.MIN_TEAM_GAMES for side in ('home', 'away')):
        raise ValueError('revision needs at least ten sourced input rows for each target team')
    schedule = manifest.get('schedule') or {}
    if not isinstance(schedule, dict):
        raise ValueError('invalid schedule context')
    for team, context in schedule.items():
        if team not in nba_identity.TEAMS or not isinstance(context, dict):
            raise ValueError('invalid schedule team identity')
        ident = context.get('source_artifact_id')
        if ident not in sources:
            raise ValueError('schedule source is missing from revision sources')
        source = sources[ident]
        full_schedule = nba_game_context.parse_schedule(team, source['data'], ident, source['received_at'])
        # Selected scoring rows are a bounded window. Older completed-game
        # coverage is independently established by this full archived schedule,
        # rather than falsely treating every row outside the window as missing.
        coverage = _source_rows(source) + manifest['rows']
        expected = nba_game_context.team_context(full_schedule, team, game['tipoff_ts'], coverage, as_of)
        fields = ('last_game_id', 'last_game_tipoff', 'complete', 'missing_game_ids', 'stale',
                  'observed_at', 'rest_source')
        if any(context.get(key) != expected[key] for key in fields) \
                or any(type(context.get(key)) is not bool for key in ('complete', 'stale')):
            raise ValueError('schedule context flags do not match the archived full schedule and as-of')


def _competition(source, game):
    data = source['data']
    events = data.get('events') if isinstance(data.get('events'), list) else [data.get('header') or {}]
    event = next((e for e in events if str(e.get('id')) == game['espn_event_id']), None)
    if not event:
        raise ValueError('market source event identity mismatch')
    comps = event.get('competitions') or []
    if len(comps) != 1:
        raise ValueError('market source competition identity mismatch')
    comp = comps[0]
    tip = parse_time(comp.get('date') or event.get('date'))
    teams = {c.get('homeAway'): nba_identity.team_code(c.get('team') or {})
             for c in comp.get('competitors', [])}
    if tip != game['tipoff_ts'] or teams != {'home': game['home_team'], 'away': game['away_team']}:
        raise ValueError('market source tipoff or team identity mismatch')
    if comp.get('status', {}).get('type', {}).get('completed') is True \
            or comp.get('status', {}).get('type', {}).get('state') not in (None, 'pre'):
        raise ValueError('market source must be pregame')
    return comp


def _validate_market(market, sources, game, as_of):
    if not isinstance(market, dict):
        raise ValueError('invalid market snapshot')
    if not market:
        return
    ident = market.get('source_artifact_id')
    if ident not in sources:
        raise ValueError('market source is missing from revision sources')
    source = sources[ident]
    observed = market.get('observed_at')
    if not _finite(observed) or observed > as_of or observed < source['received_at']:
        raise ValueError('market observation must follow its source and precede forecast as-of')
    if market.get('source_sha256') != source['sha256']:
        raise ValueError('market source hash mismatch')
    comp = _competition(source, game)
    spread = market.get('home_spread')
    if spread is not None:
        if not _finite(spread) or not _finite(market.get('away_spread')) or spread != -market['away_spread']:
            raise ValueError('market spread lines must be finite and opposite')
    total = market.get('total')
    if total is not None and not _finite(total):
        raise ValueError('market total line must be finite')
    prices = ('home_price', 'away_price', 'over_price', 'under_price', 'home_moneyline', 'away_moneyline')
    if any(market.get(k) is not None and (not _finite(market[k]) or market[k] != int(market[k])
                                       or not 100 <= abs(market[k]) <= 100000) for k in prices):
        raise ValueError('market price must be an integer American price')

    def offer_matches(offer):
        if spread is not None:
            raw = offer.get('pointSpread') or {}
            for side, expected in (('home', spread), ('away', -spread)):
                close = raw.get(side, {}).get('close') or {}
                if nba_sources.number(close.get('line')) != expected:
                    return False
                price = market.get(side + '_price')
                if price is not None and nba_sources.number(close.get('odds')) != price:
                    return False
        if total is not None:
            matching_side = False
            for side in ('over', 'under'):
                close = (offer.get('total') or {}).get(side, {}).get('close') or {}
                value = close.get('line')
                if isinstance(value, str) and value.lower().startswith(side[0]):
                    value = value[1:]
                same_line = nba_sources.number(value) == total
                matching_side = matching_side or same_line
                price = market.get(side + '_price')
                if price is not None and (not same_line or nba_sources.number(close.get('odds')) != price):
                    return False
            if not matching_side:
                return False
        for side in ('home', 'away'):
            price = market.get(side + '_moneyline')
            close = (offer.get('moneyline') or {}).get(side, {}).get('close') or {}
            if price is not None and nba_sources.number(close.get('odds')) != price:
                return False
        book = market.get('sportsbook')
        provider = offer.get('provider') or {}
        return book in (None, '', 'Unknown') or book in (provider.get('displayName'), provider.get('name'))

    has_quote = spread is not None or total is not None or any(market.get(k) is not None for k in prices)
    if has_quote and not any(offer_matches(o) for o in comp.get('odds') or []):
        raise ValueError('market line or price does not match one archived sportsbook offer')


def save_revision(game_id, projection, market, input_manifest, sources, now=None, model_info=None):
    """Validate and append an immutable as-of snapshot; identical work is a no-op."""
    at = _stamp(db.now() if now is None else now)
    enrollment = enroll(game_id, at)
    at = max(at, enrollment['enrolled_at'])
    game = nba_store.get_game(game_id)
    if not isinstance(projection, dict) or projection.get('status') != 'ok' \
            or projection.get('home') != game['home_team'] or projection.get('away') != game['away_team'] \
            or parse_time(projection.get('tipoff')) != game['tipoff_ts']:
        raise ValueError('projection identity must match its scheduled game')
    if not all(_finite(projection.get(k)) for k in ('home_margin', 'total', 'home_score', 'away_score')) \
            or abs(projection['home_score'] - projection['away_score'] - projection['home_margin']) > .011 \
            or abs(projection['home_score'] + projection['away_score'] - projection['total']) > .011:
        raise ValueError('projection scores, margin, and total do not reconcile')
    projection = dict(projection, tipoff=game['tipoff_ts'])
    source_rows = _sources(list(sources), at)
    _validate_manifest(input_manifest, source_rows, game, at)
    _validate_market(market, source_rows, game, at)
    model = model_info or {'version': projection.get('model_version') or nba_model.MODEL_VERSION}
    snapshot = {'kind': 'game-revision', 'policy_id': enrollment['policy_id'], 'projection': projection,
                'market': market, 'input_manifest': input_manifest, 'model': model,
                'source_artifacts': [{'id': sid, 'sha256': source_rows[sid]['sha256'],
                                      'received_at': source_rows[sid]['received_at']} for sid in sorted(source_rows)]}
    snapshot_sha = _sha(_json(snapshot))
    forecast_id = 'game-revision-' + game_id + '-' + snapshot_sha[:24]
    old = nba_store.get_forecast(forecast_id)
    if old:
        if old['payload'].get('snapshot_sha256') != snapshot_sha:
            raise ValueError('revision snapshot identity mismatch')
        return old
    payload = dict(snapshot, as_of=at, snapshot_sha256=snapshot_sha,
                   input_manifest_sha256=_sha(_json(input_manifest)))
    encoded = _json(payload)
    encoded_sources = _json(sorted(source_rows))
    payload_sha = _sha(encoded)
    # Source parsing, model inference, serialization, hashing, and SQLite lock
    # acquisition may all take time. Check the physical write boundary after
    # that work, not only a worker timestamp sampled before inference.
    with db.tx() as con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT status,tipoff_ts FROM nba_games WHERE game_id=?', (game_id,)).fetchone()
        written_at = max(at, _stamp(db.now()))
        if not current or current['status'] != 'scheduled' or written_at >= current['tipoff_ts'] \
                or current['tipoff_ts'] != game['tipoff_ts']:
            raise ValueError('revision cannot be written at or after tipoff or after a schedule change')
        duplicate = con.execute('SELECT payload FROM nba_forecasts WHERE forecast_id=?', (forecast_id,)).fetchone()
        if duplicate:
            if json.loads(duplicate['payload']).get('snapshot_sha256') != snapshot_sha:
                raise ValueError('revision snapshot identity mismatch')
        else:
            con.execute('INSERT INTO nba_forecasts(forecast_id,game_id,model_version,created_at,payload,'
                        'payload_sha256,source_artifact_ids) VALUES(?,?,?,?,?,?,?)',
                        (forecast_id, game_id, model['version'], written_at, encoded, payload_sha, encoded_sources))
    return nba_store.get_forecast(forecast_id)


def selected(game_id, legacy=None):
    """Choose only by immutable pretipoff time/policy; never inspect outcomes."""
    legacy = _legacy(game_id) if legacy is None else legacy
    active = policy()
    if not active or not _exists('nba_game_forecast_enrollments'):
        return legacy
    enrollment = db.q1('SELECT * FROM nba_game_forecast_enrollments WHERE game_id=?', (game_id,))
    game = nba_store.get_game(game_id)
    if not enrollment or not game or enrollment['policy_id'] != POLICY_ID \
            or enrollment['enrolled_at'] < active['activated_at']:
        return legacy
    candidates = db.q('SELECT forecast_id FROM nba_forecasts WHERE game_id=? '
                      'ORDER BY created_at DESC,forecast_id DESC', (game_id,))
    for candidate in candidates:
        row = nba_store.get_forecast(candidate['forecast_id'])
        payload = row['payload']
        if payload.get('kind') != 'game-revision' or payload.get('policy_id') != enrollment['policy_id']:
            continue
        as_of = payload.get('as_of')
        if not _finite(as_of) or not enrollment['enrolled_at'] <= as_of <= row['created_at'] < game['tipoff_ts'] \
                or payload.get('projection', {}).get('tipoff') != game['tipoff_ts']:
            continue
        if _sha(_json(payload)) != row['payload_sha256'] \
                or _sha(_json(payload.get('input_manifest'))) != payload.get('input_manifest_sha256'):
            continue
        snapshot = {k: v for k, v in payload.items() if k not in {'as_of', 'snapshot_sha256', 'input_manifest_sha256'}}
        if _sha(_json(snapshot)) != payload.get('snapshot_sha256'):
            continue
        return row
    return legacy


def status(now=None):
    at = _stamp(db.now() if now is None else now)
    active = policy()
    result = {'enabled': bool(active), 'policy_id': POLICY_ID,
              'selection': POLICY['selection'], 'cadence_seconds': POLICY['cadence_seconds'],
              'enrolled_games': 0, 'upcoming_games': 0, 'awaiting_revisions': 0, 'stale_revisions': 0,
              'designation': 'PASS', 'readiness': 'RESEARCH_ONLY'}
    if not active or not _exists('nba_game_forecast_enrollments'):
        return result
    result['activated_at'] = active['activated_at']
    enrolled = db.q('SELECT e.*,g.status,g.tipoff_ts FROM nba_game_forecast_enrollments e '
                    'JOIN nba_games g USING(game_id) WHERE e.policy_id=?', (POLICY_ID,))
    result['enrolled_games'] = len(enrolled)
    for game in enrolled:
        if game['tipoff_ts'] <= at or game['status'] != 'scheduled':
            continue
        result['upcoming_games'] += 1
        saved = selected(game['game_id'])
        if not saved or saved['payload'].get('kind') != 'game-revision':
            result['awaiting_revisions'] += 1
        elif at - saved['created_at'] > 900:
            result['stale_revisions'] += 1
    return result


def validation_snapshots(now=None, limit=60):
    """Verified selected revisions and later labels for chronological learning.

    Legacy grades and unpaired labels are excluded. Returned outcomes never
    influence which pretipoff revision is selected.
    """
    at = _stamp(db.now() if now is None else now)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError('validation snapshot limit must be between one and 1000')
    if not policy() or not _exists('nba_game_grade_revisions'):
        return []
    result = []
    games = db.q("SELECT g.* FROM nba_games g JOIN nba_game_forecast_enrollments e USING(game_id) "
                 "WHERE g.status='final' AND g.tipoff_ts<? AND e.policy_id=? "
                 "ORDER BY g.tipoff_ts DESC,g.game_id DESC", (at, POLICY_ID))
    for game in games:
        saved = selected(game['game_id'])
        if not saved or saved['payload'].get('kind') != 'game-revision':
            continue
        revisions = db.q('SELECT * FROM nba_game_grade_revisions WHERE game_id=? AND graded_at<=? ORDER BY id',
                         (game['game_id'], at))
        if not revisions:
            continue
        previous = 0
        for grade in revisions:
            if _sha(grade['payload'] + ':' + str(previous)) != grade['sha256']:
                raise ValueError('NBA game validation grade hash-chain mismatch')
            previous = grade['id']
        grade = revisions[-1]
        label = json.loads(grade['payload'])
        if (label.get('forecast_id'), label.get('forecast_sha256'), label.get('forecast_policy_id')) != \
                (saved['forecast_id'], saved['payload_sha256'], saved['payload']['policy_id']):
            continue
        if label.get('spread') not in ('won', 'lost', 'push'):
            continue
        source = _sources([grade['source_artifact_id']], grade['graded_at'])[grade['source_artifact_id']]
        parsed = nba_data.parse_summary(source['data'])
        final = parsed['game']
        if not parsed['box_complete'] or final['status'] != 'final' \
                or source['received_at'] < game['tipoff_ts'] \
                or final['game_id'] != game['game_id'] \
                or (final['home_team'], final['away_team']) != (game['home_team'], game['away_team']) \
                or parse_time(final['tipoff_utc']) != game['tipoff_ts'] \
                or (label.get('home_score'), label.get('away_score')) != (final['home_score'], final['away_score']):
            raise ValueError('NBA game validation label does not match its verified final source')
        payload = saved['payload']
        input_sources = _sources(saved['source_artifact_ids'], payload['as_of'])
        descriptors = [{'id': ident, 'sha256': input_sources[ident]['sha256'],
                        'received_at': input_sources[ident]['received_at']} for ident in sorted(input_sources)]
        if descriptors != payload.get('source_artifacts'):
            raise ValueError('NBA validation input source descriptors changed since the forecast')
        _validate_manifest(payload['input_manifest'], input_sources, game, payload['as_of'])
        _validate_market(payload.get('market') or {}, input_sources, game, payload['as_of'])
        market = payload.get('market') or {}
        if not _finite(market.get('home_spread')):
            continue
        actual_margin = final['home_score'] - final['away_score']
        pick = 1 if payload['projection']['home_margin'] + market['home_spread'] >= 0 else -1
        cushion = (actual_margin + market['home_spread']) * pick
        expected_result = 'won' if cushion > 0 else 'lost' if cushion < 0 else 'push'
        if label['spread'] != expected_result:
            raise ValueError('NBA game validation spread grade does not match original line and final score')
        result.append({
            'game_id': game['game_id'], 'home_team': game['home_team'], 'away_team': game['away_team'],
            'tipoff_ts': game['tipoff_ts'], 'as_of': payload['as_of'],
            'recorded_at': saved['created_at'],
            'forecast_id': saved['forecast_id'], 'revision_id': saved['forecast_id'],
            'forecast_sha256': saved['payload_sha256'], 'policy_id': payload['policy_id'],
            'projection': payload['projection'], 'market': market, 'input_manifest': payload['input_manifest'],
            'model': payload.get('model'), 'source_artifacts': payload['source_artifacts'],
            'source_ids': saved['source_artifact_ids'],
            'actual_margin': actual_margin, 'actual_home_margin': actual_margin,
            'home_score': final['home_score'], 'away_score': final['away_score'],
            'result_observed_at': source['received_at'], 'graded_at': grade['graded_at'],
            'grade_id': grade['id'], 'label_source_artifact_id': grade['source_artifact_id'],
            'label_source_sha256': source['sha256'], 'spread_result': label['spread'],
        })
        if len(result) >= limit:
            break
    return list(reversed(result))
