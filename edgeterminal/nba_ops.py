"""NBA daily research: cached inputs, pretipoff records, automatic verified grading."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, nba_data, nba_identity, nba_model, nba_grading, nba_quotes, nba_sources, nba_store
from .integrity import parse_time

ET = ZoneInfo('America/New_York')
LABELS = {'schedule': 'Schedule & game prices', 'history': 'Historical inputs',
          'forecasts': 'Pregame forecasts', 'props': 'Player-prop capture', 'grading': 'Final results & grading',
          'evaluation': 'Chronological evaluation'}


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


def step(name,now,seconds,action):
    previous=nba_store.get_cadence(name)
    if name=='props' and previous and (previous['last_detail'] or {}).get('status')=='history_loading':
        seconds=300
    nba_store.set_cadence(name,seconds,now=now)
    previous=nba_store.get_cadence(name)
    if previous['last_run_at'] is not None and now < previous['next_due_at']:
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
        m['sportsbook']=(offer.get('provider') or {}).get('displayName') or 'Unknown'
        if any(m[k] is not None for k in ('home_spread','total','home_moneyline','away_moneyline')):
            break
    return m


def refresh_schedule(now):
    day=date.fromisoformat(today(now))
    start,end=day-timedelta(days=1),day+timedelta(days=7)
    games=[];receipts=[]
    # This ESPN endpoint accepts one date; range requests return HTTP 400.
    for offset in range(-1,8):
        target=day+timedelta(days=offset)
        ttl=900 if offset==0 else 3600 if offset==1 else 6*3600
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
            {'season':season,'seasontype':2},ttl=7*86400 if season==year else 6*3600,now=now)
        source_id=artifact(obs,'team-history')
        rows.extend(dict(r,source_artifact_id=source_id) for r in nba_sources.team_history(obs['data'],obs['sha256']))
    # Each team schedule includes both teams' score rows; retain only the requested team.
    rows=unique_history([r for r in rows if r['team']==code],'team')
    db.kv_set('nba_team_history_'+code,rows)
    return rows


def _availability(player):
    statuses=[str(i.get('status') or '').lower() for i in player.get('injuries',[])]
    for status in ('out','doubtful','questionable','probable'):
        if any(status in s for s in statuses):
            return status
    if player.get('roster_status') not in {'active','Active'}:
        return 'inactive'
    return 'not_listed_on_roster_injury_feed'


def load_roster(code,now):
    obs=nba_sources.fetch(f'teams/{nba_identity.espn_team_id(code)}/roster',ttl=3*3600,now=now)
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
    return next((r for r in nba_store.list_forecasts(game_id) if r['payload'].get('kind')==kind),None)


def comparison_market(game_id,saved):
    market=dict(saved['payload'].get('market') or {})
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


def _public_game(g):
    saved=_forecast(g['game_id'],'game')
    game={'id':g['game_id'],'home_team':g['home_team'],'away_team':g['away_team'],
          'venue':(db.kv_get('nba_game_context_'+g['game_id']) or {}).get('venue'),
          'tipoff':g['tipoff_utc'],'kickoff_utc':g['tipoff_utc'],'status':g['status'],
          'market':comparison_market(g['game_id'],saved) if saved else db.kv_get('nba_market_'+g['game_id']) or {},
          'latest_market':db.kv_get('nba_market_'+g['game_id']) or {},'projection':saved['payload'].get('projection') if saved else None,
          'forecast_at':saved['created_at'] if saved else None,'top_props':[],
          'forecast_message':'Historical inputs are still loading.' if g['tipoff_ts']>time.time() else 'No pregame forecast was saved.',
          'prop_message':'Waiting for five supported main lines, verified identities and enough previous games.'}
    lock=nba_store.get_lock('top5-'+g['game_id'])
    if lock:
        rows=_forecast(g['game_id'],'props')['payload']['rows']
        grades=nba_store.latest_grades(lock['lock_id'])
        for rank,row in enumerate(rows,1):
            grade=grades.get(rank) or {}
            game['top_props'].append(dict(row,rank=rank,selected_at=lock['locked_at'],
                result={'win':'won','loss':'lost'}.get(grade.get('result'),grade.get('result','pending')),
                actual=grade.get('actual_value')))
    elif g['tipoff_ts']<=time.time():
        game['prop_message']='No five-prop selection was saved before tipoff. This game will not be backfilled.'
    else:
        coverage=db.kv_get('nba_prop_coverage_'+g['game_id']) or {}
        if coverage.get('message'):game['prop_message']=coverage['message']
    game['props_state']='locked' if lock else 'awaiting_props' if g['tipoff_ts']>time.time() else 'not_selected'
    return game


def board(day=None):
    ensure_schema();day=validate_date(day or today())
    games=[_public_game(g) for g in nba_store.list_games(day)]
    schedule=db.kv_get('nba_schedule') or {};perf=performance()
    checks=nba_store.list_cadences()
    attention=any(r['last_status']=='error' for r in checks)
    return {'sport':'NBA','date':day,'today':today(),'season':_season(day)[1],
        'designation':'PASS','readiness':'RESEARCH_ONLY','games':games,'record':perf['props'],
        'schedule_checked_at':schedule.get('checked_at') if schedule.get('start','9999')<=day<=schedule.get('end','0000') else None,
        'notice':'A source check needs attention; saved research remains visible.' if attention else
                 'Player lines and histories are collected by the built-in refresh.' if any(not g['top_props'] for g in games) else None}


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
    year=_season(today())[0]
    for lock in db.q('SELECT l.lock_id FROM nba_selection_locks l JOIN nba_games g ON g.game_id=l.game_id '
                     'WHERE g.season_start_year=?',(year,)):
        saved=nba_store.get_lock(lock['lock_id']);grades=nba_store.latest_grades(lock['lock_id'])
        for item in saved['items']:
            day=nba_store.get_game(item['game_id'])['game_date_et'];daily=days.setdefault(day,_record())
            state=(grades.get(item['rank']) or {}).get('result','pending')
            _inc(result['props'],state);_inc(daily,state)
    for row in db.q('SELECT f.* FROM nba_forecasts f JOIN nba_games g ON g.game_id=f.game_id '
                    'WHERE g.season_start_year=? ORDER BY f.created_at',(year,)):
        payload=json.loads(row['payload'])
        if payload.get('kind')!='game':continue
        grade=db.q1('SELECT payload FROM nba_game_grade_revisions WHERE game_id=? ORDER BY id DESC LIMIT 1',(row['game_id'],))
        outcome=json.loads(grade['payload']) if grade else {}
        for key in ('winners','spread','total'):
            # Lines absent at save time are excluded, never graded against later prices.
            if key=='winners' or comparison_market(row['game_id'],{'payload':payload}).get('home_spread' if key=='spread' else 'total') is not None:
                _inc(result[key],outcome.get(key,'pending'))
    return dict(result,sport='NBA',season=_season(today())[1],days=[{'date':d,'props':r} for d,r in sorted(days.items(),reverse=True)],
                designation='PASS',readiness='RESEARCH_ONLY')


def model_info():
    return {'version':nba_model.MODEL_VERSION,'cutoff':'2026-10-01T00:00:00Z',
            'fingerprint':nba_model.PARAMETER_FINGERPRINT}


def register_model(now):
    info=model_info()
    encoded=json.dumps(nba_model.PARAMETERS,sort_keys=True,separators=(',',':'))
    old=db.q1('SELECT * FROM nba_model_registry WHERE version=?',(info['version'],))
    if old and (old['fingerprint'],old['cutoff'],old['parameters'])!=(info['fingerprint'],info['cutoff'],encoded):
        raise ValueError('NBA model parameters/cutoff changed; existing forward research cannot be redefined')
    if not old:
        db.ex('INSERT INTO nba_model_registry VALUES(?,?,?,?,?)',
              (info['version'],info['fingerprint'],info['cutoff'],now,encoded))


def forecasts(now):
    register_model(now)
    history=_teams_history();saved=0;missing=0
    games=db.q("SELECT * FROM nba_games WHERE tipoff_ts>? AND tipoff_ts<? AND status='scheduled'",(now,now+2*86400))
    for game in games:
        existing=_forecast(game['game_id'],'game')
        if existing:
            current=db.kv_get('nba_market_'+game['game_id']) or {}
            for kind,key in [('spread-line','home_spread'),('total-line','total')]:
                if existing['payload'].get('market',{}).get(key) is None and current.get(key) is not None and not _forecast(game['game_id'],kind):
                    stamp=max(now,time.time())
                    if stamp<game['tipoff_ts'] and current.get('source_artifact_id'):
                        nba_store.save_forecast(kind+'-'+game['game_id'],game['game_id'],nba_model.MODEL_VERSION,
                            {'kind':kind,'market':current},[current['source_artifact_id']],now=stamp)
            continue
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
             'model':model_info()},sources,now=stamp)
        saved+=1
    return {'message':f'{saved} new pregame forecasts saved; {missing} await sufficient history.',
            'new_forecasts':saved,'missing_histories':missing}


def capture_props(now):
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
    history=_players_history(roster);saved=0;waiting=[]
    for game in targets:
        quotes=[q for q in result['quotes'] if q['game_id']==game['game_id']]
        stamp=max(time.time(),result['observed_at'])
        injuries=[{'player_id':p['player_id'],'status':p['availability']} for p in roster]
        selection=nba_model.select_props(quotes,history,game['tipoff_utc'],stamp,injuries=injuries)
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
            quote=nba_store.save_quote_observation(game['game_id'],nba_store.PROP_MARKETS[q['market']],q['player_id'],q['side'],q['line'],
                       q['price'],q['sportsbook'],q['observed_at'],source_artifact_id=source,now=stamp)
            player=next(p for p in roster if p['player_id']==row['player_id'])
            projection=nba_model.project_prop(row['player_id'],row['market'],history,game['tipoff_utc'],line=row['line'])
            rows.append(dict(row,confidence=row['probability'],player=q['player'],
                expected_minutes=projection.get('projected_minutes'),
                availability_note=f"ESPN roster status at capture: {player['availability']}. Minutes use prior sourced games; rotations may change."+
                    (' The latest statistics recheck failed; previously verified history was used.'
                     if db.kv_get('nba_player_history_error_'+row['player_id']) else ''),
                quote_id=quote['quote_id']))
            items.append({'forecast_id':'props-'+game['game_id'],'espn_player_id':row['player_id'],
                          'prop':row['market'],'side':row['side'],'line':row['line'],'quote_id':quote['quote_id']})
        player_ids={r['player_id'] for r in rows}
        sources=sorted({source,game['source_artifact_id']}|
                {p['source_artifact_id'] for p in roster if p['player_id'] in player_ids}|
                {h['source_artifact_id'] for h in history if h['player_id'] in player_ids})
        nba_store.save_forecast('props-'+game['game_id'],game['game_id'],nba_model.MODEL_VERSION,
            {'kind':'props','rows':rows,'model':model_info()},sources,now=stamp)
        nba_store.lock_selections('top5-'+game['game_id'],items,now=stamp)
        saved+=1
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
    ensure_schema()
    games=db.q('SELECT DISTINCT g.* FROM nba_games g JOIN nba_forecasts f ON f.game_id=g.game_id '
               "WHERE g.tipoff_ts<=? OR (g.status IN ('final','canceled') AND g.tipoff_ts<=?) "
               'ORDER BY g.tipoff_ts DESC',(now-4*3600,now))
    count=0;pending=0;corrections=0;errors=[]
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
            # New finals bypass a previous live check; unresolved evidence retries every five minutes.
            seconds=300 if (waiting or not terminal) and now-game['tipoff_ts']<=7*86400 else 86400
            if (now-last<seconds and not first_final) or (not waiting and terminal and now-game['tipoff_ts']>7*86400):continue
            db.kv_set('nba_grade_checked_status_'+game['game_id'],game['status'])
            obs=nba_sources.fetch('summary',{'event':game['espn_event_id']},ttl=0 if first_final else seconds,now=now)
            graded_at=max(now,obs['observed_at'])
            source=artifact(obs,'final-boxscore');parsed=nba_data.parse_summary(obs['data']);actual_game=parsed['game']
            if actual_game['game_id']!=game['game_id'] or (actual_game['home_team'],actual_game['away_team'])!=(game['home_team'],game['away_team']):
                raise ValueError('NBA final result identity conflict')
            if actual_game['status'] not in {'final','canceled'}:pending+=1;db.kv_set('nba_grade_checked_'+game['game_id'],graded_at);continue
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
            if forecast:
                graded=nba_grading.grade_game(_saved_markets(forecast,game),official,graded_at)
                payload={ {'moneyline':'winners','spread':'spread','total':'total'}[r['market']]:r['result'] for r in graded['grades']}
                payload.update(home_score=official['home_score'],away_score=official['away_score'])
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
                    nba_store.add_grade_revision(lock['lock_id'],item['rank'],grade_result,graded['actual'],source,
                        detail={'reason':graded['reason'],'source':'ESPN final box score','box_complete':True},now=graded_at)
                    count+=1
                    pending+=graded['result']=='pending'
            db.kv_set('nba_grade_checked_'+game['game_id'],graded_at)
        except Exception as exc:
            db.kv_set('nba_grade_checked_'+game['game_id'],graded_at)
            db.kv_set('nba_grade_error_'+game['game_id'],{'checked_at':graded_at,'error':type(exc).__name__})
            errors.append(game['game_id'])
    return {'ok':not errors,'source_errors':errors,'message':f'{count} new statistical grades recorded; {pending} completed-game results still pending; {len(errors)} source errors.'+
                       (f' {corrections} final-score corrections retained as new evidence.' if corrections else ''),
            'new_grades':count,'pending':pending,'corrections':corrections}


def evaluate(now):
    register_model(now)
    cutoff='2026-03-01T00:00:00Z';end=parse_time(model_info()['cutoff'])
    teams=[r for r in _teams_history() if (parse_time(r['played_at']) or end)>=0 and (parse_time(r['played_at']) or end)<end]
    game_report=nba_model.evaluate_history(teams,cutoff)
    roster=[p for code in nba_identity.TEAMS for p in (db.kv_get('nba_roster_'+code) or [])]
    histories=_players_history(roster);markets={};start=parse_time(cutoff)
    for market in nba_model.MARKETS:
        errors=[]
        for pid in sorted({r['player_id'] for r in histories}):
            player_history=[r for r in histories if r['player_id']==pid]
            rows=sorted([r for r in player_history if start<=(parse_time(r['played_at']) or 0)<end],
                        key=lambda r:r['played_at'])
            for actual in rows:
                prediction=nba_model.project_prop(pid,market,player_history,actual['played_at'])
                if prediction['status']=='ok':errors.append(prediction['projection']-actual[market])
        markets[market]={'n':len(errors),'mae':round(sum(abs(e) for e in errors)/len(errors),3) if errors else None}
    n=sum(m['n'] for m in markets.values())
    report={'model':model_info(),'holdout_start':cutoff,'holdout_end':model_info()['cutoff'],
        'games':game_report,'markets':markets,'evaluated_at':now,'player_count':len({r['player_id'] for r in histories}),
        'team_count':len({r['team'] for r in teams}),
        'summary':f"Chronological 2025–26 holdout: {game_report.get('projected_games',0)} game forecasts; {n} player/stat forecasts across {len({r['player_id'] for r in histories})} currently rostered players.",
        'note':'Fixed descriptive baseline. Current-roster historical sample has survivorship bias. No historical betting lines were used; no retrospective selection win rate or profitability is claimed. Each holdout forecast uses only earlier games.'}
    db.kv_set('nba_evaluation',report)
    return {'message':report['summary'],'prop_predictions':n,'game_predictions':game_report.get('projected_games',0)}


def run(now=None):
    stamp=time.time() if now is None else now
    ensure_schema();register_model(stamp)
    results={}
    results['schedule']=step('schedule',stamp,900,lambda:refresh_schedule(stamp))
    # Advance bounded history bootstrap every five minutes until all current roster identities are checked.
    results['history']=step('history',stamp,300,lambda:histories(stamp))
    results['forecasts']=step('forecasts',stamp,300,lambda:forecasts(stamp))
    previous=nba_store.get_cadence('props')
    cadence=3*3600
    if previous and (previous['last_detail'] or {}).get('status')=='captured':
        unlocked=[g for g in nba_store.list_games(today(stamp)) if g['tipoff_ts']>stamp and not nba_store.get_game_lock(g['game_id'])]
        if unlocked:
            cadence=max(300,min(g['tipoff_ts'] for g in unlocked)-45*60-previous['last_run_at'])
    results['props']=step('props',stamp,cadence,lambda:capture_props(stamp))
    # History loading should not consume the paid-capture cadence.
    if results['props'].get('result',{}).get('status')=='history_loading':
        nba_store.set_cadence('props',300,now=stamp)
    results['grading']=step('grading',stamp,300,lambda:grade_due(stamp))
    # Refresh the descriptive report as source coverage grows, without fitting parameters.
    coverage=db.kv_get('nba_evaluation') or {}
    current_players=sum(bool(db.kv_get('nba_player_history_'+p['player_id'])) for code in nba_identity.TEAMS
                         for p in (db.kv_get('nba_roster_'+code) or []))
    if current_players>coverage.get('player_count',0) or not coverage:
        results['evaluation']=step('evaluation',stamp,1800,lambda:evaluate(stamp))
    return {'ok':True,'needs_attention':any(not r['ok'] for r in results.values()),'checks':results,
            'message':'NBA built-in refresh checked due sources, captures and results.',
            'designation':'PASS','readiness':'RESEARCH_ONLY'}


def status():
    ensure_schema()
    checks=[{'name':r['name'],'label':LABELS.get(r['name'],r['name'].replace('_',' ').title()),
             'status':r['last_status'] or 'not_checked','checked_at':r['last_run_at'],'next_at':r['next_due_at'],
             'message':(r['last_detail'] or {}).get('message',r['last_status'] or 'Not checked yet')}
            for r in nba_store.list_cadences()]
    return {'sport':'NBA','checks':checks,'budget':nba_quotes.budget(time.time()),'model':model_info(),
            'evaluation':db.kv_get('nba_evaluation') or {},'designation':'PASS','readiness':'RESEARCH_ONLY'}
