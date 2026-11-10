"""Read-only, receipt-bound totals evidence, independent of spread row choices.

Serving replays only selected source IDs. Historical reconstruction is an offline
worker operation and carries an explicit availability assumption. No fetch or
mutable history cache is used. All artifact identities live in the portable DB.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from . import db, nba_data, nba_game_forecasts, nba_identity, nba_model, nba_store

SCHEMA = 'nba-totals-input-1'
BASE = '/apis/site/v2/sports/basketball/nba/'
FEATURE_LAG = 4 * 3600
MAX_ROWS = 6000
MAX_BODY = 8 * 1024 * 1024
HISTORICAL_BOX_POLICY = 'verified_box_stat_correction_as_of_v1'
_PARSED = {}
_PARSED_CONNECTION = None


class ResultPending(ValueError):
    """Verified newer result exists; its matching immutable grade is still due."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def map_hash(value):
    return hashlib.sha256(_json({k:v for k,v in value.items() if k!='sha256'}).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value):
        raise ValueError('Totals evidence requires a finite number')
    return float(value)


def _stamp(value):
    return _number(value) if isinstance(value,(int,float)) else nba_identity.parse_tipoff(value).timestamp()


def _event_id(value):
    result = str(value).rsplit('-',1)[-1]
    if not re.fullmatch(r'[1-9]\d{5,11}',result):
        raise ValueError('Totals evidence has an invalid event identity')
    return result


def _season(stamp):
    moment = datetime.fromtimestamp(stamp,timezone.utc)
    return moment.year if moment.month>=7 else moment.year-1


def _source(ident, as_of):
    if isinstance(ident,bool) or not isinstance(ident,int) or ident<=0:
        raise ValueError('Totals source artifact ID is invalid')
    source = db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(ident,))
    if not source or source['http_status']!=200:
        raise ValueError('Totals source evidence is missing or unsuccessful')
    raw=source['body']
    if not isinstance(raw,bytes) or len(raw)>MAX_BODY or hashlib.sha256(raw).hexdigest()!=source['sha256']:
        raise ValueError('Totals source evidence hash/size conflict')
    if _number(source['received_at'])>as_of:
        raise ValueError('Totals source received after as-of')
    url=urlsplit(source['url'])
    if (url.scheme!='https' or url.netloc!='site.api.espn.com' or url.fragment
            or url.username or url.password or '%' in url.path or '%' in url.query):
        raise ValueError('Totals source URL identity is invalid')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('Totals source has duplicate JSON keys')
            result[key]=value
        return result
    global _PARSED_CONNECTION
    if _PARSED_CONNECTION is not db.conn():
        _PARSED.clear(); _PARSED_CONNECTION=db.conn()
    key=(ident,source['sha256'])
    try:
        if key not in _PARSED:
            if len(_PARSED)>=1024: _PARSED.pop(next(iter(_PARSED)))
            _PARSED[key]=json.loads(raw,object_pairs_hook=unique)
        body=_PARSED[key]
    except (ValueError,UnicodeDecodeError) as exc: raise ValueError('Totals source is not valid JSON') from exc
    if not isinstance(body,dict): raise ValueError('Totals source must be a JSON object')
    return source,body,url


def _descriptor(source, role):
    return {k:source[k] for k in ('id','kind','url','sha256','received_at','http_status')} | {'role':role}


def _score(value):
    if isinstance(value,dict): value=value.get('value')
    if isinstance(value,str) and re.fullmatch(r'\d{1,3}',value): value=int(value)
    number=_number(value)
    if number!=int(number) or not 1<=number<=250: raise ValueError('Totals score is invalid')
    return int(number)


def _event(ev, year):
    comps=ev.get('competitions') or []
    if len(comps)!=1: raise ValueError('Totals event requires exactly one competition')
    comp=comps[0]
    if (comp.get('type') or {}).get('abbreviation')=='ALLSTAR': raise ValueError('Totals exhibition event is forbidden')
    status=comp.get('status') or ev.get('status') or {}
    kind=status.get('type') or {}
    if kind.get('completed') is not True: return None
    if kind.get('state') not in (None,'post'): raise ValueError('Totals event final status conflict')
    if (ev.get('seasonType') or {}).get('type',2)!=2: return None
    declared=ev.get('season') or {}
    if declared and (declared.get('year',year)!=year or declared.get('type',2)!=2):
        raise ValueError('Totals event season conflict')
    competitors=comp.get('competitors') or []; sides={c.get('homeAway'):c for c in competitors}
    if len(competitors)!=2 or set(sides)!={'home','away'}: raise ValueError('Totals event team orientation conflict')
    codes={side:nba_identity.team_code(item.get('team') or {}) for side,item in sides.items()}
    if codes['home']==codes['away']: raise ValueError('Totals event duplicate teams')
    stamp=_stamp(ev.get('date') or comp.get('date'))
    if _season(stamp)!=year-1: raise ValueError('Totals event season/time conflict')
    scores={side:_score(item.get('score')) for side,item in sides.items()}
    if scores['home']==scores['away']: raise ValueError('Totals final cannot be tied')
    return [dict(team=codes[side],opponent=codes[other],game_id=_event_id(ev['id']),played_at=stamp,
        points=scores[side],opponent_points=scores[other],home=side=='home',season_start_year=year-1)
        for side,other in (('home','away'),('away','home'))]


