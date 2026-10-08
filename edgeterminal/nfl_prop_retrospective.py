"""Offline historical top-five replay; never writes a live selection or freeze.

The fixed 2024 family/sigma is replayed against archived main-line pairs. Earlier
weeks use a shorter training cutoff, so their outcomes cannot enter selection.
Artifacts disclose that these forecasts and rankings were calculated afterward.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime

import numpy as np

from . import db, nfl_identity, nfl_prop_lab as lab, nfl_prop_result_ingest, nfl_top_five
from .config import ROOT
from .integrity import parse_time

POLICY = 'retrospective-top-five-v1'
MODEL_SOURCE_SHA256 = 'b586bbc8a3cd0eeb49c2c9f6afb5554a43ce8df616ec0ad605cc1b06aae01627'
DIRECTORY = ROOT / 'data/research/prop_retrospective'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _games(season):
    output = {}
    for gid, r in lab._schedule().items():
        if int(r['season']) == season:
            game = f"nfl-{season}-reg-{int(r['week']):02d}-{lab._team(r['away_team'])}-{lab._team(r['home_team'])}"
            # nflverse schedule times are Eastern, including daylight saving.
            from zoneinfo import ZoneInfo
            kickoff = datetime.fromisoformat(r['gameday'] + 'T' + r['gametime']).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
            output[game] = dict(nflverse_id=gid, kickoff=kickoff, week=int(r['week']),
                                away=lab._team(r['away_team']), home=lab._team(r['home_team']))
    return output


def archived_quotes(schedule, exclude):
    """Read saved quotes only; exact game/team/time matches, main paired markets."""
    groups = defaultdict(list)
    for row in db.q("SELECT s.*,e.home_team,e.away_team,e.start_time,e.meta FROM odds_snapshots s "
                    "JOIN events e ON e.id=s.event_id WHERE s.sport='NFL' AND s.market='player_prop' AND s.is_demo=0"):
        if row['source'] != 'theoddsapi' or json.loads(row['meta'] or '{}').get('is_demo'):
            continue
        matches = [g for g, s in schedule.items() if g not in exclude and
                   s['home'] == nfl_identity.team_code(row['home_team']) and
                   s['away'] == nfl_identity.team_code(row['away_team']) and
                   s['kickoff'] == parse_time(row['start_time'])]
        match = re.fullmatch(r'(.+) (Over|Under) (passing_yards|rushing_yards|receiving_yards|receptions)', row['selection'])
        if len(matches) != 1 or not match:
            continue
        game = matches[0]
        # The legacy adapter requested these main markets, never alternate markets.
        groups[(game, f"legacy:{row['captured_at']}")].append(dict(
            id=row['id'], canonical_game_id=game, player_name=match[1], player_id=None, team=None,
            market=match[3], side=match[2].lower(), line=row['line'], price=row['price'],
            sportsbook=row['book'], retrieved_at=row['captured_at'], kickoff_utc=schedule[game]['kickoff'],
            provider_updated_at=row['provider_updated_at'], provider='theoddsapi', line_type='main',
            source_table='odds_snapshots', source_sha256=digest({k: row[k] for k in row if k not in {'home_team','away_team','start_time','meta'}})))
    for row in db.q("SELECT * FROM nfl_prop_snapshots WHERE canonical_game_id LIKE ? AND line_type='main'",
                    (f'nfl-{next(iter(schedule)).split("-")[1]}-reg-%',)):
        game = row['canonical_game_id']
        if game in schedule and game not in exclude and row['provider'] == 'sportsgameodds':
            groups[(game, row['batch_id'])].append(dict(row, source_table='nfl_prop_snapshots', source_sha256=digest(row)))
    return groups


def pairs(quotes, kickoff):
    grouped = defaultdict(lambda: defaultdict(list))
    for q in quotes:
        if (q['market'] not in lab.MARKETS or q['side'] not in {'over', 'under'} or
                not isinstance(q['line'], (int, float)) or not math.isfinite(q['line']) or q['line'] < 0 or
                not isinstance(q['price'], (int, float)) or not math.isfinite(q['price']) or abs(q['price']) < 100 or
                not 0 < q['retrieved_at'] < kickoff or q['kickoff_utc'] != kickoff or
                (q.get('provider_updated_at') is not None and
                 not 0 < q['provider_updated_at'] <= q['retrieved_at'])):
            continue
        key = (q['player_id'] or lab._norm_name(q['player_name']), q['market'], q['sportsbook'], q['line'])
        grouped[key][q['side']].append(q)
    return [q for g in grouped.values() if set(g) == {'over', 'under'} and all(len(v) == 1 for v in g.values())
            for side in ('over', 'under') for q in g[side]]


def model_context(season, week, validation, expected_hashes):
    cutoff = min((season, week), (2026, 3))
    datasets, history, hashes = lab.build_dataset(cutoff=cutoff)
    if hashes != expected_hashes:
        raise ValueError('Historical replay training files differ from the original frozen inputs')
    fitted, counts = {}, {}
    for market in lab.MARKETS:
        train = [r for r in datasets[market] if (r['season'], r['week']) < cutoff]
        counts[market] = len(train)
        family = validation[market]['selected_on_2024']
        if family != 'rolling':
            x, y = lab._matrix(train)
            fitted[market] = lab._estimator(family).fit(x, y)
    prepared = lab._week3_features(history, season, week)
    recent, _ = lab._read_stats()
    names = defaultdict(set)
    for r in recent:
        if season - 1 <= int(r['season']) and (int(r['season']), int(r['week'])) < (season, week):
            names[(lab._norm_name(r['player_display_name']), lab._team(r['team']))].add(r['player_id'])
    return dict(cutoff=cutoff, counts=counts, fitted=fitted, prepared=prepared, names=names, points={})


def project(quotes, context, validation, game, metadata):
    output = []
    for q in quotes:
        teams = [nfl_identity.team_code(q['team'])] if q.get('team') else [metadata['away'], metadata['home']]
        matches = {(pid, team) for team in teams for pid in context['names'].get((lab._norm_name(q['player_name']), team), set())}
        if len(matches) != 1:
            continue
        gsis, team = next(iter(matches))
        p = context['prepared'].get((gsis, team))
        market = q['market']
        if not p or p['game']['game_id'] != metadata['nflverse_id'] or not lab._eligible(market, p['player']):
            continue
        key = (gsis, team, market)
        feature = dict(p['feature'], opponent_allowed_last3=lab._avg(p['opp_hist'], lab.LABEL[market]))
        family = validation[market]['selected_on_2024']
        if key not in context['points']:
            point = (feature[f'{lab.LABEL[market]}_last3'] if family == 'rolling' else
                     float(context['fitted'][market].predict(np.array([[feature[k] for k in lab.FEATURES]]))[0]))
            context['points'][key] = max(0., point)
        point = context['points'][key]
        sigma = validation[market]['uncertainty_sigma_from_2024']
        over = lab._prob_over(q['line'], point, sigma)
        output.append(dict(quote_id=q['id'], source_table=q['source_table'], source_sha256=q['source_sha256'],
                           source_provider=q['provider'], quote_source=f"{q['provider']} archive / {q['sportsbook']}",
                           canonical_game_id=game, player_id=q['player_id'] or gsis, gsis_player_id=gsis,
                           player_name=q['player_name'], player=q['player_name'], market=market,
                           model_version=lab.VERSION+':'+family, projection=round(point,4), uncertainty_sigma=round(sigma,4),
                           sportsbook=q['sportsbook'], line=q['line'], side=q['side'], price=q['price'],
                           observed_at=q['retrieved_at'], quote_observed_at=q['retrieved_at'], kickoff_utc=q['kickoff_utc'],
                           probability_over=round(over,6), probability_under=round(1-over,6)))
    return output


def grade(row, stats):
    value = stats.get(row['gsis_player_id'], {}).get(lab.LABEL[row['market']])
    if value in (None, ''):
        return dict(result='pending', actual=None, grading_note='Participation or official stat unavailable')
    actual = float(value)
    if not math.isfinite(actual) or (row['market'] in {'receptions','rushing_attempts'} and actual < 0):
        raise ValueError('Invalid retrospective result stat')
    delta = (actual-row['line']) * (1 if row['side']=='over' else -1)
    return dict(result='won' if delta>0 else 'lost' if delta<0 else 'push', actual=actual)


def build(season=2026):
    from . import nfl_season_record
    lab_raw = (ROOT / 'data/research/nfl_prop_lab_2026_week3.json').read_bytes()
    original = json.loads(lab_raw)
    manifest = json.loads((ROOT / 'data/research/nfl_prop_lab_inputs_2026w3_manifest.json').read_text())
    schedule_sha = hashlib.sha256(lab.SCHEDULE.read_bytes()).hexdigest()
    if (hashlib.sha256(lab_raw).hexdigest() != MODEL_SOURCE_SHA256 or
            schedule_sha != manifest['inputs']['schedule/games.csv.gz']):
        raise ValueError('Historical replay original model/schedule source mismatch')
    existing = nfl_season_record.props(season, include_replay=False)
    excluded = {g['game_id'] for g in existing['games']}
    schedule = _games(season)
    finals = {r['game_id'] for r in nfl_season_record.games(season)['rows'] if r['final_home_points'] is not None}
    groups = archived_quotes(schedule, excluded)
    source = db.q1("SELECT a.sha256,a.source_file,a.payload FROM nfl_prop_grade_source_artifacts a "
                    "JOIN nfl_prop_result_ingestions i ON i.source_sha256=a.sha256 WHERE i.source_kind='player_stats' "
                    "ORDER BY i.retrieved_at DESC LIMIT 1")
    if not source or source['source_file'] != 'https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv':
        raise ValueError('No accepted official historical player-stat source')
    raw = bytes(source['payload'])
    if hashlib.sha256(raw).hexdigest() != source['sha256']:
        raise ValueError('Historical player-stat source hash mismatch')
    contexts, chosen, audit = {}, {}, []
    # Prefer the latest complete pregame snapshot, independent of any outcome.
    for (game,batch), quotes in sorted(groups.items(), key=lambda x: (-max(q['retrieved_at'] for q in x[1]), x[0])):
        if game in chosen or game not in finals:
            continue
        meta = schedule[game]
        if meta['week'] > 3:
            continue  # No replay past the original fixed training/feature history.
        complete = pairs(quotes,meta['kickoff'])
        if not complete:
            continue
        week = meta['week']
        if week not in contexts:
            contexts[week] = model_context(season,week,original['validation'],original['dataset_sha256'])
        projected = project(complete,contexts[week],original['validation'],game,meta)
        ranked = nfl_top_five.rank(projected)
        if len(ranked) != 5:
            audit.append(dict(game_id=game,batch=batch,supported_count=len(ranked),reason='Fewer than five distinct supported markets'))
            continue
        # Outcomes are read only after all five sides have been selected.
        stats = nfl_prop_result_ingest._stats(raw,meta['nflverse_id'],game)
        rows = [dict(q,rank=n,confidence=q['probability_'+q['side']],stronger_side=q['side'],selected_at=None,
                     **grade(q,stats)) for n,q in enumerate(ranked,1)]
        chosen[game] = dict(game_id=game,week=week,basis='reconstructed_replay',batch_id=batch,
                            saved_at=None,selected_at=None,training_cutoff=list(contexts[week]['cutoff']),rows=rows)
    stamp = time.time()
    result = dict(policy=POLICY,season=season,generated_at=stamp,readiness='RESEARCH_ONLY',designation='PASS',
                  games=[dict(g,generated_at=stamp) for _,g in sorted(chosen.items())],audit=audit,
                  model_source_sha256=hashlib.sha256(lab_raw).hexdigest(),schedule_sha256=schedule_sha,
                  input_sha256=original['dataset_sha256'],
                  result_source_sha256=source['sha256'],result_source_url=source['source_file'],
                  training_rows={w:c['counts'] for w,c in contexts.items()},
                  methodology='Latest complete archived pregame main-line pairs; fixed 2024 family and sigma; '
                              'training and features stop before the target week (maximum original Week 3 cutoff). '
                              'The top five are chosen by confidence before reading outcomes. These forecasts were reconstructed after the games. '
                              'Historical availability/injury inputs are not reconstructed; missing lines and unmatched players are excluded. '
                              'This is a partial retrospective sample, not the prospective live record.',
                  coverage=[dict(week=w,games=sum(g['week']==w for g in chosen.values()),
                                 archived_forecast_games=sum(g['week']==w for g in existing['games']),
                                 note='No saved prop lines' if w==4 else 'Only locally archived supported lines are included') for w in range(1,5)])
    DIRECTORY.mkdir(parents=True,exist_ok=True)
    path = DIRECTORY / f'{season}-{digest(result)}.json'
    path.write_text(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False))
    db.kv_set(f'nfl_prop_retrospective_{season}', {'artifact': path.name})
    return result,path


def load(season=2026):
    registered = db.kv_get(f'nfl_prop_retrospective_{season}')
    if not registered or 'artifact' not in registered:
        return {'games':[]}
    name = registered['artifact']
    if not re.fullmatch(rf'{season}-[0-9a-f]{{64}}\.json', name):
        raise ValueError('Retrospective artifact registration mismatch')
    path = DIRECTORY / name
    body = json.loads(path.read_text())
    if digest(body) != path.stem.split('-',1)[1] or body.get('policy') != POLICY or body.get('season') != season:
        raise ValueError('Retrospective report integrity mismatch')
    source = db.q1('SELECT payload,source_file FROM nfl_prop_grade_source_artifacts WHERE sha256=?',
                   (body['result_source_sha256'],))
    if (not source or hashlib.sha256(bytes(source['payload'])).hexdigest() != body['result_source_sha256'] or
            source['source_file'] != body['result_source_url']):
        raise ValueError('Retrospective result source mismatch')
    for game in body['games']:
        if (game['basis'] != 'reconstructed_replay' or len(game['rows']) != 5 or game['selected_at'] is not None or
                len({(q['player_id'], q['market']) for q in game['rows']}) != 5):
            raise ValueError('Retrospective provenance mismatch')
        for q in game['rows']:
            if q['source_table'] not in {'odds_snapshots','nfl_prop_snapshots'}:
                raise ValueError('Unknown historical quote source')
            saved = db.q1(f"SELECT * FROM {q['source_table']} WHERE id=?",(q['quote_id'],))
            if not saved or digest(saved) != q['source_sha256']:
                raise ValueError('Retrospective quote source mismatch')
    return body


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    report,path = build()
    counts=defaultdict(int)
    for g in report['games']:
        for r in g['rows']: counts[r['result']]+=1
    print(json.dumps(dict(path=str(path),games=len(report['games']),record=dict(counts),coverage=report['coverage'])))
