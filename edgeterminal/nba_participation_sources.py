"""Bounded official-gamebook evidence for absent, already sealed NBA players.

No missing row becomes a zero. Only the existing strict participation validator
may append an inactive proof; the ordinary grader decides the later void.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re
import time

import httpx

from . import db, nba_data, nba_identity, nba_participation, nba_store

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_REQUESTS_PER_RUN = 4
IO_TIMEOUT = 3.0
STREAM_DEADLINE = 6.0
RETRY_SECONDS = 15 * 60
KEY_PREFIX = 'nba_participation_attempt:'
ROUTE = re.compile(r'https://statsdmz\.nba\.com/pdfs/(\d{8})/\1_([A-Z]{3})([A-Z]{3})(?:_book)?\.pdf')


def _download(url):
    """Stream one exact official route without redirects or an unbounded body."""
    if not isinstance(url,str) or not ROUTE.fullmatch(url):
        raise ValueError('untrusted official gamebook route')
    started=time.monotonic();parts=[];size=0
    with httpx.stream('GET',url,timeout=IO_TIMEOUT,follow_redirects=False,
                      headers={'Accept':'application/pdf','User-Agent':'EdgeTerminal/0.4'}) as response:
        if 300<=response.status_code<400:raise ValueError('official gamebook redirect refused')
        length=response.headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length)>MAX_PDF_BYTES):
            raise ValueError('official gamebook exceeds size limit')
        for chunk in response.iter_bytes():
            size+=len(chunk)
            if size>MAX_PDF_BYTES:raise ValueError('official gamebook exceeds size limit')
            if time.monotonic()-started>STREAM_DEADLINE:
                raise TimeoutError('official gamebook streaming deadline exceeded')
            parts.append(chunk)
        return response.status_code,b''.join(parts)


def _urls(game):
    day=datetime.strptime(game['game_date_et'],'%Y-%m-%d').strftime('%Y%m%d')
    away,home=game['away_team'],game['home_team']
    if away not in nba_identity.TEAMS or home not in nba_identity.TEAMS or away==home:
        raise ValueError('official gamebook needs two canonical NBA teams')
    if nba_identity.eastern_date(nba_identity.parse_tipoff(game['tipoff_utc']))!=game['game_date_et']:
        raise ValueError('official gamebook date does not match saved tipoff')
    stem=f'https://statsdmz.nba.com/pdfs/{day}/{day}_{away}{home}'
    return [stem+'.pdf',stem+'_book.pdf']


def _cached_sources(url,now):
    return db.q("SELECT * FROM nba_source_artifacts WHERE url=? AND http_status=200 "
                "AND received_at<=? AND kind IN ('official-participation','official-participation-pdf') "
                'ORDER BY received_at DESC,id DESC LIMIT 1',(url,now))


def _identities(game,lock,missing,now):
    result={}
    for pid in missing:
        items=[i for i in lock['items'] if i['espn_player_id']==pid]
        ids=set()
        for item in items:
            forecast=nba_store.get_forecast(item['forecast_id'])
            if (not forecast or forecast['game_id']!=game['game_id'] or
                    hashlib.sha256(nba_store._json(forecast['payload']).encode()).hexdigest()!=forecast['payload_sha256']):
                raise ValueError('selected participation forecast hash or identity mismatch')
            ids.update(forecast['source_artifact_ids'])
        matches=[]
        for aid in sorted(ids):
            source=nba_store.get_artifact(aid)
            if source['kind']!='roster':continue
            raw=json.loads(source['body'])
            roster=nba_data.parse_roster(raw)
            for person in roster['players']:
                if person['espn_player_id']==pid:
                    matches.append((roster['team'],person['name'],aid))
        if len(matches)!=1:raise ValueError('selected participation roster identity is ambiguous')
        team,name,aid=matches[0]
        nba_participation._identity(game,pid,team,name,aid,now)
        result[pid]=(team,name,aid)
    return result


def collect_missing(game,lock,official,*,box_complete,now=None,max_requests=2,getter=None):
    """Resolve supported absent players; return source failures without grading.

    The caller holds the shared refresh lock and budgets requests across games.
    Cached failures stay visible until a retry or new verified evidence resolves
    them. A depleted cycle budget defers the lookup without fabricating success.
    """
    stamp=time.time() if now is None else now
    if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp):
        raise ValueError('official participation check requires a finite time')
    if isinstance(max_requests,bool) or not isinstance(max_requests,int) or not 0<=max_requests<=MAX_REQUESTS_PER_RUN:
        raise ValueError('invalid official participation request budget')
    result=dict(ok=True,status='not_applicable',requests=0,new_evidence=0,
                pending_player_ids=[],errors=[],checked_at=stamp)
    if (not lock or official.get('game_status')!='final' or official.get('verified_final') is not True
            or box_complete is not True):return result
    if official.get('game_id')!=game['game_id'] or lock.get('game_id')!=game['game_id']:
        raise ValueError('official participation game identity mismatch')
    present=[str(p.get('player_id')) for p in official.get('players',[])]
    if len(present)!=len(set(present)):raise ValueError('official participation player identity is ambiguous')
    missing=sorted({i['espn_player_id'] for i in lock['items']}-set(present))
    if not missing:return result
    result.update(status='pending',pending_player_ids=missing)
    if (official.get('home_score'),official.get('away_score'))!=(game['home_score'],game['away_score']):
        return dict(result,ok=False,errors=['official participation final score conflict'])
    canonical=[{k:item[k] for k in nba_store._ITEM_FIELDS} for item in sorted(lock['items'],key=lambda i:i['rank'])]
    if (len(canonical)!=5 or [i['rank'] for i in canonical]!=list(range(1,6)) or
            any(i['game_id']!=game['game_id'] for i in canonical) or lock['locked_at']>=game['tipoff_ts'] or
            hashlib.sha256(nba_store._json(canonical).encode()).hexdigest()!=lock['sha256']):
        return dict(result,ok=False,errors=['sealed participation selection hash or identity mismatch'])
    nba_participation.ensure_schema()
    existing=nba_participation.overrides(game['game_id'],official,box_complete=True,now=stamp)
    missing=[pid for pid in missing if pid not in existing]
    result['pending_player_ids']=missing
    if not missing:return dict(result,status='resolved')
    key=KEY_PREFIX+game['game_id'];prior=db.kv_get(key) or {}
    try:
        identities=_identities(game,lock,missing,stamp)
        urls=_urls(game)
    except (ValueError,TypeError,KeyError):
        return dict(result,ok=False,errors=['frozen participation identity could not be verified'])
    cached={url:_cached_sources(url,stamp) for url in urls}
    new_source=any(s['received_at']>prior.get('checked_at',0) for sources in cached.values() for s in sources)
    if not new_source and 0<=stamp-prior.get('checked_at',0)<RETRY_SECONDS:
        return dict(result,ok=not bool(prior.get('errors')),status='retry_wait',errors=prior.get('errors',[]))
    for url in urls:
        sources=cached[url]
        if not sources:
            if result['requests']>=max_requests:
                if not result['errors']:result['status']='deferred'
                break
            result['requests']+=1
            db.kv_set(key,dict(checked_at=stamp,status='running',errors=['official participation check unfinished']))
            try:
                status,raw=(getter or _download)(url)
                result['checked_at']=max(result['checked_at'],time.time() if getter is None else stamp)
                if not isinstance(raw,bytes) or len(raw)>MAX_PDF_BYTES:
                    raise ValueError('official gamebook exceeds size limit')
                source=dict(kind='official-participation',url=url,received_at=result['checked_at'],
                            http_status=status,sha256=hashlib.sha256(raw).hexdigest(),body=raw)
                aid=nba_store.save_artifact(source)
                if status!=200:continue
                sources=[dict(source,id=aid)]
            except Exception:
                result['errors'].append('official gamebook source unavailable')
                continue
        for source in sources:
            if len(source['body'])>MAX_PDF_BYTES or hashlib.sha256(source['body']).hexdigest()!=source['sha256']:
                result.update(ok=False,status='pending',errors=['official gamebook archived bytes failed verification'])
                db.kv_set(key,dict(checked_at=result['checked_at'],status=result['status'],errors=result['errors']))
                return result
            for pid in list(result['pending_player_ids']):
                team,name,roster_id=identities[pid]
                try:
                    nba_participation.record_verified_inactive(game['game_id'],pid,team,name,source['id'],
                        roster_id,page=0,now=result['checked_at'])
                except (ValueError,TypeError,KeyError):
                    continue
                result['new_evidence']+=1
                result['pending_player_ids'].remove(pid)
        if not result['pending_player_ids']:
            result.update(ok=True,status='resolved',errors=[])
            break
    if result['pending_player_ids'] and result['status']!='deferred':
        if not result['errors']:result['errors']=['official gamebook does not establish selected inactivity']
        result.update(ok=False,status='pending')
    db.kv_set(key,dict(checked_at=result['checked_at'],status=result['status'],errors=result['errors']))
    return result
