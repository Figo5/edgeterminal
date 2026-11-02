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

from . import db,nba_store,nba_spread_data as data,nba_spread_model as model,nba_spread_market as market_model
from . import learning_publication as publication

DAY=86400;WEEK=7*DAY
MIN_VALIDATION_GAMES=60
MIN_VALIDATION_DAYS=7
MIN_NEW_GAMES=10
EVALUATION_FAMILY='nba-spread-market-v2'
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
    db.ex('CREATE TABLE IF NOT EXISTS nba_spread_validation_consumption ('
          'game_id TEXT PRIMARY KEY,family TEXT NOT NULL,version TEXT NOT NULL '
          'REFERENCES nba_spread_model_candidates(version),consumed_at REAL NOT NULL,'
          'game_date_et TEXT NOT NULL,forecast_sha256 TEXT NOT NULL,label_sha256 TEXT NOT NULL)')
    db.ex('CREATE TABLE IF NOT EXISTS nba_spread_publication_proofs ('
          'version TEXT PRIMARY KEY REFERENCES nba_spread_model_candidates(version),'
          'prepared_at REAL NOT NULL,state TEXT NOT NULL,code_sha256 TEXT NOT NULL)')
    db.ex("CREATE TRIGGER IF NOT EXISTS nba_spread_activation_gate BEFORE INSERT ON nba_spread_model_events "
          "WHEN NEW.kind='activated' AND NOT EXISTS (SELECT 1 FROM nba_spread_model_candidates c "
          "WHERE c.version=NEW.version AND c.training_cutoff<NEW.created_at "
          "AND json_extract(c.evaluation,'$.passed')=1 AND json_extract(c.evaluation,'$.rows')>=60 "
          "AND json_extract(c.evaluation,'$.days')>=7) "
          "BEGIN SELECT RAISE(ABORT,'NBA spread candidate has not passed validation'); END")
    db.ex("CREATE TRIGGER IF NOT EXISTS nba_spread_market_consumption_gate BEFORE INSERT ON nba_spread_model_events "
          "WHEN NEW.kind='activated' AND EXISTS (SELECT 1 FROM nba_spread_model_candidates c "
          "WHERE c.version=NEW.version AND json_extract(c.artifact,'$.kind')='nba-spread-market-v2') "
          "AND (SELECT COUNT(*) FROM nba_spread_validation_consumption v WHERE v.version=NEW.version)<60 "
          "BEGIN SELECT RAISE(ABORT,'NBA spread market holdout has not been sealed'); END")
    for table in ('nba_spread_model_candidates','nba_spread_model_events','nba_spread_learning_runs','nba_spread_validation_consumption','nba_spread_publication_proofs'):
        for action in ('UPDATE','DELETE'):
            db.ex(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                  "BEGIN SELECT RAISE(ABORT,'NBA spread research is append-only'); END")


