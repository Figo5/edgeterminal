"""Pure, chronological market anchoring of sourced NBA game forecasts.

Market inputs are the exact contemporaneous ESPN line, never a later closing
price. Positive convex anchoring changes point error and uncertainty; by itself
it preserves the feature model's side against that same line.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
import statistics
from zoneinfo import ZoneInfo

from .pricing import norm_cdf

MIN_FIT=40
MIN_DEVELOPMENT=20
MIN_CALIBRATION=20
MIN_VALIDATION=60
MIN_VALIDATION_DAYS=7
PENALTIES=(0.,25.,100.)
ET=ZoneInfo('America/New_York')


def _finite(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('NBA market input must be finite')
    return float(value)


def quote_context(quote,as_of,tipoff):
    """Validate evidence identifiers/times; the snapshot writer checks the bytes."""
    as_of,tipoff=_finite(as_of),_finite(tipoff)
    if not as_of<tipoff:raise ValueError('NBA market as-of must precede tipoff')
    if not quote or quote.get('home_spread') is None:return None
    line=_finite(quote['home_spread'])
    if abs(line)>100:raise ValueError('NBA market spread is outside the supported range')
    away=quote.get('away_spread')
    if away is not None and _finite(away)!=-line:raise ValueError('NBA market spreads must be opposite')
    observed=_finite(quote.get('observed_at'))
    ident=quote.get('source_artifact_id');sha=quote.get('source_sha256')
    if (not isinstance(ident,int) or isinstance(ident,bool) or ident<=0
            or not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha)
            or not observed<=as_of<tipoff):
        raise ValueError('NBA market quote lacks pretipoff source evidence')
    return dict(margin=-line,home_spread=line,observed_at=observed,
                source_artifact_id=ident,source_sha256=sha)


def blend(feature_margin,market_margin,weight):
    feature_margin,market_margin,weight=map(_finite,(feature_margin,market_margin,weight))
    if not 0<weight<=1:raise ValueError('NBA market anchor requires a positive convex model weight')
    return market_margin+weight*(feature_margin-market_margin)


def cover_probabilities(margin,sd,line):
    """Integrate integer home margins and keep pushes separate from either side."""
    margin,sd,line=map(_finite,(margin,sd,line))
    if sd<=0:raise ValueError('NBA market uncertainty must be positive')
    if abs(line-round(line))<1e-9:
        home=norm_cdf((margin+line-.5)/sd)
        away=norm_cdf((-line-.5-margin)/sd)
        push=max(0.,1-home-away)
    else:
        home=norm_cdf((margin+line)/sd);away=1-home;push=0.
    binary=home+away
    return dict(home=home,away=away,push=push,conditional_home=home/binary if binary else .5)


def _valid_record(row,now,*,needs_core=False):
    as_of,tip,label=map(_finite,(row['as_of'],row['tipoff_ts'],row['result_observed_at']))
    if not as_of<tip<label:raise ValueError('NBA market record chronology is invalid')
    _finite(row['actual_margin'])
    if needs_core:_finite(row['core_margin'])
    context=quote_context(row.get('market'),as_of,tip)
    return label<=now and context is not None


def _take_tail(groups,min_games,min_days=1):
    chosen=[];count=0
    while groups and (count<min_games or len(chosen)<min_days):
        group=groups.pop();chosen.append(group);count+=len(group)
    return [row for group in reversed(chosen) for row in group]


def partition(records,*,now,excluded_validation_game_ids=(),excluded_validation_dates=()):
    """Reserve entire eligible Eastern-date groups; purge late prior labels.

    Every fitting/development/calibration result must have been received before
    the earliest next-phase forecast. Sorting game tips alone cannot prove that.
    """
    now=_finite(now);ids=set();by_day=defaultdict(list)
    for row in records:
        if row['game_id'] in ids:raise ValueError('NBA market cohorts repeat a game')
        ids.add(row['game_id'])
        if not _valid_record(row,now):continue
        day=datetime.fromtimestamp(row['tipoff_ts'],ET).date().isoformat()
        by_day[day].append(row)
    groups=[sorted(by_day[day],key=lambda r:(r['as_of'],r['game_id'])) for day in sorted(by_day)]
    excluded_ids=set(excluded_validation_game_ids);excluded_days=set(excluded_validation_dates)
    fresh_groups=[]
    for day in sorted(by_day):
        if day in excluded_days:continue
        group=[r for r in by_day[day] if r['game_id'] not in excluded_ids]
        if group:fresh_groups.append(sorted(group,key=lambda r:(r['as_of'],r['game_id'])))
    validation=_take_tail(fresh_groups,MIN_VALIDATION,MIN_VALIDATION_DAYS)
    first_day=min((datetime.fromtimestamp(r['tipoff_ts'],ET).date().isoformat() for r in validation),default='')
    groups=[group for group in groups if datetime.fromtimestamp(group[0]['tipoff_ts'],ET).date().isoformat()<first_day]
    cohorts=dict(validation=validation);purged=[]
    for name,minimum in (('calibration',MIN_CALIBRATION),('development',MIN_DEVELOPMENT)):
        later=cohorts['validation' if name=='calibration' else 'calibration']
        boundary=min((r['as_of'] for r in later),default=-math.inf)
        clean=[]
        for group in groups:
            kept=[r for r in group if r['result_observed_at']<boundary]
            purged.extend(r['game_id'] for r in group if r['result_observed_at']>=boundary)
            if kept:clean.append(kept)
        groups=clean;cohorts[name]=_take_tail(groups,minimum)
    boundary=min((r['as_of'] for r in cohorts['development']),default=-math.inf)
    fit=[r for group in groups for r in group if r['result_observed_at']<boundary]
    purged.extend(r['game_id'] for group in groups for r in group if r['result_observed_at']>=boundary)
    cohorts['fit']=fit
    requirements=dict(fit=MIN_FIT,development=MIN_DEVELOPMENT,calibration=MIN_CALIBRATION,validation=MIN_VALIDATION)
    counts={key:len(cohorts[key]) for key in requirements}
    days=len({datetime.fromtimestamp(r['tipoff_ts'],ET).date() for r in validation})
    ready=all(counts[key]>=minimum for key,minimum in requirements.items()) and days>=MIN_VALIDATION_DAYS
    result=dict(status='ready' if ready else 'waiting',cohorts=cohorts,counts=counts,
        required=requirements,validation_days=days,purged_game_ids=sorted(set(purged)),
        reason=None if ready else 'more_chronological_market_anchor_games')
    if ready:
        result.update(core_training_cutoff=min(r['as_of'] for r in fit)-1.,
                      training_cutoff=min(r['as_of'] for r in validation)-1.)
    return result


def fit_anchor(fit,development,calibration):
    """Fit positive weights on fit games; choose on development; calibrate later."""
    phases=(fit,development,calibration)
    if any(len(rows)<minimum for rows,minimum in zip(phases,(MIN_FIT,MIN_DEVELOPMENT,MIN_CALIBRATION))):
        raise ValueError('Insufficient NBA market anchor cohorts')
    ids=[{r['game_id'] for r in rows} for rows in phases]
    if sum(map(len,ids))!=len(set.union(*ids)):raise ValueError('NBA market anchor cohorts overlap')
    for rows in phases:
        for row in rows:_valid_record(row,math.inf,needs_core=True)
    for before,after in zip(phases,phases[1:]):
        if max(r['result_observed_at'] for r in before)>=min(r['as_of'] for r in after):
            raise ValueError('NBA market anchor phase chronology is invalid')
    # Ridge prior is the identity (weight=1). Non-positive estimated weights
    # are not serving candidates: the market-only forecast is a reference with
    # no direction against its own line, not a reason to invent all-home picks.
    xx=sum((r['core_margin']+r['market']['home_spread'])**2 for r in fit)
    xy=sum((r['core_margin']+r['market']['home_spread'])*(r['actual_margin']+r['market']['home_spread']) for r in fit)
    candidates=[dict(weight=1.,penalty=None)]
    for penalty in PENALTIES:
        weight=min(1.,(xy+penalty)/(xx+penalty)) if xx+penalty else 1.
        if weight>0:candidates.append(dict(weight=weight,penalty=penalty))
    for candidate in candidates:
        candidate['development_mae']=statistics.mean(abs(blend(r['core_margin'],-r['market']['home_spread'],candidate['weight'])-r['actual_margin']) for r in development)
    chosen=min(candidates,key=lambda r:(r['development_mae'],-r['weight'],r['penalty'] if r['penalty'] is not None else -1.))
    residuals=[r['actual_margin']-blend(r['core_margin'],-r['market']['home_spread'],chosen['weight']) for r in calibration]
    sd=max(1.,min(100.,statistics.stdev(residuals)))
    return dict(kind='positive_convex_market_anchor',weight=chosen['weight'],penalty=chosen['penalty'],
        calibration_sd=sd,development=candidates,
        fit_game_ids=[r['game_id'] for r in fit],development_game_ids=[r['game_id'] for r in development],
        calibration_game_ids=[r['game_id'] for r in calibration],
        latest_label_at=max(r['result_observed_at'] for r in calibration),
        market_basis='exact_saved_ESPN_home_spread',ats_effect='preserves_feature_model_side_against_same_line')


def benchmark(rows):
    """Compare the four forecasts on the same actual lines; market has no ATS pick."""
    result={}
    for key in ('baseline','feature_only','market_only','candidate'):
        subset=[r for r in rows if key+'_margin' in r]
        if len(subset)!=len(rows):raise ValueError('NBA spread benchmark coverage differs')
        errors=[r[key+'_margin']-r['actual_margin'] for r in subset]
        binary=[r for r in subset if r['actual_margin']+r['home_spread']!=0]
        item=dict(n=len(subset),margin_mae=statistics.mean(abs(v) for v in errors),
            margin_rmse=math.sqrt(statistics.mean(v*v for v in errors)),
            cover_brier=statistics.mean((r[key+'_probability']-(r['actual_margin']+r['home_spread']>0))**2 for r in binary) if binary else None)
        if key=='market_only':item.update(ats_wins=None,ats_note='No direction versus the same line; not a claimed selection record.')
        else:item['ats_wins']=sum((r[key+'_margin']+r['home_spread']>=0)==(r['actual_margin']+r['home_spread']>0) for r in binary)
        result[key]=item
    result['anchor_changed_feature_side']=sum((r['feature_only_margin']+r['home_spread']>=0)!=(r['candidate_margin']+r['home_spread']>=0) for r in rows)
    return result
