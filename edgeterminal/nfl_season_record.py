"""Read-only season records from saved pregame forecasts and sourced grades.

Historic props are ranked now from the earliest saved pregame freeze, visibly
separate from selections actually locked by the current top-five policy.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict

from . import db, nfl_prop_grading, nfl_top_five
from .integrity import parse_time
from .nfl_identity import team_code, canonical_id
from .config import ROOT


def summary(rows: list[dict], field: str = 'result') -> dict:
    counts = Counter(r.get(field, 'pending') for r in rows)
    decisions = counts['won'] + counts['lost']
    return {'record': dict(counts), 'count': len(rows),
            'win_rate': counts['won'] / decisions if decisions else None}


def props(season: int = 2026, now: float | None = None, *, include_replay: bool = True) -> dict:
    stamp = time.time() if now is None else now
    live = nfl_top_five.view()
    games = [{**g, 'basis': 'locked', 'week': int(g['game_id'].split('-')[3])}
             for g in live['games'] if g['game_id'].startswith(f'nfl-{season}-reg-')]
    seen = {g['game_id'] for g in games}
    for record in db.q('SELECT freeze_sha,frozen_at FROM nfl_prop_research_freezes ORDER BY frozen_at,freeze_sha'):
        frozen = nfl_prop_grading._freeze(record['freeze_sha'])
        grouped = defaultdict(list)
        for row in frozen['projections']:
            game = row['canonical_game_id']
            if (game.startswith(f'nfl-{season}-reg-') and game not in seen and
                    row['observed_at'] <= record['frozen_at'] < row['kickoff_utc'] <= stamp):
                grouped[game].append(row)
        if not grouped:
            continue
        grades = {g['quote_id']: g for g in nfl_prop_grading._latest(record['freeze_sha'])}
        for game, candidates in grouped.items():
            ranked = nfl_top_five.rank(candidates)
            rows = []
            for n, q in enumerate(ranked, 1):
                grade = grades.get(q['quote_id'], {})
                rows.append({**q, 'rank': n, 'player': q['player_name'], 'stronger_side': q['side'],
                             'confidence': q['probability_' + q['side']],
                             'result': grade.get('research_statistical_result', 'pending'),
                             'actual': grade.get('actual_stat'), 'revision': grade.get('revision'),
                             'graded_at': grade.get('graded_at'), 'selected_at': None,
                             'quote_observed_at': q['observed_at']})
            games.append({'game_id': game, 'week': int(game.split('-')[3]),
                          'freeze_sha': record['freeze_sha'], 'saved_at': record['frozen_at'],
                          'selected_at': None, 'basis': 'archived_ranking', 'rows': rows})
            seen.add(game)
    if include_replay:
        from . import nfl_prop_retrospective
        for game in nfl_prop_retrospective.load(season)['games']:
            if game['game_id'] not in seen:
                games.append(game)
                seen.add(game['game_id'])
    games.sort(key=lambda g: (g['week'], g['game_id']), reverse=True)
    rows = [r for g in games for r in g['rows']]
    cohorts = {}
    for basis, name in (('locked', 'locked'), ('archived_ranking', 'archived'), ('reconstructed_replay', 'replayed')):
        subset = [g for g in games if g['basis'] == basis]
        cohort_rows = [r for g in subset for r in g['rows']]
        cohorts[name] = {**summary(cohort_rows), 'game_count': len(subset),
                         'selection_count': len(cohort_rows),
                         'weekly': {w: summary([r for g in subset if g['week'] == w for r in g['rows']])
                                    for w in sorted({g['week'] for g in subset})},
                         'diagnostics': diagnostics(cohort_rows)}
    return {'season': season, 'games': games, 'game_count': len(games), **cohorts,
            'selection_count': len(rows), 'readiness': 'RESEARCH_ONLY', **summary(rows),
            'weekly': {w: summary([r for g in games if g['week'] == w for r in g['rows']])
                       for w in sorted({g['week'] for g in games})},
            'diagnostics': diagnostics(rows),
            'historical_count': sum(len(g['rows']) for g in games if g['basis'] != 'locked')}


def diagnostics(rows: list[dict]) -> dict:
    """Describe the selected sides only. Never fit or recalibrate a model here."""
    decided = [r for r in rows if r.get('result') in {'won', 'lost'} and
               isinstance(r.get('confidence'), (int, float)) and 0 <= r['confidence'] <= 1]
    bands = []
    for low, high in ((.5, .6), (.6, .7), (.7, .8), (.8, .9), (.9, 1.01)):
        sample = [r for r in decided if low <= r['confidence'] < high]
        bands.append({'label': f'{int(low*100)}–{min(100, round(high*100))}%',
                      'count': len(sample),
                      'average_probability': sum(r['confidence'] for r in sample) / len(sample) if sample else None,
                      'win_rate': sum(r['result'] == 'won' for r in sample) / len(sample) if sample else None})
    return {'graded_count': len(decided), 'probability_bands': bands,
            'brier_score': sum((r['confidence'] - (r['result'] == 'won'))**2 for r in decided) / len(decided) if decided else None,
            'by_market': {m: summary([r for r in rows if r.get('market') == m])
                          for m in sorted({r['market'] for r in rows if r.get('market')})},
            'note': 'Descriptive results for this selection policy; correlated props and small samples do not establish calibration or profitability.'}


def _final_scores(season: int) -> dict:
    scores = {}
    for row in db.q('SELECT g.* FROM nfl_research_grades g JOIN nfl_research_freezes f '
                    'ON f.freeze_sha=g.freeze_sha WHERE f.season=? ORDER BY g.source_retrieved_at,g.revision',
                    (season,)):
        body = json.loads(row['payload'])
        if (body.get('freeze_sha256') != row['freeze_sha'] or body.get('game_id') != row['game_id'] or
                body.get('final_home_score') != row['home_score'] or
                body.get('final_away_score') != row['away_score']):
            raise ValueError('final game grade source mismatch')
        parts = row['game_id'].split('_')
        away, home = team_code(body.get('away_team')), team_code(body.get('home_team'))
        if (len(parts) != 4 or parts[0] != str(season) or not home or not away or
                team_code(parts[2]) != away or team_code(parts[3]) != home):
            raise ValueError('invalid final game identity')
        game = f'nfl-{season}-reg-{int(parts[1]):02d}-{away}-{home}'
        scores[game] = (row['home_score'], row['away_score'])
    return scores


def _result(value: float) -> str:
    return 'won' if value > 0 else 'lost' if value < 0 else 'push'


def games(season: int = 2026) -> dict:
    finals = _final_scores(season)
    saved = db.q('SELECT p.*,s.payload,e.start_time FROM nfl_game_predictions p '
                 'JOIN nfl_game_snapshots s ON s.id=p.snapshot_id JOIN events e ON e.id=p.game_id '
                 'WHERE p.game_id LIKE ? ORDER BY p.created_at DESC,p.id DESC', (f'nfl-{season}-reg-%',))
    seen, rows = set(), []
    for item in saved:
        game = item['game_id']
        snapshot = json.loads(item['payload'])
        identity = snapshot.get('game') or {}
        if identity.get('id') != game:
            raise ValueError('saved forecast game identity mismatch')
        kickoff = parse_time((snapshot.get('game') or {}).get('kickoff_utc') or item['start_time'])
        current_kickoff = parse_time(item['start_time'])
        if (game in seen or not kickoff or not current_kickoff or
                item['created_at'] >= min(kickoff, current_kickoff)):
            continue
        # Match the forecast used by the board: latest saved before kickoff.
        seen.add(game)
        p = snapshot.get('projection') or {}
        probability = p.get('home_win_prob', item['home_win_probability'])
        margin = p.get('margin', item['projected_margin'])
        total = p.get('total', item['projected_total'])
        home = identity.get('home_team')
        if (team_code(home) != game.split('-')[-1] or
                team_code(identity.get('away_team')) != game.split('-')[-2]):
            raise ValueError('saved forecast team identity mismatch')
        quotes = [q for q in snapshot.get('market', []) if not q.get('is_demo') and
                  q.get('event_id') == game and parse_time(q.get('captured_at')) is not None and
                  parse_time(q['captured_at']) <= item['created_at'] and
                  parse_time(q['captured_at']) < kickoff]
        spread = next((q['line'] for q in quotes if q.get('market') == 'spread' and
                       q.get('selection') == home and q.get('line') is not None), None)
        line = next((q['line'] for q in quotes if q.get('market') == 'total' and q.get('line') is not None), None)
        results = dict(winner='pending', spread='pending', total='pending')
        if game in finals:
            h, a = finals[game]
            results['winner'] = ('unavailable' if probability is None else 'push' if h == a else
                                 _result((h-a) * (1 if probability >= .5 else -1)))
            results['spread'] = ('unavailable' if spread is None or margin is None else
                                 'pass' if margin + spread == 0 else
                                 _result((h-a+spread) * (1 if margin+spread > 0 else -1)))
            results['total'] = ('unavailable' if line is None or total is None else
                                'pass' if total == line else
                                _result((h+a-line) * (1 if total > line else -1)))
        rows.append({'game_id': game, 'week': int(game.split('-')[3]), 'saved_at': item['created_at'],
                     'model_version': item['model_version'], 'results': results,
                     'winner_team': home if probability is not None and probability >= .5 else
                                    identity.get('away_team') if probability is not None else None,
                     'projected_home_points': p.get('proj_home', item['projected_home_points']),
                     'projected_away_points': p.get('proj_away', item['projected_away_points']),
                     'spread_line': spread, 'total_line': line,
                     'final_home_points': finals.get(game, (None, None))[0],
                     'final_away_points': finals.get(game, (None, None))[1]})
    rows.extend(_legacy_games(season, seen))
    def sums(items):
        return {market: summary([{'result': r['results'][market]} for r in items])
                for market in ('winner', 'spread', 'total')}
    return {'rows': rows, 'legacy_game_count': sum(r.get('basis') == 'legacy_market' for r in rows), **sums(rows), 'weekly': {w: sums([r for r in rows if r['week'] == w])
                                                for w in sorted({r['week'] for r in rows})}}


def view(season: int = 2026) -> dict:
    prop, game = props(season), games(season)
    current = db.kv_get('nfl_current_schedule') or {}
    max_week = max([current.get('week') or 0, *prop['weekly'], *game['weekly'], 1])
    weekly = [{'week': w, 'game_count': sum(r['week'] == w for r in game.get('rows', [])), 'props': prop['weekly'].get(w, summary([])),
               'locked_props': prop.get('locked', {}).get('weekly', {}).get(w, summary([])),
               'archived_props': prop.get('archived', {}).get('weekly', {}).get(w, summary([])),
               'replayed_props': prop.get('replayed', {}).get('weekly', {}).get(w, summary([])),
               'games': game['weekly'].get(w, {m: summary([]) for m in ('winner', 'spread', 'total')})}
              for w in range(1, max_week + 1)]
    return {'season': season, 'props': prop, 'games': game, 'weekly': weekly,
            'historical_note': 'The season total includes locked selections, rankings from saved pregame forecasts, '
                               'and labeled historical replays using archived lines and earlier training cutoffs. '
                               'Coverage is partial; weeks without supported saved lines have no prop record.'}


def capture_legacy_results(season=2026, weeks=(1, 2), provider=None):
    """Cache sourced scores for old market forecasts; never create predictions."""
    from .providers.espn import EspnEvents
    directory = ROOT / 'data/research/season_results'
    directory.mkdir(parents=True, exist_ok=True)
    for week in weeks:
        events = (provider or EspnEvents()).list_events_for_week(season, 2, week)
        body = dict(season=season, week=week, retrieved_at=time.time(),
                    source='ESPN scoreboard', events=events)
        raw = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = directory / f'{season}-week{week:02d}-{digest}.json'
        path.write_bytes(raw)


def _legacy_games(season, exclude):
    directory = ROOT / 'data/research/season_results'
    events = {}
    sources = []
    for path in directory.glob(f'{season}-week*-*.json'):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem.rsplit('-', 1)[-1]:
            raise ValueError('legacy results source hash mismatch')
        body = json.loads(raw)
        if body['season'] != season or body['source'] != 'ESPN scoreboard':
            raise ValueError('legacy results source mismatch')
        sources.append(body)
    for body in sorted(sources, key=lambda b: b['retrieved_at']):
        for event in body['events']:
            meta = event.get('meta') or {}
            canonical = canonical_id(event)
            if (not canonical or not canonical.startswith(f'nfl-{season}-reg-') or
                    int(canonical.split('-')[3]) != body['week'] or meta.get('is_demo') or
                    meta.get('source') != 'ESPN scoreboard'):
                raise ValueError('legacy results game identity mismatch')
            events[canonical] = event
    grouped = defaultdict(dict)
    saved = db.q("SELECT p.*,e.home_team,e.away_team,e.start_time,e.meta FROM predictions p "
                 "JOIN events e ON e.id=p.event_id WHERE p.sport='NFL' "
                 "AND p.event_id NOT LIKE 'demo-%' ORDER BY p.created_at DESC,p.id DESC")
    for row in saved:
        if row['market'] not in {'moneyline', 'spread', 'total'}:
            continue
        if (json.loads(row['meta'] or '{}').get('is_demo') or
                row['created_at'] >= (parse_time(row['start_time']) or 0)):
            continue
        matches = [gid for gid, e in events.items() if gid not in exclude and
                   e['home_team'] == row['home_team'] and e['away_team'] == row['away_team'] and
                   parse_time(e['start_time']) == parse_time(row['start_time'])]
        if len(matches) != 1:
            continue
        game = matches[0]
        context = json.loads(row['data_snapshot'] or '{}')
        quote_at = parse_time(context.get('selected_quote_at'))
        if (quote_at is None or quote_at > row['created_at'] or
                not isinstance(row['model_prob'], (int, float)) or
                not math.isfinite(row['model_prob']) or not 0 <= row['model_prob'] <= 1):
            continue
        grouped[game].setdefault(row['market'], row)
    rows = []
    for game, predictions in grouped.items():
        event, results = events[game], {}
        meta = event['meta']
        final = event['status'] == 'STATUS_FINAL'
        scores = (int(meta['home_score']), int(meta['away_score'])) if final else (None, None)
        winner, spread, total = predictions.get('moneyline'), predictions.get('spread'), predictions.get('total')
        winner_team = None
        for market, key in (('moneyline', 'winner'), ('spread', 'spread'), ('total', 'total')):
            p = predictions.get(market)
            if not p:
                results[key] = 'unavailable'
                continue
            if market in {'moneyline', 'spread'} and p['selection'] not in {event['home_team'], event['away_team']}:
                raise ValueError('legacy forecast team identity mismatch')
            direction = 1 if p['model_prob'] >= .5 else -1
            if market == 'moneyline':
                winner_team = (p['selection'] if direction == 1 else
                               event['away_team'] if p['selection'] == event['home_team'] else event['home_team'])
            if not final:
                results[key] = 'pending'
                continue
            h, a = scores
            if market == 'moneyline':
                value = (h-a) * (1 if p['selection'] == event['home_team'] else -1)
            elif p['line'] is None:
                results[key] = 'unavailable'
                continue
            elif market == 'spread':
                value = (h-a if p['selection'] == event['home_team'] else a-h) + p['line']
            else:
                if p['selection'] not in {'Over', 'Under'}:
                    raise ValueError('legacy forecast total side mismatch')
                value = (h+a-p['line']) * (1 if p['selection'] == 'Over' else -1)
            results[key] = 'pass' if p['model_prob'] == .5 and value != 0 else _result(value * direction)
        rows.append(dict(game_id=game, week=int(game.split('-')[3]), basis='legacy_market',
                         saved_at=max(p['created_at'] for p in predictions.values()),
                         model_version=next(iter(predictions.values()))['model_version'],
                         results=results, winner_team=winner_team,
                         projected_home_points=None, projected_away_points=None,
                         spread_line=(spread['line'] * (1 if spread['selection'] == event['home_team'] else -1))
                                     if spread and spread['line'] is not None else None,
                         total_line=total['line'] if total else None,
                         final_home_points=scores[0], final_away_points=scores[1]))
    return rows