def code_catalog():
    names=('nba_spread_model.py','nba_spread_learning.py','nba_spread_data.py','nba_spread_market.py','nba_model.py',
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
    return helper(now,limit=240) if helper else []


class _FeatureReplay:
    """Source-bound extra-team views at each forecast as-of, cached per proposal."""
    def __init__(self):
        self.parsed={};self.used={};self.identities={}

    def history(self,as_of):
        heads=db.q("SELECT id,url,kind,received_at,http_status,sha256 FROM (SELECT id,url,kind,received_at,http_status,sha256, "
            "ROW_NUMBER() OVER(PARTITION BY url ORDER BY received_at DESC,id DESC) AS position "
            "FROM nba_source_artifacts a WHERE kind IN ('team-history','spread-team-history') "
            "AND received_at<=?) WHERE position=1",(as_of,))
        rows=[]
        for head in heads:
            identity=(head['sha256'],head['received_at'])
            if head['id'] in self.identities and self.identities[head['id']]!=identity:
                raise ValueError('NBA feature-replay source identity changed within the proposal')
            self.identities[head['id']]=identity
            key=(head['id'],head['sha256'])
            if key not in self.parsed:
                source=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(head['id'],))
                data.verify_source(source,received_by=as_of)
                # Parse all rows present in these already-received bytes. The
                # shared scorer then enforces the exact target feature cutoff;
                # parsing is not a claim of historical publication availability.
                self.parsed[key]=data.source_history(source,cutoff=as_of+366*DAY)
            rows.extend(self.parsed[key])
        return data.enrich_from_boxes(rows,as_of)

    def use(self,rows,as_of):
        identifiers={r['source_artifact_id'] for r in rows}
        identifiers.update(r['possession_source_artifact_id'] for r in rows if r.get('possession_source_artifact_id'))
        for ident in identifiers:
            if ident not in self.identities:
                source=db.q1('SELECT sha256,received_at FROM nba_source_artifacts WHERE id=?',(ident,))
                if not source:raise ValueError('NBA feature-replay source is missing')
                self.identities[ident]=(source['sha256'],source['received_at'])
            self.used[ident]=min(as_of,self.used.get(ident,as_of))

    def catalog(self):
        catalog=[]
        for ident,as_of in sorted(self.used.items()):
            source=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(ident,))
            data.verify_source(source,received_by=as_of)
            if (source['sha256'],source['received_at'])!=self.identities[ident]:
                raise ValueError('NBA feature-replay source changed after prediction')
            catalog.append(dict(kind='nba_source_artifact',id=ident,sha256=source['sha256'],
                received_at=source['received_at'],available_by=as_of,role='feature_replay'))
        return catalog


def _replay(record,history,artifact,replay=None):
    """Use frozen target inputs plus earlier, source-bound opponent context."""
    rows=record['input_manifest']['rows'];schedule=record['input_manifest'].get('schedule')
    current=replay.history(record['as_of']) if replay else history
    merged={(x['team'],str(x['game_id'])):x for x in current}
    merged.update({(x['team'],str(x['game_id'])):x for x in rows})
    out=model.project_game(record['home_team'],record['away_team'],list(merged.values()),record['tipoff_ts'],
        as_of=record['as_of'],schedule=schedule,artifact=artifact,market=record['market'])
    if out['status']!='ok':raise ValueError('NBA spread frozen inputs cannot reproduce every supported validation game')
    if replay:replay.use(out.get('selected_rows',[]),record['as_of'])
    return out


def _market_training_sources(cohorts):
    """Carry exact prefix input/quote/final receipts into the new artifact."""
    catalog={}
    for phase in ('fit','development','calibration'):
        for record in cohorts[phase]:
            quote=market_model.quote_context(record['market'],record['as_of'],record['tipoff_ts'])
            descriptors=record.get('source_artifacts') or []
            matched=False
            for source in descriptors:
                if source['received_at']>record['as_of']:
                    raise ValueError('NBA market-training source was received after its forecast')
                if source['id']==quote['source_artifact_id']:
                    if source['sha256']!=quote['source_sha256'] or source['received_at']>quote['observed_at']:
                        raise ValueError('NBA market-training quote source descriptor mismatch')
                    matched=True
                descriptor=dict(kind='nba_source_artifact',id=source['id'],sha256=source['sha256'],
                    received_at=source['received_at'],role='market_'+phase)
                catalog[(descriptor['id'],descriptor['role'])]=descriptor
            if not matched:raise ValueError('NBA market-training quote lacks its frozen source descriptor')
            if not record.get('label_source_artifact_id') or not record.get('label_source_sha256'):
                raise ValueError('NBA market-training result lacks its verified final source')
            label=dict(kind='nba_source_artifact',id=record['label_source_artifact_id'],
                sha256=record['label_source_sha256'],received_at=record['result_observed_at'],role='market_'+phase+'_label')
            catalog[(label['id'],label['role'])]=label
    return sorted(catalog.values(),key=lambda r:(r['role'],r['id']))


