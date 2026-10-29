"""Separate, versioned NBA game component with point-in-time feature transforms.

The pinned prop implementation is never edited or refitted here. Without an
independently accepted game artifact this component keeps its scoring baseline,
but uses explicit schedule context for rest and records all input coverage.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics

from . import nba_model as baseline

BASELINE_VERSION = 'nba-game-live-1.2.0'
FEATURE_NAMES = ('baseline_margin', 'recent_net_difference', 'season_net_difference',
                 'opponent_strength_difference', 'possession_net_difference',
                 'possession_coverage')
ROOT = Path(__file__).resolve().parents[1]


def _active():
    from . import nba_spread_learning
    return nba_spread_learning.active()


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _season(stamp):
    when=datetime.fromtimestamp(stamp,timezone.utc)
    return when.year if when.month>=7 else when.year-1


def _finite(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('NBA game value must be finite')
    return float(value)


def validate_artifact(artifact):
    if (not isinstance(artifact,dict) or artifact.get('schema')!=1
            or artifact.get('kind')!='nba-spread-residual-v1'
            or artifact.get('feature_names')!=list(FEATURE_NAMES)):
        raise ValueError('Invalid NBA spread artifact contract')
    _finite(artifact['training_cutoff']); _finite(artifact['intercept'])
    for name in ('coefficients','means','scales'):
        if len(artifact[name])!=len(FEATURE_NAMES):raise ValueError('Invalid NBA spread coefficient count')
        for value in artifact[name]:_finite(value)
    if any(s<=0 for s in artifact['scales']):raise ValueError('Invalid NBA spread feature scale')
    if not 1<=_finite(artifact['sd_margin'])<=100:raise ValueError('Invalid NBA spread uncertainty')
    return artifact


def baseline_info():
    names=('nba_spread_model.py','nba_game_context.py','nba_model.py','pricing.py','analytics/nba.py')
    sources={f'edgeterminal/{name}':hashlib.sha256((ROOT/'edgeterminal'/name).read_bytes()).hexdigest()
             for name in names if (ROOT/'edgeterminal'/name).is_file()}
    params=dict(kind=BASELINE_VERSION,pinned_parameter_fingerprint=baseline.PARAMETER_FINGERPRINT,
                code_sha256=sources,
                schedule_rest='verified_full_schedule',availability='context_only',possessions='context_only')
    return dict(version=BASELINE_VERSION,game_component_version=BASELINE_VERSION,
                fingerprint=hashlib.sha256(_json(params).encode()).hexdigest(),
                cutoff='2026-10-01T00:00:00Z',code_sha256=sources)


def model_info():
    current=_active()
    if not current:return baseline_info()
    artifact=validate_artifact(current['artifact'])
    return dict(version=artifact['version'],game_component_version=artifact['version'],
                fingerprint=current['sha256'],cutoff=artifact['training_cutoff'])


def prepare_history(history,cutoff,*,received_by=None):
    """Reject contradictory identities and never admit post-as-of scores."""
    groups=defaultdict(list)
    for raw in history or []:
        ts=baseline.parse_time(raw.get('played_at',raw.get('date')))
        points=baseline.valid_number(raw.get('points'),nonnegative=True)
        allowed=baseline.valid_number(raw.get('opponent_points'),nonnegative=True)
        team=raw.get('team')
        receipt=raw.get('received_at')
        if (team not in baseline.NBA_TEAMS or ts is None or ts>cutoff
                or points is None or allowed is None or not 1<=points<=250 or not 1<=allowed<=250
                or points!=int(points) or allowed!=int(allowed) or not raw.get('game_id')
                or (receipt is not None and (_finite(receipt)>(cutoff if received_by is None else received_by)))):continue
        row=dict(raw,ts=ts,played_at=raw.get('played_at',raw.get('date')))
        groups[(team,str(raw['game_id']))].append(row)
    by_team=defaultdict(list)
    for (team,_),rows in groups.items():
        signatures={(r['ts'],float(r['points']),float(r['opponent_points']),r.get('opponent'),r.get('home')) for r in rows}
        if len(signatures)!=1:
            raise ValueError('NBA game history has contradictory duplicate identities or scores')
        by_team[team].append(rows[0])
    return {team:sorted(rows,key=lambda r:(r['ts'],str(r['game_id']))) for team,rows in by_team.items()}


def _base(home,away,selected,tipoff,schedule):
    clean=[{k:v for k,v in r.items() if k not in {'possessions','received_at'}} for r in selected]
    out=baseline.project_game(home,away,clean,tipoff,history_complete=schedule is None)
    if out['status']!='ok' or schedule is None:return out
    total,margin=out['total'],out['margin']; rest={}; days={}; notes=[]
    for side,team in (('home',home),('away',away)):
        context=schedule.get(team) or {}
        last=baseline.parse_time(context.get('last_game_tipoff'))
        complete=context.get('complete') is True and not context.get('missing_game_ids') and not context.get('stale')
        elapsed=(tipoff-last)/86400 if last is not None and last<tipoff else None
        label='unknown' if not complete or elapsed is None else 'b2b' if elapsed<baseline.REST_B2B_DAYS else 'normal'
        rest[side]=label;days[side]=round(elapsed,2) if elapsed is not None else None
        if label=='b2b':
            adjustment=baseline.B2B_HOME_ADJ if side=='home' else baseline.B2B_AWAY_ADJ
            total+=adjustment
            margin+=adjustment if side=='home' else -adjustment
            notes.append(f'{side} schedule verifies back-to-back: fixed {adjustment:+.1f} points')
        elif label=='unknown':notes.append(f'{side} full schedule/rest coverage unknown; no rest adjustment')
    ph,pa=(total+margin)/2,(total-margin)/2
    out.update(proj_home=round(ph,2),proj_away=round(pa,2),margin=round(margin,2),total=round(ph+pa,2),
               home_win_probability=round(baseline.norm_cdf(margin/baseline.SD_MARGIN),4),rest=rest,rest_days=days,notes=notes)
    return out


def features(home,away,history,tipoff,*,as_of=None,schedule=None):
    tip=baseline.parse_time(tipoff)
    stamp=tip-baseline.FEATURE_LAG_S if as_of is None else _finite(as_of)
    if tip is None or stamp>=tip:raise ValueError('NBA game as_of must be before tipoff')
    cutoff=min(stamp,tip-baseline.FEATURE_LAG_S)
    grouped=prepare_history(history,cutoff,received_by=stamp)
    windows={team:rows[-20:] for team,rows in grouped.items()}
    selected=windows.get(home,[])+windows.get(away,[])
    before=_base(home,away,selected,tip,schedule)
    if before['status']!='ok':return dict(status='unavailable',baseline=before,selected_rows=selected,feature_cutoff=cutoff)
    net=lambda rows:statistics.mean(float(r['points'])-float(r['opponent_points']) for r in rows)
    h,a=windows[home],windows[away]
    season=_season(tip)
    def season_net(rows):
        current=[r for r in rows if _season(r['ts'])==season]
        strength=len(current)/(len(current)+10.)
        return net(rows) if not current else (1-strength)*net(rows)+strength*net(current)
    opponent_rows=[]
    def opp(rows):
        values=[]
        for row in rows:
            earlier=[r for r in grouped.get(row.get('opponent'),[]) if r['ts']<=row['ts']-baseline.FEATURE_LAG_S][-20:]
            if len(earlier)>=5:values.append(net(earlier));opponent_rows.extend(earlier)
        return statistics.mean(values) if values else 0.
    opponent_difference=opp(h)-opp(a)
    def possession_net(rows):
        if not all(baseline.valid_number(r.get('possessions')) and r['possessions']>0 for r in rows):return None
        return statistics.mean(100*(r['points']-r['opponent_points'])/r['possessions'] for r in rows)
    hp,ap=possession_net(h),possession_net(a)
    values=[before['margin'],net(h[-5:])-net(a[-5:]),season_net(h)-season_net(a),
            opponent_difference,(hp-ap) if hp is not None and ap is not None else 0.,
            float(hp is not None and ap is not None)]
    unique={(r['team'],str(r['game_id'])):r for r in selected+opponent_rows}
    return dict(status='ok',baseline=before,values=values,selected_rows=list(unique.values()),feature_cutoff=cutoff,
                target_rows=selected,coverage=dict(current_season_rows=sum(_season(r['ts'])==season for r in selected),
                target_rows=len(selected),possession_rows=sum(bool(r.get('possessions')) for r in selected),
                opponent_feature_rows=len(unique)-len(selected)))


def project_game(home,away,history,tipoff,*,as_of=None,availability=None,schedule=None,artifact=None):
    context=features(home,away,history,tipoff,as_of=as_of,schedule=schedule)
    out=dict(context['baseline']);info=baseline_info()
    if context['status']!='ok':return out
    current=_active() if artifact is None else None
    artifact=artifact if artifact is not None else current['artifact'] if current else None
    tip=baseline.parse_time(tipoff);stamp=tip-baseline.FEATURE_LAG_S if as_of is None else float(as_of)
    used=False
    if artifact is not None:
        validate_artifact(artifact)
        if artifact['training_cutoff']<stamp:
            residual=artifact['intercept']+sum(c*(v-m)/s for c,v,m,s in
                zip(artifact['coefficients'],context['values'],artifact['means'],artifact['scales']))
            margin=out['margin']+residual;total=out['total'];sd=artifact['sd_margin']
            out.update(margin=round(margin,2),proj_home=round((total+margin)/2,2),
                       proj_away=round((total-margin)/2,2),sd_margin=sd,
                       home_win_probability=round(baseline.norm_cdf(margin/sd),4))
            out['uncertainty']=dict(out.get('uncertainty') or {},sd_margin=sd,
                epistemic='Validated residual game model; player availability is recorded without an unvalidated point adjustment.')
            info=dict(version=artifact['version'],game_component_version=artifact['version'],cutoff=artifact['training_cutoff'],
                      fingerprint=hashlib.sha256(_json(artifact).encode()).hexdigest());used=True
    selected=context['selected_rows'] if used else context['target_rows']
    out.update(model_version=info['version'],game_component_version=info['version'],
               parameter_fingerprint=info['fingerprint'],training_cutoff=info['cutoff'],
               home_score=out['proj_home'],away_score=out['proj_away'],home_margin=out['margin'],
               selected_rows=selected,feature_cutoff=context['feature_cutoff'],coverage=context['coverage'],
               availability_context=dict(players=list(availability or []),numeric_adjustment_applied=False,
                   note='Availability is sourced context; historical player-impact coefficients have not been validated.'),
               features_applied=used,feature_names=list(FEATURE_NAMES) if used else [],
               away_win_probability=round(1-out['home_win_probability'],4))
    return out