def source_history(source_id, as_of):
    """Strict schedule replay; only its numeric owner rows are scoring inputs."""
    source,body,url=_source(source_id,as_of)
    if source['kind'] not in ('team-history','spread-team-history'): raise ValueError('Totals scoring source kind conflict')
    match=re.fullmatch(BASE+r'teams/([1-9]\d?)/schedule',url.path)
    query=parse_qs(url.query,keep_blank_values=True)
    if not match or url.query!='&'.join(f'{key}={value[0]}' for key,value in sorted(query.items())) or set(query)!={'season','seasontype'} or query['seasontype']!=['2'] or len(query['season'])!=1:
        raise ValueError('Totals schedule route/query identity is invalid')
    year_text=query['season'][0]
    if not re.fullmatch(r'20\d{2}',year_text): raise ValueError('Totals schedule season is invalid')
    year=int(year_text)
    owner=next((code for code,row in nba_identity.TEAMS.items() if row[0]==match[1]),None)
    if not owner: raise ValueError('Totals schedule numeric team identity is invalid')
    if body.get('team') and nba_identity.team_code(body['team'])!=owner: raise ValueError('Totals schedule owner conflict')
    if not isinstance(body.get('events'),list): raise ValueError('Totals schedule events missing')
    result={}
    for ev in body['events']:
        rows=_event(ev,year)
        if rows is None: continue
        if owner not in {r['team'] for r in rows}: raise ValueError('Totals schedule event excludes owner')
        row=next(r for r in rows if r['team']==owner); key=row['game_id']
        if row['played_at']>=source['received_at']: raise ValueError('Totals final source precedes game')
        if key in result and {k:result[key][k] for k in row}!=row: raise ValueError('Totals schedule duplicate correction conflict')
        result[key]=dict(row,source_artifact_id=source_id,source_sha256=source['sha256'],received_at=source['received_at'])
    return list(result.values()), source


def box_features(source_id, as_of):
    """Full-game possessions plus duration only when final-period evidence proves it."""
    source,body,url=_source(source_id,as_of)
    return _box_evidence(source,body,url)[:3]


