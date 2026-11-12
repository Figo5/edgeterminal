"""Contemporaneous full-offer snapshots for future NBA selection-policy tests.

Capture uses already fetched sources and records all offers before tipoff. Reads
replay immutable evidence and verified later final boxes; they never fetch, fit,
activate a model, or treat an absent player as a zero or inactive player.
"""
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import parse_qs, urlparse

from . import db, nba_data, nba_quotes, nba_store
from .integrity import parse_time

SCHEMA = 'nba-full-offer-policy-1'
_VALIDATION_CACHE = ContextVar('nba_policy_validation_cache', default=None)


@contextmanager
def validation_scope():
    """Reuse verified immutable evidence only inside one registry verification."""
    if _VALIDATION_CACHE.get() is not None:
        yield
        return
    token = _VALIDATION_CACHE.set(dict(contexts={}, finals={}, sources={}, identities={}))
    try:
        yield
    finally:
        _VALIDATION_CACHE.reset(token)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def ensure_schema():
    nba_store.ensure_schema()
    db.ex('CREATE INDEX IF NOT EXISTS ix_nba_sources_kind_url_time '
          'ON nba_source_artifacts(kind,url,received_at)')
    db.ex('CREATE TABLE IF NOT EXISTS nba_prop_policy_contexts ('
          'context_id TEXT PRIMARY KEY,game_id TEXT NOT NULL REFERENCES nba_games(game_id),'
          'as_of REAL NOT NULL,tipoff_ts REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        db.ex(f'CREATE TRIGGER IF NOT EXISTS nba_prop_policy_contexts_no_{action.lower()} BEFORE {action} '
              "ON nba_prop_policy_contexts BEGIN SELECT RAISE(ABORT,'NBA policy contexts are immutable'); END")
    db.ex("CREATE TRIGGER IF NOT EXISTS nba_policy_context_pretip BEFORE INSERT ON nba_prop_policy_contexts "
          "WHEN NOT EXISTS (SELECT 1 FROM nba_games WHERE game_id=NEW.game_id AND status='scheduled' "
          "AND tipoff_ts=NEW.tipoff_ts AND tipoff_ts>NEW.as_of) "
          "BEGIN SELECT RAISE(ABORT,'NBA policy context must be contemporaneous before tipoff'); END")


def _source_cache():
    scope = _VALIDATION_CACHE.get()
    return scope['sources'] if scope is not None else {}


def _sources(ids, as_of, cache=None):
    from .nba_learning_data import verify_source
    cache = _source_cache() if cache is None else cache
    rows = []
    for ident in sorted(set(ids)):
        if ident not in cache:
            row = nba_store.get_artifact(ident)
            cache[ident] = (row, verify_source(row, received_by=as_of))
        row, _ = cache[ident]
        if row['received_at'] > as_of:
            raise ValueError('NBA policy source was received after its capture')
        rows.append(row)
    return rows


def _identities(sources, observed_at):
    """Reuse the same verified provider-name catalog across one capture batch."""
    scope = _VALIDATION_CACHE.get()
    if scope is None:
        return nba_quotes.player_identity_catalog(sources, observed_at)
    # Rosters and player histories are not inputs to this provider-name map.
    # Include the receipt cutoff: an earlier capture cannot reuse later names.
    quotes = [s for s in sources if s['kind'] == 'player-quotes']
    key = (observed_at, tuple(sorted((s['id'], s['sha256'], s['received_at']) for s in quotes)))
    if key not in scope['identities']:
        scope['identities'][key] = nba_quotes.player_identity_catalog(quotes, observed_at)
    return scope['identities'][key]


_LEAGUE_ROWS = {}  # parsed rows of immutable gamelog bytes, by sha256


