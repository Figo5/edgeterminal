"""NBA daily research: cached inputs, pretipoff records, automatic verified grading."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, nba_data, nba_identity, nba_model, nba_learning, nba_grading, nba_quotes, nba_sources, nba_store, nba_participation
from . import nba_game_context, nba_game_forecasts
from .integrity import parse_time

ET = ZoneInfo('America/New_York')
LABELS = {'schedule': 'Schedule & game prices', 'history': 'Historical inputs',
          'forecasts': 'Pregame forecasts', 'props': 'Player-prop capture', 'grading': 'Final results & grading',
          'spread_learning': 'Spread inputs & model checks',
          'totals_learning': 'Totals inputs & model checks',
          'evaluation': 'Chronological evaluation'}
PROP_BUDGET_REASONS = {
    'nba_monthly_budget':'the NBA monthly request allowance is exhausted',
    'nba_daily_budget':'the NBA daily request allowance is exhausted',
    'nba_object_budget':'the NBA monthly data allowance is exhausted',
    'local_30_day_quota_guard':'the shared monthly allowance is exhausted',
    'provider_quota_cooldown':'the provider quota cooldown is active',
    'provider_remaining_low':'the provider reports little remaining capacity',
    'nfl_capacity_reserved':'remaining shared requests are reserved for NFL',
    'local_per_minute_request_guard':'the shared request limit for this minute is active',
}


def ensure_schema():
    nba_store.ensure_schema()
    nba_quotes.ensure_schema()
    db.ex('CREATE TABLE IF NOT EXISTS nba_game_grade_revisions ('
          'id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, source_artifact_id INTEGER NOT NULL, '
          'graded_at REAL NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL, '
          'UNIQUE(game_id,sha256))')
    for action in ('UPDATE','DELETE'):
        db.ex(f'CREATE TRIGGER IF NOT EXISTS nba_game_grades_no_{action.lower()} BEFORE {action} '
              "ON nba_game_grade_revisions BEGIN SELECT RAISE(ABORT, 'NBA grades are append-only'); END")
    db.ex('CREATE TABLE IF NOT EXISTS nba_model_registry ('
          'version TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, cutoff TEXT NOT NULL, '
          'registered_at REAL NOT NULL, parameters TEXT NOT NULL)')
    for action in ('UPDATE','DELETE'):
        db.ex(f'CREATE TRIGGER IF NOT EXISTS nba_model_registry_no_{action.lower()} BEFORE {action} '
              "ON nba_model_registry BEGIN SELECT RAISE(ABORT, 'NBA model is fixed'); END")


def today(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now,ET).date().isoformat()


def validate_date(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        raise ValueError('Date must use YYYY-MM-DD')
    parsed=date.fromisoformat(value)
    if not 2025 <= parsed.year <= 2030:
        raise ValueError('NBA date is outside the supported research range')
    return parsed.isoformat()


def _season(day):
    d=date.fromisoformat(day)
    year=d.year if d.month>=9 else d.year-1
    return year, f'{year}-{(year+1)%100:02d}'


def step(name,now,seconds,action,*,force=False):
    previous=nba_store.get_cadence(name)
    if name=='props' and previous and (previous['last_detail'] or {}).get('status')=='history_loading':
        seconds=300
    nba_store.set_cadence(name,seconds,now=now)
    previous=nba_store.get_cadence(name)
    if not force and previous['last_run_at'] is not None and now < previous['next_due_at']:
        return {'ok': previous['last_status']!='error','status':'cached'}
    try:
        result=action()
        if isinstance(result,dict) and result.get('ok') is False:
            raise ValueError(result.get('message','NBA check incomplete'))
        detail=result if isinstance(result,dict) else {'status':'checked'}
        nba_store.record_run(name,'ok',detail,now=now)
        return {'ok':True,'result':detail}
    except Exception as exc:
        nba_store.record_run(name,'error',{'message':str(exc)[:250],'error_type':type(exc).__name__},now=now)
        return {'ok':False,'error':type(exc).__name__}


def artifact(observation,kind):
    return nba_store.save_artifact({'kind':kind,'url':observation['url'],
        'received_at':observation['observed_at'],'http_status':200,'sha256':observation['sha256'],
        'body':nba_sources.body(observation['sha256'])})


def parse_market(comp,observed_at,source_sha):
    offers=comp.get('odds') or []
    m={'home_spread':None,'away_spread':None,'home_price':None,'away_price':None,
       'total':None,'over_price':None,'under_price':None,'home_moneyline':None,'away_moneyline':None,
       'observed_at':observed_at,'source_sha256':source_sha,'source':'ESPN'}
    def price(value):
        v=nba_sources.number(value)
        return int(v) if v is not None and 100 <= abs(v) <= 100000 and v==int(v) else None
    for offer in offers:
        spread=offer.get('pointSpread') or {}
        total=offer.get('total') or {}
        ml=offer.get('moneyline') or {}
        hs=nba_sources.number((spread.get('home',{}).get('close') or {}).get('line'))
        aws=nba_sources.number((spread.get('away',{}).get('close') or {}).get('line'))
        if hs is not None and aws is not None and abs(hs+aws)<0.001 and abs(hs)<100:
            m.update(home_spread=hs,away_spread=aws,
                home_price=price(spread['home']['close'].get('odds')),
                away_price=price(spread['away']['close'].get('odds')))
        for side in ('over','under'):
            close=(total.get(side,{}) or {}).get('close') or {}
            raw_line=close.get('line')
            if isinstance(raw_line,str) and re.fullmatch(side[0]+r'\d+(?:\.\d+)?',raw_line.lower()):
                raw_line=raw_line[1:]
            line=nba_sources.number(raw_line)
            if line is not None and 100 <= line <= 400 and (m['total'] is None or m['total']==abs(line)):
                m['total']=abs(line);m[side+'_price']=price(close.get('odds'))
        for side in ('home','away'):
            m[side+'_moneyline']=price(((ml.get(side) or {}).get('close') or {}).get('odds'))
        m['sportsbook']=((offer.get('provider') or {}).get('displayName') or
                         (offer.get('provider') or {}).get('name') or 'Unknown')
        if any(m[k] is not None for k in ('home_spread','total','home_moneyline','away_moneyline')):
            break
    return m


def _schedule_cadence(now):
    next_game=db.q1("SELECT MIN(tipoff_ts) AS tip FROM nba_games WHERE status='scheduled' AND tipoff_ts>?",(now,))
    return 300 if next_game and next_game['tip'] is not None and next_game['tip']-now<=3600 else 900


def refresh_schedule(now):
    day=date.fromisoformat(today(now))
    start,end=day-timedelta(days=1),day+timedelta(days=7)
    games=[];receipts=[]
    # This ESPN endpoint accepts one date; range requests return HTTP 400.
    for offset in range(-1,8):
        target=day+timedelta(days=offset)
        ttl=_schedule_cadence(now) if offset==0 else 3600 if offset==1 else 6*3600
        obs=nba_sources.fetch('scoreboard',{'dates':target.strftime('%Y%m%d'),'limit':100},ttl=ttl,now=now)
        if not isinstance(obs['data'].get('events'),list):
            raise ValueError('NBA schedule response lacks an events list')
        source_id=artifact(obs,'scoreboard');receipts.append(obs['observed_at'])
        for raw in obs['data'].get('events',[]):
            if (raw.get('season') or {}).get('type')!=2:continue
            game=nba_data.parse_event(raw)
            if game['game_date_et']!=target.isoformat():
                raise ValueError('NBA schedule returned a game outside the requested Eastern date')
            nba_store.upsert_game(game,source_id,now=obs['observed_at'])
            games.append(game['game_id'])
            comp=(raw.get('competitions') or [{}])[0]
            db.kv_set('nba_game_context_'+game['game_id'],{'venue':(comp.get('venue') or {}).get('fullName')})
            if parse_time(game['tipoff_utc'])>obs['observed_at'] and game['status']=='scheduled':
                market=parse_market((raw.get('competitions') or [{}])[0],obs['observed_at'],obs['sha256'])
                market['source_artifact_id']=source_id
                db.kv_set('nba_market_'+game['game_id'],market)
        db.kv_set('nba_schedule_date_'+target.isoformat(),{'checked_at':obs['observed_at']})
    db.kv_set('nba_schedule',{'checked_at':max(receipts),'start':start.isoformat(),'end':end.isoformat(),
                              'game_ids':games})
    return {'message':f'{len(games)} regular season games checked; Eastern daily dates.', 'games':len(games)}


def unique_history(rows,identity):
    seen={}
    for row in rows:
        key=(str(row['game_id']),str(row[identity]))
        if key in seen and {k:v for k,v in row.items() if k not in {'source_sha256','source_artifact_id'}} != {
                k:v for k,v in seen[key].items() if k not in {'source_sha256','source_artifact_id'}}:
            raise ValueError('NBA history conflict')
        seen[key]=row
    return list(seen.values())


def load_team_history(code,year,now):
    rows=[]
    for season in (year,year+1):
        obs=nba_sources.fetch(f'teams/{nba_identity.espn_team_id(code)}/schedule',
            {'season':season,'seasontype':2},
            ttl=7*86400 if season==year else nba_game_context.source_ttl(code,now),now=now)
        source_id=artifact(obs,'team-history')
        rows.extend(dict(r,source_artifact_id=source_id) for r in nba_sources.team_history(obs['data'],obs['sha256']))
        if season==year+1:
            context=nba_game_context.parse_schedule(code,obs['data'],source_id,obs['observed_at'])
            db.kv_set('nba_team_schedule_context_'+code,context)
    # Each team schedule includes both teams' score rows; retain only the requested team.
    rows=unique_history([r for r in rows if r['team']==code],'team')
    db.kv_set('nba_team_history_'+code,rows)
    return rows


def _availability(player):
    return nba_game_context.roster_availability(player)


def load_roster(code,now):
    obs=nba_sources.fetch(f'teams/{nba_identity.espn_team_id(code)}/roster',
                          ttl=nba_game_context.source_ttl(code,now,'roster'),now=now)
    parsed=nba_data.parse_roster(obs['data'])
    if parsed['team']!=code:
        raise ValueError('NBA roster team identity conflict')
    if parsed['season_label']!=_season(today(now))[1]:
        raise ValueError('NBA roster season does not match the current research season')
    if len(parsed['players'])<5 or len({p['espn_player_id'] for p in parsed['players']})!=len(parsed['players']):
        raise ValueError('NBA current roster is incomplete or contains duplicate identities')
    source_id=artifact(obs,'roster')
    rows=[{'player_id':p['espn_player_id'],'player':p['name'],'team':code,
           'availability':_availability(p),'injuries':p['injuries'],'observed_at':obs['observed_at'],
           'source_artifact_id':source_id} for p in parsed['players']]
    db.kv_set('nba_roster_'+code,rows)
    return rows


def load_player_history(player,year,now):
    # Historical seasons cache for a week; current season comes from final box scores below.
    obs=nba_sources.fetch(f'athletes/{player["player_id"]}/gamelog',{'season':year},ttl=7*86400,now=now)
    source_id=artifact(obs,'player-history')
    rows=[dict(r,player=player['player'],source_artifact_id=source_id) for r in
          nba_sources.player_history(obs['data'],player['player_id'],obs['sha256'])]
    current=nba_sources.fetch(f'athletes/{player["player_id"]}/gamelog',{'season':year+1},ttl=6*3600,now=now)
    current_source=artifact(current,'player-history')
    live=[dict(r,player=player['player'],source_artifact_id=current_source) for r in
          nba_sources.player_history(current['data'],player['player_id'],current['sha256'])]
    # A new player/transfer's sourced old-team games remain history, with current roster used for identity.
    rows=unique_history(rows+live,'player_id')
    db.kv_set('nba_player_history_'+player['player_id'],rows)
    db.kv_set('nba_player_history_checked_'+player['player_id'],now)
    return rows


def histories(now):
    day=today(now); year,_=_season(day)
    games=db.q("SELECT * FROM nba_games WHERE tipoff_ts>? AND tipoff_ts<? AND status='scheduled'",
               (now,now+2*86400))
    codes=list(dict.fromkeys(g[s] for g in sorted(games,key=lambda g:g['tipoff_ts']) for s in ('home_team','away_team')))
    roster=[];team_rows=[];errors=[]
    for code in codes:
        try:
            team_rows.extend(load_team_history(code,year,now))
            roster.extend(load_roster(code,now))
        except Exception as exc:
            db.kv_set('nba_team_history_error_'+code,{'checked_at':now,'error':type(exc).__name__})
            errors.append(code)
    # Bounded public-source work per worker tick; resumes the remaining roster on the next tick.
    pending=sorted([p for p in roster if p['availability'] not in {'out','doubtful','inactive'} and
                    now-(db.kv_get('nba_player_history_checked_'+p['player_id']) or 0)>=6*3600],
                   key=lambda p:db.kv_get('nba_player_history_checked_'+p['player_id']) or 0)
    for player in pending[:24]:
        try:
            load_player_history(player,year,now)
            db.kv_set('nba_player_history_error_'+player['player_id'],None)
        except Exception as exc:
            # A failed identity gets a timed retry and cannot starve the remaining queue.
            db.kv_set('nba_player_history_checked_'+player['player_id'],now)
            db.kv_set('nba_player_history_error_'+player['player_id'],{'checked_at':now,'error':type(exc).__name__})
            errors.append(player['player_id'])
    return {'ok':not errors,'message':f'{len(codes)} teams, {len(roster)} roster identities; {min(24,len(pending))} player histories checked. {len(errors)} source errors remain isolated.',
            'teams':len(codes),'roster':len(roster),'remaining_player_histories':max(0,len(pending)-24),'source_errors':errors}


def _teams_history():
    return unique_history([r for code in nba_identity.TEAMS for r in (db.kv_get('nba_team_history_'+code) or [])],'team')


def _rosters(games):
    rows=[p for code in sorted({g[s] for g in games for s in ('home_team','away_team')})
            for p in (db.kv_get('nba_roster_'+code) or [])]
    counts=Counter(p['player_id'] for p in rows)
    # A stale roster around a trade must not lend that player's identity to either team.
    return [p for p in rows if counts[p['player_id']]==1]


def _players_history(roster):
    return [r for p in roster for r in (db.kv_get('nba_player_history_'+p['player_id']) or [])]


def _record():
    return {'won':0,'lost':0,'push':0,'void':0,'pending':0}


def _inc(record,result):
    result={'win':'won','loss':'lost'}.get(result,result)
    record[result if result in record else 'pending']+=1


def _forecast(game_id,kind):
    original=next((r for r in nba_store.list_forecasts(game_id) if r['payload'].get('kind')==kind),None)
    return nba_game_forecasts.selected(game_id,legacy=original) if kind=='game' else original


def comparison_market(game_id,saved):
    market=dict(saved['payload'].get('market') or {})
    if saved['payload'].get('kind')=='game-revision':
        return market
    for kind,keys in [('spread-line',('home_spread','away_spread','home_price','away_price')),
                      ('total-line',('total','over_price','under_price'))]:
        if market.get(keys[0]) is not None:continue
        later=_forecast(game_id,kind)
        if later:
            observation=later['payload']['market']
            for key in keys:market[key]=observation.get(key)
            market['comparison_observed_at']=observation.get('observed_at')
            if not market.get('sportsbook'):market['sportsbook']=observation.get('sportsbook')
    return market


def _prop_pause_message(now,checks=None):
    checks=nba_store.list_cadences() if checks is None else checks
    if any(r['last_status']=='error' for r in checks):
        return 'A source check needs attention; saved research remains visible. The built-in refresh retries automatically.'
    budget=nba_quotes.budget(now);reason=budget.get('blocked_reason')
    if not reason:return None
    detail=PROP_BUDGET_REASONS.get(reason,reason.replace('_',' '))
    if reason=='nfl_capacity_reserved':
        detail=f"{budget.get('nfl_reserved_requests',40)} requests are reserved for NFL ({budget.get('shared_remaining','unknown')} shared requests remain)"
    return f'NBA prop capture is paused because {detail}. The built-in refresh rechecks automatically.'


def _public_game(g,prop_notice=None):
    now=time.time()
    saved=_forecast(g['game_id'],'game')
    game={'id':g['game_id'],'home_team':g['home_team'],'away_team':g['away_team'],
          'venue':(db.kv_get('nba_game_context_'+g['game_id']) or {}).get('venue'),
          'tipoff':g['tipoff_utc'],'kickoff_utc':g['tipoff_utc'],'status':g['status'],
          'market':comparison_market(g['game_id'],saved) if saved else db.kv_get('nba_market_'+g['game_id']) or {},
          'latest_market':db.kv_get('nba_market_'+g['game_id']) or {},'projection':saved['payload'].get('projection') if saved else None,
          'forecast_at':saved['created_at'] if saved else None,'top_props':[],
          'forecast_message':'Historical inputs are still loading.' if g['tipoff_ts']>now else 'No pregame forecast was saved.',
          'prop_message':'Waiting for five supported main lines, verified identities and enough previous games.'}
    if saved:
        payload=saved['payload']; live=payload.get('kind')=='game-revision'
        schedules=(payload.get('input_manifest') or {}).get('schedule') or {}
        stale=any(v.get('stale') or not v.get('complete') for v in schedules.values())
        age=max(0,now-saved['created_at'])
        upcoming=g['status']=='scheduled' and g['tipoff_ts']>now
        game.update(forecast_id=saved['forecast_id'],forecast_policy=payload.get('policy_id'),
            forecast_refresh={'enabled':live,'age_seconds':age,'inputs_stale':stale,
                'state':'stale' if upcoming and (stale or age>900) else 'live' if live and upcoming else 'saved',
                'message':('Pregame updates continue automatically; the latest saved forecast before tipoff becomes the record.'
                           if live and upcoming else 'Saved pregame forecast and comparison lines remain in the record.')})
    lock=nba_store.get_lock('top5-'+g['game_id'])
    if lock:
        rows=_forecast(g['game_id'],'props')['payload']['rows']
        grades=nba_store.latest_grades(lock['lock_id'])
        for rank,row in enumerate(rows,1):
            grade=grades.get(rank) or {}
            game['top_props'].append(dict(row,rank=rank,selected_at=lock['locked_at'],
                result={'win':'won','loss':'lost'}.get(grade.get('result'),grade.get('result','pending')),
                actual=grade.get('actual_value')))
    elif g['tipoff_ts']<=now:
        game['prop_message']='No five-prop selection was saved before tipoff. This game will not be backfilled.'
    else:
        coverage=db.kv_get('nba_prop_coverage_'+g['game_id']) or {}
        if coverage.get('message'):game['prop_message']=coverage['message']
        if g['game_date_et']==today(now):
            if prop_notice is None:prop_notice=_prop_pause_message(now)
            if prop_notice:game['prop_message']=prop_notice
    game['props_state']='locked' if lock else 'awaiting_props' if g['tipoff_ts']>now else 'not_selected'
    return game


def board(day=None):
    ensure_schema();day=validate_date(day or today())
    checks=nba_store.list_cadences()
    now=time.time();saved_games=nba_store.list_games(day)
    pause=_prop_pause_message(now,checks) if day==today(now) and any(
        g['tipoff_ts']>now and not nba_store.get_game_lock(g['game_id']) for g in saved_games) else None
    games=[_public_game(g,prop_notice=pause or '') for g in saved_games]
    schedule=db.kv_get('nba_schedule') or {};perf=performance()
    attention=any(r['last_status']=='error' for r in checks)
    return {'sport':'NBA','date':day,'today':today(),'season':_season(day)[1],
        'designation':'PASS','readiness':'RESEARCH_ONLY','games':games,'record':perf['props'],
        'schedule_checked_at':schedule.get('checked_at') if schedule.get('start','9999')<=day<=schedule.get('end','0000') else None,
        'notice':'A source check needs attention; saved research remains visible.' if attention else
                 pause or ('Player lines and histories are collected by the built-in refresh.' if any(not g['top_props'] for g in games) else None)}


def matchup(game_id):
    ensure_schema()
    if not re.fullmatch(r'nba-\d{4}-\d{6,12}',game_id):
        raise ValueError('Unknown NBA game')
    game=nba_store.get_game(game_id)
    if not game:
        raise ValueError('Unknown NBA game')
    return {'sport':'NBA','game':_public_game(game),'designation':'PASS','readiness':'RESEARCH_ONLY'}


def performance():
    ensure_schema();result={'props':_record(),'winners':_record(),'spread':_record(),'total':_record()};days={}
    prop_versions=[];game_versions=[];total_versions=[];forecasts={}
    year=_season(today())[0]
    for lock in db.q('SELECT l.lock_id FROM nba_selection_locks l JOIN nba_games g ON g.game_id=l.game_id '
                     'WHERE g.season_start_year=?',(year,)):
        saved=nba_store.get_lock(lock['lock_id']);grades=nba_store.latest_grades(lock['lock_id'])
        for item in saved['items']:
            day=nba_store.get_game(item['game_id'])['game_date_et'];daily=days.setdefault(day,_record())
            state=(grades.get(item['rank']) or {}).get('result','pending')
            _inc(result['props'],state);_inc(daily,state)
            fid=item['forecast_id']
            if fid not in forecasts:forecasts[fid]=nba_store.get_forecast(fid)
            prop_versions.append({'game_id':item['game_id'],'model_version':forecasts[fid]['model_version'],
                                  'saved_at':saved['locked_at'],'result':state})
    for item in db.q('SELECT DISTINCT f.game_id FROM nba_forecasts f JOIN nba_games g ON g.game_id=f.game_id '
                    'WHERE g.season_start_year=? ORDER BY f.game_id',(year,)):
        row=_forecast(item['game_id'],'game')
        if not row:continue
        payload=row['payload']
        grade=db.q1('SELECT payload FROM nba_game_grade_revisions WHERE game_id=? ORDER BY id DESC LIMIT 1',(row['game_id'],))
        outcome=json.loads(grade['payload']) if grade else {}
        decisions={}
        for key in ('winners','spread','total'):
            # Lines absent at save time are excluded, never graded against later prices.
            if key=='winners' or comparison_market(row['game_id'],{'payload':payload}).get('home_spread' if key=='spread' else 'total') is not None:
                _inc(result[key],outcome.get(key,'pending'))
                decisions['winner' if key=='winners' else key]=outcome.get(key,'pending')
        game_versions.append({'game_id':row['game_id'],'model_version':row['model_version'],
                              'game_component_version':payload.get('game_component_version') or
                                  (payload.get('projection') or {}).get('game_component_version'),
                              'saved_at':row['created_at'],'results':decisions})
        if 'total' in decisions:
            projection=payload.get('projection') or {}
            version=projection.get('total_component_version')
            if not version:
                version='legacy-total:'+str(projection.get('game_component_version') or row['model_version'])
            total_versions.append({'game_id':row['game_id'],'model_version':version,
                                   'saved_at':row['created_at'],'result':decisions['total']})
    return dict(result,sport='NBA',season=_season(today())[1],days=[{'date':d,'props':r} for d,r in sorted(days.items(),reverse=True)],
                model_versions={'props':_model_versions(prop_versions),'games':_model_versions(game_versions,game_markets=True),
                                'totals':_model_versions(total_versions)},
                designation='PASS',readiness='RESEARCH_ONLY')


def _model_versions(rows, *, game_markets=False):
    """Summarize the saved forecast versions behind the counted season decisions."""
    grouped={}
    for row in rows:
        version=row.get('model_version')
        version=version if isinstance(version,str) and version else None
        component=row.get('game_component_version') if game_markets else None
        component=component if isinstance(component,str) and component else None
        grouped.setdefault((version,component),[]).append(row)
    result=[]
    for (version,component),items in grouped.items():
        dates=[r['saved_at'] for r in items if isinstance(r.get('saved_at'),(int,float))
               and not isinstance(r['saved_at'],bool) and math.isfinite(r['saved_at'])]
        cohort={'model_version':version,'game_count':len({r['game_id'] for r in items}),
                'first_saved_at':min(dates) if dates else None,'last_saved_at':max(dates) if dates else None}
        def summarize(states):
            record=_record()
            for state in states:_inc(record,state)
            decided=record['won']+record['lost']
            return {'record':record,'count':sum(record.values()),
                    'win_rate':record['won']/decided if decided else None}
        if game_markets:
            cohort.update(game_component_version=component,**{
                market:summarize([r['results'][market] for r in items if market in r['results']])
                for market in ('winner','spread','total')})
        else:cohort.update(summarize([r['result'] for r in items]))
        result.append(cohort)
    return sorted(result,key=lambda r:(r['first_saved_at'] is None,
        r['first_saved_at'] if r['first_saved_at'] is not None else 0,
        r['model_version'] or '',r.get('game_component_version') or ''))


def model_info():
    return nba_learning.model_info()


def register_model(now):
    ensure_schema()
    info=model_info()
    encoded=json.dumps(nba_learning.parameters(),sort_keys=True,separators=(',',':'))
    old=db.q1('SELECT * FROM nba_model_registry WHERE version=?',(info['version'],))
    if old and (old['fingerprint'],old['cutoff'],old['parameters'])!=(info['fingerprint'],info['cutoff'],encoded):
        raise ValueError('NBA model parameters/cutoff changed; existing forward research cannot be redefined')
    if not old:
        db.ex('INSERT INTO nba_model_registry VALUES(?,?,?,?,?)',
              (info['version'],info['fingerprint'],info['cutoff'],now,encoded))


def _totals_projection(game,projection,history,stamp):
    from . import nba_totals_data, nba_totals_stages as nba_totals_learning, nba_totals_model
    if not nba_totals_learning.needs_inputs():
        return projection,None
    state=nba_totals_learning.serving_state()
    rows=nba_totals_data.select_history(game,history,stamp)
    mapped=nba_totals_data.prepare(game,rows,stamp)
    nba_totals_data.verify_input_map(mapped,game,stamp)
    return nba_totals_model.compose(projection,mapped,model_state=state),mapped


def _refresh_game_projection(game,history,now):
    from . import nba_spread_model, nba_spread_data
    stamp=max(now,time.time())
    if stamp>=game['tipoff_ts']:return False
    history=nba_spread_data.enrich_from_boxes(history,stamp)
    schedule=nba_game_context.schedules(game,history,stamp)
    availability=nba_game_context.availability(game,stamp)
    market=db.kv_get('nba_market_'+game['game_id']) or {}
    projection=nba_spread_model.project_game(game['home_team'],game['away_team'],history,
        game['tipoff_utc'],as_of=stamp,schedule=schedule,availability=availability,market=market)
    if projection['status']!='ok':return False
    rows=projection.pop('selected_rows')
    feature_cutoff=projection.pop('feature_cutoff')
    totals_map=None
    totals_key='nba_totals_projection_'+game['game_id']
    try:
        projection,totals_map=_totals_projection(game,projection,history,stamp)
        db.kv_set(totals_key,{'ok':True,'checked_at':stamp})
    except (ValueError,TypeError,KeyError,OSError) as exc:
        db.kv_set(totals_key,{'ok':False,'checked_at':stamp,'error':type(exc).__name__})
    manifest={'rows':rows,'feature_cutoff':feature_cutoff,
        'schedule':{team:context for team,context in schedule.items() if context.get('source_artifact_id')},
        'coverage':projection.get('coverage') or {}}
    sources={row['source_artifact_id'] for row in rows}
    sources.update(row['possession_source_artifact_id'] for row in rows if row.get('possession_source_artifact_id'))
    sources.update(c['source_artifact_id'] for c in manifest['schedule'].values())
    sources.update(p['source_artifact_id'] for p in availability)
    sources.add(game['source_artifact_id'])
    if market.get('source_artifact_id'):sources.add(market['source_artifact_id'])
    if totals_map:
        from . import nba_totals_data
        sources.update(nba_totals_data.source_ids(totals_map))
    old=_forecast(game['game_id'],'game')
    stamp=max(stamp,time.time())
    if stamp>=game['tipoff_ts']:return False
    saved=nba_game_forecasts.save_revision(game['game_id'],projection,market,manifest,
        sorted(sources),now=stamp,model_info={'version':projection['model_version'],
            'fingerprint':projection['parameter_fingerprint'],'cutoff':projection['training_cutoff']})
    if totals_map:
        from . import nba_totals_stages as nba_totals_learning
        try:
            nba_totals_learning.capture_forecast(saved,totals_map,now=max(stamp,time.time()))
        except (ValueError,TypeError,KeyError,OSError) as exc:
            db.kv_set(totals_key,{'ok':False,'checked_at':stamp,'error':type(exc).__name__})
    return not old or old['forecast_id']!=saved['forecast_id']


def forecasts(now):
    register_model(now)
    history=_teams_history();saved=0;missing=0;revisions=0;errors=[]
    nba_game_forecasts.activate(max(now,time.time()))
    games=db.q("SELECT * FROM nba_games WHERE tipoff_ts>? AND tipoff_ts<? AND status='scheduled'",(now,now+2*86400))
    for game in games:
        try:
            existing=_forecast(game['game_id'],'game')
            if existing:
                current=db.kv_get('nba_market_'+game['game_id']) or {}
                if existing['payload'].get('kind')!='game-revision':
                    for kind,key in [('spread-line','home_spread'),('total-line','total')]:
                        if existing['payload'].get('market',{}).get(key) is None and current.get(key) is not None and not _forecast(game['game_id'],kind):
                            stamp=max(now,time.time())
                            if stamp<game['tipoff_ts'] and current.get('source_artifact_id'):
                                nba_store.save_forecast(kind+'-'+game['game_id'],game['game_id'],nba_model.MODEL_VERSION,
                                    {'kind':kind,'market':current},[current['source_artifact_id']],now=stamp)
            else:
                projection=nba_model.project_game(game['home_team'],game['away_team'],history,game['tipoff_utc'])
                if projection['status']!='ok':missing+=1;continue
                sources=sorted({r['source_artifact_id'] for r in history if r['team'] in {game['home_team'],game['away_team']}}|
                               {game['source_artifact_id']})
                raw=dict(projection,home_score=projection['proj_home'],away_score=projection['proj_away'],
                         home_margin=projection['margin'],away_win_probability=round(1-projection['home_win_probability'],4))
                stamp=max(now,time.time())
                if stamp>=game['tipoff_ts']:continue
                nba_store.save_forecast('game-'+game['game_id'],game['game_id'],nba_model.MODEL_VERSION,
                    {'kind':'game','projection':raw,'market':db.kv_get('nba_market_'+game['game_id']) or {},
                     'model':{'version':nba_model.MODEL_VERSION,'cutoff':'2026-10-01T00:00:00Z',
                              'fingerprint':nba_model.PARAMETER_FINGERPRINT}},sources,now=stamp)
                saved+=1
            revisions+=_refresh_game_projection(game,history,now)
            totals_check=db.kv_get('nba_totals_projection_'+game['game_id']) or {}
            if totals_check.get('ok') is False and totals_check.get('checked_at',0)>=now:
                errors.append(game['game_id'])
        except Exception as exc:
            db.kv_set('nba_game_refresh_error_'+game['game_id'],
                {'checked_at':max(now,time.time()),'error':type(exc).__name__,'message':str(exc)[:180]})
            errors.append(game['game_id'])
    return {'ok':not errors,
            'message':f'{saved} first pregame forecasts and {revisions} updated projections saved; {missing} await sufficient history; {len(errors)} checks need review.',
            'new_forecasts':saved,'new_revisions':revisions,'missing_histories':missing,'source_errors':errors}


def capture_props(now):
    with nba_learning.snapshot():
        return _capture_props(now)


def _capture_props(now):
    register_model(now)
    games=db.q("SELECT * FROM nba_games WHERE tipoff_ts>? AND tipoff_ts<? AND status='scheduled'",(now,now+24*3600))
    targets=[g for g in games if g['game_date_et']==today(now) and not nba_store.get_lock('top5-'+g['game_id']) and _forecast(g['game_id'],'game')]
    if not targets:return {'message':'No games await five-prop capture.','status':'no_targets'}
    if min(g['tipoff_ts'] for g in targets)-now>8*3600:
        return {'message':'NBA props wait until game day, within eight hours of the first tipoff.','status':'outside_capture_window'}
    # Do not spend a paid request while the relevant player history is still bootstrapping.
    roster=_rosters(targets)
    required_teams={g[s] for g in targets for s in ('home_team','away_team')}
    if not required_teams<={p['team'] for p in roster}:
        return {'message':'Waiting for verified current rosters for both teams.','status':'history_loading'}
    incomplete=[p for p in roster if p['availability'] not in {'out','doubtful','inactive'} and
                db.kv_get('nba_player_history_'+p['player_id']) is None and not db.kv_get('nba_player_history_error_'+p['player_id'])]
    if incomplete:return {'message':f'Waiting for {len(incomplete)} sourced player histories before requesting prop lines.',
                          'status':'history_loading'}
    ready=[dict(g,id=g['game_id'],tipoff=g['tipoff_utc']) for g in targets]
    verified_roster=[p for p in roster if not db.kv_get('nba_player_history_error_'+p['player_id']) or
                    db.kv_get('nba_player_history_'+p['player_id'])]
    result=nba_quotes.capture(ready,verified_roster,now)
    status=result['status']
    if status=='source_error':raise ValueError('SportsGameOdds NBA capture failed: '+result.get('error','unavailable'))
    if status!='captured':return {'message':'Prop capture waiting: '+status.replace('_',' ')+'.','status':status}
    request=db.q1('SELECT * FROM nba_prop_requests WHERE request_id=?',(result['request_id'],))
    source=nba_store.save_artifact({'kind':'player-quotes','url':nba_quotes.URL+'?leagueID=NBA',
        'received_at':result['observed_at'],'http_status':200,'sha256':request['raw_sha256'],'body':request['raw_body']})
    history=_players_history(roster);saved=0;waiting=[];context_errors=[]
    for game in targets:
        quotes=[q for q in result['quotes'] if q['game_id']==game['game_id']]
        stamp=max(time.time(),result['observed_at'])
        from . import nba_policy_evidence
        try:
            context=nba_policy_evidence.capture_context(game,quotes,verified_roster,history,source,stamp)
        except ValueError as exc:
            context_errors.append(game['game_id']+': '+str(exc))
            continue
        quotes=context['quotes']
        game_history=context['history']
        selection=context['selection']
        if selection['status']!='ok':
            waiting.append(game['game_id'])
            db.kv_set('nba_prop_coverage_'+game['game_id'],{'checked_at':stamp,'message':
                f"{selection.get('eligible_candidates',0)} supported player/stat markets were available at the last check; five are required. Missing lines or histories wait for the next guarded check."})
            continue
        rows=[];items=[]
        for row in selection['selections']:
            q=next(q for q in quotes if str(q['player_id'])==str(row['player_id']) and
                   q['market']==row['market'] and q['side']==row['side'] and q['line']==row['line'] and
                   q['sportsbook']==row['sportsbook'])
            quote={'quote_id':q['quote_id']}
            player=next(p for p in roster if p['player_id']==row['player_id'])
            projection=nba_learning.project_prop(row['player_id'],row['market'],game_history,game['tipoff_utc'],line=row['line'])
            rows.append(dict(row,confidence=row['probability'],player=q['player'],
                provider_player_id=q.get('provider_player_id'),
                identity_source_artifact_id=q.get('identity_source_artifact_id'),
                policy_context_id=context['context_id'],
                expected_minutes=projection.get('projected_minutes'),
                availability_note=f"ESPN roster status at capture: {player['availability']}. Minutes are forecast from prior sourced games; rotations may change."+
                    (' The latest statistics recheck failed; previously verified history was used.'
                     if db.kv_get('nba_player_history_error_'+row['player_id']) else ''),
                quote_id=quote['quote_id']))
            items.append({'forecast_id':'props-'+game['game_id'],'espn_player_id':row['player_id'],
                          'prop':row['market'],'side':row['side'],'line':row['line'],'quote_id':quote['quote_id']})
        player_ids={r['player_id'] for r in rows}
        sources=sorted({source,game['source_artifact_id']}|
                {r['identity_source_artifact_id'] for r in rows if r.get('identity_source_artifact_id') is not None}|
                {p['source_artifact_id'] for p in roster if p['player_id'] in player_ids}|
                {h['source_artifact_id'] for h in history if h['player_id'] in player_ids})
        stamp=max(stamp,time.time(),db.now())
        if stamp>=game['tipoff_ts']:
            context_errors.append(game['game_id']+': prop selection crossed tipoff')
            continue
        nba_store.save_forecast('props-'+game['game_id'],game['game_id'],model_info()['version'],
            {'kind':'props','rows':rows,'model':model_info()},sources,now=stamp)
        stamp=max(stamp,time.time(),db.now())
        if stamp>=game['tipoff_ts']:
            context_errors.append(game['game_id']+': prop locking crossed tipoff')
            continue
        nba_store.lock_selections('top5-'+game['game_id'],items,now=stamp)
        saved+=1
    if context_errors:
        raise ValueError('NBA per-game prop evidence checks failed after processing other targets: '+'; '.join(context_errors))
    return {'message':f'{saved} games received five saved selections; {len(waiting)} await five supported markets.',
            'status':'captured','new_locks':saved,'waiting_games':waiting,'quotes':len(result['quotes']),
            'identity_omissions':result.get('warnings',{}).get('player_identity',0)}


def _saved_markets(forecast,game):
    p=forecast['payload']['projection'];m=comparison_market(game['game_id'],forecast)
    markets=[{'market':'moneyline','side':'home' if p['home_margin']>=0 else 'away','line':None}]
    if m.get('home_spread') is not None:
        side='home' if p['home_margin']+m['home_spread']>=0 else 'away'
        markets.append({'market':'spread','side':side,
                        'line':m['home_spread'] if side=='home' else -m['home_spread']})
    if m.get('total') is not None:
        markets.append({'market':'total','side':'over' if p['total']>=m['total'] else 'under','line':m['total']})
    return {'game_id':game['game_id'],'tipoff':game['tipoff_utc'],'markets':markets}


def grade_due(now):
    from . import nba_participation_sources
    ensure_schema()
    games=db.q('SELECT DISTINCT g.* FROM nba_games g JOIN nba_forecasts f ON f.game_id=g.game_id '
               "WHERE g.tipoff_ts<=? OR (g.status IN ('final','canceled') AND g.tipoff_ts<=?) "
               'ORDER BY g.tipoff_ts DESC',(now-4*3600,now))
    count=0;pending_games=0;corrections=0;errors=[];participation_requests=0
    for game in games:
        graded_at=now
        try:
            last=db.kv_get('nba_grade_checked_'+game['game_id']) or 0
            lock=nba_store.get_lock('top5-'+game['game_id'])
            grades=nba_store.latest_grades(lock['lock_id']) if lock else {}
            forecast=_forecast(game['game_id'],'game')
            prior=db.q1('SELECT id,payload FROM nba_game_grade_revisions WHERE game_id=? ORDER BY id DESC LIMIT 1',(game['game_id'],))
            waiting=any((grades.get(i['rank']) or {}).get('result','pending')=='pending' for i in (lock or {}).get('items',[]))
            waiting=waiting or bool(forecast and (not prior or 'pending' in json.loads(prior['payload']).values()))
            terminal=game['status'] in {'final','canceled'}
            first_final=terminal and waiting and db.kv_get('nba_grade_checked_status_'+game['game_id'])!=game['status']
            new_review=nba_participation.has_new_review(game['game_id'],last,now)
            # New finals bypass a previous live check; unresolved evidence retries every five minutes.
            seconds=300 if (waiting or not terminal) and now-game['tipoff_ts']<=7*86400 else 86400
            if (now-last<seconds and not first_final and not new_review) or (not waiting and terminal and now-game['tipoff_ts']>7*86400 and not new_review):continue
            db.kv_set('nba_grade_checked_status_'+game['game_id'],game['status'])
            obs=nba_sources.fetch('summary',{'event':game['espn_event_id']},ttl=0 if first_final or new_review else seconds,now=now)
            graded_at=max(now,obs['observed_at'])
            source=artifact(obs,'final-boxscore');parsed=nba_data.parse_summary(obs['data']);actual_game=parsed['game']
            if actual_game['game_id']!=game['game_id'] or (actual_game['home_team'],actual_game['away_team'])!=(game['home_team'],game['away_team']):
                raise ValueError('NBA final result identity conflict')
            if actual_game['status'] not in {'final','canceled'}:
                pending_games+=not terminal
                db.kv_set('nba_grade_checked_'+game['game_id'],graded_at)
                continue
            if actual_game['status']=='final' and not parsed['box_complete']:
                raise ValueError('NBA final box score does not reconcile to team totals')
            if game['status']!='final':
                nba_store.upsert_game(actual_game,source,now=obs['observed_at'])
            elif (game['home_score'],game['away_score'])!=(actual_game['home_score'],actual_game['away_score']):
                corrections+=1
            players=[]
            for p in parsed['players']:
                stats=p.get('stats') or {}
                players.append({'player_id':p['espn_player_id'],'status':'active' if p['status']=='played' else
                    p['status'] if p['status'] in {'inactive','dnp'} else 'unknown',
                    'stats':p.get('stats'),'minutes':stats.get('minutes'),
                    'participation':{'played':True} if p['status']=='played' else None})
            official={'game_id':game['game_id'],'game_status':actual_game['status'],'home_score':actual_game['home_score'],
                      'away_score':actual_game['away_score'],'players':players,'verified_final':True}
            proof_budget=min(2,nba_participation_sources.MAX_REQUESTS_PER_RUN-participation_requests)
            try:
                proof_check=nba_participation_sources.collect_missing(
                    nba_store.get_game(game['game_id']),lock,official,box_complete=parsed['box_complete'],
                    now=graded_at,max_requests=proof_budget)
                participation_requests+=proof_check['requests']
                graded_at=max(graded_at,proof_check['checked_at'])
                proof_failed=not proof_check['ok']
            except (ValueError,TypeError,KeyError,OSError):
                # An interrupted lookup may already have used its full allowance.
                participation_requests+=proof_budget
                proof_failed=True
            if proof_failed:
                errors.append(game['game_id'])
                db.kv_set('nba_grade_error_'+game['game_id'],
                          {'checked_at':graded_at,'error':'OfficialParticipationSource'})
            participation=nba_participation.overrides(game['game_id'],official,box_complete=parsed['box_complete'],now=graded_at)
            players.extend({'player_id':pid,'status':'inactive','stats':None} for pid in participation)
            if forecast:
                graded=nba_grading.grade_game(_saved_markets(forecast,game),official,graded_at)
                payload={ {'moneyline':'winners','spread':'spread','total':'total'}[r['market']]:r['result'] for r in graded['grades']}
                payload.update(home_score=official['home_score'],away_score=official['away_score'])
                if forecast['payload'].get('kind')=='game-revision':
                    payload.update(forecast_id=forecast['forecast_id'],
                        forecast_sha256=forecast['payload_sha256'],
                        forecast_policy_id=forecast['payload']['policy_id'])
                encoded=json.dumps(payload,sort_keys=True)
                sha=hashlib.sha256((encoded+':'+str(prior['id'] if prior else 0)).encode()).hexdigest()
                if not prior or prior['payload']!=encoded:
                    db.ex('INSERT INTO nba_game_grade_revisions(game_id,source_artifact_id,graded_at,payload,sha256) VALUES(?,?,?,?,?)',
                          (game['game_id'],source,graded_at,encoded,sha));count+=1
            if lock:
                for item in lock['items']:
                    saved={'game_id':item['game_id'],'tipoff':game['tipoff_utc'],'player_id':item['espn_player_id'],
                           'market':item['prop'],'side':item['side'],'line':item['line']}
                    graded=nba_grading.grade_prop(saved,official,graded_at)
                    grade_result={'won':'win','lost':'loss'}.get(graded['result'],graded['result'])
                    old=grades.get(item['rank']) or {}
                    if old.get('result')==grade_result and old.get('actual_value')==graded['actual']:continue
                    proof=participation.get(item['espn_player_id'])
                    detail={'reason':graded['reason'],'source':'ESPN final box score','box_complete':True}
                    if proof:
                        proof_body=json.loads(proof['proof'])
                        label=('NBA official machine-verified participation'
                               if proof_body.get('verification_method')==nba_participation.AUTOMATIC_METHOD
                               else 'NBA official reviewed participation')
                        detail.update(source=label,participation_evidence_id=proof['id'],
                            box_source_artifact_id=source,proof=proof_body)
                    nba_store.add_grade_revision(lock['lock_id'],item['rank'],grade_result,graded['actual'],
                        proof['source_artifact_id'] if proof else source,detail=detail,now=graded_at)
                    count+=1
            db.kv_set('nba_grade_checked_'+game['game_id'],graded_at)
        except Exception as exc:
            db.kv_set('nba_grade_checked_'+game['game_id'],graded_at)
            db.kv_set('nba_grade_error_'+game['game_id'],{'checked_at':graded_at,'error':type(exc).__name__})
            if game['game_id'] not in errors:errors.append(game['game_id'])
    # Report the saved unresolved total even when a grade is unchanged or not due.
    pending_props=db.q1("SELECT COUNT(*) n FROM nba_selection_lock_items i "
        "JOIN nba_selection_locks l ON l.lock_id=i.lock_id JOIN nba_games g ON g.game_id=i.game_id "
        "WHERE g.status IN ('final','canceled') AND COALESCE((SELECT r.result FROM nba_grade_revisions r "
        "WHERE r.lock_id=i.lock_id AND r.rank=i.rank ORDER BY r.revision DESC LIMIT 1),'pending')='pending'")['n']
    return {'ok':not errors,'source_errors':errors,'message':f'{count} new statistical grades recorded; {pending_props} completed-game props still pending; {pending_games} games awaiting final results; {len(errors)} source errors.'+
                       (f' {corrections} final-score corrections retained as new evidence.' if corrections else ''),
            'new_grades':count,'pending':pending_props+pending_games,'pending_props':pending_props,
            'pending_games':pending_games,'corrections':corrections,
            'participation_requests':participation_requests}


def evaluate(now):
    register_model(now)
    historical=nba_learning.historical_evaluation()
    cutoff=nba_model.historical_evaluation()['holdout_start']
    end=parse_time('2026-10-01T00:00:00Z')
    teams=[r for r in _teams_history() if (parse_time(r['played_at']) or end)>=0 and (parse_time(r['played_at']) or end)<end]
    game_report=nba_model.evaluate_history(teams,cutoff)
    markets={market:dict(score,n=score['holdout_n'],mae=round(score['upgraded_mae'],3))
             for market,score in historical['markets'].items()}
    n=sum(m['n'] for m in markets.values())
    report=dict(historical,model=model_info(),games=game_report,markets=markets,evaluated_at=now,
        team_count=len({r['team'] for r in teams}),
        summary=f"Retrospective historical validation: {n} player/stat forecasts for the active prop model; {game_report.get('projected_games',0)} game forecasts use the retained game baseline.",
        note=historical['design'])
    db.kv_set('nba_evaluation',report)
    return {'message':report['summary'],'prop_predictions':n,'game_predictions':game_report.get('projected_games',0)}


def _props_cadence(now,previous):
    cadence=3*3600
    if not previous or previous['last_run_at'] is None:return cadence
    state=(previous['last_detail'] or {}).get('status');last=previous['last_run_at']
    if state in PROP_BUDGET_REASONS or state in {'history_loading','missing_api_key'}:return 300
    unlocked=[g for g in nba_store.list_games(today(now)) if g['tipoff_ts']>now and
              g['status']=='scheduled' and not nba_store.get_game_lock(g['game_id']) and _forecast(g['game_id'],'game')]
    if not unlocked:return cadence
    tip=min(g['tipoff_ts'] for g in unlocked)
    if state=='captured' and today(last)==today(now):return max(300,tip-45*60-last)
    if state in {'outside_capture_window','no_targets','no_pregame_targets','captured'}:
        midnight=datetime.fromtimestamp(now,ET).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        window=max(midnight,tip-8*3600,last+cadence if state=='captured' else 0)
        return max(1,window-last) if window>last else 300
    return cadence


def run(now=None):
    stamp=time.time() if now is None else now
    ensure_schema();register_model(stamp)
    results={}
    results['schedule']=step('schedule',stamp,_schedule_cadence(stamp),lambda:refresh_schedule(stamp))
    # Advance bounded history bootstrap every five minutes until all current roster identities are checked.
    results['history']=step('history',stamp,300,lambda:histories(stamp))
    results['forecasts']=step('forecasts',stamp,300,lambda:forecasts(stamp))
    previous=nba_store.get_cadence('props')
    results['props']=step('props',stamp,_props_cadence(stamp,previous),lambda:capture_props(stamp))
    if 'result' in results['props']:
        nba_store.set_cadence('props',_props_cadence(stamp,nba_store.get_cadence('props')),now=stamp)
    results['grading']=step('grading',stamp,300,lambda:grade_due(stamp))
    from . import nba_spread_learning
    results['spread_learning']=step('spread_learning',stamp,300,lambda:nba_spread_learning.refresh_inputs(stamp))
    from . import nba_totals_stages as nba_totals_learning
    results['totals_learning']=step('totals_learning',stamp,300,lambda:nba_totals_learning.refresh_inputs(stamp))
    # Publish frozen historical evidence for this version, without refitting it.
    coverage=db.kv_get('nba_evaluation') or {}
    changed_model=(coverage.get('model') or {}).get('fingerprint')!=model_info()['fingerprint']
    if not coverage or changed_model:
        results['evaluation']=step('evaluation',stamp,1800,lambda:evaluate(stamp),force=changed_model)
    healthy=all(r['ok'] for r in results.values())
    return {'ok':healthy,'needs_attention':not healthy,'checks':results,
            'message':'NBA built-in refresh checked due sources, captures and results.' if healthy else 'An NBA source check needs attention; the built-in refresh retries automatically.',
            'designation':'PASS','readiness':'RESEARCH_ONLY'}


def status():
    from . import model_learning, nba_spread_model, nba_spread_learning
    from . import nba_totals_stages as nba_totals_learning
    from .config import settings
    ensure_schema()
    checks=[{'name':r['name'],'label':LABELS.get(r['name'],r['name'].replace('_',' ').title()),
             'status':r['last_status'] or 'not_checked','checked_at':r['last_run_at'],'next_at':r['next_due_at'],
             'message':(r['last_detail'] or {}).get('message',r['last_status'] or 'Not checked yet')}
            for r in nba_store.list_cadences()]
    model=model_info();evaluation=db.kv_get('nba_evaluation') or {}
    if (evaluation.get('model') or {}).get('fingerprint')!=model['fingerprint']:
        evaluation={'status':'pending','model':model,
                    'summary':'The active model changed. Its chronological evaluation is due on the built-in refresh.'}
    return {'sport':'NBA','checks':checks,'budget':nba_quotes.budget(time.time()),'model':model,
            'game_model':nba_spread_model.model_info(),'game_learning':nba_spread_learning.status(now=time.time()),
            'totals_learning':nba_totals_learning.status(now=time.time()),
            'game_forecasts':nba_game_forecasts.status(),
            'runtime':{'mode':'hosted' if settings.runtime_mode=='always_on' else 'local',
                       'requires_awake_mac':settings.runtime_mode!='always_on'},
            'evaluation':evaluation,'learning':model_learning.status('NBA'),
            'designation':'PASS','readiness':'RESEARCH_ONLY'}
