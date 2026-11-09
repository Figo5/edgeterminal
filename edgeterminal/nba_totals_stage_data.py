"""Explicit staged label boundary; legacy reservation semantics remain unchanged."""
from . import nba_totals_data as legacy, nba_totals_stage_store as store
from .nba_totals_data import (db, nba_game_forecasts, nba_model, nba_data, BASE,
    _number, _exists, _source, _stamp, _event_id, _json, _verify_total_market,
    validation_state, source_history, training_examples, forecast_inputs, prepare,
    verify_input_map, source_ids, ResultPending, _selected_input)
import hashlib
import json
import math
import re
from datetime import datetime
from urllib.parse import parse_qs

def _verified_reservation(value):
    if isinstance(value,dict) and value.get('inner_stage_reservation'):
        from . import nba_totals_stages
        return nba_totals_stages.validate_inner_reservation(value)
    return store.validate_reservation(value)

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
        market=saved['payload'].get('market') or {}; line=market.get('total')
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


inner_labels=validation_labels

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
        # Missing original maps remain explicitly unsupported; never reconstruct after tipoff.
        if mapped:
            if not set(source_ids(mapped))<=set(saved['source_artifact_ids']):
                raise ValueError('Totals input source is absent from common saved source list')
            saved_descriptors={r['id']:r for r in payload.get('source_artifacts') or []}
            if saved_descriptors:
                for item in mapped['source_artifacts']:
                    if saved_descriptors.get(item['id'])!={k:item[k] for k in ('id','sha256','received_at')}:
                        raise ValueError('Totals input receipt differs from common saved source descriptor')
        market=payload.get('market') or {}
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