def _opponent_context(game, roster, observed_at):
    """What each side's opponent allows, from every gamelog already received at the quote time."""
    from . import nba_learning_data as evidence, nba_prop_opponent as opponent
    heads = db.q("SELECT id,sha256 FROM nba_source_artifacts WHERE id IN (SELECT MAX(id) FROM nba_source_artifacts "
                 "WHERE kind='player-history' AND received_at<=? GROUP BY url) ORDER BY id", (observed_at,))
    history = []
    for head in heads:
        if head['sha256'] not in _LEAGUE_ROWS:
            _LEAGUE_ROWS[head['sha256']] = evidence.history_from_sources([head['id']], {}, observed_at)[0]
        history.extend(_LEAGUE_ROWS[head['sha256']])
    return opponent.context(opponent.build(history), game['home_team'], game['away_team'],
                            {r['player_id']: r['team'] for r in roster}, game['tipoff_ts'])


def _uses_opponent(artifact):
    from .nba_prop_role import RATE_KIND
    return bool(artifact) and (artifact.get('kind') == RATE_KIND or any(
        _uses_opponent(artifact.get(key)) for key in ('base_artifact', 'retained_artifact')))


def _with_opponent(history, game, roster, observed_at, selector_artifact):
    """Append the opponent row. Only a selector that reads it may fail a capture or replay over it."""
    try:
        return history + [_opponent_context(game, roster, observed_at)]
    except Exception as exc:
        if _uses_opponent(selector_artifact):
            raise ValueError('NBA opponent context is unavailable to the selector that requires it') from exc
        return history


def capture_context(game, quotes, roster, history, quote_source_id, now):
    """Archive a full replay context without another provider request."""
    with validation_scope():
        return _capture_context(game, quotes, roster, history, quote_source_id, now)


def _capture_context(game, quotes, roster, history, quote_source_id, now):
    ensure_schema()
    actual = nba_store.get_game(game['game_id'])
    at = max(float(now), db.now())
    if not actual or actual['status'] != 'scheduled' or at >= actual['tipoff_ts']:
        raise ValueError('policy context cannot be captured after tipoff')
    codes = {actual['home_team'], actual['away_team']}
    players = [r for r in roster if r['team'] in codes]
    pids = {str(r['player_id']) for r in players}
    ids = {quote_source_id, actual['source_artifact_id']}
    ids.update(r['source_artifact_id'] for r in players)
    ids.update(r['source_artifact_id'] for r in history if str(r['player_id']) in pids)
    ids.update(q['identity_source_artifact_id'] for q in quotes if q.get('identity_source_artifact_id') is not None)
    quote_observed = nba_store.get_artifact(quote_source_id)['received_at']
    ids.update(s['id'] for s in db.q("SELECT id FROM nba_source_artifacts WHERE kind='player-quotes' AND received_at<=?",
                                    (quote_observed,)))
    sources = _sources(ids, at)
    for q in quotes:
        if q['game_id'] != actual['game_id'] or at - q['observed_at'] > 3600 or q['observed_at'] > at:
            raise ValueError('policy quotes must be fresh contemporaneous offers for one game')
    from . import nba_learning_data as evidence
    # Cache values identify which immutable sources to read; their numeric
    # contents never become the captured model inputs.
    history, _ = evidence.history_from_sources(ids, _source_cache(), at)
    players = _roster(sources, actual, at, pids)
    quote_source = next(s for s in sources if s['id'] == quote_source_id)
    if quote_source['kind'] != 'player-quotes':
        raise ValueError('NBA policy capture lacks a player-quote source')
    body = evidence.verify_source(quote_source, quote_observed)
    normalized, _ = nba_quotes.normalize(body.get('data', []),
        [dict(actual, id=actual['game_id'], tipoff=actual['tipoff_utc'])], players, quote_observed,
        identity_catalog=_identities(sources, quote_observed))
    keys = ('game_id','player_id','player','team','market','side','line','price','sportsbook',
            'observed_at','provider_player_id','identity_source_artifact_id','identity_verified','is_main','line_type')
    if len(normalized) != len(quotes) or any(
            sum(all(q.get(k) == row.get(k) for k in keys) for q in normalized) != 1 for row in quotes):
        raise ValueError('NBA capture inputs differ from the complete sourced offer universe')
    # Temporary indices allow all expensive replay/projection work to finish
    # before acquiring one transaction for quote observations and context.
    prepared = [dict(q, quote_id=index) for index, q in enumerate(normalized)]
    from . import nba_learning
    active = nba_learning._active()
    info = nba_learning.model_info()
    selector = dict(artifact=active['artifact'] if active else None, version=info['version'],
                    fingerprint=info['fingerprint'], training_cutoff=parse_time(info['cutoff']))
    history = _with_opponent(history, actual, players, quote_observed, selector['artifact'])
    selection = nba_learning.select_props(prepared, history, actual['tipoff_utc'], at,
        injuries=[dict(player_id=p['player_id'], status=p.get('availability', 'unknown')) for p in players])
    selected = _selection_rows(selection, prepared, history, actual['tipoff_utc'])
    payload = dict(schema=SCHEMA, game_id=actual['game_id'], quote_source_id=quote_source_id,
                   observed_at=nba_store.get_artifact(quote_source_id)['received_at'],
                   eligible_player_ids=sorted(pids),
                   selector=selector, selector_sha256=hashlib.sha256(_json(selector).encode()).hexdigest(),
                   incumbent=selected,
                   sources=[dict(id=s['id'], sha256=s['sha256'], received_at=s['received_at']) for s in sources],
                   quotes=[])
    rows, quote_ids = [], {}
    with db.tx() as con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        written = max(at, db.now())
        current = con.execute('SELECT status,tipoff_ts FROM nba_games WHERE game_id=?', (actual['game_id'],)).fetchone()
        if current['status'] != 'scheduled' or current['tipoff_ts'] != actual['tipoff_ts'] or written >= actual['tipoff_ts']:
            raise ValueError('policy capture cannot be persisted after tipoff')
        for q in prepared:
            recorded = max(written, db.now())
            if recorded >= actual['tipoff_ts']:
                raise ValueError('policy capture crossed tipoff')
            cur = con.execute('INSERT INTO nba_quote_observations(game_id,market,espn_player_id,side,line,price,source,'
                'observed_at,source_artifact_id,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (actual['game_id'], nba_store.PROP_MARKETS[q['market']], str(q['player_id']), q['side'], q['line'],
                 q['price'], q['sportsbook'], q['observed_at'], quote_source_id, recorded))
            quote_ids[q['quote_id']] = cur.lastrowid
            rows.append(dict(q, quote_id=cur.lastrowid))
        payload.update(quotes=rows, incumbent=[dict(r, quote_id=quote_ids[r['quote_id']]) for r in selected])
        encoded = _json(payload)
        sha = hashlib.sha256(encoded.encode()).hexdigest()
        written = max(written, db.now())
        if written >= actual['tipoff_ts']:
            raise ValueError('policy capture cannot be persisted after tipoff')
        con.execute('INSERT INTO nba_prop_policy_contexts VALUES(?,?,?,?,?,?)',
                    (sha, actual['game_id'], written, actual['tipoff_ts'], encoded, sha))
    return dict(context_id=sha, as_of=written, quotes=rows, history=history, selection=selection)