def build_candidate(now):
    ready=data.ready_at(now)
    if ready is None:return dict(status='waiting',reason='historical_seasons_bootstrapping')
    records=[r for r in _snapshots(now) if r['as_of']>ready]
    if len(records)<MIN_VALIDATION_GAMES:return dict(status='waiting',reason='more_verified_future_spread_games',validation_games=len(records),required_games=MIN_VALIDATION_GAMES)
    spent=db.q('SELECT game_id,game_date_et FROM nba_spread_validation_consumption')
    split=market_model.partition(records,now=now,
        excluded_validation_game_ids=[r['game_id'] for r in spent],
        excluded_validation_dates=[r['game_date_et'] for r in spent])
    if split['status']!='ready':
        return dict(status='waiting',reason=split['reason'],coverage=split['counts'],required=split['required'],
            validation_games=split['counts']['validation'],required_games=MIN_VALIDATION_GAMES,
            validation_days=split['validation_days'],
            message='Spread learning retains its live model until earlier fit/development/calibration cohorts and 60 unused validation games across seven unused dates are available.')
    cohorts=split['cohorts'];records=cohorts['validation'];cutoff=split['training_cutoff']
    current=active()
    if current and current['artifact']['training_cutoff']>=cutoff:return dict(status='waiting',reason='active_game_model_overlaps_validation')
    if current and len({r['game_id'] for r in records}-set(current['artifact']['validation_game_ids']))<MIN_NEW_GAMES:
        return dict(status='waiting',reason='fewer_than_ten_new_games')
    previous=db.q1('SELECT artifact FROM nba_spread_model_candidates ORDER BY created_at DESC,version DESC LIMIT 1')
    if previous and len({r['game_id'] for r in records}-set(json.loads(previous['artifact'])['validation_game_ids']))<MIN_NEW_GAMES:
        return dict(status='waiting',reason='fewer_than_ten_new_games_since_proposal')
    # The historical core must precede anchor FIT forecasts, not merely the
    # outer holdout. Otherwise prefix outcomes could train their own inputs.
    core_cutoff=split['core_training_cutoff']
    history,sources=data.training_history(core_cutoff)
    core=fit_candidate(history,core_cutoff);core['training_sources']=sources
    replay=_FeatureReplay()
    prefix={}
    for phase in ('fit','development','calibration'):
        prefix[phase]=[dict(r,core_margin=_replay(r,history,core,replay)['raw_feature_margin']) for r in cohorts[phase]]
    anchor=market_model.fit_anchor(prefix['fit'],prefix['development'],prefix['calibration'])
    anchor['core_sd_margin']=core['sd_margin']
    artifact=dict(core,schema=2,kind='nba-spread-market-v2',core_training_cutoff=core_cutoff,
        training_cutoff=cutoff,sd_margin=anchor['calibration_sd'],market_anchor=anchor,
        market_training_sources=_market_training_sources(cohorts),
        actual_line_cohorts={phase:[r['game_id'] for r in cohorts[phase]] for phase in cohorts},
        market_training_design='Whole Eastern-date fit/development/calibration prefixes; late labels purged before every next-phase as-of; untouched actual-line outer holdout.',
        purged_market_game_ids=split['purged_game_ids'])
    artifact['evaluation_family']=EVALUATION_FAMILY
    model.validate_artifact(artifact)
    paired=[]
    for r in records:
        after=_replay(r,history,artifact,replay)
        line=r['market'].get('home_spread')
        if line is None:raise ValueError('NBA spread holdout lost a supported actual line')
        actual=r['actual_margin'];before=r['projection']['home_margin']
        before_sd=r['projection'].get('sd_margin',model.baseline.SD_MARGIN)
        core_margin=after['raw_feature_margin']
        paired.append(dict(game_id=r['game_id'],actual_margin=actual,home_spread=line,
            baseline_margin=before,candidate_margin=after['home_margin'],
            feature_only_margin=core_margin,market_only_margin=-line,
            baseline_probability=market_model.cover_probabilities(before,before_sd,line)['conditional_home'],
            candidate_probability=market_model.cover_probabilities(after['home_margin'],after['sd_margin'],line)['conditional_home'],
            feature_only_probability=market_model.cover_probabilities(core_margin,core['sd_margin'],line)['conditional_home'],
            market_only_probability=.5,
            forecast_at=r['as_of'],tipoff=r['tipoff_ts'],result_at=r['result_observed_at']))
    decision=evaluate(paired,training_cutoff=cutoff)
    benchmarks=market_model.benchmark(paired)
    artifact['validation_game_ids']=[r['game_id'] for r in paired]
    artifact['validation']=decision
    artifact['same_line_benchmarks']=benchmarks
    artifact['feature_replay_sources']=replay.catalog()
    artifact['validation_receipts']=[dict(game_id=r['game_id'],game_date_et=datetime.fromtimestamp(r['tipoff_ts'],ET).date().isoformat(),
        forecast_sha256=r['forecast_sha256'],label_sha256=r['label_source_sha256']) for r in records]
    artifact['version']='nba-game-adaptive-'+_sha(artifact)[:16]
    return dict(status='ready',artifact=artifact,evaluation=dict(decision,rows_detail=paired,benchmarks=benchmarks),
                sources=sources+artifact['market_training_sources']+artifact['feature_replay_sources'])


