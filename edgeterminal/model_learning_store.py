"""Append-only governance for automatically evaluated research model updates.

This module never trains, fetches data, edits an existing forecast, or creates
an official pick. A single active artifact per sport is selected by an event
log. Only independently sourced, chronologically held-out main lines may earn
promotion. A failed proposal leaves the active model untouched.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
import time
from zoneinfo import ZoneInfo

from . import db, learning_publication as publication
from .config import ROOT

DIRECTORY = ROOT / 'data/research/model_learning'
MARKETS = {'NBA': ('points', 'rebounds', 'assists', 'threes', 'pra'),
           'NFL': ('passing_yards', 'rushing_yards', 'rushing_attempts', 'receptions', 'receiving_yards')}
NBA_MARKETS = {'points':'player_points', 'rebounds':'player_rebounds', 'assists':'player_assists',
               'threes':'player_threes', 'pra':'player_points_rebounds_assists'}
MIN_GAMES = 30
MIN_ROWS = 100
MIN_MARKET_ROWS = 20
MIN_MARKET_GAMES = 10
MIN_BRIER_IMPROVEMENT = .002
MIN_VALIDATION_DATES = 7
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def ensure_schema():
    db.ex('CREATE TABLE IF NOT EXISTS model_learning_candidates ('
          'candidate_id TEXT PRIMARY KEY,sport TEXT NOT NULL,version TEXT NOT NULL,'
          'training_cutoff REAL NOT NULL,created_at REAL NOT NULL,artifact_path TEXT NOT NULL,'
          'artifact_sha256 TEXT NOT NULL,fingerprint TEXT NOT NULL,evaluation TEXT NOT NULL,'
          'UNIQUE(sport,version))')
    db.ex('CREATE TABLE IF NOT EXISTS model_learning_events ('
          'id INTEGER PRIMARY KEY,sport TEXT NOT NULL,candidate_id TEXT,'
          'event TEXT NOT NULL CHECK(event IN ("activated","rejected","rolled_back")),'
          'previous_candidate_id TEXT,recorded_at REAL NOT NULL,reason TEXT NOT NULL,evidence TEXT NOT NULL)')
    db.ex('CREATE INDEX IF NOT EXISTS ix_model_learning_events ON model_learning_events(sport,id)')
    db.ex('CREATE TABLE IF NOT EXISTS model_learning_holdouts ('
          'sport TEXT NOT NULL,family TEXT NOT NULL,game_id TEXT NOT NULL,candidate_id TEXT NOT NULL,'
          'PRIMARY KEY(sport,family,game_id))')
    for table in ('model_learning_candidates','model_learning_events','model_learning_holdouts'):
        for action in ('UPDATE','DELETE'):
            db.ex(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                  "BEGIN SELECT RAISE(ABORT, 'model learning history is immutable'); END")


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('model learning values must be finite numbers')
    return float(value)


def _sport(value):
    if value not in MARKETS:raise ValueError('unsupported learning sport')
    return value


def _digest(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('invalid source digest')
    return value


def _code_path(name):
    value=Path(name)
    if value.is_absolute() or '..' in value.parts or not value.parts or value.parts[0]!='edgeterminal':
        raise ValueError('model code path is outside the package')
    path=(ROOT/value).resolve()
    if not path.is_relative_to((ROOT/'edgeterminal').resolve()) or path.suffix!='.py':
        raise ValueError('model code path is outside the package')
    return path


def _check_code(artifact,*,archive=False):
    if not isinstance(artifact,dict):raise ValueError('model artifact must be a JSON object')
    hashes=artifact.get('code_sha256')
    if not isinstance(hashes,dict) or not hashes:
        raise ValueError('model requires serving implementation hashes')
    for name,digest in hashes.items():
        path=_code_path(name)
        body=path.read_bytes()
        if hashlib.sha256(body).hexdigest()!=_digest(digest):
            raise ValueError('model serving implementation changed')
        if archive:_atomic_file(DIRECTORY/'code'/(digest+'.py'),body)
        else:
            archived=_owned(DIRECTORY/'code'/(digest+'.py'))
            if archived.is_symlink() or archived.read_bytes()!=body:
                raise ValueError('archived model serving implementation changed')


def _owned(path):
    base=DIRECTORY.resolve()
    if (DIRECTORY.is_symlink() or not base.is_relative_to(ROOT.resolve()) or
            not path.resolve().is_relative_to(base) or path.is_symlink()):
        raise ValueError('unsafe owned model learning path')
    return path


def _atomic_file(path,body):
    path=_owned(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes()!=body:
            raise ValueError('immutable model artifact changed')
        return
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.learning-',delete=False) as handle:
            temporary=Path(handle.name);handle.write(body);handle.flush();os.fsync(handle.fileno())
        try:os.link(str(temporary),str(path))
        except FileExistsError:
            if path.is_symlink() or path.read_bytes()!=body:raise ValueError('immutable model artifact collision')
        path.chmod(0o444)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def _source_bytes(source,*,archive=False):
    kind=source.get('kind');digest=_digest(source.get('sha256'))
    received=_number(source.get('received_at'))
    if kind in ('nba_source_artifact','nba_source_artifacts'):
        row=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(source.get('id'),))
        if not row or row['http_status']!=200 or row['sha256']!=digest or row['received_at']>received:
            raise ValueError('NBA learning source receipt or identity changed')
        body=row['body'].encode() if isinstance(row['body'],str) else bytes(row['body'])
    elif kind=='nfl_prop_grade_source_artifact':
        row=db.q1('SELECT * FROM nfl_prop_grade_source_artifacts WHERE sha256=?',(digest,))
        if not row or row['captured_at']>received:raise ValueError('NFL learning source receipt changed')
        body=bytes(row['payload'])
    elif kind=='nfl_prop_raw_response':
        row=db.q1('SELECT * FROM nfl_prop_raw_responses WHERE batch_id=? AND page_number=?',
                  (source.get('batch_id'),source.get('page_number')))
        if not row or row['http_status']!=200 or row['fetched_at']>received or row['body_sha256']!=digest:
            raise ValueError('NFL raw source receipt changed')
        body=bytes(row['body'])
    elif kind=='file':
        stored=_owned(DIRECTORY/'sources'/digest)
        if not archive:
            if stored.is_symlink():raise ValueError('unsafe archived source path')
            body=stored.read_bytes()
        else:
            relative=Path(source.get('path',''))
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('unsafe learning source path')
            path=(ROOT/relative).resolve()
            if not any(path.is_relative_to((ROOT/p).resolve()) for p in ('data/nflverse','data/research')):
                raise ValueError('learning source path outside owned data')
            if path.stat().st_mtime>received+1e-6:raise ValueError('file source was not available by its receipt')
            if path.stat().st_size>MAX_SOURCE_BYTES:raise ValueError('learning source is too large')
            body=path.read_bytes()
    else:raise ValueError('unsupported model learning source kind')
    if len(body)>MAX_SOURCE_BYTES or hashlib.sha256(body).hexdigest()!=digest:
        raise ValueError('learning source bytes changed')
    if archive and kind=='file':_atomic_file(DIRECTORY/'sources'/digest,body)
    return body


def _check_sources(sources,training_cutoff,calibration_cutoff,now,*,archive=False):
    if not isinstance(sources,list) or not sources:raise ValueError('model learning sources are required')
    if not any(s.get('role','training')=='training' for s in sources):
        raise ValueError('model requires a training source')
    for source in sources:
        role=source.get('role','training');receipt=_number(source.get('received_at'))
        limit={'training':training_cutoff,'calibration':calibration_cutoff,'validation':now}.get(role)
        if limit is None or receipt>min(now,limit):raise ValueError('learning source is after its allowed cutoff')
        _source_bytes(source,archive=archive)


def register_candidate(sport,version,training_cutoff,artifact,evaluation,sources,*,now=None):
    """Archive a proposal; registration never makes it active."""
    ensure_schema();sport=_sport(sport);stamp=time.time() if now is None else _number(now)
    cutoff=_number(training_cutoff)
    if cutoff>=stamp:raise ValueError('model training cutoff must precede registration')
    if not isinstance(version,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',version):
        raise ValueError('invalid learning model version')
    _check_code(artifact,archive=True)
    if 'training_cutoff' in artifact and _number(artifact['training_cutoff'])!=cutoff:
        raise ValueError('model artifact training cutoff differs from its registered cutoff')
    if 'version' in artifact and artifact['version']!=version:
        raise ValueError('model artifact version differs from its registered version')
    calibration=_number(evaluation.get('calibration_cutoff'))
    _check_sources(sources,cutoff,calibration,stamp,archive=True)
    envelope=dict(schema=1,sport=sport,version=version,training_cutoff=cutoff,
                  artifact=artifact,evaluation=evaluation,sources=sources)
    body=_json(envelope).encode()
    if len(body)>MAX_ARTIFACT_BYTES:raise ValueError('model artifact is too large')
    candidate_id=hashlib.sha256(body).hexdigest();relative=candidate_id+'.json'
    fingerprint=hashlib.sha256(_json({'artifact':artifact,'sources':sources}).encode()).hexdigest()
    _atomic_file(DIRECTORY/relative,body)
    with db.tx() as connection:
        connection.execute('BEGIN IMMEDIATE')
        old=connection.execute('SELECT candidate_id FROM model_learning_candidates WHERE sport=? AND version=?',
                               (sport,version)).fetchone()
        if old and old['candidate_id']!=candidate_id:raise ValueError('model version already identifies another artifact')
        family=artifact.get('evaluation_family')
        if family:
            if not isinstance(family,str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',family):
                raise ValueError('invalid evaluation family')
            games={str(row['game_id']) for row in evaluation['rows']}
            games.update(str(g['game_id']) for g in (evaluation.get('selection_policy') or {}).get('games',[]))
            previous_candidates=connection.execute('SELECT evaluation FROM model_learning_candidates '
                'WHERE sport=? AND candidate_id<>?',(sport,candidate_id)).fetchall()
            if games.intersection(_evaluated_games(previous_candidates)):
                raise ValueError('outer holdout games already consumed by an earlier model evaluation')
            for game in sorted(games):
                used=connection.execute('SELECT candidate_id FROM model_learning_holdouts '
                    'WHERE sport=? AND game_id=?',(sport,game)).fetchone()
                if used and used['candidate_id']!=candidate_id:
                    raise ValueError('outer holdout games already consumed by this evaluation family')
                connection.execute('INSERT OR IGNORE INTO model_learning_holdouts VALUES(?,?,?,?)',
                                   (sport,family,game,candidate_id))
        connection.execute('INSERT OR IGNORE INTO model_learning_candidates VALUES(?,?,?,?,?,?,?,?,?)',
            (candidate_id,sport,version,cutoff,stamp,relative,candidate_id,fingerprint,_json(evaluation)))
        from . import learning_worker
        checkpoint=getattr(learning_worker,'checkpoint_candidate',None)
        if checkpoint:checkpoint(connection,candidate_id)
    return _candidate(candidate_id)


def holdout_available(sport,family,game_ids):
    """Read-only check; rejected proposals also consume their outer games."""
    sport=_sport(sport)
    used=set()
    if db.q1("SELECT 1 FROM sqlite_master WHERE name='model_learning_holdouts'"):
        used.update(row['game_id'] for row in db.q('SELECT game_id FROM model_learning_holdouts WHERE sport=?',(sport,)))
    if db.q1("SELECT 1 FROM sqlite_master WHERE name='model_learning_candidates'"):
        used.update(_evaluated_games(db.q('SELECT evaluation FROM model_learning_candidates WHERE sport=?',(sport,))))
    return not used.intersection(str(game) for game in game_ids)


def _evaluated_games(candidates):
    """Older registered evaluations remain spent even before the ledger existed."""
    used=set()
    for candidate in candidates:
        evaluation=json.loads(candidate['evaluation'])
        used.update(str(row['game_id']) for row in evaluation.get('rows',[]))
        used.update(str(row['game_id']) for row in (evaluation.get('selection_policy') or {}).get('games',[]))
    return used


def _candidate(candidate_id):
    row=db.q1('SELECT * FROM model_learning_candidates WHERE candidate_id=?',(candidate_id,))
    if row:row['evaluation']=json.loads(row['evaluation'])
    return row


def _load(candidate):
    _digest(candidate['candidate_id']);_digest(candidate['artifact_sha256'])
    relative=candidate['artifact_path']
    if relative!=candidate['candidate_id']+'.json':raise ValueError('unsafe model artifact path')
    path=_owned(DIRECTORY/relative)
    if path.is_symlink() or path.stat().st_size>MAX_ARTIFACT_BYTES:raise ValueError('invalid model artifact path or size')
    body=path.read_bytes()
    if hashlib.sha256(body).hexdigest()!=candidate['artifact_sha256']:
        raise ValueError('immutable model artifact hash changed')
    payload=json.loads(body)
    if (payload.get('schema')!=1 or payload.get('sport')!=candidate['sport'] or
            payload.get('version')!=candidate['version'] or
            payload.get('training_cutoff')!=candidate['training_cutoff'] or
            payload.get('evaluation')!=candidate['evaluation']):
        raise ValueError('model artifact metadata changed')
    fingerprint=hashlib.sha256(_json({'artifact':payload['artifact'],'sources':payload['sources']}).encode()).hexdigest()
    if fingerprint!=candidate['fingerprint']:raise ValueError('model artifact fingerprint changed')
    _check_code(payload['artifact'])
    return payload


def _metrics(rows):
    metrics={}
    for prefix in ('baseline','candidate'):
        errors=[];brier=[];loss=[];bins=defaultdict(list)
        for row in rows:
            actual=row['actual'];won=float(actual>row['line']) if row['side']=='over' else float(actual<row['line'])
            probability=row[prefix+'_probability'];clipped=max(1e-9,min(1.-1e-9,probability))
            errors.append(abs(row[prefix+'_prediction']-actual));brier.append((probability-won)**2)
            loss.append(-won*math.log(clipped)-(1.-won)*math.log1p(-clipped))
            bins[min(9,int(probability*10))].append((probability,won))
        calibration=sum(abs(sum(p-y for p,y in values)) for values in bins.values())/len(rows)
        metrics[prefix]={'mae':statistics.mean(errors),'brier':statistics.mean(brier),
                         'log_loss':statistics.mean(loss),'calibration_error':calibration}
        if not all(math.isfinite(value) for value in metrics[prefix].values()):
            raise ValueError('nonfinite derived validation metric')
    return metrics


def _policy_gate(evaluation,artifact,sport,all_games,start,end):
    """Recompute both top-five policies over one immutable common cohort."""
    if not artifact or not artifact.get('ranking_changed'):return {},[]
    policy=evaluation.get('selection_policy') or {}
    if policy.get('kind')!='common_full_offer_universe' or not isinstance(policy.get('games'),list):
        return {},['full_policy_evidence_required']
    groups=policy['games'];seen=set();collections={'baseline':[],'candidate':[]};differences=[]
    paired={row['quote_id']:row for row in evaluation['rows']}
    retained=set(MARKETS[sport])-set(artifact.get('changed_markets',MARKETS[sport]))
    for group in groups:
        gid=str(group['game_id']);universe=group['universe_quote_ids']
        if gid in seen or not universe or len(universe)!=len(set(universe)):
            raise ValueError('invalid common policy universe')
        seen.add(gid)
        if not group.get('context_id' if sport=='NBA' else 'freeze_sha'):
            raise ValueError('policy universe source required')
        game_metrics={}
        for name in collections:
            selected=group[name]
            if len(selected)!=5:raise ValueError('policy must select five supported player markets')
            identities=set();quotes=set();decided=[]
            for raw in selected:
                row=dict(raw)
                for key in ('actual','line','baseline_prediction','candidate_prediction','baseline_probability',
                            'candidate_probability','observed_at','kickoff_at','result_observed_at'):
                    row[key]=_number(row[key])
                identity=(str(row['player_id']),row['market'])
                if (str(row['game_id'])!=gid or row['quote_id'] not in universe or identity in identities or
                        row['quote_id'] in quotes or row['market'] not in MARKETS[sport] or
                        row['side'] not in ('over','under')):
                    raise ValueError('invalid selected policy identity')
                if not row['observed_at']<row['kickoff_at']<row['result_observed_at']<=end:
                    raise ValueError('invalid selected policy source times')
                if not start<=row['kickoff_at']<end:raise ValueError('policy game outside outer window')
                if not 0<=row[name+'_probability']<=1:raise ValueError('invalid selected policy probability')
                fields=('prediction','probability')
                if row['market'] in retained and any(row['candidate_'+k]!=row['baseline_'+k] for k in fields):
                    raise ValueError('retained selected-policy market changed')
                reference=paired.get(row['quote_id'])
                if reference and any(row[p+'_'+k]!=reference[p+'_'+k] for p in collections for k in fields):
                    raise ValueError('selected-policy forecasts differ from paired quote')
                identities.add(identity);quotes.add(row['quote_id'])
                if row['actual']!=row['line']:decided.append(row)
            collections[name].extend(decided)
            if not decided:raise ValueError('policy game has no decided observations')
            game_metrics[name]=_metrics(decided)[name]
        differences.append(game_metrics['candidate']['brier']-game_metrics['baseline']['brier'])
    if seen!=all_games:raise ValueError('policy cohort differs from paired validation cohort')
    reasons=[];metrics={}
    for name,rows in collections.items():
        metrics[name]=dict(rows=len(rows),wins=sum((r['actual']>r['line'])==(r['side']=='over') for r in rows),
                           **(_metrics(rows)[name] if rows else {}))
        if len(rows)<MIN_ROWS or len(seen)<MIN_GAMES:reasons.append('insufficient_full_policy_coverage')
    if metrics['candidate']['wins']<metrics['baseline']['wins']:reasons.append('selected_policy_win_regression')
    for metric in ('brier','log_loss'):
        if metrics['candidate'].get(metric,math.inf)>metrics['baseline'].get(metric,-math.inf)+1e-12:
            reasons.append('selected_policy_regression:'+metric)
    upper=(statistics.mean(differences)+1.96*statistics.stdev(differences)/math.sqrt(len(differences))
           if len(differences)>1 else None)
    if upper is None or upper>=0:reasons.append('uncertain_selected_policy_improvement')
    return dict(games=len(seen),**metrics,game_brier_upper95=upper),reasons


def _date_bound(groups):
    """Give each Eastern slate one vote after averaging its game effects."""
    dates=defaultdict(list)
    for group in groups.values():
        date=datetime.fromtimestamp(group[0]['kickoff_at'],ZoneInfo('America/New_York')).date().isoformat()
        metrics=_metrics(group)
        dates[date].append(metrics['candidate']['brier']-metrics['baseline']['brier'])
    paired=[statistics.mean(values) for values in dates.values()]
    upper=(statistics.mean(paired)+1.96*statistics.stdev(paired)/math.sqrt(len(paired)) if len(paired)>1 else None)
    return len(dates),upper


def gate(evaluation,training_cutoff,artifact=None,*,sport=None,now=None):
    """Recompute a conservative acceptance decision from paired quote rows."""
    reasons=[];stamp=time.time() if now is None else _number(now)
    try:
        cutoff=_number(training_cutoff);start=_number(evaluation['validation_start'])
        end=_number(evaluation['validation_end']);calibration=_number(evaluation['calibration_cutoff'])
        if not cutoff<=calibration<=start<end<=stamp:reasons.append('invalid_chronological_windows')
        if evaluation.get('kind')!='actual_main_lines':reasons.append('actual_main_lines_required')
        rows=evaluation.get('rows')
        if not isinstance(rows,list):raise ValueError('validation rows required')
        if sport is None:
            names={r.get('market') for r in rows}
            sport='NBA' if names.issubset(set(MARKETS['NBA'])) else 'NFL'
        sport=_sport(sport);valid=[];identities=set();quotes=set();all_games=set();groups=defaultdict(list)
        selective=artifact is not None and 'changed_markets' in artifact
        changed=set(artifact['changed_markets']) if selective else set(MARKETS[sport])
        if selective and (not changed or not changed.issubset(MARKETS[sport]) or
                          len(changed)!=len(artifact['changed_markets'])):
            raise ValueError('invalid changed-market catalog')
        for raw in rows:
            row=dict(raw)
            for key in ('actual','line','baseline_prediction','candidate_prediction','baseline_probability',
                        'candidate_probability','observed_at','kickoff_at','result_observed_at'):
                row[key]=_number(row[key])
            if (row.get('market') not in MARKETS[sport] or row.get('side') not in ('over','under') or
                    not row.get('game_id') or not row.get('player_id') or not row.get('quote_id')):
                raise ValueError('invalid quote identity')
            identity=(row['game_id'],str(row['player_id']),row['market'])
            if identity in identities or row['quote_id'] in quotes:raise ValueError('duplicate held-out player market')
            identities.add(identity);quotes.add(row['quote_id']);all_games.add(str(row['game_id']))
            if not row['observed_at']<row['kickoff_at']<row['result_observed_at']<=end:
                raise ValueError('invalid validation source times')
            if not start<=row['kickoff_at']<end:raise ValueError('validation game outside window')
            retained=selective and row['market'] not in changed
            if retained and any(row['candidate_'+field]!=row['baseline_'+field] for field in ('prediction','probability')):
                reasons.append('retained_market_changed:'+row['market'])
            if not (0<=row['baseline_probability']<=1 and
                    (0<=row['candidate_probability']<=1 if retained else 0<row['candidate_probability']<1)):
                raise ValueError('invalid predictive probability')
            if row['actual']==row['line']:continue
            valid.append(row);groups[row['game_id']].append(row)
        if len(valid)<MIN_ROWS:reasons.append('insufficient_validation_rows')
        if len(groups)<MIN_GAMES:reasons.append('insufficient_validation_games')
        if artifact is not None:
            training=artifact.get('training') or {}
            fitted={str(g) for g in training.get('fit_game_ids') or []}
            calibrated={str(g) for g in training.get('calibration_game_ids') or []}
            development={str(g) for g in training.get('development_game_ids') or []}
            if not fitted or not calibrated:reasons.append('training_game_catalog_required')
            if (fitted|development)&calibrated or (fitted|development|calibrated)&all_games:
                reasons.append('training_validation_game_overlap')
            if not _number(training.get('fit_label_max_at'))<cutoff:reasons.append('training_label_after_cutoff')
            if development:
                if not _number(training['development_label_max_at'])<cutoff:
                    reasons.append('development_label_after_cutoff')
            if not _number(training.get('calibration_label_max_at'))<calibration:reasons.append('calibration_label_after_cutoff')
            if selective:
                choice=artifact.get('market_selection') or {}
                selected={str(g) for g in choice.get('selection_game_ids') or []}
                if set(choice.get('changed_markets') or [])!=changed or not selected:
                    reasons.append('inner_market_selection_required')
                if selected & (fitted|development|calibrated|all_games):
                    reasons.append('market_selection_game_overlap')
                if not (_number(choice.get('selection_label_max_at'))<
                        _number(choice.get('selection_cutoff'))<=calibration):
                    reasons.append('market_selection_after_cutoff')
        by_market={}
        for market in MARKETS[sport]:
            subset=[r for r in valid if r['market']==market]
            if market in changed and len(subset)<MIN_MARKET_ROWS:reasons.append('insufficient_market_rows:'+market)
            if market in changed and len({r['game_id'] for r in subset})<MIN_MARKET_GAMES:
                reasons.append('insufficient_market_games:'+market)
            if not subset:continue
            metrics=_metrics(subset);by_market[market]=dict(n=len(subset),games=len({r['game_id'] for r in subset}),**metrics)
            for metric in ('mae','brier','log_loss'):
                if metrics['candidate'][metric]>metrics['baseline'][metric]+1e-12:
                    reasons.append('market_regression:'+market+':'+metric)
        aggregate=_metrics(valid) if valid else {}
        upper=None
        if aggregate:
            difference=aggregate['candidate']['brier']-aggregate['baseline']['brier']
            if difference>-MIN_BRIER_IMPROVEMENT:reasons.append('insufficient_probability_improvement')
            paired=[_metrics(group)['candidate']['brier']-_metrics(group)['baseline']['brier']
                    for group in groups.values()]
            if len(paired)>1:
                upper=statistics.mean(paired)+1.96*statistics.stdev(paired)/math.sqrt(len(paired))
                if upper>=0:reasons.append('uncertain_game_group_improvement')
            else:reasons.append('uncertain_game_group_improvement')
        dates,date_upper=_date_bound(groups)
        if artifact and artifact.get('evaluation_family'):
            if dates<MIN_VALIDATION_DATES:reasons.append('insufficient_validation_dates')
            if date_upper is None or date_upper>=0:reasons.append('uncertain_date_group_improvement')
        policy,policy_reasons=_policy_gate(evaluation,artifact,sport,all_games,start,end)
        reasons.extend(policy_reasons)
        return {'passed':not reasons,'reasons':list(dict.fromkeys(reasons)),'rows':len(valid),
                'games':len(groups),'markets':by_market,'aggregate':aggregate,'game_brier_upper95':upper,
                'selection_policy':policy,'dates':dates,'date_brier_upper95':date_upper}
    except (ValueError,TypeError,KeyError,OverflowError) as exc:
        return {'passed':False,'reasons':list(dict.fromkeys(reasons+['invalid_evidence:'+str(exc)]))}


def _verify_row(sport,row):
    """Compare evaluation inputs with immutable pregame quotes and latest grades."""
    if sport=='NBA':
        if row.get('policy_context_id'):
            from .nba_policy_evidence import verify_policy_row
            verify_policy_row(row)
            return
        quote=db.q1('SELECT q.*,g.tipoff_ts,g.status game_status FROM nba_quote_observations q JOIN nba_games g '
                     'ON g.game_id=q.game_id WHERE q.quote_id=?',(row['quote_id'],))
        grade=db.q1('SELECT r.* FROM nba_grade_revisions r JOIN nba_selection_lock_items i '
                     'ON r.lock_id=i.lock_id AND r.rank=i.rank WHERE i.quote_id=? ORDER BY r.revision DESC LIMIT 1',
                     (row['quote_id'],))
        if not quote or not grade:raise ValueError('validation quote or grade missing')
        expected={'game_id':quote['game_id'],'player_id':quote['espn_player_id'],
                  'line':quote['line'],'side':quote['side'],'observed_at':quote['observed_at'],
                  'kickoff_at':quote['tipoff_ts'],'actual':grade['actual_value'],
                  'result_observed_at':grade['graded_at']}
        if db.q1("SELECT 1 FROM sqlite_master WHERE name='nba_forecasts'"):
            forecast=db.q1('SELECT f.* FROM nba_forecasts f JOIN nba_selection_lock_items i '
                          'ON i.forecast_id=f.forecast_id WHERE i.quote_id=? LIMIT 1',(row['quote_id'],))
            if not forecast:raise ValueError('validation frozen forecast is missing')
            from .nba_learning_data import capture_game
            current=db.q1('SELECT * FROM nba_games WHERE game_id=?',(quote['game_id'],))
            expected['kickoff_at']=capture_game(forecast,current)['tipoff_ts']
        if quote['market']!=NBA_MARKETS[row['market']]:raise ValueError('validation quote market changed')
        if grade['result'] not in ('win','loss','push'):raise ValueError('validation grade is unresolved or void')
        if quote['game_status']!='final':raise ValueError('validation game is not final')
        actual=_number(grade['actual_value']);line=_number(quote['line'])
        result='push' if actual==line else ('win' if (actual>line)==(quote['side']=='over') else 'loss')
        if grade['result']!=result:raise ValueError('validation grade disagrees with actual')
        for identity in {quote['source_artifact_id'],grade['source_artifact_id']}:
            source=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(identity,))
            limit=quote['observed_at'] if identity==quote['source_artifact_id'] else grade['graded_at']
            if not source or source['http_status']!=200 or source['received_at']>limit:
                raise ValueError('validation source unavailable by observation')
            _source_bytes({'kind':'nba_source_artifact','id':identity,'sha256':source['sha256'],
                           'received_at':source['received_at']})
    else:
        quote=db.q1('SELECT * FROM nfl_prop_snapshots WHERE id=?',(row['quote_id'],))
        grade=db.q1('SELECT r.*,e.payload evidence,e.result_retrieved_at,e.game_status '
                     'FROM nfl_prop_grade_revisions r JOIN nfl_prop_grade_evidence e ON e.evidence_sha=r.evidence_sha '
                     'WHERE r.quote_id=? ORDER BY r.graded_at DESC,r.revision DESC LIMIT 1',(row['quote_id'],))
        if not quote or not grade:raise ValueError('validation quote or grade missing')
        payload=json.loads(grade['payload'])
        freeze=db.q1('SELECT payload FROM nfl_prop_research_freezes WHERE freeze_sha=?',(grade['freeze_sha'],))
        if not freeze or hashlib.sha256(freeze['payload'].encode()).hexdigest()!=grade['freeze_sha']:
            raise ValueError('validation immutable freeze changed')
        frozen=[q for q in json.loads(freeze['payload']).get('projections',[]) if q.get('quote_id')==row['quote_id']]
        if len(frozen)!=1:raise ValueError('validation quote absent from immutable freeze')
        for key in ('canonical_game_id','player_id','market','line','side'):
            if frozen[0].get(key)!=quote[key]:raise ValueError('validation freeze quote changed')
        expected={'game_id':quote['canonical_game_id'],'player_id':quote['player_id'],'market':quote['market'],
                  'line':quote['line'],'side':quote['side'],'observed_at':quote['retrieved_at'],
                  'kickoff_at':quote['kickoff_utc'],'actual':payload.get('actual_stat'),
                  'result_observed_at':grade['graded_at']}
        if quote['line_type']!='main' or payload.get('research_statistical_result') not in ('won','lost','push'):
            raise ValueError('validation grade or main line unavailable')
        if grade['game_status']!='final':raise ValueError('validation game is not final')
        evidence=json.loads(grade['evidence'])
        digest=hashlib.sha256((grade['freeze_sha']+_json(evidence)).encode()).hexdigest()
        if digest!=grade['evidence_sha']:raise ValueError('validation grade evidence changed')
        if evidence.get('game_id')!=quote['canonical_game_id'] or evidence.get('game_status')!='final':
            raise ValueError('validation grade game identity changed')
        actual=_number(payload['actual_stat']);line=_number(quote['line'])
        result='push' if actual==line else ('won' if (actual>line)==(quote['side']=='over') else 'lost')
        if payload['research_statistical_result']!=result:raise ValueError('validation grade disagrees with actual')
        players=[p for p in evidence.get('players',[]) if p.get('gsis_player_id')==payload.get('gsis_player_id')]
        if (len(players)!=1 or players[0].get('status')!='active' or
                players[0].get('stats',{}).get(quote['market'])!=actual):
            raise ValueError('validation actual differs from final player evidence')
        for name in ('final_game_source','player_stat_source'):
            source=evidence.get(name) or {};source_sha=_digest(source.get('sha256'))
            stored=db.q1('SELECT * FROM nfl_prop_grade_source_artifacts WHERE sha256=?',(source_sha,))
            if not stored or stored['captured_at']>grade['graded_at']:
                raise ValueError('validation grade source unavailable')
            _source_bytes({'kind':'nfl_prop_grade_source_artifact','sha256':source_sha,
                           'received_at':stored['captured_at']})
        batch=db.q1('SELECT * FROM nfl_prop_capture_batches WHERE batch_id=?',(quote['batch_id'],))
        if not batch or hashlib.sha256(batch['raw_payload'].encode()).hexdigest()!=batch['raw_sha256']:
            raise ValueError('validation quote source changed')
        if batch['retrieved_at']>quote['retrieved_at']:raise ValueError('validation quote source is from the future')
    for key,value in expected.items():
        if key=='player_id':
            if str(row.get(key))!=str(value):raise ValueError('validation player identity changed')
        elif row.get(key)!=value:raise ValueError('validation quote or grade changed: '+key)


def active(sport):
    sport=_sport(sport)
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_learning_events'"):return None
    event=db.q1('SELECT * FROM model_learning_events WHERE sport=? AND event IN ("activated","rolled_back") '
                 'ORDER BY id DESC LIMIT 1',(sport,))
    if not event or event['candidate_id'] is None:return None
    candidate=_candidate(event['candidate_id'])
    if candidate is None:raise ValueError('active model candidate is missing')
    return dict(candidate,activated_at=event['recorded_at'],activation_event_id=event['id'])


def load_active(sport):
    candidate=active(sport)
    if candidate is None:return None
    payload=_load(candidate)
    return dict(candidate,artifact=payload['artifact'])


def verify_active(sport,*,now=None):
    """Full nightly integrity check; serving uses the smaller load_active check."""
    candidate=active(sport)
    if candidate is None:return None
    stamp=time.time() if now is None else _number(now)
    payload=_load(candidate)
    before=_publication_state(candidate,payload,require_current=True)
    _verify_candidate(candidate,stamp)
    publication.same(before,_publication_state(candidate,payload))
    return candidate


def _verify_candidate(candidate,stamp):
    payload=_load(candidate)
    publication.evidence_state(candidate['sport'],payload['evaluation'],payload['sources'],require_current=True)
    _check_sources(payload['sources'],candidate['training_cutoff'],payload['evaluation']['calibration_cutoff'],stamp)
    _verify_evaluation(candidate['sport'],payload['evaluation'],payload['artifact'])


def _verify_evaluation(sport,evaluation,artifact=None):
    if sport=='NBA':
        from . import nba_policy_evidence
        scope=getattr(nba_policy_evidence,'validation_scope',nullcontext)
    else:scope=nullcontext
    with scope():
        _verify_evaluation_rows(sport,evaluation,artifact)


def _verify_evaluation_rows(sport,evaluation,artifact):
    for row in evaluation['rows']:_verify_row(sport,row)
    policy=evaluation.get('selection_policy')
    if not policy:return
    if sport=='NBA':
        from .nba_policy_evidence import verify_policy_universe
    else:
        from .nfl_learning import verify_policy_universe
    for game in policy['games']:
        if sport=='NFL' and game.get('baseline_candidate_id')!=evaluation.get('baseline_candidate_id'):
            raise ValueError('selected policy baseline differs from current comparison')
        verify_policy_universe(game,artifact=artifact)
        for name in ('baseline','candidate'):
            for row in game[name]:_verify_row(sport,row)


def _publication_state(candidate,payload,*,require_current=False):
    paths=[_owned(DIRECTORY/candidate['artifact_path'])]
    for name,digest in payload['artifact']['code_sha256'].items():
        paths.extend((_code_path(name),_owned(DIRECTORY/'code'/(digest+'.py'))))
    paths.extend(_owned(DIRECTORY/'sources'/s['sha256']) for s in payload['sources'] if s.get('kind')=='file')
    games={str(r['game_id']) for r in payload['evaluation'].get('rows',[])}
    games.update(str(g['game_id']) for g in (payload['evaluation'].get('selection_policy') or {}).get('games',[]))
    holdouts=[db.q1('SELECT family,candidate_id FROM model_learning_holdouts WHERE sport=? AND game_id=?',
                   (candidate['sport'],game)) for game in sorted(games)]
    return dict(candidate=_candidate(candidate['candidate_id']),files=publication.file_seals(paths),holdouts=holdouts,
        evidence=publication.evidence_state(candidate['sport'],payload['evaluation'],payload['sources'],require_current=require_current))


def _previous_decision(candidate_id,sport):
    old=db.q1('SELECT event,evidence FROM model_learning_events WHERE candidate_id=? '
              'AND event IN ("activated","rejected") ORDER BY id DESC LIMIT 1',(candidate_id,))
    previous=db.q1('SELECT id,candidate_id,recorded_at FROM model_learning_events WHERE sport=? '
        'AND event IN ("activated","rolled_back") ORDER BY id DESC LIMIT 1',(sport,))
    if old:
        status='retained' if old['event']=='activated' and (previous or {}).get('candidate_id')!=candidate_id else old['event']
        return {'status':status,'candidate_id':candidate_id,**json.loads(old['evidence'])},previous
    return None,previous


def activate_if_passes(candidate_id,*,now=None):
    """Replay evidence without writer locks, then briefly publish unchanged proof."""
    ensure_schema();stamp=time.time() if now is None else _number(now)
    if db.conn().in_transaction:raise ValueError('learning publication requires no caller write transaction')
    candidate=_candidate(candidate_id)
    if not candidate:raise ValueError('unknown learning candidate')
    payload=_load(candidate)
    old,_=_previous_decision(candidate_id,candidate['sport'])
    if old:return old
    result=gate(payload['evaluation'],candidate['training_cutoff'],payload['artifact'],sport=candidate['sport'],now=stamp)
    invalid=None
    try:before=_publication_state(candidate,payload,require_current=True)
    except (ValueError,KeyError,TypeError,OSError) as exc:
        invalid=exc;before=_publication_state(candidate,payload)
    try:
        if invalid:raise invalid
        _check_sources(payload['sources'],candidate['training_cutoff'],payload['evaluation']['calibration_cutoff'],stamp)
        _verify_evaluation(candidate['sport'],payload['evaluation'],payload['artifact'])
    except (ValueError,KeyError,TypeError,OSError) as exc:
        result['passed']=False;result['reasons'].append('source_validation:'+str(exc))
    try:
        with publication.publish_lock():
            with db.tx() as connection:
                connection.execute('BEGIN IMMEDIATE')
                old,previous=_previous_decision(candidate_id,candidate['sport'])
                if old:return old
                publication.same(before,_publication_state(candidate,payload))
                try:_load(candidate)  # Small artifact/implementation hashes, never source replay.
                except (ValueError,OSError,KeyError,TypeError) as exc:
                    raise publication.PublicationDeferred('candidate_or_code_changed') from exc
                previous_id=previous['candidate_id'] if previous else None
                if payload['evaluation'].get('baseline_candidate_id')!=previous_id:
                    result['passed']=False;result['reasons'].append('active_baseline_changed')
                if previous and stamp<previous['recorded_at']:raise ValueError('model activation event cannot be backdated')
                if stamp<candidate['created_at']:raise ValueError('model activation predates candidate')
                event='activated' if result['passed'] else 'rejected'
                connection.execute('INSERT INTO model_learning_events(sport,candidate_id,event,previous_candidate_id,'
                    'recorded_at,reason,evidence) VALUES(?,?,?,?,?,?,?)',
                    (candidate['sport'],candidate_id,event,previous_id,stamp,
                     'paired chronological validation passed' if result['passed'] else '; '.join(result['reasons']),_json(result)))
    except publication.PublicationDeferred as exc:
        return {'status':'deferred','candidate_id':candidate_id,'passed':False,'reasons':[str(exc)]}
    return {'status':event,'candidate_id':candidate_id,**result}


def history(sport,limit=20):
    sport=_sport(sport)
    if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=200:raise ValueError('invalid history limit')
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_learning_events'"):return []
    rows=db.q('SELECT * FROM model_learning_events WHERE sport=? ORDER BY id DESC LIMIT ?',(sport,limit))
    return [dict(row,evidence=json.loads(row['evidence'])) for row in rows]


def rollback(sport,reason,*,now=None):
    """Restore the prior approved artifact, or the original fixed baseline."""
    ensure_schema();sport=_sport(sport);stamp=time.time() if now is None else _number(now)
    if not isinstance(reason,str) or not reason.strip():raise ValueError('rollback needs a recorded reason')
    current=active(sport)
    if current is None:return {'status':'baseline_active','sport':sport}
    event=db.q1('SELECT * FROM model_learning_events WHERE candidate_id=? AND event="activated" '
                'ORDER BY id DESC LIMIT 1',(current['candidate_id'],))
    if not event:raise ValueError('rollback target has no approved activation')
    if db.conn().in_transaction:raise ValueError('learning rollback requires no caller write transaction')
    target=event['previous_candidate_id'];skipped=[];seen={current['candidate_id']};proof=None;candidate=None
    while target:
        approved=db.q1('SELECT * FROM model_learning_events WHERE candidate_id=? AND event="activated" '
                        'ORDER BY id DESC LIMIT 1',(target,))
        if target in seen or not approved or approved['sport']!=sport:
            skipped.append({'candidate_id':target,'reason':'invalid approved predecessor chain'})
            target=None;break
        seen.add(target)
        try:
            candidate=_candidate(target)
            if not candidate or candidate['sport']!=sport:raise ValueError('rollback target missing or mismatched')
            payload=_load(candidate)
            proof=_publication_state(candidate,payload,require_current=True)
            _verify_candidate(candidate,stamp)
            break
        except (ValueError,OSError,KeyError,TypeError) as exc:
            skipped.append({'candidate_id':target,'reason':str(exc)[:250]})
            target=approved['previous_candidate_id']
    try:
        with publication.publish_lock(allow_near=True):
            with db.tx() as connection:
                connection.execute('BEGIN IMMEDIATE')
                latest=db.q1('SELECT id,recorded_at FROM model_learning_events WHERE sport=? '
                    'AND event IN ("activated","rolled_back") ORDER BY id DESC LIMIT 1',(sport,))
                if latest['id']!=current['activation_event_id']:
                    raise publication.PublicationDeferred('active_model_changed_during_rollback')
                if stamp<latest['recorded_at']:raise ValueError('rollback event cannot be backdated')
                if target:
                    publication.same(proof,_publication_state(candidate,payload))
                    _load(candidate)
                connection.execute('INSERT INTO model_learning_events(sport,candidate_id,event,previous_candidate_id,'
                    'recorded_at,reason,evidence) VALUES(?,?,?,?,?,?,?)',
                    (sport,target,'rolled_back',current['candidate_id'],stamp,reason,
                     _json({'previous':current['candidate_id'],'skipped_invalid':skipped})))
    except publication.PublicationDeferred as exc:
        return {'status':'deferred','sport':sport,'reason':str(exc)}
    return {'status':'rolled_back','sport':sport,'candidate_id':target,'skipped_invalid':skipped}
