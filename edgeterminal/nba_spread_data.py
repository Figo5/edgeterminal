"""Bounded score-history bootstrap and source-verified NBA game evidence.

The worker uses public ESPN routes already allowed by the app. Research history
does not spend SportsGameOdds quota. Training snapshots are receipt bounded;
newly downloaded historical scores cannot be used to validate old live picks.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db,nba_data,nba_identity,nba_sources,nba_store
from .nba_learning_data import verify_source,descriptor

HISTORICAL_YEARS=(2024,2025,2026)
_BOX_CONNECTION=None
_BOX_CACHE={}


def ingest_bootstrap(directory,*,now=None):
    """Import public raw archives under the caller's shared refresh lock."""
    stamp=time.time() if now is None else float(now)
    folder=Path(directory).resolve();manifest=json.loads((folder/'manifest.json').read_text())
    inserted=[]
    for item in manifest.get('records',[]):
        year,team=item['year'],item['team']
        if year not in HISTORICAL_YEARS or team not in nba_identity.TEAMS:
            raise ValueError('Invalid NBA spread bootstrap team/season')
        filename=f'{year}-{team}.json'
        if item['file']!=filename:raise ValueError('Invalid NBA spread bootstrap filename')
        path=(folder/filename).resolve()
        if path.parent!=folder or path.stat().st_size>8*1024*1024:raise ValueError('Invalid NBA spread bootstrap file')
        expected=nba_sources.route(f'teams/{nba_identity.espn_team_id(team)}/schedule')+f'?season={year}&seasontype=2'
        if item['url']!=expected:raise ValueError('NBA spread bootstrap URL conflict')
        source=dict(id=0,kind='spread-team-history',url=expected,received_at=item['received_at'],
                    http_status=item['http_status'],sha256=item['sha256'],body=path.read_bytes())
        source_history(source,cutoff=stamp)
        inserted.append(nba_store.save_artifact(source))
    return dict(artifacts=len(inserted),source_ids=inserted)


def source_history(source,*,cutoff):
    body=verify_source(source,received_by=cutoff)
    parsed=urlparse(source['url'])
    match=re.fullmatch(r'/apis/site/v2/sports/basketball/nba/teams/(\d+)/schedule',parsed.path)
    owner=next((code for code,entry in nba_identity.TEAMS.items() if match and entry[0]==match[1]),None)
    query=parse_qs(parsed.query)
    if (parsed.hostname!='site.api.espn.com' or not owner or set(query)!={'season','seasontype'}
            or query['seasontype']!=['2'] or len(query['season'])!=1
            or not query['season'][0].isdigit() or not 2001<=int(query['season'][0])<=2030):
        raise ValueError('NBA spread history source team/season identity is invalid')
    if body.get('team'):
        if nba_identity.team_code(body['team'])!=owner:raise ValueError('NBA spread source owner conflict')
    year=int(query['season'][0]);result=[]
    for event in body.get('events',[]):
        if event.get('seasonType',{}).get('type',2)!=2:continue
        comp=(event.get('competitions') or [{}])[0]
        if comp.get('status',{}).get('type',{}).get('completed') is not True:continue
        for competitor in comp.get('competitors',[]):
            identity=competitor.get('team') or {}
            if identity.get('id') is not None:
                # Canonical code aliases never override an explicit numeric-ID
                # contradiction from an archived provider response.
                nba_identity.team_code(identity)
    for row in nba_sources.team_history(body,source['sha256']):
        stamp=nba_identity.parse_tipoff(row['played_at']).timestamp()
        # ESPN season labels use the ending year. Refuse wrong-season responses.
        start=nba_identity.parse_tipoff(f'{year-1}-09-01T00:00:00Z').timestamp()
        end=nba_identity.parse_tipoff(f'{year}-08-01T00:00:00Z').timestamp()
        if not start<=stamp<end:raise ValueError('NBA spread historical season mismatch')
        if row['team']==owner and stamp+4*3600<cutoff:
            result.append(dict(row,source_artifact_id=source['id']))
    return result