def _publication_time(stamp):
    return max(stamp,time.time())


def _interval(last):
    return DAY if last and last['status']=='waiting' else 300 if last and last['status'] in {'error','deferred'} else WEEK


def _check_active(stamp):
    previous=db.q1("SELECT id FROM nba_spread_model_events WHERE kind IN ('activated','rollback') ORDER BY id DESC LIMIT 1")
    try:active()
    except (ValueError,KeyError,TypeError,OSError) as exc:
        try:
            with publication.publish_lock(allow_near=True):
                with db.tx() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    current=db.q1("SELECT id FROM nba_spread_model_events WHERE kind IN ('activated','rollback') ORDER BY id DESC LIMIT 1")
                    publication.same(previous,current)
                    conn.execute("INSERT INTO nba_spread_model_events(version,created_at,kind,detail) VALUES(NULL,?,'rollback',?)",
                        (_publication_time(stamp),_json(dict(reason=str(exc)[:200],fallback=model.BASELINE_VERSION))))
        except publication.PublicationDeferred as deferred:
            return dict(status='deferred',ok=False,reason=str(deferred),message='Game model integrity recovery awaits a short publication window.')
        return _record(stamp,'rolled_back',dict(reason='Invalid game artifact; restored verified baseline.',fallback=model.BASELINE_VERSION))
    return None


def refresh_inputs(now=None):
    """Bounded source collection and enqueue only; never replay or fit models."""
    stamp=time.time() if now is None else model._finite(now)
    ensure_schema()
    recovery=_check_active(stamp)
    if recovery:return recovery
    from .model_learning import _near_kickoff
    if _near_kickoff(stamp):return dict(status='deferred',ok=True,reason='near_tipoff_capture_priority',
        message='Upcoming NBA or NFL kickoff takes priority; spread learning resumes automatically.')
    try:
        bootstrap=data.refresh(stamp,max_requests=4)
        if bootstrap.get('errors'):
            return _record(stamp,'error',dict(reason='Historical NBA source checks need review; saved forecasts remain live.',bootstrap=bootstrap))
    except Exception as exc:
        return _record(stamp,'error',dict(reason='Historical NBA source check failed; the worker retries automatically.',
            error_type=type(exc).__name__))
    last=db.q1('SELECT * FROM nba_spread_learning_runs ORDER BY id DESC LIMIT 1')
    if bootstrap.get('remaining_schedules',0):
        if last and json.loads(last['detail']).get('reason')=='historical_seasons_bootstrapping':
            return dict(status='cached',ok=True,bootstrap=bootstrap,message='Historical NBA spread sources are still loading in bounded batches.')
        return _record(stamp,'waiting',dict(reason='historical_seasons_bootstrapping',bootstrap=bootstrap))
    transitioned=(last and json.loads(last['detail']).get('reason')=='historical_seasons_bootstrapping')
    if last and stamp<last['started_at']+_interval(last) and not transitioned:
        return dict(status='cached',ok=True,bootstrap=bootstrap,
            message='Spread learning is waiting for its next scheduled evidence check.')
    from . import learning_worker
    job=learning_worker.enqueue('NBA_SPREAD','retuning',now=stamp)
    state=job.get('status') or job.get('state') or 'queued'
    return dict(status=state,ok=True,bootstrap=bootstrap,job=job,
        message='Spread fitting runs separately; source capture and grading continue.')


