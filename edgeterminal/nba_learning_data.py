"""Read-only NBA learning evidence from immutable, hash-checked snapshots.

Training never reads the mutable current-history cache. Actual-line evaluation
reconstructs each capture's own inputs and checks its locked quote and final box.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

from . import db, nba_data, nba_quotes, nba_sources

ITEM_FIELDS = ('rank', 'forecast_id', 'game_id', 'espn_player_id', 'prop', 'side', 'line', 'quote_id')
TERMINAL = {'win', 'loss', 'push', 'void'}
HOSTS = {'site.api.espn.com', 'site.web.api.espn.com', 'api.sportsgameodds.com',
         'official.nba.com', 'ak-static.cms.nba.com', 'statsdmz.nba.com'}


def verify_source_bytes(row, received_by=None):
    """Validate official/quote evidence bytes, including participation PDFs."""
    if not row:
        raise ValueError('NBA learning source is missing')
    raw = row['body'].encode() if isinstance(row['body'], str) else row['body']
    url = urlparse(row['url'])
    if (row['http_status'] != 200 or url.scheme != 'https' or url.hostname not in HOSTS
            or hashlib.sha256(raw).hexdigest() != row['sha256']
            or (received_by is not None and row['received_at'] > received_by)):
        raise ValueError(f"NBA learning source {row['id']} failed hash, identity or receipt check")
    return raw


def verify_source(row, received_by=None):
    """Verify bytes, successful receipt and known host before parsing evidence."""
    raw = verify_source_bytes(row, received_by)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError) as error:
        raise ValueError(f"NBA learning source {row['id']} is not JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"NBA learning source {row['id']} is not an object")
    return payload


def descriptor(row, role):
    return dict(kind='nba_source_artifact', id=row['id'], sha256=row['sha256'],
                received_at=row['received_at'], role=role)


def _source(source_id, cache, received_by=None):
    if source_id not in cache:
        row = db.q1('SELECT * FROM nba_source_artifacts WHERE id=?', (source_id,))
        cache[source_id] = (row, verify_source(row))
    row, body = cache[source_id]
    if received_by is not None and row['received_at'] > received_by:
        raise ValueError(f'NBA learning source {source_id} was received after its capture')
    return row, body


def history_from_sources(ids, cache, received_by):
    history, used = [], []
    for source_id in ids:
        row, body = _source(source_id, cache, received_by)
        if row['kind'] != 'player-history':
            continue
        match = re.search(r'/athletes/(\d+)/gamelog(?:\?|$)', row['url'])
        if not match or urlparse(row['url']).hostname != 'site.web.api.espn.com':
            raise ValueError(f'NBA learning source {source_id} has an invalid player identity')
        history.extend(nba_sources.player_history(body, match[1], row['sha256']))
        used.append(row)
    return history, used


def training_history(cutoff):
    """Latest snapshot per URL already received before the fitting cutoff."""
    heads = db.q("SELECT MAX(id) AS id FROM nba_source_artifacts WHERE kind='player-history' "
                 'AND received_at<? GROUP BY url', (cutoff,))
    cache = {}
    history, used = history_from_sources([row['id'] for row in heads], cache, cutoff)
    return history, [descriptor(row, 'training') for row in used]


def _capture(forecast, cache):
    sources = json.loads(forecast['source_artifact_ids'])
    history, history_sources = history_from_sources(sources, cache, forecast['created_at'])
    roster, roster_sources = [], []
    for source_id in sources:
        row, body = _source(source_id, cache, forecast['created_at'])
        if row['kind'] == 'roster':
            parsed = nba_data.parse_roster(body)
            roster.extend(dict(player_id=p['espn_player_id'], player=p['name'], team=parsed['team'])
                          for p in parsed['players'])
            roster_sources.append(row)
    return history, history_sources + roster_sources, roster


def _verify_quote_body(quote, saved, game, payload, roster):
    normalized, _ = nba_quotes.normalize(payload.get('data', []),
                                         [dict(game, id=game['game_id'], tipoff=game['tipoff_utc'])],
                                         roster, quote['observed_at'])
    keys = ('player_id', 'player', 'team', 'market', 'side', 'line', 'price', 'sportsbook')
    matched = [row for row in normalized if all(row.get(key) == saved.get(key) for key in keys)]
    if len(matched) != 1 or matched[0].get('is_main') is not True:
        raise ValueError(f'NBA learning quote {quote["quote_id"]} differs from its sourced main line')


def validation_records(now, game_count=40):
    """Replay the latest complete locked games using their exact frozen inputs.

    A game enters only with all five terminal grades. Voids and pushes are
    retained as exclusions, never converted to losses or invented outcomes.
    """
    games = db.q("SELECT g.*,l.lock_id,l.locked_at,l.sha256 AS lock_sha256 "
                 'FROM nba_games g JOIN nba_selection_locks l ON l.game_id=g.game_id '
                 "WHERE g.status='final' AND g.tipoff_ts<? ORDER BY g.tipoff_ts DESC,g.game_id DESC", (now,))
    cache, captures, records, selected_games, exclusions, excluded_sources = {}, {}, [], [], [], {}
    for game in games:
        if len(selected_games) >= game_count:
            break
        gid, lock_id = game['game_id'], game['lock_id']
        items = db.q('SELECT * FROM nba_selection_lock_items WHERE lock_id=? ORDER BY rank', (lock_id,))
        if len(items) != 5:
            raise ValueError(f'NBA learning lock {lock_id} does not contain five selections')
        encoded = json.dumps([{k: i[k] for k in ITEM_FIELDS} for i in items],
                             sort_keys=True, separators=(',', ':')).encode()
        if hashlib.sha256(encoded).hexdigest() != game['lock_sha256']:
            raise ValueError(f'NBA learning lock {lock_id} hash mismatch')
        grades = [db.q1('SELECT * FROM nba_grade_revisions WHERE lock_id=? AND rank=? '
                          'ORDER BY revision DESC LIMIT 1', (lock_id, i['rank'])) for i in items]
        if any(not g or g['result'] not in TERMINAL or g['graded_at'] > now for g in grades):
            continue
        if not game['locked_at'] < game['tipoff_ts']:
            raise ValueError(f'NBA learning lock {lock_id} was captured after tipoff')
        game_records = []
        for item, grade in zip(items, grades):
            forecast = db.q1('SELECT * FROM nba_forecasts WHERE forecast_id=?', (item['forecast_id'],))
            if (not forecast or hashlib.sha256(forecast['payload'].encode()).hexdigest() != forecast['payload_sha256']
                    or forecast['created_at'] > game['locked_at']):
                raise ValueError(f'NBA learning forecast {item["forecast_id"]} failed its capture check')
            saved = json.loads(forecast['payload'])['rows'][item['rank'] - 1]
            if any(saved[key] != item[field] for key, field in
                   [('game_id', 'game_id'), ('player_id', 'espn_player_id'), ('market', 'prop'),
                    ('side', 'side'), ('line', 'line'), ('quote_id', 'quote_id')]):
                raise ValueError(f'NBA learning forecast {item["forecast_id"]} differs from its lock')
            quote = db.q1('SELECT * FROM nba_quote_observations WHERE quote_id=?', (item['quote_id'],))
            expected_market = {'points': 'player_points', 'rebounds': 'player_rebounds',
                               'assists': 'player_assists', 'threes': 'player_threes',
                               'pra': 'player_points_rebounds_assists'}[item['prop']]
            if (not quote or (quote['game_id'], quote['espn_player_id'], quote['market'], quote['side'], quote['line']) !=
                    (gid, item['espn_player_id'], expected_market, item['side'], item['line'])
                    or quote['observed_at'] > game['locked_at']):
                raise ValueError(f'NBA learning quote {item["quote_id"]} differs from its lock')
            if forecast['forecast_id'] not in captures:
                captures[forecast['forecast_id']] = _capture(forecast, cache)
            history, history_sources, roster = captures[forecast['forecast_id']]
            qsource, quote_body = _source(quote['source_artifact_id'], cache, quote['observed_at'])
            if qsource['kind'] != 'player-quotes':
                raise ValueError(f'NBA learning quote {item["quote_id"]} lacks a quote source')
            _verify_quote_body(quote, saved, game, quote_body, roster)
            rsource = db.q1('SELECT * FROM nba_source_artifacts WHERE id=?', (grade['source_artifact_id'],))
            if grade['result'] == 'void':
                # Participation-only official evidence may be PDF. The registry
                # retains its checked bytes; no numeric outcome is used for learning.
                verify_source_bytes(rsource, received_by=grade['graded_at'])
                excluded_sources[rsource['id']] = descriptor(rsource, 'validation')
                exclusions.append(dict(game_id=gid, quote_id=item['quote_id'], result='void'))
                continue
            _, result_body = _source(grade['source_artifact_id'], cache, grade['graded_at'])
            parsed = nba_data.parse_summary(result_body)
            result_game = parsed['game']
            if (urlparse(rsource['url']).hostname != 'site.api.espn.com'
                    or parsed.get('box_complete') is not True or result_game.get('status') != 'final'
                    or result_game.get('game_id') != gid
                    or any(result_game.get(key) != game[key] for key in
                           ('home_team', 'away_team', 'home_score', 'away_score'))):
                raise ValueError(f'NBA learning final source {rsource["id"]} differs from game {gid}')
            players = [p for p in parsed['players'] if p['espn_player_id'] == item['espn_player_id']]
            if (len(players) != 1 or players[0]['status'] != 'played'
                    or players[0]['team'] != saved['team'] or players[0]['name'] != saved['player']):
                raise ValueError(f'NBA learning final player identity differs for quote {item["quote_id"]}')
            actual = players[0]['stats'][item['prop']]
            expected = ('push' if actual == item['line'] else 'win'
                        if (actual > item['line']) == (item['side'] == 'over') else 'loss')
            if actual != grade['actual_value'] or expected != grade['result']:
                raise ValueError(f'NBA learning grade differs from final box for quote {item["quote_id"]}')
            if expected == 'push':
                exclusions.append(dict(game_id=gid, quote_id=item['quote_id'], result='push'))
                continue
            game_records.append(dict(game_id=gid, player_id=item['espn_player_id'], market=item['prop'],
                                     quote_id=item['quote_id'], line=item['line'], side=item['side'],
                                     observed_at=quote['observed_at'], kickoff_at=game['tipoff_ts'],
                                     result_observed_at=grade['graded_at'], actual=actual,
                                     history=history, saved=saved,
                                     sources=[descriptor(s, 'validation') for s in history_sources + [qsource, rsource]]))
        selected_games.append(gid)
        records.extend(game_records)
    sources = {(s['id'], s['role']): s for record in records for s in record['sources']}
    sources.update({(key, 'validation'): value for key, value in excluded_sources.items()})
    return records, dict(game_ids=selected_games, exclusions=exclusions), list(sources.values())