def _roster(sources, game, as_of, eligible):
    from . import nba_learning_data as evidence
    from .nba_game_context import roster_availability
    roster = []
    for source in sources:
        if source['kind'] != 'roster':
            continue
        parsed = nba_data.parse_roster(evidence.verify_source(source, as_of))
        if parsed['team'] not in {game['home_team'], game['away_team']} or parsed['season_label'] != game['season_label']:
            raise ValueError('NBA policy roster identity or season mismatch')
        roster.extend(dict(player_id=p['espn_player_id'], player=p['name'], team=parsed['team'],
            availability=roster_availability(p), observed_at=source['received_at']) for p in parsed['players'])
    if not set(eligible).issubset({str(r['player_id']) for r in roster}):
        raise ValueError('NBA policy eligible identity missing from frozen roster')
    return [r for r in roster if str(r['player_id']) in eligible]


def _context(ident):
    cache = _VALIDATION_CACHE.get()
    if cache is not None:
        if ident not in cache['contexts']:
            cache['contexts'][ident] = _load_context(ident)
        return cache['contexts'][ident]
    return _load_context(ident)


def _load_context(ident):
    from . import nba_learning_data as evidence
    row = db.q1('SELECT * FROM nba_prop_policy_contexts WHERE context_id=?', (ident,))
    if not row or hashlib.sha256(row['payload'].encode()).hexdigest() != row['sha256'] or row['sha256'] != ident:
        raise ValueError('NBA policy context hash mismatch')
    payload = json.loads(row['payload'])
    current_game = nba_store.get_game(row['game_id'])
    cache = _source_cache()
    sources = _sources([s['id'] for s in payload['sources']], row['as_of'], cache)
    game = evidence.capture_game(dict(source_artifact_ids=json.dumps([s['id'] for s in payload['sources']]),
                                      created_at=row['as_of']), current_game, cache)
    if payload.get('schema') != SCHEMA or payload.get('game_id') != game['game_id'] \
            or not row['as_of'] < row['tipoff_ts'] or row['tipoff_ts'] != game['tipoff_ts']:
        raise ValueError('NBA policy context identity or time mismatch')
    if [dict(id=s['id'], sha256=s['sha256'], received_at=s['received_at']) for s in sources] != payload['sources']:
        raise ValueError('NBA policy context source changed')
    history, used = evidence.history_from_sources([s['id'] for s in sources], cache, row['as_of'])
    eligible = set(payload['eligible_player_ids'])
    roster = _roster(sources, game, row['as_of'], eligible)
    history = _with_opponent(history, game, roster, payload['observed_at'], payload['selector']['artifact'])
    quote_source = next((s for s in sources if s['id'] == payload['quote_source_id']), None)
    if not quote_source or quote_source['kind'] != 'player-quotes' or quote_source['received_at'] != payload['observed_at'] \
            or not 0 <= row['as_of'] - payload['observed_at'] <= 3600:
        raise ValueError('NBA policy quote receipt or freshness changed')
    body = evidence.verify_source(quote_source, payload['observed_at'])
    identities = _identities(sources, payload['observed_at'])
    normalized, _ = nba_quotes.normalize(body.get('data', []),
        [dict(game, id=game['game_id'], tipoff=game['tipoff_utc'])], roster, payload['observed_at'],
        identity_catalog=identities)
    keys = ('player_id', 'team', 'market', 'side', 'line', 'price', 'sportsbook', 'observed_at',
            'provider_player_id', 'identity_source_artifact_id')
    if len(normalized) != len(payload['quotes']):
        raise ValueError('NBA policy context omitted available offers')
    for quote in payload['quotes']:
        if sum(all(q.get(k) == quote.get(k) for k in keys) for q in normalized) != 1:
            raise ValueError('NBA policy quote differs from its complete source universe')
        saved = db.q1('SELECT * FROM nba_quote_observations WHERE quote_id=?', (quote['quote_id'],))
        if not saved or saved['game_id'] != game['game_id'] or saved['recorded_at'] > row['as_of'] \
                or (saved['espn_player_id'], saved['market'], saved['side'], saved['line'], saved['price'],
                    saved['source'], saved['observed_at'], saved['source_artifact_id']) != \
                (str(quote['player_id']), nba_store.PROP_MARKETS[quote['market']], quote['side'], quote['line'],
                 quote['price'], quote['sportsbook'], quote['observed_at'], payload['quote_source_id']):
            raise ValueError('NBA policy quote observation binding mismatch')
    # Proof has consumed the bytes. Call-scoped context caches retain metadata
    # and parsed inputs, avoiding one duplicate raw archive per policy row/game.
    metadata = [{k: v for k, v in source.items() if k != 'body'} for source in sources]
    return dict(row, payload=payload, game=game, history=history, roster=roster, sources=metadata)