def _box_evidence(source, body, url, *, allow_incomplete=False):
    """Shared verification; only offline reconstruction permits missing statistics."""
    source_id=source['id']
    if source['kind']!='final-boxscore': raise ValueError('Totals possession source kind conflict')
    query=parse_qs(url.query,keep_blank_values=True)
    if url.path!=BASE+'summary' or set(query)!={'event'} or len(query['event'])!=1:
        raise ValueError('Totals box route/query identity is invalid')
    eid=_event_id(query['event'][0]); header=body.get('header') or {}
    if allow_incomplete:
        if not isinstance(header,dict) or not isinstance(header.get('season'),dict):
            raise ValueError('Totals box header/season structure is invalid')
        if str(header.get('id'))!=eid: raise ValueError('Totals box numeric event identity conflict')
        season_type=header.get('seasonType')
        if season_type is not None and not isinstance(season_type,dict):
            raise ValueError('Totals box season type structure is invalid')
        if header['season'].get('type')!=2 and (season_type or {}).get('type')!=2:
            raise ValueError('Totals box regular season is unproved')
        competitions=header.get('competitions')
        if not isinstance(competitions,list) or len(competitions)!=1 or not isinstance(competitions[0],dict):
            raise ValueError('Totals box competition structure is invalid')
        competitors=competitions[0].get('competitors')
        if not isinstance(competitors,list) or any(not isinstance(c,dict) or not isinstance(c.get('team'),dict) for c in competitors):
            raise ValueError('Totals box competitor structure is invalid')
        for status in (header.get('status'),competitions[0].get('status')):
            if status is not None and (not isinstance(status,dict) or not isinstance(status.get('type'),dict)):
                raise ValueError('Totals box status structure is invalid')
    year=(header.get('season') or {}).get('year')
    if not isinstance(year,int) or isinstance(year,bool): raise ValueError('Totals box season missing')
    rows=_event(header,year)
    if not rows or rows[0]['game_id']!=eid or rows[0]['played_at']>=source['received_at']:
        raise ValueError('Totals box final/game identity conflict')
    comp=header['competitions'][0]
    if allow_incomplete:
        if url.query!=f'event={eid}': raise ValueError('Totals box route/query identity is invalid')
        if header.get('date') and comp.get('date') and _stamp(header['date'])!=_stamp(comp['date']):
            raise ValueError('Totals box played time conflict')
        for status in (header.get('status'),comp.get('status')):
            if status is not None and ((status.get('type') or {}).get('completed') is not True
                    or (status.get('type') or {}).get('state') not in (None,'post')):
                raise ValueError('Totals box final status conflict')
    periods=[]
    for status in (header.get('status') or {},comp.get('status') or {}):
        if 'period' in status:
            period=status['period']
            if isinstance(period,bool) or not isinstance(period,int) or not 4<=period<=20:
                raise ValueError('Totals final period is invalid')
            periods.append(period)
    competitors=comp['competitors']
    line_counts=[]
    for competitor in competitors:
        lines=competitor.get('linescores')
        if lines is None: continue
        if not isinstance(lines,list) or not 4<=len(lines)<=20: raise ValueError('Totals line-score duration is invalid')
        counts=[]
        for index,line in enumerate(lines,1):
            if allow_incomplete and not isinstance(line,dict): raise ValueError('Totals line-score structure is invalid')
            observed=[]
            if 'value' in line:
                observed.append(_number(line['value']))
            if 'displayValue' in line:
                text=line['displayValue']
                if not isinstance(text,str) or not re.fullmatch(r'0|[1-9]\d{0,2}',text):
                    raise ValueError('Totals period display score is not a canonical integer')
                observed.append(float(int(text)))
            if not observed or len(set(observed))!=1:
                raise ValueError('Totals period numeric/display score conflict')
            value=observed[0]
            if value<0 or value!=int(value): raise ValueError('Totals period score is invalid')
            declared=line.get('period')
            if declared is not None and declared!=index: raise ValueError('Totals period sequence conflict')
            counts.append(value)
        if sum(counts)!=_score(competitor.get('score')): raise ValueError('Totals line-score final conflict')
        line_counts.append(len(lines))
    if line_counts and len(line_counts)!=2: raise ValueError('Totals duration needs both line scores')
    if periods+line_counts and len(set(periods+line_counts))!=1: raise ValueError('Totals duration evidence conflicts')
    period=(periods+line_counts)[0] if periods+line_counts else None
    duration={}
    if period is not None:
        minutes=48+5*(period-4)
        duration=dict(duration_minutes=minutes,overtime_periods=period-4,duration_method='explicit_final_period_v1')
    box=body.get('boxscore')
    if allow_incomplete and box is not None and not isinstance(box,dict):
        raise ValueError('Totals boxscore structure is invalid')
    teams=(box or {}).get('teams')
    if allow_incomplete and teams is None: teams=[]
    if not isinstance(teams,list) or (len(teams)!=2 and not allow_incomplete) or len(teams)>2:
        raise ValueError('Totals possessions need two team boxes')
    estimates={}; supported={}; complete=len(teams)==2
    for team in teams:
        if allow_incomplete and (not isinstance(team,dict) or not isinstance(team.get('team'),dict)):
            raise ValueError('Totals team box structure is invalid')
        code=nba_identity.team_code(team.get('team') or {})
        if code in supported or code not in {r['team'] for r in rows}: raise ValueError('Totals box team conflict')
        statistics=team.get('statistics')
        if statistics is None or not allow_incomplete: statistics=statistics or []
        if allow_incomplete and (not isinstance(statistics,list) or any(not isinstance(s,dict) for s in statistics)):
            raise ValueError('Totals box statistics structure is invalid')
        names=[s.get('name') for s in statistics]
        if allow_incomplete and any(not isinstance(name,str) for name in names):
            raise ValueError('Totals box statistic name is invalid')
        if len(set(names))!=len(names): raise ValueError('Totals duplicate box statistic')
        stats={s.get('name'):s.get('displayValue',s.get('value')) for s in statistics}
        def count(name):
            raw=stats.get(name)
            if isinstance(raw,str) and re.fullmatch(r'\d+',raw): raw=int(raw)
            result=_number(raw)
            if result<0 or result!=int(result): raise ValueError('Totals possession count is invalid')
            return result
        def attempts(name):
            raw=stats.get(name)
            match=re.fullmatch(r'(\d{1,3})-(\d{1,3})',raw or '') if isinstance(raw,str) else None
            if not match: raise ValueError('Totals possession attempts missing')
            made,attempted=map(int,match.groups())
            if not 0<=made<=attempted<=300: raise ValueError('Totals possession attempts invalid')
            return (made,attempted) if allow_incomplete else attempted
        if 'points' in stats and count('points')!=next(r['points'] for r in rows if r['team']==code):
            raise ValueError('Totals team box score conflict')
        if allow_incomplete:
            # Validate present supported inputs even when another required field is absent.
            values={name:(attempts(name) if name.endswith('Attempted') else count(name))
                for name in ('fieldGoalsMade-fieldGoalsAttempted','freeThrowsMade-freeThrowsAttempted',
                    'offensiveRebounds','totalTurnovers','turnovers') if name in stats}
            # These are distinct provider counts. Match serving's total preference.
            turnover=values.get('totalTurnovers',values.get('turnovers'))
            supported[code]=dict(values)
            supported[code].pop('turnovers',None); supported[code].pop('totalTurnovers',None)
            if turnover is not None: supported[code]['turnovers']=turnover
            required={'fieldGoalsMade-fieldGoalsAttempted','freeThrowsMade-freeThrowsAttempted','offensiveRebounds'}
            if not required<=values.keys() or turnover is None:
                complete=False
                continue
            estimates[code]=values['fieldGoalsMade-fieldGoalsAttempted'][1]+.44*values['freeThrowsMade-freeThrowsAttempted'][1]-values['offensiveRebounds']+turnover
        else:
            estimates[code]=attempts('fieldGoalsMade-fieldGoalsAttempted')+.44*attempts('freeThrowsMade-freeThrowsAttempted')-count('offensiveRebounds')+count('totalTurnovers' if 'totalTurnovers' in stats else 'turnovers')
            supported[code]=None
    evidence=dict(duration=duration,statistics=supported,complete=complete)
    if not complete: return rows,None,source,evidence
    raw=sum(estimates.values())/2
    if not 50<=raw<=200: raise ValueError('Totals raw possessions implausible')
    result=dict(raw_possessions=raw,possession_method='symmetric_box_estimate_044',possession_source_artifact_id=source_id)
    if period is not None:
        duration=48+5*(period-4)
        result.update(duration_minutes=duration,overtime_periods=period-4,pace48=raw*48/duration,
            duration_method='explicit_final_period_v1',duration_source_artifact_id=source_id)
    return rows,result,source,evidence


def _game(game):
    home=game.get('home_team',game.get('home')); away=game.get('away_team',game.get('away'))
    if home not in nba_identity.TEAMS or away not in nba_identity.TEAMS or home==away:
        raise ValueError('Totals target teams invalid')
    tip=_stamp(game.get('tipoff_ts',game.get('tipoff',game.get('tipoff_utc'))))
    eid=_event_id(game.get('game_id'))
    canonical=f'nba-{_season(tip)}-{eid}'
    if game.get('game_id')!=canonical: raise ValueError('Totals target season/game identity conflict')
    return canonical,home,away,tip


