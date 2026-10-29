"""Scheduled, separate game-model learning with immutable evidence and gates.

Historical scores are useful for fitting and development, but only actual saved
pretipoff forecast/line pairs with verified later finals can earn activation.
Nothing is fitted by reads or serving, and a failed proposal leaves the current
game model and every saved grade intact.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
from zoneinfo import ZoneInfo

from . import db,nba_store,nba_spread_data as data,nba_spread_model as model

DAY=86400;WEEK=7*DAY
MIN_VALIDATION_GAMES=60
MIN_VALIDATION_DAYS=7
MIN_NEW_GAMES=10
ALPHAS=(1.,10.,100.)
ROOT=Path(__file__).resolve().parents[1]
ET=ZoneInfo('America/New_York')


def _json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def _sha(value):return hashlib.sha256(_json(value).encode()).hexdigest()


def ensure_schema():
    nba_store.ensure_schema()
    db.ex('CREATE TABLE IF NOT EXISTS nba_spread_model_candidates ('
          'version TEXT PRIMARY KEY,created_at REAL NOT NULL,training_cutoff REAL NOT NULL,'
          'artifact TEXT NOT NULL,sha256 TEXT NOT NULL,evaluation TEXT NOT NULL)')
    db.ex('CREATE TABLE IF NOT EXISTS nba_spread_model_events ('
          'id INTEGER PRIMARY KEY,version TEXT,created_at REAL NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL)')
    db.ex('CREATE TABLE IF NOT EXISTS nba_spread_learning_runs ('
          'id INTEGER PRIMARY KEY,started_at REAL NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL)')
    db.ex("CREATE TRIGGER IF NOT EXISTS nba_spread_activation_gate BEFORE INSERT ON nba_spread_model_events "
          "WHEN NEW.kind='activated' AND NOT EXISTS (SELECT 1 FROM nba_spread_model_candidates c "
          "WHERE c.version=NEW.version AND c.training_cutoff<NEW.created_at "
          "AND json_extract(c.evaluation,'$.passed')=1 AND json_extract(c.evaluation,'$.rows')>=60 "
          "AND json_extract(c.evaluation,'$.days')>=7) "
          "BEGIN SELECT RAISE(ABORT,'NBA spread candidate has not passed validation'); END")
    for table in ('nba_spread_model_candidates','nba_spread_model_events','nba_spread_learning_runs'):
        for action in ('UPDATE','DELETE'):
            db.ex(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                  "BEGIN SELECT RAISE(ABORT,'NBA spread research is append-only'); END")


def code_catalog():
    names=('nba_spread_model.py','nba_spread_learning.py','nba_spread_data.py','nba_model.py',
           'nba_sources.py','nba_identity.py','nba_game_context.py','nba_game_forecasts.py',
           'pricing.py','analytics/nba.py','nba_model_manifest.py')
    return {f'edgeterminal/{name}':hashlib.sha256((ROOT/'edgeterminal'/name).read_bytes()).hexdigest()
            for name in names if (ROOT/'edgeterminal'/name).is_file()}


def active():
    ensure_schema()
    event=db.q1("SELECT * FROM nba_spread_model_events WHERE kind IN ('activated','rollback') ORDER BY id DESC LIMIT 1")
    if not event or not event['version']:return None
    row=db.q1('SELECT * FROM nba_spread_model_candidates WHERE version=?',(event['version'],))
    if not row or hashlib.sha256(row['artifact'].encode()).hexdigest()!=row['sha256']:
        raise ValueError('NBA spread active artifact hash mismatch')
    artifact=json.loads(row['artifact']);model.validate_artifact(artifact)
    if artifact.get('code_sha256')!=code_catalog():raise ValueError('NBA spread active code fingerprint mismatch')
    if artifact['training_cutoff']!=row['training_cutoff']:raise ValueError('NBA spread active cutoff mismatch')
    return dict(version=row['version'],sha256=row['sha256'],artifact=artifact,activated_at=event['created_at'])


def _record(now,status,detail):
    result=dict(detail,status=status,ok=status!='error')
    result.setdefault('message',str(result.get('reason',status)).replace('_',' '))
    db.ex('INSERT INTO nba_spread_learning_runs(started_at,status,detail) VALUES(?,?,?)',(now,status,_json(result)))
    return result


def examples(history,cutoff):
    """Chronological reconstructed-score features; retrieval times are not backdated."""
    groups=defaultdict(dict)
    for row in history:
        ts=model.baseline.parse_time(row.get('played_at',row.get('date')))
        if ts is None or ts+model.baseline.FEATURE_LAG_S>=cutoff:continue
        groups[str(row['game_id'])][row['team']]=row
    result=[]
    for gid,pair in groups.items():
        if len(pair)!=2:continue
        home=next((r for r in pair.values() if r.get('home') is True),None)
        away=next((r for r in pair.values() if r.get('home') is False),None)
        if not home or not away or home['opponent']!=away['team'] or away['opponent']!=home['team']:continue
        if home['points']!=away['opponent_points'] or away['points']!=home['opponent_points']:raise ValueError('NBA historical result conflict')
        ts=model.baseline.parse_time(home['played_at'])
        context=model.features(home['team'],away['team'],history,ts,as_of=ts-model.baseline.FEATURE_LAG_S)
        if context['status']=='ok':
            result.append(dict(game_id=gid,ts=ts,values=context['values'],
                baseline_margin=context['baseline']['margin'],actual_margin=home['points']-away['points']))
    return sorted(result,key=lambda r:(r['ts'],r['game_id']))


def fit_candidate(history,cutoff):
    """Three fixed ridge penalties; development chooses MAE before calibration."""
    import numpy as np
    from sklearn.linear_model import Ridge
    rows=examples(history,cutoff)
    if len(rows)<900 or len({model._season(r['ts']) for r in rows})<2:
        raise ValueError('insufficient multi-season NBA spread fitting history')
    fit,dev,cal=rows[:-240],rows[-240:-120],rows[-120:]
    xx=np.asarray([r['values'] for r in fit],dtype=float)
    means=xx.mean(axis=0);scales=xx.std(axis=0);scales[scales<1e-9]=1.
    residual=np.asarray([r['actual_margin']-r['baseline_margin'] for r in fit])
    scores=[];models={}
    for alpha in ALPHAS:
        fitted=Ridge(alpha=alpha).fit((xx-means)/scales,residual);models[alpha]=fitted
        errors=[abs(r['baseline_margin']+float(fitted.predict([(np.asarray(r['values'])-means)/scales])[0])-r['actual_margin']) for r in dev]
        scores.append((statistics.mean(errors),alpha))
    _,alpha=min(scores);chosen=models[alpha]
    residuals=[r['actual_margin']-(r['baseline_margin']+float(chosen.predict([(np.asarray(r['values'])-means)/scales])[0])) for r in cal]
    artifact=dict(schema=1,kind='nba-spread-residual-v1',feature_names=list(model.FEATURE_NAMES),
        training_cutoff=cutoff,intercept=float(chosen.intercept_),coefficients=chosen.coef_.tolist(),
        means=means.tolist(),scales=scales.tolist(),sd_margin=max(1.,min(100.,statistics.stdev(residuals))),
        development_alpha=alpha,development=[dict(alpha=a,margin_mae=e) for e,a in scores],
        code_sha256=code_catalog(),training=dict(fit_game_ids=[r['game_id'] for r in fit],
        development_game_ids=[r['game_id'] for r in dev],calibration_game_ids=[r['game_id'] for r in cal],
        fit_n=len(fit),development_n=len(dev),calibration_n=len(cal),
        latest_label_at=max(r['ts']+model.baseline.FEATURE_LAG_S for r in rows),
        historical_receipt_note='Final-score reconstruction assumes four-hour completion lag; archives were not available historically.'))
    artifact['version']='nba-game-adaptive-'+_sha(artifact)[:16]
    model.validate_artifact(artifact)
    return artifact


def evaluate(rows,*,training_cutoff):
    """Actual-line gates: material point accuracy, cover calibration, no ATS regression."""
    if len({r['game_id'] for r in rows})!=len(rows):raise ValueError('NBA spread validation repeats a game')
    for r in rows:
        if r.get('home_spread') is None:raise ValueError('NBA spread validation requires actual saved line')
        if not training_cutoff<r['forecast_at']<r['tipoff']<r['result_at']:
            raise ValueError('NBA spread validation chronology is invalid')
        for name in ('actual_margin','home_spread','baseline_margin','candidate_margin','baseline_probability','candidate_probability'):
            model._finite(r[name])
        if not 0<=r['baseline_probability']<=1 or not 0<=r['candidate_probability']<=1:
            raise ValueError('NBA spread cover probability outside zero-one')
    days={datetime.fromtimestamp(r['tipoff'],ET).date().isoformat() for r in rows}
    reasons=[]
    if len(rows)<MIN_VALIDATION_GAMES:reasons.append('fewer_than_sixty_whole_games')
    if len(days)<MIN_VALIDATION_DAYS:reasons.append('fewer_than_seven_distinct_days')
    if not rows:return dict(passed=False,reasons=reasons,rows=0)
    base_mae=statistics.mean(abs(r['baseline_margin']-r['actual_margin']) for r in rows)
    cand_mae=statistics.mean(abs(r['candidate_margin']-r['actual_margin']) for r in rows)
    if base_mae-cand_mae<max(.25,.02*base_mae):reasons.append('margin_improvement_too_small')
    binary=[r for r in rows if r['actual_margin']+r['home_spread']!=0]
    def brier(label):return statistics.mean((r[label+'_probability']-(r['actual_margin']+r['home_spread']>0))**2 for r in binary) if binary else None
    before,after=brier('baseline'),brier('candidate')
    if before is None or before-after<.002:reasons.append('cover_calibration_not_improved')
    def wins(label):return sum((r[label+'_margin']+r['home_spread']>=0)==(r['actual_margin']+r['home_spread']>0) for r in binary)
    if wins('candidate')<wins('baseline'):reasons.append('spread_win_count_regressed')
    # Shared daily effects mean games cannot be treated as independent trials.
    # Paired calendar-day resampling is fixed in advance, not tuned per night.
    intervals={}
    if len(days)>=MIN_VALIDATION_DAYS and binary:
        import numpy as np
        blocks={day:[r for r in rows if datetime.fromtimestamp(r['tipoff'],ET).date().isoformat()==day] for day in days}
        ordered=sorted(blocks);rng=np.random.default_rng(20261029);deltas=[]
        for _ in range(1000):
            sample=[r for i in rng.integers(0,len(ordered),len(ordered)) for r in blocks[ordered[i]]]
            mae=statistics.mean(abs(r['candidate_margin']-r['actual_margin'])-abs(r['baseline_margin']-r['actual_margin']) for r in sample)
            scored=[r for r in sample if r['actual_margin']+r['home_spread']!=0]
            if scored:
                brier_delta=statistics.mean((r['candidate_probability']-(r['actual_margin']+r['home_spread']>0))**2-
                    (r['baseline_probability']-(r['actual_margin']+r['home_spread']>0))**2 for r in scored)
                deltas.append((mae,brier_delta))
        if deltas:
            for index,key in enumerate(('margin_mae','cover_brier')):
                low,high=np.quantile([row[index] for row in deltas],[.025,.975])
                intervals[key]=dict(low=float(low),high=float(high))
                if high>=0:reasons.append(key+'_improvement_not_established')
        else:reasons.append('insufficient_binary_day_blocks')
    return dict(passed=not reasons,reasons=reasons,rows=len(rows),whole_games=len(rows),days=len(days),
                baseline_mae=base_mae,candidate_mae=cand_mae,baseline_brier=before,candidate_brier=after,
                baseline_wins=wins('baseline'),candidate_wins=wins('candidate'),pushes=len(rows)-len(binary),
                paired_day_intervals=intervals,all_supported_games_predicted=True)


def _snapshots(now):
    from . import nba_game_forecasts
    helper=getattr(nba_game_forecasts,'validation_snapshots',None)
    return helper(now,limit=120) if helper else []


def build_candidate(now):
    ready=data.ready_at(now)
    if ready is None:return dict(status='waiting',reason='historical_seasons_bootstrapping')
    records=[r for r in _snapshots(now) if r['as_of']>ready]
    if len(records)<MIN_VALIDATION_GAMES:return dict(status='waiting',reason='more_verified_future_spread_games',validation_games=len(records),required_games=MIN_VALIDATION_GAMES)
    cutoff=min(r['as_of'] for r in records)-1
    current=active()
    if current and current['artifact']['training_cutoff']>=cutoff:return dict(status='waiting',reason='active_game_model_overlaps_validation')
    if current and len({r['game_id'] for r in records}-set(current['artifact']['validation_game_ids']))<MIN_NEW_GAMES:
        return dict(status='waiting',reason='fewer_than_ten_new_games')
    previous=db.q1('SELECT artifact FROM nba_spread_model_candidates ORDER BY created_at DESC,version DESC LIMIT 1')
    if previous and len({r['game_id'] for r in records}-set(json.loads(previous['artifact'])['validation_game_ids']))<MIN_NEW_GAMES:
        return dict(status='waiting',reason='fewer_than_ten_new_games_since_proposal')
    history,sources=data.training_history(cutoff)
    artifact=fit_candidate(history,cutoff);artifact['training_sources']=sources
    paired=[]
    for r in records:
        rows=r['input_manifest']['rows'];schedule=r['input_manifest'].get('schedule')
        # Extra opponent context is from source snapshots already available at
        # the training cutoff; target-team snapshots remain the actual capture's.
        merged={(x['team'],str(x['game_id'])):x for x in history}
        merged.update({(x['team'],str(x['game_id'])):x for x in rows})
        after=model.project_game(r['home_team'],r['away_team'],list(merged.values()),r['tipoff_ts'],
                                 as_of=r['as_of'],schedule=schedule,artifact=artifact)
        if after['status']!='ok':raise ValueError('NBA spread frozen inputs cannot reproduce validation game')
        line=r['market'].get('home_spread')
        if line is None:continue
        actual=r['actual_margin'];before=r['projection']['home_margin']
        before_sd=r['projection'].get('sd_margin',model.baseline.SD_MARGIN)
        paired.append(dict(game_id=r['game_id'],actual_margin=actual,home_spread=line,
            baseline_margin=before,candidate_margin=after['home_margin'],
            baseline_probability=model.baseline.norm_cdf((before+line)/before_sd),
            candidate_probability=model.baseline.norm_cdf((after['home_margin']+line)/after['sd_margin']),
            forecast_at=r['as_of'],tipoff=r['tipoff_ts'],result_at=r['result_observed_at']))
    decision=evaluate(paired,training_cutoff=cutoff)
    artifact['validation_game_ids']=[r['game_id'] for r in paired]
    artifact['validation']=decision
    artifact['version']='nba-game-adaptive-'+_sha(artifact)[:16]
    return dict(status='ready',artifact=artifact,evaluation=dict(decision,rows_detail=paired),sources=sources)


def run(now=None):
    """Called under the shared worker lock; never blocks source capture for fitting."""
    stamp=time.time() if now is None else model._finite(now)
    ensure_schema()
    try:active()
    except (ValueError,KeyError,TypeError,OSError) as exc:
        # Restore the separately versioned baseline if an active bundle is
        # damaged. The event and visible run record preserve the incident.
        db.ex("INSERT INTO nba_spread_model_events(version,created_at,kind,detail) VALUES(NULL,?,'rollback',?)",
              (stamp,_json(dict(reason=str(exc)[:200],fallback=model.BASELINE_VERSION))))
        return _record(stamp,'rolled_back',dict(reason='Invalid game artifact; restored verified baseline.',fallback=model.BASELINE_VERSION))
    last=db.q1('SELECT * FROM nba_spread_learning_runs ORDER BY id DESC LIMIT 1')
    from .model_learning import _near_kickoff
    if _near_kickoff(stamp):return dict(status='deferred',ok=True,reason='near_tipoff_capture_priority',
        message='Upcoming NBA or NFL kickoff takes priority; spread learning resumes automatically.')
    # Bootstrap continues each tick while fitting checks are independently cached.
    try:
        bootstrap=data.refresh(stamp,max_requests=4)
        if bootstrap.get('errors'):
            return _record(stamp,'error',dict(reason='Historical NBA source checks need review; saved forecasts remain live.',bootstrap=bootstrap))
    except Exception as exc:
        return _record(stamp,'error',dict(reason='Historical NBA source check failed; the worker retries automatically.',
            error_type=type(exc).__name__))
    interval=DAY if last and last['status']=='waiting' else 300 if last and last['status']=='error' else WEEK
    transitioned=(last and last['status']=='waiting' and
        json.loads(last['detail']).get('reason')=='historical_seasons_bootstrapping' and
        bootstrap.get('remaining_schedules')==0)
    if last and stamp<last['started_at']+interval and not transitioned:return dict(status='cached',ok=True,bootstrap=bootstrap,
        message='Spread learning is waiting for its next scheduled evidence check.')
    try:
        proposal=build_candidate(stamp)
        if proposal['status']!='ready':return _record(stamp,'waiting',dict(proposal,bootstrap=bootstrap))
        artifact=proposal['artifact'];encoded=_json(artifact);sha=hashlib.sha256(encoded.encode()).hexdigest()
        existing=db.q1('SELECT sha256 FROM nba_spread_model_candidates WHERE version=?',(artifact['version'],))
        if existing and existing['sha256']!=sha:raise ValueError('NBA spread immutable version conflict')
        if not existing:db.ex('INSERT INTO nba_spread_model_candidates VALUES(?,?,?,?,?,?)',
            (artifact['version'],stamp,artifact['training_cutoff'],encoded,sha,_json(proposal['evaluation'])))
        passed=proposal['evaluation']['passed'];kind='activated' if passed else 'rejected'
        db.ex('INSERT INTO nba_spread_model_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
              (artifact['version'],stamp,kind,_json(proposal['evaluation'])))
        return _record(stamp,kind,dict(version=artifact['version'],evaluation=proposal['evaluation'],bootstrap=bootstrap))
    except Exception as exc:
        return _record(stamp,'error',dict(reason=str(exc)[:250],error_type=type(exc).__name__,bootstrap=bootstrap))


def status(*,now=None):
    stamp=time.time() if now is None else float(now);ensure_schema()
    current=active();last=db.q1('SELECT * FROM nba_spread_learning_runs ORDER BY id DESC LIMIT 1')
    interval=DAY if last and last['status']=='waiting' else 300 if last and last['status']=='error' else WEEK
    return dict(enabled=True,component='NBA_GAME',active={k:current[k] for k in ('version','sha256','activated_at')} if current else None,
                status=last['status'] if last else 'not_checked',checked_at=last['started_at'] if last else None,
                next_due_at=last['started_at']+interval if last else stamp,
                detail=json.loads(last['detail']) if last else {},
                message=(json.loads(last['detail']).get('message') or last['status']) if last else 'Game-model learning awaits its first scheduled check.',
                schedule='Bounded historical bootstrap; weekly game-model proposals after sufficient verified future games.',
                all_supported_games_selected=True,designation='PASS',readiness='RESEARCH_ONLY')