def _final(context, source_id, received_by):
    cache = _VALIDATION_CACHE.get()
    if cache is not None:
        key = (context['context_id'], source_id)
        if key not in cache['finals']:
            cache['finals'][key] = _load_final(context, source_id, received_by)
        source, players = cache['finals'][key]
        if source['received_at'] > received_by:
            raise ValueError('NBA policy final source was received after its result observation')
        return source, players
    return _load_final(context, source_id, received_by)


def _load_final(context, source_id, received_by):
    from .nba_learning_data import verify_source
    source = nba_store.get_artifact(source_id)
    parsed = nba_data.parse_summary(verify_source(source, received_by))
    game = context['game']
    actual = parsed['game']
    current_game = nba_store.get_game(game['game_id'])
    url = urlparse(source['url'])
    if source['kind'] != 'final-boxscore' or url.hostname != 'site.api.espn.com' \
            or not url.path.endswith('/summary') or parse_qs(url.query).get('event') != [game['espn_event_id']] \
            or source['received_at'] <= max(game['tipoff_ts'], current_game['tipoff_ts']) or actual['status'] != 'final' or not parsed['box_complete'] \
            or actual['game_id'] != game['game_id'] or actual['home_team'] != game['home_team'] \
            or actual['away_team'] != game['away_team'] or parse_time(actual['tipoff_utc']) != current_game['tipoff_ts']:
        raise ValueError('NBA policy final result identity or completeness mismatch')
    return source, {p['espn_player_id']: p for p in parsed['players']}