def select_history(game, history, as_of):
    """Select bounded target/opponent windows from in-memory history only."""
    gid,home,away,tip=_game(game); at=_number(as_of); cutoff=min(at,tip-FEATURE_LAG)
    if at>=tip: raise ValueError('Totals selection must precede tipoff')
    grouped={}; seen={}; current_season=_season(tip)
    for row in history:
        team=row.get('team'); opponent=row.get('opponent')
        if team not in nba_identity.TEAMS or opponent not in nba_identity.TEAMS or team==opponent:
            raise ValueError('Totals selected history identity invalid')
        when=_stamp(row.get('played_at')); event=_event_id(row.get('game_id'))
        signature=tuple(row.get(k) for k in ('opponent','points','opponent_points','home','possessions',
            'raw_possessions','duration_minutes','possession_source_artifact_id'))+(when,)
        key=(team,event)
        if key in seen and seen[key]!=signature: raise ValueError('Totals selection duplicate/correction conflict')
        seen[key]=signature
        if event==_event_id(gid) or when>=cutoff or _season(when) not in (current_season-1,current_season): continue
        grouped.setdefault(team,{})[event]=row
    ordered={team:sorted(rows.values(),key=lambda r:(_stamp(r['played_at']),_event_id(r['game_id']))) for team,rows in grouped.items()}
    targets=[r for team in (home,away) for r in ordered.get(team,[])[-20:]]
    selected={(r['team'],_event_id(r['game_id'])):r for r in targets}
    for row in targets:
        before=_stamp(row['played_at'])-FEATURE_LAG
        opponent=[r for r in ordered.get(row['opponent'],[]) if _stamp(r['played_at'])<=before][-20:]
        selected.update({(r['team'],_event_id(r['game_id'])):r for r in opponent})
    return sorted(selected.values(),key=lambda r:(_stamp(r['played_at']),_event_id(r['game_id']),r['team']))


def prepare(game, history, as_of):
    """Replay selected source IDs only; do not widen the spread's original map.

    Contradictory evidence fails closed. Missing optional boxes keep score rows.
    The source manifest never contains host filesystem paths.
    """
    at=_number(as_of); gid,home,away,tip=_game(game)
    if at>=tip: raise ValueError('Totals serving as-of must precede tipoff')
    cutoff=min(at,tip-FEATURE_LAG)
    if not isinstance(history,(list,tuple)) or len(history)>MAX_ROWS: raise ValueError('Totals selected history is unbounded')
    schedules={}; boxes={}; sources={}; selected={}
    base_keys=('team','opponent','game_id','played_at','points','opponent_points','home')
    advanced_keys=('raw_possessions','duration_minutes','pace48','offensive_efficiency','defensive_efficiency',
        'overtime_periods','possession_method','duration_method')
    for original in history:
        sid=original.get('source_artifact_id')
        if sid not in schedules:
            parsed,source=source_history(sid,at); schedules[sid]={(r['team'],r['game_id']):r for r in parsed}
            sources[sid]=_descriptor(source,'scoring')
        team=original.get('team'); eid=_event_id(original.get('game_id'))
        row=schedules[sid].get((team,eid))
        if row is None: raise ValueError('Totals scoring row is absent from its source')
        if eid==_event_id(gid) or row['played_at']>=cutoff: raise ValueError('Totals target/future scoring row is forbidden')
        if not isinstance(original.get('home'),bool): raise ValueError('Totals home orientation must be boolean')
        for key in base_keys:
            actual=_stamp(original[key]) if key=='played_at' else _event_id(original[key]) if key=='game_id' else original.get(key)
            if actual!=row[key] or (key in ('points','opponent_points') and isinstance(actual,bool)):
                raise ValueError('Totals scoring row source semantic conflict')
        for key in ('source_sha256','received_at','season_start_year'):
            if key in original and original[key]!=row[key]: raise ValueError('Totals scoring receipt/hash/season conflict')
        result=dict(row)
        bid=original.get('possession_source_artifact_id') or original.get('duration_source_artifact_id')
        if bid:
            if original.get('duration_source_artifact_id',bid)!=bid: raise ValueError('Totals duration/possession source must match')
            if bid not in boxes: boxes[bid]=box_features(bid,at)
            boxrows,features,source=boxes[bid]
            matched=next((r for r in boxrows if r['team']==team and r['game_id']==eid),None)
            if not matched or any(matched[k]!=row[k] for k in base_keys): raise ValueError('Totals box scoring identity conflict')
            result.update(features,offensive_efficiency=row['points']/features['raw_possessions'],
                defensive_efficiency=row['opponent_points']/features['raw_possessions'])
            for key in advanced_keys:
                if key in original and original[key]!=result.get(key): raise ValueError('Totals advanced feature source conflict')
            if 'possessions' in original and original['possessions']!=features['raw_possessions']:
                raise ValueError('Totals raw possession conflict')
            sources[bid]=_descriptor(source,'possession_duration')
        elif any(key in original for key in advanced_keys) or 'possessions' in original:
            raise ValueError('Totals advanced feature lacks source evidence')
        key=(team,eid)
        if key in selected:
            previous=selected[key]
            semantic=lambda r:{k:v for k,v in r.items() if k not in ('source_artifact_id','source_sha256','received_at')}
            if semantic(previous)!=semantic(result): raise ValueError('Totals duplicate/correction source conflict')
            # Equal duplicate evidence uses the earliest receipt then immutable ID.
            if (result['received_at'],sid)<(previous['received_at'],previous['source_artifact_id']): selected[key]=result
        else: selected[key]=result
    from . import nba_totals_model
    context=nba_totals_model.features(home,away,list(selected.values()),tip,as_of=at,baseline_total=200.)
    selected={(r['team'],r['game_id']):r for r in context['selected_rows']}
    used_ids={r['source_artifact_id'] for r in selected.values()}
    used_ids.update(r['possession_source_artifact_id'] for r in selected.values() if r.get('possession_source_artifact_id'))
    mapped=dict(schema=SCHEMA,selection_method='target20_opponent_prior_scoring20_v1',game_id=gid,home=home,away=away,tipoff=tip,as_of=at,feature_cutoff=cutoff,
        rows=sorted(selected.values(),key=lambda r:(r['played_at'],r['game_id'],r['team'])),
        source_artifacts=[sources[i] for i in sorted(used_ids)])
    mapped['sha256']=map_hash(mapped)
    return mapped


