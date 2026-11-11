"""Read-only audit splitting NBA prop residuals into minutes and production-rate parts.

For a saved row with exact pinned replay rate r, predicted minutes Mhat, verified
actual minutes M, actual stat A and saved rounded projection S:

    A - S = r*(M - Mhat) + (A - r*M) + (r*Mhat - S)

The largest term is a descriptive label (dominant_component), not a cause. No
fitting, ranking, network access or schema creation happens here.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from . import db, nba_data, nba_learning_data, nba_model as baseline, nba_prop_model

TERMS = ('minutes_deviation', 'rate_residual', 'rounding_residual')
MARKETS = ('points', 'rebounds', 'assists', 'threes', 'pra')


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def decompose(rate, predicted_minutes, actual_minutes, actual_stat, projection):
    """Exact three-term split of A - S; unavailable rows carry an explicit reason."""
    if not (_finite(rate) and _finite(predicted_minutes) and predicted_minutes > 0):
        return dict(unavailable='rate_or_predicted_minutes_unavailable')
    if not (_finite(actual_minutes) and actual_minutes > 0):
        return dict(unavailable='actual_minutes_missing_or_nonpositive')
    if not (_finite(actual_stat) and _finite(projection)):
        return dict(unavailable='stat_or_projection_missing')
    terms = dict(minutes_deviation=rate * (actual_minutes - predicted_minutes),
                 rate_residual=actual_stat - rate * actual_minutes,
                 rounding_residual=rate * predicted_minutes - projection)
    return dict(terms, dominant_component=max(TERMS, key=lambda name: abs(terms[name])))


def decided_confidence(probability, push):
    """Side probability given no push; identical to the saved value on half lines."""
    push = push or 0.
    if not (_finite(probability) and _finite(push) and 0 <= push < 1):
        return None
    return probability / (1. - push)


def replay(history, player_id, market, tipoff, saved):
    """Replay the pinned opportunity projection; reject anything it cannot reproduce exactly."""
    if saved.get('model_version') != baseline.MODEL_VERSION or saved.get('parameter_fingerprint') != baseline.PARAMETER_FINGERPRINT:
        return dict(unavailable='unsupported_model_version')
    if saved.get('point_method') != 'opportunity':
        return dict(unavailable='unsupported_point_method:%s' % saved.get('point_method'))
    tip = baseline.parse_time(tipoff)
    rows, _ = baseline._player_rows(history, str(player_id), market, tip)
    if len(rows) < baseline.MIN_PROP_GAMES:
        return dict(unavailable='insufficient_replay_games')
    features, minutes = nba_prop_model.features(rows, tip, baseline.PROP_PARAMETERS['minutes_model'])
    rate = features['rate15']
    if (round(max(0., rate * minutes), 2) != saved.get('projection')
            or round(minutes, 1) != saved.get('projected_minutes')):
        return dict(unavailable='replay_mismatch')
    return dict(rate=rate, predicted_minutes=minutes)


def _marks(values):
    return ','.join('?' * len(values))


def collect(now, market, games):
    """Verified rows for one market from the existing reader, plus lock/grade identifiers."""
    records, context, _ = nba_learning_data.validation_records(now, games)
    ids = context['game_ids']
    items, dates, cache, rows = {}, {}, {}, []
    if ids:
        for item in db.q('SELECT * FROM nba_selection_lock_items WHERE game_id IN (%s)' % _marks(ids), ids):
            items[(item['game_id'], item['quote_id'])] = item
        for game in db.q('SELECT game_id,game_date_et FROM nba_games WHERE game_id IN (%s)' % _marks(ids), ids):
            dates[game['game_id']] = game['game_date_et']
    counts = dict(push=0, void=0)
    for excluded in context['exclusions']:
        item = items[(excluded['game_id'], excluded['quote_id'])]
        if item['prop'] == market:
            counts[excluded['result']] += 1
    for record in records:
        if record['market'] != market:
            continue
        item = items[(record['game_id'], record['quote_id'])]
        grade = db.q1('SELECT * FROM nba_grade_revisions WHERE lock_id=? AND rank=? '
                      'ORDER BY revision DESC LIMIT 1', (item['lock_id'], item['rank']))
        saved = record['saved']
        replayed = replay(record['history'], record['player_id'], market, record['kickoff_at'], saved)
        _, body = nba_learning_data._source(grade['source_artifact_id'], cache, grade['graded_at'])
        actual_minutes = (nba_data.player_line(nba_data.parse_summary(body), record['player_id']).get('stats') or {}).get('minutes')
        decomposed = decompose(replayed.get('rate'), replayed.get('predicted_minutes'),
                               actual_minutes, record['actual'], saved['projection'])
        reason = replayed.get('unavailable') or decomposed.get('unavailable')
        won = (record['actual'] > record['line']) == (record['side'] == 'over')
        row = dict(lock_id=item['lock_id'], forecast_id=item['forecast_id'], grade_revision_id=grade['id'],
                   game_id=record['game_id'], game_date_et=dates.get(record['game_id']),
                   player_id=record['player_id'], market=market, side=record['side'], line=record['line'],
                   quote_id=record['quote_id'], result='win' if won else 'loss',
                   saved_projection=saved['projection'], saved_confidence=saved.get('probability'),
                   decided_confidence=decided_confidence(saved.get('probability'), saved.get('probability_push')),
                   predicted_minutes=replayed.get('predicted_minutes'), actual_minutes=actual_minutes,
                   actual_stat=record['actual'], rate=replayed.get('rate'),
                   source_artifact_ids=sorted(s['id'] for s in record['sources']),
                   unavailable=reason, dominant_component=None,
                   **{term: None for term in TERMS})
        if reason is None:
            row.update({term: decomposed[term] for term in TERMS}, dominant_component=decomposed['dominant_component'])
        rows.append(row)
    return dict(rows=rows, push=counts['push'], void=counts['void'], game_ids=ids,
                dates=sorted(set(dates[g] for g in ids if dates.get(g))))


def build_report(now, market, games, collected):
    """Deterministic JSON report; rows are sorted so input order cannot change it."""
    rows = sorted(collected['rows'], key=lambda r: (r['game_date_et'] or '', r['game_id'], r['player_id'], r['quote_id']))
    decomposed = [r for r in rows if not r['unavailable']]

    def dominant(group):
        counts = dict((name, 0) for name in TERMS)
        for row in group:
            if row['dominant_component']:
                counts[row['dominant_component']] += 1
        return dict(n=len(group), unavailable=sum(1 for r in group if r['unavailable']),
                    dominant_component=counts)

    scored = [(r['decided_confidence'], r['result'] == 'win', r.get('saved_confidence')) for r in rows
              if r.get('decided_confidence') is not None]
    confidence = dict(n=len(scored), win_rate=mean(float(w) for _, w, _ in scored),
                      mean_saved_confidence=mean(s for _, _, s in scored),
                      mean_decided_confidence=mean(p for p, _, _ in scored),
                      brier_decided=mean((p - w) ** 2 for p, w, _ in scored)) if scored else None

    return dict(
        schema='nba-prop-miss-audit-v1', market=market, requested=dict(complete_games=games),
        as_of=datetime.fromtimestamp(now, timezone.utc).isoformat(), as_of_epoch=now,
        included=dict(game_ids=sorted(collected['game_ids']), dates=collected['dates']),
        counts=dict(decided=len(rows), wins=sum(r['result'] == 'win' for r in rows),
                    losses=sum(r['result'] == 'loss' for r in rows), push=collected['push'],
                    void=collected['void'], unavailable=len(rows) - len(decomposed),
                    decomposed=len(decomposed)),
        clusters=dict(games=len({r['game_id'] for r in rows}), dates=len({r['game_date_et'] for r in rows}),
                      players=len({r['player_id'] for r in rows}),
                      player_games=len({(r['game_id'], r['player_id']) for r in rows})),
        by_result=dict(win=dominant([r for r in rows if r['result'] == 'win']),
                       loss=dominant([r for r in rows if r['result'] == 'loss'])),
        confidence=confidence,
        mean_absolute_terms=dict((term, mean(abs(r[term]) for r in decomposed)) for term in TERMS)
        if decomposed else None,
        note=('Descriptive split only: dominant_component is the largest residual term, not a proven cause. '
              'Rows are not independent trials; use clusters for effective sample size.'),
        rows=rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only NBA prop miss decomposition.')
    parser.add_argument('--db', required=True)
    parser.add_argument('--market', default='rebounds', choices=MARKETS)
    parser.add_argument('--output', required=True)
    parser.add_argument('--games', type=int, default=40)
    args = parser.parse_args(argv)
    connection = sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    # Route the existing reader's queries through this read-only handle; db.conn() would otherwise run schema DDL.
    db._conn = connection
    try:
        now = time.time()
        report = build_report(now, args.market, args.games, collect(now, args.market, args.games))
    finally:
        db._conn = None
        connection.close()
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