def ready_contexts(now, limit=120):
    # Games share archived rosters, histories and quote batches. Verify each
    # source once per review, with receipt checks on every use.
    with validation_scope():
        return _ready_contexts(now, limit)


def _ready_contexts(now, limit):
    if not db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name='nba_prop_policy_contexts'"):
        return []
    output = []
    rows = db.q('SELECT c.context_id FROM nba_prop_policy_contexts c JOIN nba_games g USING(game_id) '
                "WHERE g.status='final' AND g.tipoff_ts<? ORDER BY c.as_of DESC,c.context_id DESC", (now,))
    seen = set()
    for row in rows:
        context = _context(row['context_id'])
        game = context['game']
        if game['game_id'] in seen:
            continue
        # The archived URL already identifies the game and has an index. Do
        # not parse every final response for each game; _final still verifies
        # the selected body's hash, event, teams, tipoff and reconciled box.
        source = db.q1("SELECT * FROM nba_source_artifacts WHERE kind='final-boxscore' AND url=? "
                        "AND received_at<=? ORDER BY received_at DESC,id DESC LIMIT 1",
                        (nba_data.summary_url(game['espn_event_id']), now))
        if not source:
            continue
        final_source, players = _final(context, source['id'], now)
        context.update(final_source=final_source, players=players)
        output.append(context)
        seen.add(game['game_id'])
        if len(output) >= limit:
            break
    return list(reversed(output))


def verify_policy_row(row):
    """Registry gate: rederive an unlocked quote and actual from frozen evidence."""
    context = _context(row['policy_context_id'])
    quotes = [q for q in context['payload']['quotes'] if q['quote_id'] == row['quote_id']]
    if len(quotes) != 1:
        raise ValueError('NBA policy row quote was not saved contemporaneously')
    quote = quotes[0]
    game = context['game']
    if any(row[k] != value for k, value in (
            ('game_id', game['game_id']), ('player_id', str(quote['player_id'])), ('market', quote['market']),
            ('side', quote['side']), ('line', quote['line']), ('observed_at', quote['observed_at']),
            ('kickoff_at', game['tipoff_ts']))):
        raise ValueError('NBA policy row differs from its sourced offer')
    source, players = _final(context, row['result_source_artifact_id'], row['result_observed_at'])
    player = players.get(str(row['player_id']))
    if not player or player['status'] != 'played' or not player.get('stats') \
            or player['team'] != quote['team'] or player['name'] != quote['player']:
        raise ValueError('NBA policy selected participation/stat is unknown or inactive')
    stats = player['stats']
    actual = sum(stats[k] for k in ('points', 'rebounds', 'assists')) if row['market'] == 'pra' else stats.get(row['market'])
    if actual is None or actual != row['actual'] or source['received_at'] != row['result_observed_at']:
        raise ValueError('NBA policy actual differs from its verified final source')
    return True


def _selection_rows(selection, quotes, history, tipoff):
    from . import nba_learning
    from .nba_prop_quality import conditional_probability
    result = []
    for row in selection.get('selections') or []:
        matching = [q for q in quotes if all(q[k] == row[k] for k in
                    ('game_id', 'player_id', 'market', 'side', 'line', 'sportsbook'))]
        if len(matching) != 1:
            raise ValueError('NBA policy selected quote identity is ambiguous')
        projected = nba_learning.project_prop(row['player_id'], row['market'], history, tipoff, row['line'])
        result.append(dict(quote_id=matching[0]['quote_id'], player_id=row['player_id'], market=row['market'],
            side=row['side'], line=row['line'], prediction=projected['raw_projection'], probability=row['probability'],
            probability_push=projected['probability_push'],
            # A price-anchored selector's probability depends on the offer's price,
            # which the price-free projection cannot reproduce.
            conditional_probability=row['policy_conditional_probability'] if 'policy_conditional_probability' in row
            else conditional_probability(projected, row['side'])))
    return result