def source_ids(input_map):
    return sorted({item['id'] for item in input_map['source_artifacts']})


def verify_input_map(input_map, game, as_of):
    if not isinstance(input_map,dict) or input_map.get('schema')!=SCHEMA or input_map.get('sha256')!=map_hash(input_map):
        raise ValueError('Totals input map seal conflict')
    replay=prepare(game,input_map.get('rows'),as_of)
    if replay!=input_map: raise ValueError('Totals input map semantic/receipt conflict')
    return replay


def _exists(table):
    return db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)) is not None



def _selected_input(game, at):
    """Mirror the frozen selector using only non-label game columns."""
    candidates=db.q('SELECT forecast_id FROM nba_forecasts WHERE game_id=? AND created_at<=? '
        'ORDER BY created_at,forecast_id',(game['game_id'],at))
    forecasts=[nba_store.get_forecast(row['forecast_id']) for row in candidates]
    legacy=next((row for row in forecasts if row['payload'].get('kind')=='game'),None)
    policy=nba_game_forecasts.policy()
    if not policy or not _exists('nba_game_forecast_enrollments'): return legacy
    enrollment=db.q1('SELECT * FROM nba_game_forecast_enrollments WHERE game_id=?',(game['game_id'],))
    if not enrollment or enrollment['policy_id']!=nba_game_forecasts.POLICY_ID or enrollment['enrolled_at']<policy['activated_at']:
        return legacy
    for row in reversed(forecasts):
        payload=row['payload']; asof=payload.get('as_of')
        if payload.get('kind')!='game-revision' or payload.get('policy_id')!=enrollment['policy_id']: continue
        if not isinstance(asof,(int,float)) or isinstance(asof,bool) or not math.isfinite(asof): continue
        if not enrollment['enrolled_at']<=asof<=row['created_at']<game['tipoff_ts'] or payload.get('projection',{}).get('tipoff')!=game['tipoff_ts']: continue
        if hashlib.sha256(_json(payload).encode()).hexdigest()!=row['payload_sha256']: continue
        if hashlib.sha256(_json(payload.get('input_manifest')).encode()).hexdigest()!=payload.get('input_manifest_sha256'): continue
        snapshot={k:v for k,v in payload.items() if k not in {'as_of','snapshot_sha256','input_manifest_sha256'}}
        if hashlib.sha256(_json(snapshot).encode()).hexdigest()!=payload.get('snapshot_sha256'): continue
        return row
    return legacy


def _verify_total_market(market, game, at, saved_ids):
    if market.get('total',market.get('total_line')) is None: return
    ident=market.get('source_artifact_id')
    if ident not in saved_ids: raise ValueError('Totals line source absent from saved forecast')
    source,body,url=_source(ident,at); query=parse_qs(url.query,keep_blank_values=True)
    if url.path==BASE+'scoreboard':
        if source['kind']!='scoreboard': raise ValueError('Totals line source kind conflict')
        expected=nba_identity.eastern_date(datetime.fromtimestamp(game['tipoff_ts'],timezone.utc)).replace('-','')
        if set(query)!={'dates','limit'} or query['dates']!=[expected] or query['limit'] not in (['100'],['200']):
            raise ValueError('Totals line scoreboard date/query identity conflict')
    elif url.path==BASE+'summary':
        if source['kind']!='summary': raise ValueError('Totals line source kind conflict')
        if query!={'event':[_event_id(game['game_id'])]}: raise ValueError('Totals line summary event identity conflict')
    else: raise ValueError('Totals line source route is invalid')
    canonical=dict(game,espn_event_id=_event_id(game['game_id']))
    nba_game_forecasts._validate_market(market,{ident:dict(source,data=body)},canonical,at)