def run(now=None):
    """Compatibility entry point for the fast ordinary source worker."""
    return refresh_inputs(now)


def _proposal_games(artifact):
    games=set(artifact.get('validation_game_ids') or [])
    games.update(r['game_id'] for r in artifact.get('validation_receipts') or [])
    for values in (artifact.get('actual_line_cohorts') or {}).values():games.update(values)
    return games


def _verify_current_receipts(artifact,state):
    for receipt in artifact.get('validation_receipts') or []:
        game,grade,latest=state['games'].get(receipt['game_id'],(None,None,None))
        if not game or game['status']!='final' or not grade:
            raise ValueError('NBA spread validation no longer has a verified final grade')
        payload=json.loads(grade['payload'])
        source=db.q1('SELECT sha256 FROM nba_source_artifacts WHERE id=?',(grade['source_artifact_id'],))
        if (payload.get('forecast_sha256')!=receipt['forecast_sha256'] or not source or
                source['sha256']!=receipt['label_sha256'] or not latest or
                latest['sha256']!=receipt['label_sha256']):
            raise ValueError('NBA spread validation forecast or latest result source changed')


def _verify_proposal_sources(artifact,evaluation,state,stamp):
    receipts=artifact.get('validation_receipts') or []
    if len(receipts)!=evaluation['rows'] or len(receipts)<MIN_VALIDATION_GAMES:
        raise ValueError('NBA spread approval needs exact source-bound validation receipts')
    _verify_current_receipts(artifact,state)
    catalog={}
    for source in (artifact.get('training_sources') or [])+(artifact.get('market_training_sources') or [])+(artifact.get('feature_replay_sources') or []):
        catalog[source['id']]=source['sha256']
    for receipt in receipts:
        _,grade,_=state['games'][receipt['game_id']]
        catalog[grade['source_artifact_id']]=receipt['label_sha256']
    for ident,digest in catalog.items():
        row=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(ident,))
        data.verify_source(row,received_by=stamp)
        if row['sha256']!=digest:raise ValueError('NBA spread proposal source bytes changed')


def _register_prepared(proposal,expected,code,stamp):
    artifact=proposal['artifact'];encoded=_json(artifact);sha=hashlib.sha256(encoded.encode()).hexdigest()
    with db.tx() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing=db.q1('SELECT sha256 FROM nba_spread_model_candidates WHERE version=?',(artifact['version'],))
        if existing and existing['sha256']!=sha:raise ValueError('NBA spread immutable version conflict')
        if not existing:
            conn.execute('INSERT INTO nba_spread_model_candidates VALUES(?,?,?,?,?,?)',
                (artifact['version'],stamp,artifact['training_cutoff'],encoded,sha,_json(proposal['evaluation'])))
            if artifact.get('kind')=='nba-spread-market-v2':
                receipts=artifact.get('validation_receipts') or []
                if len(receipts)!=proposal['evaluation']['rows']:
                    raise ValueError('NBA spread holdout receipts do not match evaluation coverage')
                for receipt in receipts:
                    for field in ('forecast_sha256','label_sha256'):
                        value=receipt.get(field)
                        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
                            raise ValueError('NBA spread holdout needs exact forecast and label hashes')
                    conn.execute('INSERT INTO nba_spread_validation_consumption VALUES(?,?,?,?,?,?,?)',
                        (receipt['game_id'],EVALUATION_FAMILY,artifact['version'],stamp,
                         receipt['game_date_et'],receipt['forecast_sha256'],receipt['label_sha256']))
            conn.execute('INSERT INTO nba_spread_publication_proofs VALUES(?,?,?,?)',
                (artifact['version'],stamp,_json(expected),_json(code)))
        from . import learning_worker
        checkpoint=getattr(learning_worker,'checkpoint_candidate',None)
        if checkpoint:checkpoint(conn,artifact['version'])
    return artifact['version']