def replay_incumbent(context):
    from . import nba_learning
    payload = context['payload']
    selector = payload['selector']
    if hashlib.sha256(_json(selector).encode()).hexdigest() != payload['selector_sha256']:
        raise ValueError('NBA policy selector binding changed')
    active = None if selector['artifact'] is None else dict(artifact=selector['artifact'], version=selector['version'],
        fingerprint=selector['fingerprint'], training_cutoff=selector['training_cutoff'])
    token = nba_learning._SNAPSHOT.set(active)
    try:
        selection = nba_learning.select_props(payload['quotes'], context['history'], context['game']['tipoff_utc'],
            context['as_of'], injuries=[dict(player_id=p['player_id'], status=p.get('availability', 'unknown'))
                                       for p in context['roster']])
        rows = _selection_rows(selection, payload['quotes'], context['history'], context['game']['tipoff_utc'])
    finally:
        nba_learning._SNAPSHOT.reset(token)
    if rows != payload['incumbent'] or len(rows) != 5:
        raise ValueError('NBA policy frozen incumbent cannot reproduce its original five selections')
    return rows


def verify_policy_universe(group, artifact=None):
    ident = group.get('policy_context_id') or group.get('context_id')
    context = _context(ident)
    if group['game_id'] != context['game']['game_id'] or sorted(group.get('universe_quote_ids') or []) != \
            sorted(q['quote_id'] for q in context['payload']['quotes']):
        raise ValueError('NBA policy full universe identity or coverage changed')
    incumbent = replay_incumbent(context)
    expected = {r['quote_id']: r for r in incumbent}
    if len(group['baseline']) != 5 or [r['quote_id'] for r in incumbent] != [r['quote_id'] for r in group['baseline']]:
        raise ValueError('NBA policy baseline membership differs from its frozen incumbent')
    for row in group['baseline']:
        captured = expected[row['quote_id']]
        if row['side'] != captured['side'] or abs(row['baseline_prediction'] - captured['prediction']) > 1e-9 \
                or abs(row['baseline_probability'] - captured['conditional_probability']) > 1e-9:
            raise ValueError('NBA policy baseline reinterprets its frozen incumbent')
    if artifact is not None:
        from . import nba_learning
        active = dict(artifact=artifact, version=artifact['version'], training_cutoff=artifact['training_cutoff'],
                      fingerprint=hashlib.sha256(_json(artifact).encode()).hexdigest())
        token = nba_learning._SNAPSHOT.set(active)
        try:
            selection = nba_learning.select_props(context['payload']['quotes'], context['history'],
                context['game']['tipoff_utc'], context['as_of'],
                injuries=[dict(player_id=p['player_id'], status=p.get('availability', 'unknown')) for p in context['roster']])
            proposed = _selection_rows(selection, context['payload']['quotes'], context['history'], context['game']['tipoff_utc'])
        finally:
            nba_learning._SNAPSHOT.reset(token)
        expected_candidate = {r['quote_id']:r for r in proposed}
        if len(proposed) != 5 or len(group['candidate']) != 5 \
                or [r['quote_id'] for r in proposed] != [r['quote_id'] for r in group['candidate']]:
            raise ValueError('NBA candidate policy does not reproduce its full-universe five')
        for row in group['candidate']:
            proposed_row = expected_candidate[row['quote_id']]
            if row['side'] != proposed_row['side'] or abs(row['candidate_prediction'] - proposed_row['prediction']) > 1e-9 \
                    or abs(row['candidate_probability'] - proposed_row['conditional_probability']) > 1e-9:
                raise ValueError('NBA candidate policy fields do not match its frozen artifact')
    return True
