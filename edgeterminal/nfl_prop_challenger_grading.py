"""Verified results for sealed challenger picks absent from the main freeze.

Only result evidence is appended. Pregame selections and models are never rebuilt.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from collections import defaultdict
from urllib.parse import urlencode

from . import db, nfl_identity, nfl_official_participation as official, nfl_result_cache
from . import nfl_prop_grading as grading, nfl_prop_result_ingest as ingest
from . import nfl_prop_challenger as challenger, nfl_prop_lab as lab
from .providers.espn import SCOREBOARD_URL

RESULT_POLL_S = 300
AUDIT_INTERVAL_S = 86400
RECENT_GAME_WINDOW_S = 7 * 86400


def _key(row):
    return row['game_id'], row['player_id'], row['market']


def _archive(raw, url, now):
    digest = hashlib.sha256(raw).hexdigest()
    db.ex('INSERT OR IGNORE INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',
          (digest, url, now, raw))
    return {'source': url, 'file': ingest._source_file(digest),
            'sha256': digest, 'retrieved_at': now}


def _quote(row, stats_rows):
    """Bind a sealed pick to its immutable source quote and unique prior-week GSIS."""
    source = db.q1('SELECT * FROM nfl_prop_snapshots WHERE id=?', (row['quote_id'],))
    expected = {'canonical_game_id': row['game_id'], 'player_id': row['player_id'],
                'player_name': row['player'], 'line_type': 'main',
                'market': row['market'], 'line': row['line'], 'side': row['side'],
                'sportsbook': row['sportsbook'], 'price': row['price'],
                'retrieved_at': row['observed_at'], 'kickoff_utc': row['kickoff_utc']}
    if not source or any(source[k] != v for k, v in expected.items()):
        raise ValueError('challenger selection conflicts with original quote')
    if not row['observed_at'] <= row['selected_at'] < row['kickoff_utc']:
        raise ValueError('challenger selection was not saved before kickoff')
    season, week = int(row['game_id'].split('-')[1]), int(row['game_id'].split('-')[3])
    team = nfl_identity.team_code(source['team'])
    ids = {r['player_id'] for r in stats_rows if r.get('season_type') == 'REG' and
           season - 1 <= int(r['season']) and (int(r['season']), int(r['week'])) < (season, week) and
           nfl_identity.team_code(r['team']) == team and
           lab._norm_name(r.get('player_display_name', '')) == lab._norm_name(row['player'])}
    if len(ids) != 1 or team not in row['game_id'].split('-')[-2:]:
        raise ValueError('challenger player lacks unique prior-game identity')
    return {**row, 'canonical_game_id': row['game_id'], 'player_name': row['player'],
            'gsis_player_id': next(iter(ids))}


def _settled(quote, checked):
    player = checked['players'].get(quote['gsis_player_id'])
    if not player or player['status'] == 'unknown':
        return None
    if player['status'] == 'inactive':
        actual, result = None, 'void'
    elif quote['market'] in player['stats']:
        actual = float(player['stats'][quote['market']])
        result = grading._settlement(quote['side'], actual, quote['line'])
    else:
        return None
    return {'actual_stat': actual, 'research_statistical_result': result}


def _main_results():
    output = {}
    for freeze in db.q('SELECT freeze_sha FROM nfl_prop_research_freezes'):
        for result in grading._latest(freeze['freeze_sha']):
            if result['actual_stat'] is None and result.get('research_statistical_result') != 'void':
                continue
            key = result['canonical_game_id'], result['player_id'], result['market']
            if key in output and output[key]['actual_stat'] != result['actual_stat']:
                raise ValueError('conflicting challenger result evidence')
            output[key] = result
    return output


def verified_results(selected, now=None):
    stamp = time.time() if now is None else now
    grouped = defaultdict(list)
    for row in selected:
        grouped[(row['selection_sha256'], row['game_id'])].append(row)
    output = {}
    files = []
    stat_rows = {}
    for path in (challenger.DIRECTORY / 'results').glob('*.json'):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem:
            raise ValueError('challenger result evidence changed')
        data = json.loads(raw)
        if data['created_at'] > stamp:
            raise ValueError('challenger result evidence is from the future')
        files.append(data)
    for data in sorted(files, key=lambda d: d['created_at']):
        game = data['game_id']
        rows = grouped.get((data['selection_sha256'], game))
        if not rows:
            raise ValueError('challenger evidence has no matching sealed selections')
        if 'quote_ids' in data:
            ids = data['quote_ids']
            if (not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids) or
                    len(ids) != len(set(ids)) or not set(ids) <= {r['quote_id'] for r in rows}):
                raise ValueError('challenger result quote IDs do not match sealed selections')
            rows = [r for r in rows if r['quote_id'] in ids]
        evidence = data['evidence']
        checked = grading._validate_evidence(evidence, {game}, stamp)
        verified = evidence.get('verified_final') is True and checked['status'] == 'final'
        if checked['status'] != 'final' or (not verified and
                stamp < checked['kickoff'] + grading.POSTGAME_DELAY_S):
            raise ValueError('challenger results require a completed game and postgame wait')
        digest = checked['stat_source']['sha256']
        if digest not in stat_rows:
            stats_raw = ingest._raw(checked['stat_source'])
            stat_rows[digest] = list(csv.DictReader(io.StringIO(stats_raw.decode('utf-8-sig'))))
        for row in rows:
            if row['selected_at'] >= checked['kickoff']:
                raise ValueError('challenger selection was saved after actual kickoff')
            quote = _quote(row, stat_rows[digest])
            result = _settled(quote, checked)
            if result:
                output[_key(row)] = {**result, 'created_at': data['created_at'],
                    'evidence_sha256': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}
    return output


def grade_due(now=None, *, fetcher=None, participation_reviews=None):
    stamp = time.time() if now is None else now
    selected = challenger.selected_rows()
    main = _main_results()
    known = verified_results(selected, now=stamp)
    for key in main.keys() & known.keys():
        if main[key]['actual_stat'] != known[key]['actual_stat']:
            raise ValueError('challenger result conflicts with main result evidence')
    selected_games, groups = defaultdict(list), {}
    for row in selected:
        selected_games[row['game_id']].append(row)
    for game, rows in selected_games.items():
        remaining = [r for r in rows if _key(r) not in main]
        kickoff = ingest._expected_kickoff(game, rows)
        if not remaining or stamp < kickoff:
            continue
        pending = any(_key(r) not in known for r in remaining)
        cadence = (RESULT_POLL_S if pending and stamp-kickoff <= RECENT_GAME_WINDOW_S
                   else AUDIT_INTERVAL_S)
        key = 'nfl_challenger_result_check:' + rows[0]['selection_sha256'] + ':' + game
        last = (db.kv_get(key) or {}).get('checked_at')
        if last is None and not pending:
            last = max(known[_key(r)]['created_at'] for r in remaining)
        if (fetcher is None or not pending) and last is not None and stamp < last + cadence:
            continue
        groups[game] = rows
        db.kv_set(key, {'checked_at': stamp, 'cadence_seconds': cadence})
    if not groups:
        return {**challenger.report(), 'games_checked': 0, 'checks': []}
    use_cache = fetcher is None
    fetcher = fetcher or ingest._fetch
    feeds, checks = {}, []
    def read(url):
        if url not in feeds:
            raw, retrieved = (nfl_result_cache.read(url, fetcher, now=stamp)
                              if use_cache else (fetcher(url), stamp))
            feeds[url] = raw, _archive(raw, url, retrieved)
        return feeds[url]
    for game, pending in groups.items():
        try:
            season, week = int(game.split('-')[1]), int(game.split('-')[3])
            score_url = SCOREBOARD_URL + '?' + urlencode({'dates': season, 'seasontype': 2, 'week': week, 'limit': 100})
            score_raw, score_source = read(score_url)
            state = ingest._scoreboard(score_raw, {game: pending}).get(game)
            if not state or state['status'] != 'final' or stamp < state['kickoff']:
                checks.append({'game_id': game, 'status': 'waiting_for_final'})
                continue
            if any(r['selected_at'] >= state['kickoff'] for r in pending):
                raise ValueError('challenger selection was saved after actual kickoff')
            stats_raw, stat_source = read(ingest.STATS_URL.format(season=season))
            stats_rows = list(csv.DictReader(io.StringIO(stats_raw.decode('utf-8-sig'))))
            quotes = [_quote(row, stats_rows) for row in pending]
            schedule_id = ingest._schedule_ids({game: quotes})[game]
            stats = ingest._stats(stats_raw, schedule_id, game)
            players, _ = ingest._player_evidence(quotes, stats, stat_source, None, None)
            review = (participation_reviews or {}).get(game)
            if review is not None:
                validated, part = ingest._participation(review, game, {q['gsis_player_id'] for q in quotes})
                if not state['kickoff'] <= validated['reviewed_at'] <= stamp:
                    raise ValueError('challenger participation review is outside completed game')
                source = _archive(part['source_bytes'], validated['source_url'], stamp)
                manifest = _archive(review, 'local:challenger-reviewed-participation', stamp)
                source['supporting_sources'] = [manifest]
                part['manifest_sha256'] = manifest['sha256']
                players, _ = ingest._player_evidence(quotes, stats, stat_source, part, source)
            unresolved = {p['gsis_player_id'] for p in players if p['status'] == 'unknown' or
                          (p['status'] == 'active' and any(q['market'] not in p['stats'] for q in quotes
                                                        if q['gsis_player_id'] == p['gsis_player_id']))}
            if unresolved:
                try:
                    part, source = official.resolve(game, [q for q in quotes if q['gsis_player_id'] in unresolved],
                                                    stats_raw, state['kickoff'], state['score'], stamp)
                    manifest = _archive(json.dumps({'participation': part, 'sources': source}, sort_keys=True).encode(),
                                        'local:challenger-official-participation', stamp)
                    source['supporting_sources'].append(manifest)
                    part['manifest_sha256'] = manifest['sha256']
                    players, _ = ingest._player_evidence(quotes, stats, stat_source, part, source)
                except Exception as exc:
                    checks.append({'game_id': game, 'status': 'needs_review', 'reason': str(exc)})
            sources = [score_source, stat_source]
            for player in players:
                ref = player.get('participation_evidence')
                if ref:
                    sources.extend([ref, *ref.get('supporting_sources', [])])
            evidence = {'game_id': game, 'game_status': 'final', 'final_score': state['score'],
                        'actual_kickoff_utc': state['kickoff'], 'final_game_source': score_source,
                        'player_stat_source': stat_source, 'players': players,
                        'result_retrieved_at': max(r['retrieved_at'] for r in sources)}
            checked = grading._validate_evidence(evidence, {game}, stamp)
            evidence['verified_final'] = True
            changed = False
            for quote in quotes:
                result = _settled(quote, checked)
                if result is None:
                    continue
                key = _key(quote)
                if key in main and main[key]['actual_stat'] != result['actual_stat']:
                    raise ValueError('challenger result conflicts with main result evidence')
                if key not in main and any(known.get(key, {}).get(k) != v for k, v in result.items()):
                    changed = True
            if not changed:
                checks.append({'game_id': game, 'status': 'checked'})
                continue
            data = {'version': 1, 'game_id': game, 'created_at': stamp,
                    'quote_ids': sorted(q['quote_id'] for q in quotes if _key(q) not in main),
                    'selection_sha256': pending[0]['selection_sha256'], 'evidence': evidence}
            raw = json.dumps(data, sort_keys=True, allow_nan=False).encode()
            directory = challenger.DIRECTORY / 'results'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (hashlib.sha256(raw).hexdigest() + '.json')
            if not path.exists():
                with path.open('xb') as handle:
                    handle.write(raw)
                path.chmod(0o444)
            checks.append({'game_id': game, 'status': 'checked'})
        except Exception as exc:
            checks.append({'game_id': game, 'status': 'needs_review', 'reason': str(exc)})
    return {**challenger.report(), 'games_checked': len(groups), 'checks': checks}
