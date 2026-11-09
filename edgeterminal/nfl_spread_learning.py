"""Automatic NFL spread staging and sealed, untouched whole-week evaluation.

Capture appends cheap exact-snapshot observations. Heavy source verification,
fit/development/calibration and final evaluation execute only in the isolated
learner; reads never fit. No public parallel model record is introduced.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import statistics
import time

from . import db, nfl_spread_model as model, nfl_spread_store as store, nfl_spread_data as data
from . import learning_publication as publication

DAY=86400
WEEK=7*DAY


class FinalEvidencePending(ValueError):
    """A consumed cohort awaits an authenticated final, never an assumed label."""


def _now(now=None):
    return max(model.number(time.time() if now is None else now),time.time())


def _record(stamp,status,detail):
    store.ensure_schema()
    result=dict(status=status,ok=status!='error',detail=detail,message=str(detail.get('reason',status)).replace('_',' '))
    db.ex('INSERT INTO nfl_spread_runs(created_at,status,detail) VALUES(?,?,?)',(stamp,status,model.encoded(detail)))
    return result


def active():
    event=store.active_event()
    if not event or not event['version']: return None
    return _verified_active(event)


def _verified_active(event):
    candidate=store.candidate(event['version'],current_code=True)
    artifact=candidate['artifact']
    proof=db.q1('SELECT * FROM nfl_spread_proofs WHERE version=?',(event['version'],))
    if not proof: raise ValueError('NFL spread active proof missing')
    payload=data.decode(proof['payload'])
    evaluation=db.q1('SELECT * FROM nfl_spread_evaluations WHERE reservation_id=?',(proof['reservation_id'],))
    reserved=store.reservation(proof['reservation_id'])
    if (not candidate['created_at']<event['created_at'] or (event['kind']=='activated' and proof['created_at']!=event['created_at'])
            or store.sha(payload)!=proof['sha256'] or payload['candidate_sha256']!=candidate['sha256']
            or reserved['version']!=candidate['version'] or reserved['payload']['version']!=candidate['version']
            or reserved['payload']['candidate_sha256']!=candidate['sha256']
            or payload['reservation_sha256']!=reserved['sha256'] or not evaluation
            or payload['evaluation_sha256']!=evaluation['sha256']
            or store.sha(data.decode(evaluation['payload']))!=evaluation['sha256']
            or not data.decode(evaluation['payload']).get('passed')):
        raise ValueError('NFL spread active publication seal mismatch')
    current_head=_evidence_state(artifact,reserved)
    checked=db.q1('SELECT * FROM nfl_spread_integrity WHERE version=? ORDER BY id DESC LIMIT 1',(candidate['version'],))
    expected=payload['evidence_state_sha256'];passed=True
    if checked:
        assessment=data.decode(checked['payload'])
        if store.sha(assessment)!=checked['sha256']:raise ValueError('NFL spread integrity assessment seal mismatch')
        expected=checked['head_sha256'];passed=bool(checked['passed'])
    return dict(version=candidate['version'],sha256=candidate['sha256'],artifact=artifact,
                activated_at=event['created_at'],event_id=event['id'],evidence_current=passed and expected==store.sha(current_head))


def _feature_row(projection,event,market,input_map,as_of):
    game=data.event_game(event); kickoff=data.parse_time(game['start_time'])
    if input_map.get('status')=='legacy_fallback': return None
    if not input_map.get('source_id'): raise ValueError('NFL spread source map identity missing')
    from . import nfl_game_sources
    nfl_game_sources.verify_input_map(input_map,game,input_map['as_of'],deep=False)
    if not input_map['as_of']<=as_of<kickoff: raise ValueError('NFL spread serving input chronology conflict')
    quote=data.quote_context(market,game,as_of,kickoff)
    if quote is None: return None
    h,a=input_map['teams'][game['home_team']],input_map['teams'][game['away_team']]
    parts=game['id'].split('-'); groups=projection.get('feature_groups') or {}
    return dict(game_id=game['id'],season=int(parts[1]),week=int(parts[3]),as_of=as_of,kickoff=kickoff,
        formula_version=model.FORMULA_VERSION,input_policy_id=input_map['policy_id'],
        feature_received_at=max(input_map['original_received_at'],input_map['adopted_at'],input_map.get('published_at',input_map['adopted_at'])),
        source_sha256=input_map['sha256'],snapshot_sha256='0'*64,
        baseline_margin=projection['margin'],incumbent_margin=projection['margin'],baseline_sd=projection['sd_margin'],
        home_spread=quote['line'],quote_sha256=quote['sha256'],
        scoring_margin=groups.get('scoring_margin',projection['margin']-groups.get('efficiency_points',0.)),
        efficiency_points=groups.get('efficiency_points',0.),home_games=h['games'],away_games=a['games'],
        home_plays=h['plays'],away_plays=a['plays'],advanced_available=float(min(h['games'],a['games'],h['plays'],a['plays'])>0))


def project_component(raw_projection,event,market,input_map,as_of):
    """Pure inference from an accepted artifact; original projection untouched."""
    current=active()
    if not current or not current.get('evidence_current',True): return None
    row=_feature_row(raw_projection,event,market,input_map,as_of)
    artifact=current['artifact']
    if row is None: return None
    if row['input_policy_id']!=artifact['input_policy_id'] or as_of<=artifact['training_cutoff']:
        return None
    margin=model.predict(artifact['member'],row)
    return dict(used=True,margin=margin,sd_margin=artifact['sd_margin'],
        home_cover_probability=model.probability(margin,artifact['sd_margin'],row['home_spread']),
        home_spread=row['home_spread'],version=current['version'],artifact_sha256=current['sha256'],
        input_policy_id=row['input_policy_id'],quote_sha256=row['quote_sha256'],
        forecast_input_sha256=model.sha(dict(game_id=row['game_id'],formula_version=row['formula_version'],
            input_policy_id=row['input_policy_id'],features=model.feature_values(artifact['member']['method'],row),
            source_id=input_map.get('source_id'),raw_sha256=input_map.get('raw_sha256'),
            aggregate_sha256=input_map.get('aggregate_sha256'),publication_event_id=input_map.get('publication_event_id'),
            quote_sha256=row['quote_sha256'])),fallback=None)


def capture(snapshot_id,now=None):
    """Cheap pregame enrollment/freeze; never fit or fetch a provider."""
    stamp=_now(now); store.ensure_schema()
    raw=db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(snapshot_id,))
    if not raw: raise ValueError('NFL spread capture snapshot missing')
    body=data.decode(raw['payload']); game=body.get('game') or {}
    kickoff=data.parse_time(game.get('kickoff_utc') or game.get('start_time'))
    if kickoff is None or stamp>=kickoff:
        return dict(status='skipped',reason='physical_kickoff_passed')
    source=body.get('game_input_map')
    if not source or source.get('status')=='legacy_fallback':
        return dict(status='waiting',reason='verified_input_policy_required')
    if str(game.get('id','')).startswith(('nfl-2026-reg-08-','nfl-2026-reg-09-')):
        return dict(status='skipped',reason='inspected_week_excluded_whole')
    observation=data.observation(snapshot_id,now=stamp,deep=False)
    if observation is None: return dict(status='waiting',reason='supported_ESPN_home_line_required')
    old=db.q1('SELECT * FROM nfl_spread_observations WHERE snapshot_id=?',(snapshot_id,))
    if old:
        if old['sha256']!=model.sha(observation): raise ValueError('NFL spread observation changed')
    else:
        db.ex('INSERT INTO nfl_spread_observations VALUES(?,?,?,?,?,?,?)',
            (snapshot_id,observation['game_id'],observation['season'],observation['week'],stamp,
             model.encoded(observation),model.sha(observation)))
    pending=_pending()
    frozen=None
    if pending:
        artifact=pending['artifact']
        if (observation['season']==artifact['outer_season'] and observation['week']>=artifact['outer_first_week']
                and pending['created_at']<observation['as_of']
                and observation['input_policy_id']==artifact['input_policy_id']):
            margin=model.predict(artifact['member'],observation)
            frozen=store.freeze(pending['version'],observation,dict(margin=margin,
                home_cover_probability=model.probability(margin,artifact['sd_margin'],observation['home_spread'])),now=stamp)
    return dict(status='captured',snapshot_id=snapshot_id,frozen=bool(frozen))


def _pending():
    store.ensure_schema()
    row=db.q1("SELECT c.version FROM nfl_spread_candidates c WHERE NOT EXISTS "
        "(SELECT 1 FROM nfl_spread_events e WHERE e.version=c.version AND e.kind IN ('activated','rejected','abandoned','rollback')) "
        'ORDER BY c.created_at,c.version LIMIT 1')
    return store.candidate(row['version'],current_code=True) if row else None


def _latest_rows(rows):
    result={}
    for row in sorted(rows,key=lambda r:(r['as_of'],r['snapshot_id'])):
        result[row['game_id']]=row
    return list(result.values())


def _groups(rows,now):
    data.prepare_schedule_proofs({r['season'] for r in rows},now)
    groups=defaultdict(list)
    for row in _latest_rows(rows): groups[(row['season'],row['week'])].append(row)
    result={}
    for key,items in groups.items():
        if not data.complete_week(*key,now): continue
        universe=data.week_universe(*key)
        if universe and {e['id'] for e in universe}!={r['game_id'] for r in items}: continue
        result[key]=sorted(items,key=lambda r:r['game_id'])
    return result


def _whole_week_evidence(items,stamp):
    if not data.prepare_schedule_proofs({r['season'] for r in items},stamp):return False
    by_week=defaultdict(set)
    for row in items:by_week[(row['season'],row['week'])].add(row['game_id'])
    for key,games in by_week.items():
        universe=data.week_universe(*key)
        if not universe:return False
        if {e['id'] for e in universe}!=games:
            raise ValueError('NFL spread sealed whole-week universe changed')
        if not data.complete_week(*key,stamp):return False
    return True


def _verify_stage_sources(state):
    """Recheck sealed input/label heads outside publication/writer locks."""
    items=[r for phase in ('fit','development','calibration') for r in state.get(phase+'_rows',[])]
    if not items:return
    if not _whole_week_evidence(items,time.time()):
        raise FinalEvidencePending('verified_whole_week_schedule_required')
    labels=data.labels(items,now=time.time()); proof_cache={}
    for previous in items:
        current=data.observation(previous['snapshot_id'],now=time.time(),deep=True,proof_cache=proof_cache)
        if current is None or any(current.get(k)!=previous.get(k) for k in current):
            raise ValueError('NFL spread staged source correction conflict')
        final=labels.get(previous['game_id'])
        if not final or any(final.get(k)!=previous.get(k) for k in final):
            raise ValueError('NFL spread staged result head changed')



def _verify_training_stage(state,artifact):
    """Independently reproduce the finite inner experiment before outer access."""
    phases={name:state.get(name+'_rows',[]) for name in model.MINIMUMS}
    for name,rows in phases.items():
        if len(rows)<model.MINIMUMS[name]:raise ValueError('NFL spread candidate inner phase coverage missing')
        if len({r['game_id'] for r in rows})!=len(rows):raise ValueError('NFL spread inner duplicate game')
        for row in rows:model.validate_example(row)
        by_week=defaultdict(set)
        for row in rows:by_week[(row['season'],row['week'])].add(row['game_id'])
        for key,games in by_week.items():
            expected={e['id'] for e in data.week_universe(*key)}
            if not expected:raise FinalEvidencePending('verified_whole_week_schedule_required')
            if expected!=games:raise ValueError('NFL spread candidate inner week universe changed')
    if len({r['game_id'] for rows in phases.values() for r in rows})!=sum(len(rows) for rows in phases.values()):
        raise ValueError('NFL spread inner phases overlap games')
    weeks=[{(r['season'],r['week']) for r in phases[name]} for name in model.MINIMUMS]
    if max(weeks[0])>=min(weeks[1]) or max(weeks[1])>=min(weeks[2]):
        raise ValueError('NFL spread inner whole-week chronology conflict')
    receipt={name:max(max(r['label_received_at'],r['feature_received_at']) for r in rows) for name,rows in phases.items()}
    if state['fit_cutoff']!=receipt['fit'] or artifact['training_cutoff']!=receipt['calibration']:
        raise ValueError('NFL spread candidate inner cutoff proof conflict')
    bank=model.fit_bank(phases['fit'],cutoff=receipt['fit'])
    def member_equal(before,after):
        for key in ('method','feature_names','alpha'):
            if before[key]!=after[key]:return False
        # Mathematical replay permits only rounding noise from an installed BLAS
        # after relocation. Stored serving coefficients/versions remain exact.
        return (math.isclose(before['intercept'],after['intercept'],rel_tol=1e-12,abs_tol=1e-12)
            and all(len(before[k])==len(after[k]) and all(math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12)
                for a,b in zip(before[k],after[k])) for k in ('means','scales','coefficients')))
    old=state['bank']
    if any(bank[k]!=old[k] for k in bank if k!='members') or len(bank['members'])!=len(old['members']) or not all(member_equal(a,b) for a,b in zip(bank['members'],old['members'])):
        raise ValueError('NFL spread fit bank derivation conflict')
    selected=model.select(old,phases['development'],previous_receipt=receipt['fit'])
    if selected['method']=='incumbent' or not member_equal(selected,state['member']) or not member_equal(selected,artifact['member']):
        raise ValueError('NFL spread development choice proof conflict')
    sigma=model.calibrate(old,selected,phases['calibration'],previous_receipt=receipt['development'])
    if not math.isclose(sigma,artifact['sd_margin'],rel_tol=1e-12,abs_tol=1e-12):
        raise ValueError('NFL spread calibration proof conflict')
    if (artifact['formula_version'],artifact['input_policy_id'],artifact['incumbent_version'])!=model.policy_identity(phases['fit']):
        raise ValueError('NFL spread candidate input policy proof conflict')
    return True

def outer_window(season,first_week,consumed):
    first=max(1,int(first_week)); occupied={tuple(x) for x in consumed}
    available=[w for w in range(first,19) if (season,w) not in occupied and (season,w) not in {(2026,8),(2026,9)}]
    if len(available)<6:
        return dict(reason='insufficient_season_remaining',season=season+1,first_week=1,
                    remaining_weeks=len(available),required_weeks=6,next_eligible_season=season+1)
    return dict(reason='fresh_complete_weeks_required',season=season,first_week=first,remaining_weeks=len(available),required_weeks=6)


@data.with_schedule_proofs
def _integrity(stamp):
    current=None
    try:
        current=active()
        if not current or current.get('evidence_current',True):return False
        proposal=store.candidate(current['version'],current_code=True)
        proof=db.q1('SELECT reservation_id FROM nfl_spread_proofs WHERE version=?',(current['version'],))
        reserved=store.reservation(proof['reservation_id']);artifact=proposal['artifact']
        before=_evidence_state(artifact,reserved)
        prior=db.q1('SELECT * FROM nfl_spread_integrity WHERE version=? ORDER BY id DESC LIMIT 1',(current['version'],))
        if prior and prior['head_sha256']==store.sha(before) and data.decode(prior['payload']).get('reason')=='official_final_coverage_pending':
            return 'pending'
        stage=db.q1('SELECT state FROM nfl_spread_stages WHERE id=?',(artifact['stage_id'],))
        inner=data.decode(stage['state'])
        _verify_stage_sources(inner);_verify_training_stage(inner,artifact)
        rows=_verified_reserved_rows(proposal,reserved,_now(stamp))
        evaluation=evaluate(rows)
        publication.same(before,_evidence_state(artifact,reserved))
        with publication.publish_lock():
            publication.same(before,_evidence_state(artifact,reserved))
            assessment=dict(reason='accepted_evidence_rechecked',evaluation=evaluation,
                source_result_heads_sha256=store.sha(before),fixed_candidate=current['version'])
            db.ex('INSERT INTO nfl_spread_integrity(version,created_at,head_sha256,passed,payload,sha256) VALUES(?,?,?,?,?,?)',
                (current['version'],stamp,store.sha(before),int(evaluation['passed']),store.encoded(assessment),store.sha(assessment)))
        if evaluation['passed']:return False
        raise ValueError('NFL spread accepted evidence no longer passes after correction')
    except FinalEvidencePending:
        # Keep the accepted pointer as historical evidence, while inference uses
        # baseline fallback. No partial evaluation or new activation is allowed.
        publication.same(before,_evidence_state(artifact,reserved))
        with publication.publish_lock():
            publication.same(before,_evidence_state(artifact,reserved))
            assessment=dict(reason='official_final_coverage_pending',fixed_candidate=current['version'],source_result_heads_sha256=store.sha(before))
            db.ex('INSERT INTO nfl_spread_integrity(version,created_at,head_sha256,passed,payload,sha256) VALUES(?,?,?,?,?,?)',
                (current['version'],stamp,store.sha(before),0,store.encoded(assessment),store.sha(assessment)))
        return 'pending'
    except publication.PublicationDeferred:
        raise
    except (ValueError,OSError,KeyError,TypeError) as exc:
        previous=None
        for event in db.q("SELECT * FROM nfl_spread_events WHERE kind='activated' ORDER BY id DESC"):
            try:
                verified=_verified_active(event)
                if verified.get('evidence_current',False):previous=event['version'];break
            except (ValueError,OSError,KeyError,TypeError):continue
        store.event(previous,'rollback',dict(reason='active_integrity_failed',error_type=type(exc).__name__),now=stamp)
        return True


@data.with_schedule_proofs
def run_fitting(now=None):
    stamp=_now(now); store.ensure_schema()
    integrity=_integrity(stamp)
    if integrity=='pending':
        return _record(stamp,'waiting',dict(stage='integrity',reason='official_final_coverage_pending',next_at=stamp+300))
    try:pending=_pending()
    except (ValueError,OSError,KeyError,TypeError) as exc:
        row=db.q1("SELECT c.version FROM nfl_spread_candidates c WHERE NOT EXISTS (SELECT 1 FROM nfl_spread_events e WHERE e.version=c.version AND e.kind IN ('activated','rejected','abandoned','rollback')) ORDER BY c.created_at,c.version LIMIT 1")
        if row:
            store.event(row['version'],'abandoned',dict(reason=str(exc)[:250]),now=stamp)
            _terminal_stage(row['version'],'invalidated',dict(reason=str(exc)[:250]),stamp)
        return _record(stamp,'error',dict(stage='invalidated',reason=str(exc)[:250],next_at=stamp+WEEK))
    if pending:return resume_publication(pending['version'],now=stamp)
    previous=store.latest_stage(); state=previous['state'] if previous else None
    if state and state.get('stage') in {'retained','rejected','invalidated','complete'}:
        if stamp<state.get('next_at',previous['created_at']+WEEK):
            return _record(stamp,'retained' if state['stage']=='retained' else 'waiting',state)
        state=None
    if state:
        try:
            if 'incumbent_event_id' in state and state['incumbent_event_id']!=(store.active_event() or {}).get('id'):
                raise ValueError('NFL spread staged incumbent changed')
            if state['code_sha256']!=store.code_catalog() or state['protocol_sha256']!=model.sha(model.protocol()):
                raise ValueError('NFL spread staged code/protocol changed')
            _verify_stage_sources(state)
        except FinalEvidencePending as exc:
            return _record(stamp,'waiting',dict(stage=state.get('stage','fit'),reason=str(exc),next_at=stamp+300))
        except (ValueError,OSError,KeyError,TypeError) as exc:
            invalid=dict(state,stage='invalidated',reason=str(exc),next_at=stamp+WEEK)
            store.append_stage(previous['id'],invalid,now=stamp)
            return _record(stamp,'error',invalid)
    rows,unknown=data.observations(now=stamp,deep=True)
    current_event=store.active_event()
    incumbent=(current_event or {}).get('version') or model.FORMULA_VERSION
    incompatible=sum(r.get('incumbent_version',model.FORMULA_VERSION)!=incumbent for r in rows)
    rows=[r for r in rows if r.get('incumbent_version',model.FORMULA_VERSION)==incumbent]
    groups=_groups(rows,stamp)
    if state is None:
        state=dict(schema=1,stage='fit',protocol_sha256=model.sha(model.protocol()),code_sha256=store.code_catalog(),
                   fit_rows=[],development_rows=[],calibration_rows=[],unverifiable_legacy_snapshots=unknown)
    state=dict(state,unverifiable_legacy_snapshots=unknown,incompatible_incumbent_snapshots=incompatible)
    for phase in ('fit','development','calibration'):
        if state['stage']!=phase:continue
        before=0. if phase=='fit' else state['phase_receipt']
        last_week=tuple(state.get('last_week',[0,0]))
        selected=[]
        for week,items in sorted(groups.items()):
            if week<=last_week or min(r['as_of'] for r in items)<=before:continue
            identity={(r['formula_version'],r['input_policy_id'],r.get('incumbent_version',model.FORMULA_VERSION)) for r in items}
            if len(identity)!=1:continue
            if state.get('input_policy_id') and identity!={(state['formula_version'],state['input_policy_id'],state['incumbent_version'])}:continue
            finals=data.labels(items,now=stamp)
            if set(finals)!={r['game_id'] for r in items}:continue
            selected.extend(dict(r,**finals[r['game_id']]) for r in items)
            if len(selected)>=model.MINIMUMS[phase]:break
        state[phase+'_coverage']=len(selected)
        state[phase+'_rows']=selected
        if len(selected)<model.MINIMUMS[phase]:
            state.update(reason='more_verified_'+phase+'_weeks_required',next_at=stamp+DAY)
            if not previous or state!=previous['state']:
                previous=store.append_stage(previous['id'] if previous else None,state,now=stamp)
            return _record(stamp,'waiting',state)
        receipt=max(max(r['label_received_at'],r['feature_received_at']) for r in selected)
        for r in selected:model.validate_example(r)
        if phase=='fit':
            bank=model.fit_bank(selected,cutoff=receipt)
            state.update(bank=bank,formula_version=bank['formula_version'],input_policy_id=bank['input_policy_id'],incumbent_version=bank['incumbent_version'],incumbent_event_id=(current_event or {}).get('id'),stage='development',fit_cutoff=receipt)
        elif phase=='development':
            member=model.select(state['bank'],selected,previous_receipt=before)
            state['member']=member
            if member['method']=='incumbent':
                state.update(stage='retained',reason='incumbent_selected_in_development',next_at=stamp+WEEK)
            else:state['stage']='calibration'
        else:
            state.update(sd_margin=model.calibrate(state['bank'],state['member'],selected,previous_receipt=before),stage='outer')
        state.update({phase+'_rows':selected},phase_receipt=receipt,last_week=list(max((r['season'],r['week']) for r in selected)))
        previous=store.append_stage(previous['id'] if previous else None,state,now=stamp)
        if state['stage']=='retained':return _record(stamp,'retained',state)
    if state['stage']=='outer':
        occupied=[(r['season'],r['week']) for r in db.q('SELECT season,week FROM nfl_spread_consumed_weeks')]
        last=state['last_week']
        past=[int(e['id'].split('-')[3]) for e in db.q("SELECT id,start_time FROM events WHERE id LIKE ?",(f'nfl-{last[0]}-reg-%',)) if (data.parse_time(e['start_time']) or float('inf'))<=stamp]
        first=max(last[1]+1,max(past,default=0)+1)
        window=outer_window(last[0],first,occupied)
        artifact=dict(kind=model.KIND,member=state['member'],sd_margin=state['sd_margin'],
            training_cutoff=state['phase_receipt'],formula_version=state['formula_version'],input_policy_id=state['input_policy_id'],incumbent_version=state['incumbent_version'],
            protocol=model.protocol(),code_sha256=state['code_sha256'],stage_id=previous['id'],stage_sha256=previous['sha256'],
            incumbent_event_id=state['incumbent_event_id'],outer_season=window['season'],outer_first_week=window['first_week'])
        proposal=store.register(artifact,now=stamp)
        state=dict(state,candidate_version=proposal['version'],reason=window['reason'],outer_season=window['season'],outer_first_week=window['first_week'])
        store.append_stage(previous['id'],state,now=stamp)
        return _record(stamp,'waiting',state)
    return _record(stamp,'waiting',state)


def evaluate(rows):
    """Paired whole-week MAE/conditional-home-cover gates, fixed before labels."""
    for row in rows:model.validate_example(row)
    if len({(r['season']) for r in rows})>1: raise ValueError('NFL spread outer cohort crosses a season')
    if len({r['game_id'] for r in rows})!=len(rows):raise ValueError('NFL spread outer repeated game')
    weeks=sorted({(r['season'],r['week']) for r in rows}); reasons=[]
    if len(rows)<60:reasons.append('fewer_than_sixty_games')
    if len(weeks)<6:reasons.append('fewer_than_six_complete_weeks')
    if not rows:return dict(passed=False,reasons=reasons,games=0,weeks=0,bootstrap_unit='NFL_week')
    for row in rows:
        for k in ('candidate_margin','incumbent_probability','candidate_probability'):model.number(row[k])
        if not 0<=row['incumbent_probability']<=1 or not 0<=row['candidate_probability']<=1:
            raise ValueError('NFL spread outer probability invalid')
    def metrics(sample,key):
        signed=[r[key+'_margin']-r['actual_margin'] for r in sample]
        binary=[r for r in sample if r['actual_margin']+r['home_spread']!=0]
        return dict(mae=statistics.mean(abs(x) for x in signed),rmse=math.sqrt(statistics.mean(x*x for x in signed)),
            absolute_bias=abs(statistics.mean(signed)),coverage=sum(r[key+'_margin']+r['home_spread']!=0 for r in sample),brier=statistics.mean((r[key+'_probability']-float(r['actual_margin']+r['home_spread']>0))**2 for r in binary) if binary else None,
            wins=sum((r[key+'_margin']+r['home_spread']>0)==(r['actual_margin']+r['home_spread']>0) for r in binary
                if r[key+'_margin']+r['home_spread']!=0))
    before,after=metrics(rows,'incumbent'),metrics(rows,'candidate')
    benchmark=statistics.mean(abs(-r['home_spread']-r['actual_margin']) for r in rows)
    if before['mae']-after['mae']<max(.25,.02*before['mae']):reasons.append('mae_improvement_too_small')
    if before['brier'] is None or after['brier'] is None or before['brier']-after['brier']<.002:reasons.append('brier_improvement_too_small')
    for key in ('rmse','absolute_bias'):
        if after[key]>before[key]:reasons.append(key+'_regressed')
    if after['wins']<before['wins']:reasons.append('decided_wins_regressed')
    if after['coverage']<before['coverage']:reasons.append('coverage_regressed')
    if after['mae']>benchmark:reasons.append('saved_line_benchmark_not_met')
    intervals={}
    if len(weeks)>=6:
        import numpy as np
        rng=np.random.default_rng(20261109); blocks=[[r for r in rows if (r['season'],r['week'])==w] for w in weeks]
        differences={'mae':[],'brier':[]}
        for _ in range(2000):
            sample=[r for i in rng.integers(0,len(blocks),len(blocks)) for r in blocks[i]]
            a,b=metrics(sample,'incumbent'),metrics(sample,'candidate')
            differences['mae'].append(b['mae']-a['mae'])
            if a['brier'] is not None:differences['brier'].append(b['brier']-a['brier'])
        for key in ('mae','brier'):
            if not differences[key]:reasons.append('insufficient_decided_week_blocks');continue
            lo,hi=np.quantile(differences[key],[.025,.975]).tolist()
            intervals[key]=[lo,hi]
            if hi>=0:reasons.append(key+'_interval_not_improved')
    return dict(passed=not reasons,reasons=reasons,games=len(rows),weeks=len(weeks),
        season=rows[0]['season'],incumbent=before,candidate=after,saved_line_mae=benchmark,
        intervals=intervals,bootstrap_unit='NFL_week')


def _selected_freezes(version,now):
    rows=db.q('SELECT freeze_id,payload FROM nfl_spread_freezes WHERE version=? ORDER BY created_at,freeze_id',(version,))
    latest={}
    for raw in rows:
        frozen=store.verify_freeze(raw['freeze_id']); item=frozen['payload']
        if item['kickoff']>=now:continue
        key=(item['as_of'],item['snapshot_id'])
        old=latest.get(item['game_id'])
        if old is None or key>(old['payload']['as_of'],old['payload']['snapshot_id']):latest[item['game_id']]=frozen
    return list(latest.values())


def _same_counted_snapshot(item):
    current=db.q1('SELECT p.snapshot_id FROM nfl_game_predictions p JOIN nfl_game_snapshots s ON s.id=p.snapshot_id '
        'WHERE p.game_id=? AND p.created_at<? ORDER BY p.created_at DESC,p.id DESC LIMIT 1',(item['game_id'],item['kickoff']))
    if not current or current['snapshot_id']!=item['snapshot_id']:
        raise ValueError('NFL spread freeze differs from counted incumbent revision')



def _source_paths(items):
    from . import nfl_game_sources
    paths=set();seen=set()
    for item in items:
        saved=db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(item['snapshot_id'],))
        if not saved or __import__('hashlib').sha256(saved['payload'].encode()).hexdigest()!=item['snapshot_sha256']:
            raise ValueError('NFL spread evidence snapshot hash changed')
        mapping=data.decode(saved['payload']).get('game_input_map') or {}
        if mapping.get('sha256')!=item['source_sha256']:raise ValueError('NFL spread evidence source identity changed')
        ident=mapping.get('source_id')
        if ident in seen:continue
        seen.add(ident)
        paths.update(nfl_game_sources.input_paths(mapping))
    paths.update(data.schedule_paths({item['season'] for item in items}))
    return sorted(paths,key=str)


def _evidence_items(artifact,reserved):
    stage=db.q1('SELECT state FROM nfl_spread_stages WHERE id=?',(artifact['stage_id'],))
    if not stage:raise ValueError('NFL spread active inner stage missing')
    state=data.decode(stage['state'])
    inner=[r for name in model.MINIMUMS for r in state.get(name+'_rows',[])]
    outer=[store.verify_freeze(f['freeze_id'])['payload'] for f in reserved['payload']['freezes']]
    return inner+outer


def _evidence_state(artifact,reserved):
    items=_evidence_items(artifact,reserved)
    state=_sql_state(items);state.pop('active',None)
    # Stat checks only; no annual archive is read or replayed by product paths.
    paths=_source_paths(items)
    parent=store.settings.db_path.parent
    state['files']=[(str(path.relative_to(parent)),)+tuple(seal[1:]) for path,seal in zip(paths,publication.file_seals(paths))]
    return state

def _sql_state(items):
    """Only cheap heads, no source body replay, while the publication lock is held."""
    from urllib.parse import urlsplit,parse_qsl
    weeks={(r['season'],r['week']) for r in items}
    heads=[]
    for row in db.q('SELECT sha256,source_file,captured_at FROM nfl_prop_grade_source_artifacts '
                   'WHERE source_file LIKE ? ORDER BY captured_at,sha256',(data.SCOREBOARD+'%',)):
        query=dict(parse_qsl(urlsplit(row['source_file']).query))
        try:key=(int(query.get('dates','0')),int(query.get('week','0')))
        except ValueError:continue
        if key in weeks:heads.append((row['sha256'],row['captured_at']))
    return dict(active=store.active_event(),result_heads=heads,
        schedule_heads=[data.schedule_head(season) for season in sorted({s for s,w in weeks})],
        events=[db.q('SELECT id,start_time,status,home_team,away_team,meta FROM events WHERE id LIKE ? ORDER BY id',
            (f'nfl-{season}-reg-{week:02d}-%',)) for season,week in sorted(weeks)],
        predictions=[db.q1('SELECT id,snapshot_id,created_at FROM nfl_game_predictions WHERE game_id=? AND created_at<? '
            'ORDER BY created_at DESC,id DESC LIMIT 1',(r['game_id'],r['kickoff'])) for r in items])


def _verified_reserved_rows(proposal,reserved,stamp):
    body=reserved['payload']
    if body['candidate_sha256']!=proposal['sha256']:raise ValueError('NFL spread reservation candidate mismatch')
    freezes=[store.verify_freeze(r['freeze_id']) for r in body['freezes']]
    if any(f['sha256']!=proof['sha256'] for f,proof in zip(freezes,body['freezes'])):
        raise ValueError('NFL spread reserved freeze changed')
    if not _whole_week_evidence([f['payload'] for f in freezes],stamp):
        raise FinalEvidencePending('verified_whole_week_schedule_required')
    observations=[]; cache={}
    for frozen in freezes:
        item=frozen['payload']; _same_counted_snapshot(item)
        verified=data.observation(item['snapshot_id'],now=stamp,deep=True,proof_cache=cache)
        if verified is None or any(verified.get(k)!=item.get(k) for k in verified):
            raise ValueError('NFL spread reserved source/input changed')
        expected=model.predict(proposal['artifact']['member'],verified)
        probability=model.probability(expected,proposal['artifact']['sd_margin'],item['home_spread'])
        if item['candidate_margin']!=expected or item['candidate_probability']!=probability:
            raise ValueError('NFL spread reserved prediction replay conflict')
        observations.append(item)
    finals=data.labels(observations,now=stamp)  # Only after a durable reservation.
    if set(finals)!=set(body['game_ids']):raise FinalEvidencePending('official_final_coverage_pending')
    return [dict(item,**finals[item['game_id']]) for item in observations]


def _terminal_stage(version,stage,detail,stamp):
    previous=store.latest_stage()
    if previous and previous['state'].get('candidate_version')==version:
        state=dict(previous['state'],stage=stage,next_at=stamp+WEEK,**detail)
        store.append_stage(previous['id'],state,now=stamp)


@data.with_schedule_proofs
def resume_publication(version,now=None):
    stamp=_now(now); proposal=store.candidate(version,current_code=True); artifact=proposal['artifact']
    terminal=db.q1("SELECT kind,detail FROM nfl_spread_events WHERE version=? AND kind IN ('activated','rejected','abandoned') ORDER BY id DESC LIMIT 1",(version,))
    if terminal:
        return _record(stamp,'activated' if terminal['kind']=='activated' else 'rejected' if terminal['kind']=='rejected' else 'error',
            dict(candidate_version=version,stage='complete' if terminal['kind']=='activated' else 'rejected',reason='sealed_decision_already_recorded',next_at=stamp+WEEK))
    try:
        stage=db.q1('SELECT * FROM nfl_spread_stages WHERE id=?',(artifact['stage_id'],))
        if not stage or stage['sha256']!=artifact['stage_sha256'] or model.sha(data.decode(stage['state']))!=stage['sha256']:
            raise ValueError('NFL spread candidate stage proof conflict')
        inner_state=data.decode(stage['state'])
        inner_items=[r for name in model.MINIMUMS for r in inner_state.get(name+'_rows',[])]
        inner_heads=_sql_state(inner_items)
        inner_paths=_source_paths(inner_items);inner_files=publication.file_seals(inner_paths)
        _verify_stage_sources(inner_state)
        _verify_training_stage(inner_state,artifact)
        publication.same(inner_heads,_sql_state(inner_items));publication.same(inner_files,publication.file_seals(inner_paths))
        event=store.active_event()
        if (event or {}).get('id')!=artifact.get('incumbent_event_id'):
            raise ValueError('NFL spread compared incumbent changed')
        existing=db.q1('SELECT reservation_id FROM nfl_spread_reservations WHERE version=?',(version,))
        if existing:
            reserved=store.reservation(existing['reservation_id'])
        else:
            chosen=_selected_freezes(version,stamp); groups=defaultdict(list)
            data.prepare_schedule_proofs({f['season'] for f in chosen},stamp)
            for frozen in chosen:groups[(frozen['season'],frozen['week'])].append(frozen)
            complete=[]; universe={}
            for week,items in sorted(groups.items()):
                if week[0]!=artifact['outer_season'] or not data.complete_week(*week,stamp):continue
                if db.q1('SELECT 1 FROM nfl_spread_consumed_weeks WHERE season=? AND week=?',week):continue
                games=[e['id'] for e in data.week_universe(*week)]
                if set(games)!={f['game_id'] for f in items}:continue
                for frozen in items:_same_counted_snapshot(frozen['payload'])
                universe[week]=games; complete.extend(items)
                if len(complete)>=60 and len(universe)>=6:break
            if len(complete)<60 or len(universe)<6:
                return _record(stamp,'waiting',dict(stage='outer',reason='fresh_complete_weeks_required',candidate_version=version,
                    coverage=dict(outer_games=len(complete),outer_weeks=len(universe)),outer_season=artifact['outer_season']))
            reserved=store.reserve(version,complete,universe,now=stamp)
        # Snapshot mutable heads BEFORE slow source/label replay. Capturing them
        # afterward would bless a correction that arrived during verification.
        before_items=[store.verify_freeze(r['freeze_id'])['payload'] for r in reserved['payload']['freezes']]
        before_items=inner_items+before_items
        dependencies=_sql_state(before_items)
        source_paths=_source_paths(before_items);source_files=publication.file_seals(source_paths)
        publication.same(inner_files,publication.file_seals(inner_paths))
        verification_stamp=_now(stamp)
        if any(received>verification_stamp for _,received in dependencies['result_heads']):
            raise publication.PublicationDeferred('final_receipt_clock_ahead')
        rows=_verified_reserved_rows(proposal,reserved,verification_stamp)
        publication.same(dependencies,_sql_state(before_items));publication.same(source_files,publication.file_seals(source_paths))
        evaluation=evaluate(rows)
        seal_state={r['game_id']:{k:r[k] for k in ('snapshot_sha256','source_sha256','label_sha256','label_received_at')} for r in rows}
        prior=db.q1('SELECT * FROM nfl_spread_evaluations WHERE reservation_id=?',(reserved['reservation_id'],))
        if prior:
            decoded=data.decode(prior['payload'])
            if store.sha(decoded)!=prior['sha256'] or decoded['state']!=seal_state or any(decoded.get(k)!=v for k,v in evaluation.items()):
                raise ValueError('NFL spread evaluation/result head changed after sealing')
            evaluated=prior
        else:evaluated=store.record_evaluation(reserved,evaluation,seal_state,now=stamp)
        if not evaluation['passed']:
            store.event(version,'rejected',dict(evaluation_sha256=evaluated['sha256'],reasons=evaluation['reasons']),now=stamp)
            _terminal_stage(version,'rejected',dict(reason='prospective_gates_not_met'),stamp)
            return _record(stamp,'rejected',dict(stage='rejected',candidate_version=version,reasons=evaluation['reasons'],next_at=stamp+WEEK))
        if (dependencies['active'] or {}).get('id')!=artifact.get('incumbent_event_id'):
            raise ValueError('NFL spread incumbent changed before publication')
        catalog=store.code_catalog()
        if artifact['code_sha256']!=catalog:raise ValueError('NFL spread candidate code changed before publication')
        paths=[store.safe_path(store.ROOT,name) for name in catalog]; files=publication.file_seals(paths)
        with publication.publish_lock():
            publication.same(files,publication.file_seals(paths)); publication.same(dependencies,_sql_state(before_items));publication.same(source_files,publication.file_seals(source_paths))
            proof=dict(candidate_sha256=proposal['sha256'],reservation_sha256=reserved['sha256'],
                evaluation_sha256=evaluated['sha256'],code_sha256=catalog,state_sha256=store.sha(dependencies),
                evidence_state_sha256=store.sha(_evidence_state(artifact,reserved)))
            with db.tx() as con:
                con.execute('INSERT INTO nfl_spread_proofs VALUES(?,?,?,?,?)',
                    (version,reserved['reservation_id'],stamp,store.encoded(proof),store.sha(proof)))
                con.execute('INSERT INTO nfl_spread_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
                    (version,stamp,'activated',store.encoded(dict(proof_sha256=store.sha(proof)))))
        _terminal_stage(version,'complete',dict(reason='prospective_gates_passed'),stamp)
        return _record(stamp,'activated',dict(stage='complete',candidate_version=version,evaluation=evaluation,next_at=stamp+WEEK))
    except FinalEvidencePending as exc:
        return _record(stamp,'waiting',dict(stage='outer',candidate_version=version,reason=str(exc),next_at=stamp+300))
    except publication.PublicationDeferred as exc:
        return _record(stamp,'deferred',dict(stage='outer',candidate_version=version,reason=str(exc),next_at=stamp+300))
    except Exception as exc:
        from .learning_worker import LearningDeadlineExceeded
        if isinstance(exc,(LearningDeadlineExceeded,TimeoutError)):raise
        # A consumed holdout stays consumed even if verification/evaluation failed.
        store.event(version,'abandoned',dict(reason=str(exc)[:250]),now=stamp)
        _terminal_stage(version,'invalidated',dict(reason=str(exc)[:250]),stamp)
        return _record(stamp,'error',dict(stage='invalidated',candidate_version=version,reason=str(exc)[:250],next_at=stamp+WEEK))


def refresh_checks(now=None):
    """Idempotent automatic dispatch only; network and fitting are forbidden here."""
    stamp=_now(now); store.ensure_schema()
    last=db.q1('SELECT * FROM nfl_spread_runs ORDER BY id DESC LIMIT 1')
    detail=data.decode(last['detail']) if last else {}
    due=detail.get('next_at',(last['created_at']+DAY) if last else 0.)
    needs=False
    try:
        current=active()
        needs=bool(current and not current.get('evidence_current',True))
    except (ValueError,OSError,KeyError,TypeError):needs=True
    if not needs and stamp<due:
        return dict(ok=True,status='cached',next_at=due,designation='PASS',readiness='RESEARCH_ONLY')
    from . import learning_worker
    job=learning_worker.enqueue('NFL_SPREAD','retuning',now=stamp)
    return dict(ok=True,status=job['state'],job_id=job['id'],designation='PASS',readiness='RESEARCH_ONLY')


def status(now=None):
    stamp=_now(now)
    if not store.schema_ready():
        return dict(enabled=True,component='NFL_SPREAD',active=None,status='not_checked',
            message='NFL spread learning has not been checked by scheduled maintenance yet.',
            detail=dict(stage='fit',reason='not_checked',coverage=dict(fit=0,development=0,calibration=0,validation=0,validation_weeks=0),
                required=dict(model.MINIMUMS,outer_games=60,outer_weeks=6,validation=60)),
            checked_at=None,last_check_at=None,next_due_at=None,
            all_supported_games_selected=True,designation='PASS',readiness='RESEARCH_ONLY')
    run=db.q1('SELECT * FROM nfl_spread_runs ORDER BY id DESC LIMIT 1')
    stage=store.latest_stage(); detail=data.decode(run['detail']) if run else {}
    if stage and not detail:detail=dict(stage['state'])
    required=dict(model.MINIMUMS,outer_games=60,outer_weeks=6,validation=60)
    detail=dict(detail,required=required)
    detail.setdefault('stage',(stage or {}).get('state',{}).get('stage','fit'))
    coverage=detail.get('coverage') or {k:detail.get(k+'_coverage',len(detail.get(k+'_rows',[]))) for k in model.MINIMUMS}
    # Counts describe complete supported weeks using statuses only; no label
    # bodies are read by About & data or any other product page.
    coverage=dict(coverage,validation=coverage.get('outer_games',coverage.get('validation',0)),validation_weeks=coverage.get('outer_weeks',coverage.get('validation_weeks',0)))
    detail['coverage']=coverage
    try:
        current=active(); error='NFL spread accepted evidence needs offline review; supported forecasts use the baseline.' if current and not current.get('evidence_current',True) else None
    except (ValueError,OSError,KeyError,TypeError) as exc:current=None; error=str(exc)
    # Internal rows/coefficients are not copied into the product status payload.
    visible={k:v for k,v in detail.items() if k not in {'fit_rows','development_rows','calibration_rows','bank','code_sha256','member'}}
    return dict(enabled=True,component='NFL_SPREAD',active={k:current[k] for k in ('version','sha256','activated_at')} if current else None,
        status='error' if error else run['status'] if run else 'waiting',
        message=error or str(detail.get('reason','More verified NFL game evidence is required.')).replace('_',' '),
        detail=visible,checked_at=run['created_at'] if run else None,last_check_at=run['created_at'] if run else None,next_due_at=detail.get('next_at'),
        all_supported_games_selected=True,designation='PASS',readiness='RESEARCH_ONLY')