def forecast_inputs(now=None, limit=1000, *, start_date=None):
    """Full supported saved forecast universe without reading outcomes or grades.

    Includes games without a total quote. Filtering on finals, won/lost grades,
    or available labels here would expose an outcome-selected outer cohort.
    """
    at=_number(db.now() if now is None else now)
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=10000:
        raise ValueError('Totals forecast limit must be one to 10000')
    if not _exists('nba_games') or not _exists('nba_forecasts'): return []
    if start_date is not None:
        if not isinstance(start_date,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',start_date):
            raise ValueError('Totals universe date floor must be an ISO Eastern date')
        datetime.strptime(start_date,'%Y-%m-%d')
    games=db.q('SELECT game_id,home_team,away_team,tipoff_ts,game_date_et FROM nba_games '
        'WHERE tipoff_ts>? AND game_date_et>=? ORDER BY tipoff_ts,game_id',(0,start_date or '0000-00-00'))
    result=[]
    for game in games:
        saved=_selected_input(game,at)
        if not saved or saved['created_at']>at: continue
        payload=saved['payload']; projection=payload.get('projection') or {}
        if payload.get('kind') not in ('game','game-revision') or projection.get('status','ok')!='ok': continue
        baseline=projection.get('total')
        if not isinstance(baseline,(int,float)) or isinstance(baseline,bool) or not math.isfinite(baseline): continue
        if hashlib.sha256(_json(payload).encode()).hexdigest()!=saved['payload_sha256']:
            raise ValueError('Totals saved forecast hash conflict')
        asof=_number(payload.get('as_of',saved['created_at']))
        if not asof<=saved['created_at']<game['tipoff_ts']: raise ValueError('Totals forecast does not precede tipoff')
        manifest=payload.get('input_manifest') or {}
        mapped=projection.get('total_input_map') or payload.get('total_input_map')
        feature_asof=asof
        if mapped:
            feature_asof=_number(mapped.get('as_of'))
            if feature_asof>asof: raise ValueError('Totals map is later than saved snapshot as-of')
            verify_input_map(mapped,game,feature_asof)
        elif manifest.get('rows'): mapped=prepare(game,manifest['rows'],asof)
        if mapped:
            if not set(source_ids(mapped))<=set(saved['source_artifact_ids']):
                raise ValueError('Totals input source is absent from common saved source list')
            saved_descriptors={r['id']:r for r in payload.get('source_artifacts') or []}
            if saved_descriptors:
                for item in mapped['source_artifacts']:
                    if saved_descriptors.get(item['id'])!={k:item[k] for k in ('id','sha256','received_at')}:
                        raise ValueError('Totals input receipt differs from common saved source descriptor')
        from . import nba_game_lines
        market=nba_game_lines.effective_market(saved, market='total')
        line=market.get('total',market.get('total_line'))
        if line is not None:
            _number(line)
            _verify_total_market(market,game,asof,saved['source_artifact_ids'])
        probability=None
        if line is not None:
            sigma=_number(projection.get('sd_total',nba_model.SD_TOTAL))
            if sigma<=0: raise ValueError('Totals incumbent uncertainty is invalid')
            over,under,push=nba_model.probabilities('total',baseline,sigma,line)
            probability=over/(over+under) if over+under>0 else .5
        row=dict(game_id=game['game_id'],date_et=game['game_date_et'],home=game['home_team'],away=game['away_team'],
            home_team=game['home_team'],away_team=game['away_team'],tipoff=game['tipoff_ts'],tipoff_ts=game['tipoff_ts'],
            as_of=feature_asof,forecast_at=feature_asof,snapshot_as_of=asof,recorded_at=saved['created_at'],forecast_id=saved['forecast_id'],
            forecast_sha256=saved['payload_sha256'],saved_total_line=line,baseline_total=float(baseline),
            baseline_probability=probability,input_map=mapped,rows=manifest.get('rows') or [],context=manifest,
            model_base_total=_number(projection.get('total_base_total',baseline)),
            model_base_sd_total=_number(projection.get('total_base_sd_total',projection.get('sd_total',nba_model.SD_TOTAL))),
            projection=projection,market=market,source_ids=saved['source_artifact_ids'],
            source_artifacts=payload.get('source_artifacts') or [],policy_id=payload.get('policy_id'))
        result.append(row)
    # Never cut an Eastern date in half at the bound.
    if len(result)>limit:
        boundary=result[limit-1]['date_et']; result=[r for r in result if r['date_et']<=boundary]
    return result


validation_inputs=forecast_inputs
unlabeled_forecast_inputs=forecast_inputs


def _verified_reservation(reservation):
    from . import nba_totals_store
    return nba_totals_store.validate_reservation(reservation)


def validation_state(game_ids, now=None):
    """Cheap exact selected-forecast, final-grade, source heads for publication."""
    at=_number(db.now() if now is None else now); result={}; dates=set()
    for gid in sorted(set(game_ids)):
        identity=db.q1('SELECT game_id,home_team,away_team,tipoff_ts,game_date_et,status FROM nba_games WHERE game_id=?',(gid,))
        if identity: dates.add(identity['game_date_et'])
        saved=nba_game_forecasts.selected(gid)
        head=None
        if _exists('nba_game_grade_revisions'):
            head=db.q1('SELECT id,sha256,source_artifact_id,graded_at FROM nba_game_grade_revisions '
                'WHERE game_id=? AND graded_at<=? ORDER BY id DESC LIMIT 1',(gid,at))
        source=None
        if head: source=db.q1('SELECT id,kind,url,http_status,sha256,received_at FROM nba_source_artifacts WHERE id=?',(head['source_artifact_id'],))
        final_url='https://site.api.espn.com'+BASE+'summary?event='+_event_id(gid)
        raw_head=db.q1('SELECT id,kind,url,http_status,sha256,received_at FROM nba_source_artifacts '
            'WHERE url=? ORDER BY received_at DESC,id DESC LIMIT 1',(final_url,))
        if raw_head:
            raw_head={k:raw_head[k] for k in ('kind','url','http_status','sha256')}
        result[gid]=dict(game=identity,raw_source_head=raw_head,forecast_id=saved['forecast_id'] if saved else None,
            forecast_sha256=saved['payload_sha256'] if saved else None,grade=head,source=source)
    membership={}
    for day in sorted(dates):
        members=[]
        games=db.q('SELECT game_id,home_team,away_team,tipoff_ts,game_date_et,status FROM nba_games '
                   'WHERE game_date_et=? ORDER BY game_id',(day,))
        for game in games:
            saved=nba_game_forecasts.selected(game['game_id'])
            projection=(saved or {}).get('payload',{}).get('projection',{})
            total=projection.get('total')
            if saved and projection.get('status','ok')=='ok' and isinstance(total,(int,float)) and not isinstance(total,bool) and math.isfinite(total):
                members.append(dict(game,forecast_id=saved['forecast_id'],forecast_sha256=saved['payload_sha256']))
        membership[day]=members
    result['_date_universe']=membership
    return result


def validation_labels(reservation, now=None):
    """Open only the persisted reserved game/date identities, after seal replay."""
    sealed=_verified_reservation(reservation)  # Deliberately first: no DB result reads before this guard.
    at=_number(db.now() if now is None else now)
    gids=sealed.get('game_ids') or []; dates=sealed.get('date_ids') or []
    if not gids or not dates: raise ValueError('Totals label reservation has no whole-date cohort')
    universe={}; games={}
    for day in dates:
        items=db.q('SELECT game_id,home_team,away_team,tipoff_ts,game_date_et FROM nba_games WHERE game_date_et=? ORDER BY game_id',(day,))
        supported=[]
        for game in items:
            saved=nba_game_forecasts.selected(game['game_id'])
            projection=(saved or {}).get('payload',{}).get('projection',{})
            total=projection.get('total')
            if saved and projection.get('status','ok')=='ok' and isinstance(total,(int,float)) and not isinstance(total,bool) and math.isfinite(total):
                supported.append(game['game_id']); games[game['game_id']]=(game,saved)
        universe[day]=supported
        if not set(supported)<=set(gids): raise ValueError('Totals reservation omits supported game on reserved date')
    rows=[]; complete=[]
    for gid in gids:
        if gid not in games: raise ValueError('Totals reserved game is absent from date universe')
        game,saved=games[gid]
        frozen=next((r for r in sealed.get('games',[]) if r['game_id']==gid),None)
        if frozen and frozen.get('forecast_sha256')!=saved['payload_sha256']:
            raise ValueError('Totals reserved forecast is no longer the selected snapshot')
        if hashlib.sha256(_json(saved['payload']).encode()).hexdigest()!=saved['payload_sha256']:
            raise ValueError('Totals selected forecast hash conflict')
        grades=db.q('SELECT * FROM nba_game_grade_revisions WHERE game_id=? AND graded_at<=? ORDER BY id',(gid,at)) if _exists('nba_game_grade_revisions') else []
        previous=0
        for grade in grades:
            digest=hashlib.sha256((grade['payload']+':'+str(previous)).encode()).hexdigest()
            if digest!=grade['sha256']: raise ValueError('Totals grade hash chain conflict')
            previous=grade['id']
        if not grades: continue
        grade=grades[-1]; label=json.loads(grade['payload'])
        if (label.get('forecast_id'),label.get('forecast_sha256'),label.get('forecast_policy_id'))!=(saved['forecast_id'],saved['payload_sha256'],saved['payload'].get('policy_id')):
            raise ValueError('Totals grade selected forecast identity conflict')
        source,body,url=_source(grade['source_artifact_id'],min(at,grade['graded_at']))
        query=parse_qs(url.query,keep_blank_values=True)
        if source['kind']!='final-boxscore': raise ValueError('Totals label source kind conflict')
        if url.path!=BASE+'summary' or query!={'event':[_event_id(gid)]}: raise ValueError('Totals label source route identity conflict')
        final=nba_data.parse_summary(body)
        parsed=final['game']
        if (not final['box_complete'] or parsed['status']!='final' or parsed['game_id']!=gid
            or (parsed['home_team'],parsed['away_team'])!=(game['home_team'],game['away_team'])
            or _stamp(parsed['tipoff_utc'])!=game['tipoff_ts'] or source['received_at']<=game['tipoff_ts']
            or (label.get('home_score'),label.get('away_score'))!=(parsed['home_score'],parsed['away_score'])):
            raise ValueError('Totals label source score/team/time conflict')
        actual_total=parsed['home_score']+parsed['away_score']
        latest=db.q1("SELECT id,sha256 FROM nba_source_artifacts WHERE url=? AND kind='final-boxscore' "
                      'AND http_status=200 ORDER BY received_at DESC,id DESC LIMIT 1',(source['url'],))
        if latest and latest['sha256']!=source['sha256']:
            _,latest_body,_=_source(latest['id'],max(at,db.now()))
            current=nba_data.parse_summary(latest_body)
            latest_game=current['game']
            if (not current['box_complete'] or latest_game.get('status')!='final'
                or latest_game.get('game_id')!=gid
                or (latest_game.get('home_team'),latest_game.get('away_team'))!=(game['home_team'],game['away_team'])
                or _stamp(latest_game['tipoff_utc'])!=game['tipoff_ts']):
                raise ValueError('Totals latest final source coverage/game/team/time conflict')
            if (latest_game['home_score'],latest_game['away_score'])!=(parsed['home_score'],parsed['away_score']):
                raise ResultPending('Totals latest final correction is awaiting a matching graded label')
        from . import nba_game_lines
        market=nba_game_lines.effective_market(saved, market='total'); line=market.get('total')
        if line is not None:
            _verify_total_market(market,game,saved['payload'].get('as_of',saved['created_at']),saved['source_artifact_ids'])
            over=saved['payload']['projection']['total']>=line
            edge=(actual_total-line)*(1 if over else -1)
            expected='won' if edge>0 else 'lost' if edge<0 else 'push'
            if label.get('total')!=expected: raise ValueError('Totals grade does not match saved actual line')
        rows.append(dict(game_id=gid,date_et=game['game_date_et'],actual_total=parsed['home_score']+parsed['away_score'],
            home_score=parsed['home_score'],away_score=parsed['away_score'],result_at=source['received_at'],
            label_sha256=grade['sha256'],label_source_sha256=source['sha256'],label_source_artifact_id=source['id'],
            grade_id=grade['id'],graded_at=grade['graded_at']))
    resolved={r['game_id'] for r in rows}
    for day,ids in universe.items():
        if ids and set(ids)<=resolved: complete.append(day)
    return dict(rows=rows,complete_dates=sorted(complete),universe_game_ids_by_date=universe,
        state=validation_state(gids,at),reservation_id=sealed['reservation_id'])


read_labels=validation_labels


def _historical_boxes(history, sources, at, signature):
    """Resolve verified event-wide versions without changing selected-ID serving."""
    event_rows={}
    for (team,eid),row in history.items(): event_rows.setdefault(eid,{})[team]=row
    events=set(event_rows)
    heads=db.q("SELECT id,url FROM nba_source_artifacts WHERE kind='final-boxscore' "
        'AND http_status=200 AND received_at<=? ORDER BY received_at,id',(at,))
    versions={}
    for head in heads:
        route=urlsplit(head['url']); query=parse_qs(route.query,keep_blank_values=True)
        relevant=set()
        for value in query.get('event',[]):
            try: relevant.add(_event_id(value))
            except ValueError: continue
        if not relevant & events: continue
        source,body,route=_source(head['id'],at)
        rows,features,source,evidence=_box_evidence(source,body,route,allow_incomplete=True)
        eid=rows[0]['game_id']
        box_by_team={row['team']:row for row in rows}
        for team,original in event_rows[eid].items():
            matched=box_by_team.get(team)
            if matched is None or signature(original)!=signature(matched):
                raise ValueError('Totals historical box score/team identity conflict')
        versions.setdefault(eid,[]).append((source,features,evidence,rows))
    for eid,candidates in versions.items():
        candidates.sort(key=lambda item:(item[0]['received_at'],item[0]['id']))
        fixed=None; clocks={}
        for source,features,evidence,rows in candidates:
            invariant=_json(dict(duration=evidence['duration'],possession_method='symmetric_box_estimate_044'))
            if fixed is not None and fixed!=invariant:
                raise ValueError('Totals historical box duration/method correction conflict')
            fixed=invariant
            semantic=_json(evidence)
            clock=source['received_at']
            if clock in clocks and clocks[clock]!=semantic:
                raise ValueError('Totals historical box equal-clock correction is ambiguous')
            clocks[clock]=semantic
        complete=[item for item in candidates if item[1] is not None]
        selected=None
        if complete:
            latest=max(item[0]['received_at'] for item in candidates)
            if any(item[1] is None and item[0]['received_at']==latest for item in candidates):
                raise ValueError('Totals historical box newest correction is incomplete')
            selected=min((item for item in complete if item[0]['received_at']==latest),key=lambda item:item[0]['id'])
            source,features,evidence,rows=selected
            for row in rows:
                original=history.get((row['team'],eid))
                if original is None: continue  # Absent owner history is coverage, never synthesized evidence.
                original.update(features,offensive_efficiency=original['points']/features['raw_possessions'],
                    defensive_efficiency=original['opponent_points']/features['raw_possessions'],
                    possession_received_at=source['received_at'],possession_source_sha256=source['sha256'])
                if features.get('duration_source_artifact_id'):
                    original.update(duration_received_at=source['received_at'],duration_source_sha256=source['sha256'])
        selected_id=selected[0]['id'] if selected else None
        for source,features,evidence,rows in candidates:
            disposition=('incomplete' if features is None else 'selected' if source['id']==selected_id
                else 'duplicate' if selected and source['received_at']==selected[0]['received_at'] else 'superseded')
            sources[source['id']]=dict(_descriptor(source,'historical_possession_duration'),
                resolution_policy=HISTORICAL_BOX_POLICY,event_id=eid,
                selected_source_artifact_id=selected_id,disposition=disposition)


def training_examples(cutoff):
    """Earlier-score reconstructions for fitting, never saved prospective pairs.

    Sources are actually received by cutoff. A historical target may precede
    receipt: that assumption is explicit and must use the model's reconstructed
    feature mode. Label receipt and all feature receipts remain unmodified.
    """
    at=_number(cutoff)
    if not _exists('nba_source_artifacts'): return [],[]
    heads=db.q("SELECT id FROM nba_source_artifacts WHERE kind IN ('team-history','spread-team-history') "
        'AND http_status=200 AND received_at<=? ORDER BY received_at,id',(at,))
    history={}; sources={}
    signature=lambda r:tuple(r[k] for k in ('team','opponent','game_id','played_at','points','opponent_points','home','season_start_year'))
    for head in heads:
        rows,source=source_history(head['id'],at)
        for row in rows:
            key=(row['team'],row['game_id'])
            if key in history and signature(history[key])!=signature(row):
                raise ValueError('Totals historical source correction conflict')
            if key not in history: history[key]=row
        sources[source['id']]=_descriptor(source,'historical_reconstruction')
    # Every relevant eligible body is verified before optional missingness/selection.
    _historical_boxes(history,sources,at,signature)
    games={}
    for row in history.values():
        games.setdefault(row['game_id'],{})[row['team']]=row
    examples=[]
    ordered=sorted(games.values(),key=lambda entries:next(iter(entries.values()))['played_at'])
    allrows=list(history.values())
    for entries in ordered:
        sample=next(iter(entries.values())); target=sample['played_at']
        if target+FEATURE_LAG>=at: continue
        home=next((r for r in entries.values() if r['home']),None)
        away=next((r for r in entries.values() if not r['home']),None)
        if not home or not away: continue
        if (home['opponent'],home['points'],home['opponent_points'],home['played_at'])!=(away['team'],away['opponent_points'],away['points'],away['played_at']):
            raise ValueError('Totals reconstructed target teams disagree')
        prior=[dict(r) for r in allrows if r['played_at']<target-FEATURE_LAG and r['game_id']!=sample['game_id']]
        game=dict(game_id=f"nba-{home['season_start_year']}-{sample['game_id']}",home_team=home['team'],away_team=away['team'],tipoff_ts=target)
        prior=select_history(game,prior,target-3600)
        baseline=nba_model.project_game(home['team'],away['team'],prior,target)
        if baseline.get('status')!='ok': continue
        label_sources=[home['source_artifact_id'],away['source_artifact_id']]
        examples.append(dict(game_id=f"nba-{home['season_start_year']}-{sample['game_id']}",
            date_et=nba_identity.eastern_date(datetime.fromtimestamp(target,timezone.utc)),
            home=home['team'],away=away['team'],tipoff=target,tipoff_ts=target,as_of=target-3600,
            forecast_at=target-3600,rows=prior,actual_total=home['points']+away['points'],
            baseline_total=baseline['total'],label_received_at=max(home['received_at'],away['received_at']),
            result_at=max(home['received_at'],away['received_at']),label_source_ids=sorted(set(label_sources)),
            source_ids=sorted({r['source_artifact_id'] for r in prior}|set(label_sources)|
                {r['possession_source_artifact_id'] for r in prior if r.get('possession_source_artifact_id')}),
            reconstructed=True,reconstruction_cutoff=at,availability='reconstructed_at_cutoff'))
    return examples,[sources[i] for i in sorted(sources)]
