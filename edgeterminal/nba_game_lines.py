"""Verified per-market comparison lines sealed before NBA tipoff.

Fresh projection inputs stay in ``payload.market``. Comparison evidence may be
retained, but its original receipt, sportsbook and origin never become fresh.
This module is passive: readers neither repair history nor query a provider.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

from . import db, nba_store

POLICY_ID = 'nba-saved-market-lines-v1'
FIELDS = {'spread': ('home_spread', 'away_spread', 'home_price', 'away_price'),
          'total': ('total', 'over_price', 'under_price'),
          'moneyline': ('home_moneyline', 'away_moneyline')}
META = ('source', 'source_artifact_id', 'source_sha256', 'observed_at', 'sportsbook')


def _forecast(ident):
    return nba_store._forecast(db.q1('SELECT * FROM nba_forecasts WHERE forecast_id=?', (ident,)))


def _game(ident):
    return db.q1('SELECT * FROM nba_games WHERE game_id=?', (ident,))


def _artifact(ident):
    return db.q1('SELECT * FROM nba_source_artifacts WHERE id=?', (ident,))


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _group(raw, kind):
    fields = FIELDS[kind]
    for key in fields:
        value = raw.get(key)
        if value is not None and not _finite(value):
            raise ValueError('NBA comparison market values must be finite')
    required = {'spread': ('home_spread', 'away_spread'), 'total': ('total',),
                'moneyline': ('home_moneyline', 'away_moneyline')}[kind]
    if any(raw.get(key) is None for key in required):
        return None
    group = {key: raw[key] for key in fields + META if key in raw}
    if not isinstance(group.get('sportsbook'), str) or group['sportsbook'].strip().lower() in ('', 'unknown'):
        raise ValueError('NBA comparison market needs a named sportsbook')
    if kind == 'spread' and group['home_spread'] != -group['away_spread']:
        raise ValueError('NBA comparison spread sides must be opposite')
    if kind == 'total' and not 0 < group['total'] <= 1000:
        raise ValueError('NBA comparison total line is outside supported bounds')
    if kind == 'spread' and abs(group['home_spread']) > 100:
        raise ValueError('NBA comparison spread line is outside supported bounds')
    return group


def fresh_validation_market(raw):
    """Incomplete offer groups are absent, never completed with unrelated sides."""
    result = {key: raw[key] for key in META if key in raw}
    for kind in FIELDS:
        group = _group(raw, kind)
        if group:
            result.update({key: value for key, value in group.items() if key in FIELDS[kind]})
    return result


def _check_fresh(raw, game, at):
    from . import nba_game_forecasts as live
    if raw.get('suspended') or raw.get('isSuspended') or str(raw.get('status', '')).lower() in ('suspended', 'withdrawn', 'closed'):
        raise ValueError('NBA market explicitly suspended or withdrawn')
    if not raw:
        return
    _, comp = _market_source(raw, game, at)
    for offer in comp.get('odds') or []:
        provider = offer.get('provider') or {}
        book = raw.get('sportsbook')
        if (not isinstance(book, str) or book.strip().lower() in ('', 'unknown')
                or book in (provider.get('name'), provider.get('displayName'))):
            if offer.get('suspended') or offer.get('isSuspended') or str(offer.get('status', '')).lower() in ('suspended', 'withdrawn', 'closed'):
                raise ValueError('NBA market source explicitly suspended or withdrawn')


def _market_source(group, game, at):
    from . import nba_game_forecasts as live
    ident = group.get('source_artifact_id')
    sources = live._sources([ident], at, readonly=True)
    live._validate_market({key: group[key] for key in META if key in group}, sources, game, at)
    source = sources[ident]
    url = urlsplit(source['url'])
    query = parse_qs(url.query, keep_blank_values=True)
    base = '/apis/site/v2/sports/basketball/nba/'
    if (url.scheme != 'https' or url.hostname != 'site.api.espn.com' or url.port not in (None, 443)
            or url.username or url.password or url.fragment):
        raise ValueError('NBA comparison source URL is not the approved origin')
    if url.path == base + 'scoreboard':
        day = datetime.fromtimestamp(game['tipoff_ts'], timezone.utc).astimezone(ZoneInfo('America/New_York')).strftime('%Y%m%d')
        if (source['kind'] != 'scoreboard' or set(query) != {'dates', 'limit'}
                or query['dates'] != [day] or query['limit'] not in (['100'], ['200'])):
            raise ValueError('NBA comparison scoreboard source date or route conflict')
    elif url.path == base + 'summary':
        if source['kind'] != 'summary' or query != {'event': [game['espn_event_id']]}:
            raise ValueError('NBA comparison summary source route identity conflict')
    else:
        raise ValueError('NBA comparison source route is not approved')
    if group.get('source') != 'ESPN':
        raise ValueError('NBA retained market requires the approved ESPN source')
    if group['observed_at'] >= game['tipoff_ts']:
        raise ValueError('NBA comparison quote must precede tipoff')
    comp = live._competition(source, game)
    return source, comp


def _validate_group(group, kind, game, at):
    from . import nba_game_forecasts as live
    source, comp = _market_source(group, game, at)
    ident = source['id']
    sources = {ident: source}
    live._validate_market(group, sources, game, at)
    offers = [o for o in comp.get('odds') or []
              if group['sportsbook'] in ((o.get('provider') or {}).get('name'),
                                          (o.get('provider') or {}).get('displayName'))]
    if not offers:
        raise ValueError('NBA comparison offer sportsbook identity conflict')
    # Reject contradictory same-book offers rather than hiding ambiguity by id.
    signatures = set()
    for offer in offers:
        if kind == 'spread':
            raw = offer.get('pointSpread') or {}
            signature = tuple((raw.get(side) or {}).get('close', {}).get('line') for side in ('home', 'away'))
        elif kind == 'total':
            raw = offer.get('total') or {}
            signature = tuple((raw.get(side) or {}).get('close', {}).get('line') for side in ('over', 'under'))
            # A retained total must be bound to both sides of the source offer.
            from .nba_sources import number
            for side, value in zip(('over', 'under'), signature):
                if isinstance(value, str) and value.lower().startswith(side[0]):
                    value = value[1:]
                if number(value) != group['total']:
                    raise ValueError('NBA comparison total needs matching source sides')
        else:
            raw = offer.get('moneyline') or {}
            signature = tuple((raw.get(side) or {}).get('close', {}).get('odds') for side in ('home', 'away'))
        from .nba_sources import number
        sides = ('over', 'under') if kind == 'total' else ('home', 'away')
        normalized = tuple(number(value[1:] if kind == 'total' and isinstance(value, str) and value.lower().startswith(side[0]) else value)
                           for side, value in zip(sides, signature))
        prices = tuple(number((raw.get(side) or {}).get('close', {}).get('odds')) for side in sides)
        signatures.add((normalized, prices))
    if len(signatures) != 1:
        raise ValueError('NBA comparison market has ambiguous same-book offers')
    return sources[ident]


def _verify_row(saved, game):
    from . import nba_game_forecasts as live
    p = saved['payload']
    if (saved['game_id'] != game['game_id'] or p.get('kind') != 'game-revision'
            or p.get('policy_id') != live.POLICY_ID
            or not _finite(p.get('as_of')) or not p['as_of'] <= saved['created_at'] < game['tipoff_ts']
            or p.get('projection', {}).get('tipoff') != game['tipoff_ts']
            or p.get('projection', {}).get('home') != game['home_team']
            or p.get('projection', {}).get('away') != game['away_team']):
        raise ValueError('NBA comparison origin forecast identity or chronology conflict')
    if (saved.get('forecast_id') != 'game-revision-' + game['game_id'] + '-' + str(p.get('snapshot_sha256', ''))[:24]
            or saved.get('model_version') != (p.get('model') or {}).get('version')):
        raise ValueError('NBA comparison origin forecast identifier or model identity conflict')
    descriptors = p.get('source_artifacts') or []
    if ([row.get('id') for row in descriptors] != sorted(saved.get('source_artifact_ids') or [])
            or len({row.get('id') for row in descriptors}) != len(descriptors)):
        raise ValueError('NBA comparison origin source catalog identity conflict')
    snapshot = {k: v for k, v in p.items() if k not in ('as_of', 'snapshot_sha256', 'input_manifest_sha256')}
    if (live._sha(live._json(p)) != saved['payload_sha256']
            or live._sha(live._json(snapshot)) != p.get('snapshot_sha256')
            or live._sha(live._json(p.get('input_manifest'))) != p.get('input_manifest_sha256')):
        raise ValueError('NBA comparison origin forecast hash conflict')
    return p


def verify_origin(saved, game, market):
    """Verify one existing priced pair without looking at any result labels."""
    p = _verify_row(saved, game)
    _check_fresh(p.get('market') or {}, game, p['as_of'])
    group = _group(p.get('market') or {}, market)
    if group is None:
        return None
    source = _validate_group(group, market, game, p['as_of'])
    if source['id'] not in saved['source_artifact_ids']:
        raise ValueError('NBA comparison origin source is not sealed with forecast')
    descriptor = {'id': source['id'], 'sha256': source['sha256'], 'received_at': source['received_at']}
    if descriptor not in p.get('source_artifacts', []):
        raise ValueError('NBA comparison origin source descriptor conflict')
    return dict(origin_forecast_id=saved['forecast_id'], origin_payload_sha256=saved['payload_sha256'],
                origin_as_of=p['as_of'], origin_created_at=saved['created_at'],
                game_id=game['game_id'], tipoff_ts=game['tipoff_ts'],
                source_received_at=source['received_at'], market=group)


def _legacy_reference(game):
    enrollment = db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name='nba_game_forecast_enrollments'")
    if enrollment:
        row = db.q1('SELECT original_forecast_id FROM nba_game_forecast_enrollments WHERE game_id=?', (game['game_id'],))
        if row:
            return _forecast(row['original_forecast_id']) if row['original_forecast_id'] else None
    # The first producer resolves before enrollment is written. Use precisely
    # the same original legacy game record; never choose after observing labels.
    row = db.q1("SELECT forecast_id FROM nba_forecasts WHERE game_id=? AND json_extract(payload,'$.kind')='game' "
                'ORDER BY created_at,forecast_id LIMIT 1', (game['game_id'],))
    return _forecast(row['forecast_id']) if row else None


def verify_legacy_origin(saved, game, market):
    """Strict adapter for the enrolled original, never a weak revision verifier."""
    from . import nba_game_forecasts as live
    p = saved['payload']
    group = _group(p.get('market') or {}, market)
    if group is None:
        if any((p.get('market') or {}).get(key) is not None for key in FIELDS[market]):
            raise ValueError('NBA priced legacy comparison group is incomplete and unsupported')
        return None
    projection = p.get('projection') or {}
    reference = _legacy_reference(game)
    if (not reference or reference['forecast_id'] != saved['forecast_id']
            or saved['forecast_id'] != 'game-' + game['game_id']
            or saved['game_id'] != game['game_id'] or p.get('kind') != 'game'
            or live._sha(live._json(p)) != saved['payload_sha256']
            or not _finite(saved['created_at']) or not saved['created_at'] < game['tipoff_ts']
            or projection.get('status') != 'ok' or projection.get('home') != game['home_team']
            or projection.get('away') != game['away_team']
            or projection.get('tipoff') != game['tipoff_ts']
            or (p.get('model') or {}).get('version') != saved['model_version']
            or projection.get('model_version') != saved['model_version']):
        raise ValueError('NBA priced legacy origin identity, hash, model or chronology is unsupported')
    _check_fresh(p.get('market') or {}, game, saved['created_at'])
    source = _validate_group(group, market, game, saved['created_at'])
    if source['id'] not in saved['source_artifact_ids']:
        raise ValueError('NBA priced legacy source is absent from its sealed catalog')
    if len(set(saved['source_artifact_ids'])) != len(saved['source_artifact_ids']):
        raise ValueError('NBA priced legacy source catalog is ambiguous')
    return dict(origin_kind='legacy-game', origin_forecast_id=saved['forecast_id'],
                origin_payload_sha256=saved['payload_sha256'], origin_as_of=saved['created_at'],
                origin_created_at=saved['created_at'], game_id=game['game_id'], tipoff_ts=game['tipoff_ts'],
                source_received_at=source['received_at'], market=group)


def resolve(game, fresh, as_of):
    """Resolve comparison evidence before inference; do not promote old prices."""
    if not _finite(as_of) or as_of >= game['tipoff_ts'] or game['status'] != 'scheduled':
        raise ValueError('NBA comparison resolution requires a future scheduled game')
    if not isinstance(fresh, dict):
        raise ValueError('NBA fresh market must be an object')
    groups = {}
    _check_fresh(fresh, game, as_of)
    for kind in FIELDS:
        group = _group(fresh, kind)
        if group is not None:
            source = _validate_group(group, kind, game, as_of)
            groups[kind] = {'basis': 'fresh', 'market': group, 'source_received_at': source['received_at']}
    parent_row = db.q1("SELECT forecast_id FROM nba_forecasts WHERE game_id=? AND created_at<=? "
                       "AND json_extract(payload,'$.kind')='game-revision' AND json_extract(payload,'$.projection.tipoff')=? "
                       'ORDER BY created_at DESC,forecast_id DESC LIMIT 1', (game['game_id'], as_of, game['tipoff_ts']))
    parent = parent_row['forecast_id'] if parent_row else None
    if parent:
        parent_saved = _forecast(parent)
        _verify_row(parent_saved, game)
        for kind, current in groups.items():
            old = verify_origin(parent_saved, game, kind)
            if not old:
                entry = (parent_saved['payload'].get('line_context') or {}).get('groups', {}).get(kind)
                if entry:
                    _verify_entry(entry, kind, game, parent_saved['payload']['as_of'], parent_saved['payload'].get('market') or {})
                    old = entry
            if old:
                old_market = old['market']
                current_market = current['market']
                if (old_market['sportsbook'] == current_market['sportsbook']
                        and old_market['observed_at'] == current_market['observed_at']
                        and old_market != current_market):
                    raise ValueError('NBA comparison contradictory offers share one observation clock')
                if old_market['observed_at'] > current_market['observed_at']:
                    # This is an out-of-order provider response, not a new quote.
                    # Do not select a favorable older price under a new receipt.
                    raise ValueError('NBA comparison quote observation moved backwards')
    # Query the relevant market, not hundreds of full forecast/source payloads.
    # An invalid newest candidate is an error; do not fish for a nicer older one.
    primary = {'spread': 'home_spread', 'total': 'total', 'moneyline': 'home_moneyline'}
    for kind in FIELDS:
        if kind in groups:
            continue
        item = db.q1("SELECT forecast_id FROM nba_forecasts WHERE game_id=? AND created_at<=? "
                     "AND json_extract(payload,'$.kind')='game-revision' AND json_extract(payload,'$.projection.tipoff')=? "
                     "AND (json_extract(payload,?) IS NOT NULL OR json_extract(payload,?) IS NOT NULL) "
                     'ORDER BY created_at DESC,forecast_id DESC LIMIT 1',
                     (game['game_id'], as_of, game['tipoff_ts'], '$.market.' + primary[kind], '$.line_context.groups.' + kind))
        if not item:
            continue
        saved = _forecast(item['forecast_id'])
        _verify_row(saved, game)
        origin = verify_origin(saved, game, kind)
        if origin:
            groups[kind] = dict(origin, basis='retained')
        else:
            prior = (saved['payload'].get('line_context') or {}).get('groups', {}).get(kind)
            if prior:
                _verify_entry(prior, kind, game, saved['payload']['as_of'], saved['payload'].get('market') or {})
                groups[kind] = dict(prior, basis='retained')
    legacy = _legacy_reference(game)
    if legacy and (legacy['payload'].get('projection') or {}).get('tipoff') == game['tipoff_ts']:
        for kind in FIELDS:
            if kind not in groups:
                origin = verify_legacy_origin(legacy, game, kind)
                if origin:
                    if origin['origin_created_at'] > as_of:
                        raise ValueError('NBA priced legacy origin was saved after forecast as-of')
                    groups[kind] = dict(origin, basis='retained')
    from . import nba_game_forecasts as live
    return {'policy_id': POLICY_ID, 'groups': groups, 'parent_forecast_id': parent,
            'market_head_sha256': live._sha(live._json(db.kv_get('nba_market_' + game['game_id']))),
            'source_ids': sorted({g['market']['source_artifact_id'] for g in groups.values()})}


def publication_check(game, context):
    """Short write-boundary ownership/hash check; no source replay or inference."""
    from . import nba_game_forecasts as live
    parent = db.q1("SELECT forecast_id FROM nba_forecasts WHERE game_id=? "
                    "AND json_extract(payload,'$.kind')='game-revision' AND json_extract(payload,'$.projection.tipoff')=? "
                    'ORDER BY created_at DESC,forecast_id DESC LIMIT 1', (game['game_id'], game['tipoff_ts']))
    if (parent['forecast_id'] if parent else None) != context['parent_forecast_id']:
        raise ValueError('NBA comparison parent changed before forecast publication')
    if live._sha(live._json(db.kv_get('nba_market_' + game['game_id']))) != context['market_head_sha256']:
        raise ValueError('NBA comparison market source head changed before publication')
    for entry in context['groups'].values():
        market = entry['market']
        source = _artifact(market['source_artifact_id'])
        if (not source or source['http_status'] != 200
                or source['received_at'] != entry['source_received_at']
                or source['sha256'] != market['source_sha256']
                or live._sha(source['body']) != source['sha256']):
            raise ValueError('NBA comparison source changed before publication')


def _verify_entry(entry, kind, game, at, fresh):
    if not isinstance(entry, dict) or entry.get('basis') not in ('fresh', 'retained'):
        raise ValueError('NBA comparison binding basis is invalid')
    if entry['basis'] == 'fresh':
        group = _group(fresh, kind)
        if group is None or group != entry.get('market'):
            raise ValueError('NBA comparison fresh market differs from sealed input')
        source = _validate_group(group, kind, game, at)
        if source['received_at'] != entry.get('source_received_at'):
            raise ValueError('NBA comparison source receipt changed')
        return
    if _group(fresh, kind) is not None:
        raise ValueError('NBA complete fresh market cannot be displaced by retained evidence')
    origin = _forecast(entry.get('origin_forecast_id'))
    if not origin or origin['created_at'] > at:
        raise ValueError('NBA comparison origin is missing or from the future')
    expected = (verify_legacy_origin(origin, game, kind) if entry.get('origin_kind') == 'legacy-game'
                else verify_origin(origin, game, kind))
    if expected is None or dict(expected, basis='retained') != entry:
        raise ValueError('NBA comparison retained origin binding conflict')


def verify(saved, game=None):
    """Validate new-context sources and bindings on passive serving/grade reads."""
    p = saved['payload']
    context = p.get('line_context')
    if context is None:
        return None
    game = game or _game(saved['game_id'])
    _verify_row(saved, game)
    if context.get('policy_id') != POLICY_ID or set(context) != {'policy_id', 'groups'}:
        raise ValueError('NBA comparison context policy or schema conflict')
    if not isinstance(context['groups'], dict) or set(context['groups']) - set(FIELDS):
        raise ValueError('NBA comparison context has unknown markets')
    fresh = p.get('market') or {}
    if any(_group(fresh, kind) is not None and kind not in context['groups'] for kind in FIELDS):
        raise ValueError('NBA comparison context omitted a complete fresh market')
    for kind, entry in context['groups'].items():
        _verify_entry(entry, kind, game, p['as_of'], p.get('market') or {})
        ident = entry['market']['source_artifact_id']
        source = _artifact(ident)
        descriptor = {'id': ident, 'sha256': source['sha256'], 'received_at': source['received_at']}
        if ident not in saved['source_artifact_ids'] or descriptor not in p.get('source_artifacts', []):
            raise ValueError('NBA retained source is not sealed in new forecast')
    return context


def sealed_context(context):
    return {'policy_id': context['policy_id'], 'groups': copy.deepcopy(context['groups'])}


def effective_market(saved, market=None):
    """Return comparison groups; per-market requests retain exact source fields."""
    context = verify(saved)
    if context is None:
        return dict(saved['payload'].get('market') or {})
    groups = context['groups']
    if not groups:
        return {}
    if market:
        entry = groups.get(market)
        return dict(entry['market'], line_basis=entry['basis'],
                    origin_forecast_id=entry.get('origin_forecast_id')) if entry else {}
    result = {}
    refs = {}
    for kind, entry in groups.items():
        result.update({key: value for key, value in entry['market'].items() if key in FIELDS[kind]})
        refs[kind] = dict(entry, observed_at=entry['market']['observed_at'])
    if len({tuple(entry['market'].get(k) for k in META) for entry in groups.values()}) == 1 and groups:
        result.update({key: value for key, value in next(iter(groups.values()))['market'].items() if key in META})
    result['line_refs'] = refs
    return result
