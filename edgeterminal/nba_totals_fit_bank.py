"""Pure fixed-family fitting stages; no I/O, source discovery or activation.

The arithmetic intentionally reuses the incumbent totals transforms.  A bank
freezes fit-only coefficients; later stages only select a member and estimate
its residual uncertainty.  Receipt/date membership never depends on accuracy.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import statistics

from . import nba_totals_model as model

CONTRACT = 'nba-totals-receipt-stages-2'
CANDIDATE_BASIS = model.MODEL_BASE
COMPARISON_BASIS = 'original_saved_served_incumbent_total'
FLOORS = {'fit': 660, 'development': 120, 'calibration': 120}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _seal(value):
    return dict(value, sha256=sha(value))


def _verified(value):
    if not isinstance(value, dict) or value.get('sha256') != sha({k:v for k,v in value.items() if k!='sha256'}):
        raise ValueError('NBA totals stage seal mismatch')
    return value


def raw_basis(row):
    """Candidate arithmetic uses the raw pre-totals base, never its incumbent."""
    return model._finite(row.get('model_base_total',row['baseline_total']))


def _validated(rows, cutoff):
    result=[]; seen=set()
    for supplied in rows:
        row=deepcopy(supplied)
        gid=str(row.get('game_id',''))
        if not gid or gid in seen:
            raise ValueError('NBA totals duplicate or missing stage game identity')
        seen.add(gid)
        tip=model._time(row.get('ts', row.get('tipoff')))
        asof=model._time(row.get('as_of', row.get('forecast_at')))
        label=model._time(row.get('label_received_at', row.get('result_at')))
        actual=model._finite(row['actual_total']); baseline=raw_basis(row)
        model._finite(row['baseline_total'])  # Retained comparison, not candidate input.
        if not asof<tip<=label<=cutoff or not 2<=actual<=500:
            raise ValueError('NBA totals stage label receipt/cutoff conflict')
        ctx=row['context']
        if (ctx.get('status')!='ok' or ctx.get('as_of')!=asof or ctx.get('tipoff')!=tip
                or ctx.get('baseline_total')!=baseline):
            raise ValueError('NBA totals stage context identity conflict')
        if any(str(r.get('game_id'))==gid for r in ctx.get('selected_rows',[])):
            raise ValueError('NBA totals stage target appears in its features')
        receipt=model.feature_evidence_received_at(ctx)
        if receipt>cutoff:
            raise ValueError('NBA totals stage feature receipt exceeds cutoff')
        model._totals(ctx)
        row.update(ts=tip,as_of=asof,label_received_at=label,feature_evidence_received_at=receipt,
                   model_base_total=baseline,
                   day=datetime.fromtimestamp(tip,model.ET).date().isoformat())
        result.append(row)
    return sorted(result,key=lambda r:(r['ts'],str(r['game_id'])))


def fit_prefix(rows, *, min_fit=660, after_day=None):
    """Earliest sufficient whole-date prefix, selected without reading labels."""
    groups=defaultdict(list)
    for row in rows:
        tip=model._time(row.get('ts',row.get('tipoff')))
        groups[datetime.fromtimestamp(tip,model.ET).date().isoformat()].append(row)
    result=[]
    for day in sorted(groups):
        result.extend(sorted(groups[day],key=lambda r:str(r['game_id'])))
        if ((after_day is None or day>after_day) and len(result)>=min_fit
                and len({model._season(model._time(r.get('ts',r.get('tipoff')))) for r in result})>=2):
            return result
    return []


def family():
    return ([dict(method='incumbent',shrinkage=10.,recent_weight=.25),
             dict(method='intercept',shrinkage=10.,recent_weight=.25)]
            +[dict(method=m,shrinkage=s,recent_weight=r) for m in ('season','pace')
              for s in model.SHRINKAGES for r in model.RECENT_WEIGHTS])


def _summary(rows):
    return dict(rows=len(rows),dates=sorted({r['day'] for r in rows}),
                receipt_max=max(r['label_received_at'] for r in rows),
                feature_receipt_max=max(r['feature_evidence_received_at'] for r in rows),
                asof_min=min(r['as_of'] for r in rows),game_ids=sorted(str(r['game_id']) for r in rows))


def fit_bank(rows, cutoff, *, min_fit=660):
    cutoff=model._time(cutoff); rows=_validated(rows,cutoff)
    if len(rows)<min_fit:
        raise ValueError('Insufficient NBA totals fit history')
    if len({model._season(r['ts']) for r in rows})<2:
        raise ValueError('NBA totals fitting needs multiple seasons')
    pace=any(r['context']['coverage']['pace_supported'] and r['context']['coverage']['opponent_supported'] for r in rows)
    members=[]
    for index,params in enumerate(family()):
        intercept=(0. if params['method']=='incumbent' else statistics.mean(
            r['actual_total']-model._candidate_mean(params,r['context'],raw_basis(r))[0] for r in rows))
        members.append(dict(index=index,params=params,intercept=intercept,
                            eligible=params['method']!='pace' or pace))
    summary=_summary(rows)
    return _seal(dict(contract=CONTRACT,candidate_basis=CANDIDATE_BASIS,comparison_basis=COMPARISON_BASIS,
                      cutoff=cutoff,members=members,fit_rows=len(rows),fit=summary,
                      receipt_max=max(summary['receipt_max'],summary['feature_receipt_max']),
                      last_day=max(summary['dates']),example_sha256=sha(rows)))


def verify_bank(bank):
    _verified(bank)
    if (bank.get('contract')!=CONTRACT or len(bank.get('members',[]))!=20
            or bank.get('candidate_basis')!=CANDIDATE_BASIS
            or bank.get('comparison_basis')!=COMPARISON_BASIS):
        raise ValueError('NBA totals bank contract mismatch')
    if [m['params'] for m in bank['members']]!=family():
        raise ValueError('NBA totals bank finite family mismatch')
    return bank


def inner_eligible(row):
    """New development/calibration require own-asof inputs and a saved baseline."""
    digest=row.get('forecast_sha256')
    if (not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)
            or row.get('availability',row.get('context',{}).get('availability'))!='received_by_asof'):
        return False
    ctx=row.get('context') or {}
    asof=model._time(row.get('as_of',row.get('forecast_at')))
    return (ctx.get('status')=='ok' and not ctx.get('reconstructed')
            and ctx.get('baseline_total')==raw_basis(row)
            and model.feature_evidence_received_at(ctx)<=asof)


def select_whole_dates(rows, previous, *, minimum=120):
    """Select only complete caller-verified dates after a frozen phase boundary."""
    groups=defaultdict(list)
    for row in rows:
        day=datetime.fromtimestamp(model._time(row.get('ts',row.get('tipoff'))),model.ET).date().isoformat()
        groups[day].append(row)
    result=[]
    receipt=max(previous['receipt_max'],previous.get('feature_receipt_max',0))
    last=max(previous['dates'])
    for day in sorted(groups):
        group=groups[day]
        if day<=last or any(not inner_eligible(r) or model._time(r.get('as_of',r.get('forecast_at')))<=receipt for r in group):
            continue
        result.extend(group)
        if len(result)>=minimum:
            break
    return result


def predictions(bank, context, baseline):
    verify_bank(bank)
    if context.get('baseline_total')!=model._finite(baseline):
        raise ValueError('NBA totals candidate raw-basis context conflict')
    return [model._finite(model._candidate_mean(m['params'],context,baseline)[0]+m['intercept']) for m in bank['members']]


def select_member(bank, rows, *, min_games=120):
    verify_bank(bank)
    cutoff=max((model._time(r.get('label_received_at',r.get('result_at'))) for r in rows),default=bank['cutoff'])
    rows=_validated(rows,cutoff)
    if len(rows)<min_games:
        raise ValueError('Insufficient NBA totals development history')
    if any(r['as_of']<=bank['receipt_max'] or r['day']<=bank['last_day'] for r in rows):
        raise ValueError('NBA totals development chronology conflict')
    scores=[]
    for m in bank['members']:
        if m['eligible']:
            error=statistics.mean(abs(r['actual_total']-predictions(bank,r['context'],raw_basis(r))[m['index']]) for r in rows)
            scores.append((error,m['index']))
    error,index=min(scores)
    return _seal(dict(contract=CONTRACT,bank_sha256=bank['sha256'],member=bank['members'][index],
                      development=_summary(rows),mae=error,example_sha256=sha(rows),
                      identical_served_incumbent=bank['members'][index]['params']['method']=='incumbent'
                      and all(raw_basis(r)==r['baseline_total'] for r in rows)))


def calibrate(bank, selected, rows, cutoff, *, min_games=120):
    verify_bank(bank); _verified(selected)
    if selected['bank_sha256']!=bank['sha256']:
        raise ValueError('NBA totals selected member bank seal mismatch')
    rows=_validated(rows,model._time(cutoff))
    if len(rows)<min_games:
        raise ValueError('Insufficient NBA totals calibration history')
    previous=selected['development']
    if any(r['as_of']<=max(previous['receipt_max'],previous['feature_receipt_max']) or r['day']<=max(previous['dates']) for r in rows):
        raise ValueError('NBA totals calibration chronology conflict')
    member=selected['member']
    errors=[r['actual_total']-predictions(bank,r['context'],raw_basis(r))[member['index']] for r in rows]
    phases=dict(fit=bank['fit'],development=previous,calibration=_summary(rows))
    artifact=dict(schema=1,kind=model.KIND,feature_names=list(model.FEATURE_NAMES),training_cutoff=model._time(cutoff),
                  params=member['params'],intercept=member['intercept'],sd_total=max(1.,min(100.,math.sqrt(statistics.mean(e*e for e in errors)))),
                  phase_dates={k:v['dates'] for k,v in phases.items()},phase_rows={k:v['rows'] for k,v in phases.items()},
                  phase_receipt_max={k:v['receipt_max'] for k,v in phases.items()},
                  phase_feature_receipt_max={k:v['feature_receipt_max'] for k,v in phases.items()},
                  phase_asof_min={k:v['asof_min'] for k,v in phases.items()},
                  preprocessing=model.PREPROCESSING,probability=model.PROBABILITY,
                  family='incumbent-intercept-season-pace-20-fixed-candidates-v1',model_base=model.MODEL_BASE)
    artifact['version']=model._version(artifact)
    return model.validate_artifact(artifact)