def training_history(cutoff):
    nba_store.ensure_schema()
    ids=db.q("SELECT MAX(id) AS id FROM nba_source_artifacts WHERE kind IN ('team-history','spread-team-history') "
             'AND received_at<? GROUP BY url',(cutoff,))
    rows={};sources=[]
    for item in ids:
        source=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(item['id'],))
        parsed=source_history(source,cutoff=cutoff)
        sources.append(descriptor(source,'training'))
        for row in parsed:
            key=(row['team'],str(row['game_id']))
            signature=lambda r:tuple(r.get(k) for k in ('played_at','points','opponent_points','opponent','home'))
            if key in rows and signature(rows[key])!=signature(row):raise ValueError('NBA spread history conflict')
            rows[key]=row
    enriched=enrich_from_boxes(list(rows.values()),cutoff)
    extra={row['possession_source_artifact_id'] for row in enriched if row.get('possession_source_artifact_id')}
    for ident in sorted(extra):
        source=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(ident,))
        # A cached parse avoids repeated transformation work, but fitting must
        # still bind its evidence to the currently archived bytes and receipt.
        verify_source(source,received_by=cutoff)
        sources.append(descriptor(source,'training'))
    return enriched,sources


def _box_sources(game_ids,asof):
    if not db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name='nba_game_grade_revisions'"):
        return []
    found={}
    for offset in range(0,len(game_ids),400):
        chunk=game_ids[offset:offset+400]
        placeholders=','.join('?' for _ in chunk)
        query=('SELECT DISTINCT a.id,a.kind,a.url,a.received_at,a.http_status,a.sha256,g.espn_event_id AS event_id '
               'FROM nba_source_artifacts a JOIN nba_game_grade_revisions r ON r.source_artifact_id=a.id '
               'JOIN nba_games g ON g.game_id=r.game_id WHERE a.received_at<=? '
               "AND a.kind='final-boxscore' AND g.espn_event_id IN ("+placeholders+')')
        for row in db.q(query,(asof,*chunk)):found[row['id']]=row
    return sorted(found.values(),key=lambda r:(r['received_at'],r['id']),reverse=True)


def enrich_from_boxes(history,asof):
    """Read existing verified final boxes only; sparse evidence remains sparse.

    No HTTP or source acquisition occurs. Cached parses are scoped to the exact
    SQLite connection plus immutable artifact ID/hash. Revision validation
    independently rechecks every referenced source body before publication.
    """
    global _BOX_CONNECTION
    if not history:return []
    if _BOX_CONNECTION is not db.conn():
        _BOX_CACHE.clear();_BOX_CONNECTION=db.conn()
    identifiers=sorted({str(r['game_id']).rsplit('-',1)[-1] for r in history})
    boxes={}
    for source in _box_sources(identifiers,asof):
        if source['received_at']>asof:continue
        gid=source['event_id']
        if gid in boxes:continue
        key=(source['id'],source['sha256'])
        if key not in _BOX_CACHE or 'body' in source:
            body=source.get('body')
            if body is None:body=db.q1('SELECT body FROM nba_source_artifacts WHERE id=?',(source['id'],))['body']
            payload=verify_source(dict(source,body=body),received_by=asof)
            parsed=nba_data.parse_summary(payload);game=parsed['game']
            url=urlparse(source['url'])
            if (game['status']!='final' or not parsed['box_complete'] or game['espn_event_id']!=gid
                    or nba_identity.parse_tipoff(game['tipoff_utc']).timestamp()>=source['received_at']
                    or url.hostname!='site.api.espn.com' or url.path!=urlparse(nba_sources.route('summary')).path
                    or parse_qs(url.query).get('event')!=[gid]):
                raise ValueError('NBA possession source final/game identity conflict')
            _BOX_CACHE[key]=(game,box_features(payload))
        boxes[gid]=(*_BOX_CACHE[key],source['id'])
    result=[]
    for original in history:
        row=dict(original);entry=boxes.get(str(row['game_id']).rsplit('-',1)[-1])
        if entry:
            game,stats,ident=entry
            side='home' if row['team']==game['home_team'] else 'away' if row['team']==game['away_team'] else None
            other='away' if side=='home' else 'home'
            if (side is None or row['points']!=game[side+'_score'] or row['opponent_points']!=game[other+'_score']
                    or nba_identity.parse_tipoff(game['tipoff_utc']).timestamp()!=nba_identity.parse_tipoff(row['played_at']).timestamp()):
                raise ValueError('NBA possession source score/time/team conflict')
            row.update(possessions=stats['possessions'],possession_method=stats['possession_method'],
                       possession_source_artifact_id=ident)
        result.append(row)
    return result