def resume_publication(version,now=None):
    """Publish one already fitted/registered proposal; retries never fit it again."""
    stamp=time.time() if now is None else model._finite(now)
    ensure_schema()
    try:
        row=db.q1('SELECT * FROM nba_spread_model_candidates WHERE version=?',(version,))
        proof=db.q1('SELECT * FROM nba_spread_publication_proofs WHERE version=?',(version,))
        if not row or not proof:raise ValueError('prepared NBA spread proposal or proof is unavailable')
        if hashlib.sha256(row['artifact'].encode()).hexdigest()!=row['sha256']:
            raise ValueError('prepared NBA spread artifact hash mismatch')
        artifact=json.loads(row['artifact']);evaluation=json.loads(row['evaluation'])
        games=_proposal_games(artifact);kind='activated' if evaluation['passed'] else 'rejected'
        invalid=None
        try:
            if _json(publication.spread_state(games))!=proof['state'] or _json(code_catalog())!=proof['code_sha256']:
                raise publication.PublicationDeferred('publication_dependencies_changed')
            if evaluation['passed']:
                model.validate_artifact(artifact)
                _verify_proposal_sources(artifact,evaluation,publication.spread_state(games),_publication_time(stamp))
        except (ValueError,KeyError,TypeError,OSError) as exc:
            invalid=str(exc)
        with publication.publish_lock():
            with db.tx() as conn:
                conn.execute('BEGIN IMMEDIATE')
                old=db.q1('SELECT kind,detail FROM nba_spread_model_events WHERE version=? '
                    'AND kind IN ("activated","rejected") ORDER BY id DESC LIMIT 1',(version,))
                if old:
                    latest=db.q1('SELECT version FROM nba_spread_model_events WHERE kind IN ("activated","rollback") ORDER BY id DESC LIMIT 1')
                    status='retained' if old['kind']=='activated' and (latest or {}).get('version')!=version else old['kind']
                    return dict(status=status,ok=True,version=version,evaluation=json.loads(old['detail']))
                if _json(publication.spread_state(games))!=proof['state'] or _json(code_catalog())!=proof['code_sha256']:
                    invalid='publication_dependencies_changed'
                if invalid:
                    kind='rejected'
                    evaluation=dict(evaluation,passed=False,reasons=list(evaluation.get('reasons') or [])+['publication:'+invalid])
                conn.execute('INSERT INTO nba_spread_model_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
                    (version,_publication_time(stamp),kind,_json(evaluation)))
        return _record(_publication_time(stamp),kind,dict(version=version,evaluation=evaluation))
    except publication.PublicationDeferred as exc:
        return _record(_publication_time(stamp),'deferred',dict(version=version,reason=str(exc),
            message='Prepared spread model retained for publication; capture and grading take priority.'))
    except Exception as exc:
        return _record(_publication_time(stamp),'error',dict(reason=str(exc)[:250],error_type=type(exc).__name__))


def run_fitting(now=None):
    """Dedicated learner entry point; never requests source data or owns capture."""
    stamp=time.time() if now is None else model._finite(now)
    ensure_schema()
    recovery=_check_active(stamp)
    if recovery:return recovery
    from .model_learning import _near_kickoff
    if _near_kickoff(stamp):return _record(stamp,'deferred',dict(reason='near_tipoff_capture_priority'))
    before=publication.spread_state();code_before=code_catalog()
    try:
        proposal=build_candidate(stamp)
        if proposal['status']!='ready':return _record(stamp,'waiting',proposal)
        games=_proposal_games(proposal['artifact'])
        expected=dict(before,games={gid:before['games'].get(gid,(None,None,None)) for gid in sorted(games)})
        version=_register_prepared(proposal,expected,code_before,_publication_time(stamp))
        return resume_publication(version,now=now)
    except Exception as exc:
        return _record(_publication_time(stamp),'error',dict(reason=str(exc)[:250],error_type=type(exc).__name__))


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