def refresh(now=None,max_requests=4):
    """Archive at most four missing team/season schedules per worker tick."""
    stamp=time.time() if now is None else float(now)
    if isinstance(max_requests,bool) or not isinstance(max_requests,int) or not 0<=max_requests<=4:
        raise ValueError('NBA spread bootstrap permits zero to four requests per tick')
    nba_store.ensure_schema();done=0;remaining=0;errors=[]
    targets=[(year,code) for year in HISTORICAL_YEARS for code in sorted(nba_identity.TEAMS)]
    for year,code in targets:
        path=f'teams/{nba_identity.espn_team_id(code)}/schedule'
        url=nba_sources.route(path)+f'?season={year}&seasontype=2'
        previous=db.q1('SELECT * FROM nba_source_artifacts WHERE url=? AND received_at<=? '
                        'AND http_status=200 ORDER BY received_at DESC,id DESC LIMIT 1',(url,stamp))
        if previous:
            try:source_history(previous,cutoff=stamp+1)
            except Exception as exc:errors.append(dict(team=code,season=year,error=type(exc).__name__,reason='Archived source integrity needs review.'))
            continue
        remaining+=1
        if done>=max_requests:continue
        done+=1
        try:
            obs=nba_sources.fetch(path,{'season':year,'seasontype':2},ttl=30*86400,now=stamp)
            raw=nba_sources.body(obs['sha256'])
            draft=dict(id=0,kind='spread-team-history',url=obs['url'],received_at=obs['observed_at'],
                       http_status=200,sha256=obs['sha256'],body=raw)
            source_history(draft,cutoff=max(stamp,obs['observed_at'])+1)
            nba_store.save_artifact(draft)
            remaining-=1
        except Exception as exc:
            errors.append(dict(team=code,season=year,error=type(exc).__name__))
    return dict(status='ok' if not errors else 'retry',requests=done,remaining_schedules=remaining,
                errors=errors,required_seasons=list(HISTORICAL_YEARS),
                message='Historical NBA spread inputs bootstrap automatically in bounded batches.')


def ready_at(now=None):
    """Fixed first complete archive receipt; it cannot slide with later refreshes."""
    stamp=time.time() if now is None else float(now)
    receipts=[]
    for year in HISTORICAL_YEARS:
        for code in nba_identity.TEAMS:
            url=nba_sources.route(f'teams/{nba_identity.espn_team_id(code)}/schedule')+f'?season={year}&seasontype=2'
            row=db.q1('SELECT * FROM nba_source_artifacts WHERE url=? AND http_status=200 AND received_at<=? '
                      'ORDER BY received_at,id LIMIT 1',(url,stamp))
            if not row:return None
            source_history(row,cutoff=stamp)
            receipts.append(row['received_at'])
    return max(receipts)


def box_features(payload):
    """Parse a symmetric, explicitly estimated possession count from a final box.

    This is feature evidence, not an automatic instruction to change the active
    scoring model. The estimate's impact must pass game-model validation first.
    """
    comp=(payload.get('header',{}).get('competitions') or [{}])[0]
    if comp.get('status',{}).get('type',{}).get('completed') is not True:
        raise ValueError('NBA possession evidence is not final')
    teams=payload.get('boxscore',{}).get('teams') or []
    if len(teams)!=2:raise ValueError('NBA possession evidence needs two team boxes')
    output=[]
    for entry in teams:
        code=nba_identity.team_code(entry['team'])
        stats={s.get('name'):s.get('displayValue',s.get('value')) for s in entry.get('statistics',[])}
        def count(name):
            value=nba_sources.number(stats.get(name))
            if value is None or value<0 or value!=int(value):raise ValueError('NBA possession count missing')
            return value
        def attempts(name):
            raw=str(stats.get(name,''));parts=raw.split('-')
            if len(parts)!=2:raise ValueError('NBA possession attempts missing')
            made,attempted=map(nba_sources.number,parts)
            if made is None or attempted is None or not 0<=made<=attempted<=300:raise ValueError('NBA possession attempts invalid')
            return attempted
        fga=attempts('fieldGoalsMade-fieldGoalsAttempted');fta=attempts('freeThrowsMade-freeThrowsAttempted')
        orb=count('offensiveRebounds');turnovers=count('totalTurnovers' if 'totalTurnovers' in stats else 'turnovers')
        output.append(dict(team=code,field_goal_attempts=fga,free_throw_attempts=fta,
                           offensive_rebounds=orb,turnovers=turnovers,
                           own_estimate=fga+.44*fta-orb+turnovers))
    if output[0]['team']==output[1]['team']:raise ValueError('NBA possession team identity conflict')
    estimate=sum(r['own_estimate'] for r in output)/2
    if not 50<=estimate<=200:raise ValueError('NBA possession estimate outside plausible range')
    return dict(possessions=estimate,possession_method='symmetric_box_estimate_044',teams=output)
