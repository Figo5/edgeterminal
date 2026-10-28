"""Append-only reviewed NBA participation evidence; no fetching or live picks.

An official final gamebook or dated game-specific Out report may establish
inactivity for a selected player omitted entirely from a reconciled box score.
Every use rechecks the archived source, frozen roster identity and game scope.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import pdfplumber

from . import db, nba_data, nba_identity, nba_store


def ensure_schema():
    nba_store.ensure_schema()
    db.ex('CREATE TABLE IF NOT EXISTS nba_reviewed_participation ('
          'id INTEGER PRIMARY KEY, game_id TEXT NOT NULL REFERENCES nba_games(game_id), '
          'espn_player_id TEXT NOT NULL, team TEXT NOT NULL, full_name TEXT NOT NULL, '
          "status TEXT NOT NULL CHECK(status='inactive'), source_artifact_id INTEGER NOT NULL "
          'REFERENCES nba_source_artifacts(id), roster_artifact_id INTEGER NOT NULL REFERENCES nba_source_artifacts(id), '
          'source_sha256 TEXT NOT NULL, roster_sha256 TEXT NOT NULL, home_score INTEGER NOT NULL, '
          'away_score INTEGER NOT NULL, page INTEGER NOT NULL CHECK(page>=0), proof TEXT NOT NULL, '
          'reviewed_at REAL NOT NULL, UNIQUE(game_id,espn_player_id,source_artifact_id,roster_artifact_id))')
    for action in ('UPDATE','DELETE'):
        db.ex(f'CREATE TRIGGER IF NOT EXISTS nba_participation_no_{action.lower()} BEFORE {action} '
              "ON nba_reviewed_participation BEGIN SELECT RAISE(ABORT, 'NBA participation evidence is immutable'); END")


def _compact(value):
    return re.sub(r'\s+','',value.replace('\u2019',"'")).casefold()


def _artifact(artifact_id,now):
    row=nba_store.get_artifact(artifact_id)
    if not row or row['http_status']!=200 or not math.isfinite(row['received_at']) or row['received_at']>now:
        raise ValueError('participation source needs HTTP 200 and a nonfuture receipt')
    if hashlib.sha256(row['body']).hexdigest()!=row['sha256']:
        raise ValueError('participation source hash mismatch')
    return row


def _identity(game,player_id,team,full_name,roster_id,now):
    selections=db.q('SELECT i.forecast_id,f.created_at,f.source_artifact_ids,f.payload FROM nba_selection_lock_items i '
        'JOIN nba_selection_locks l ON l.lock_id=i.lock_id JOIN nba_forecasts f ON f.forecast_id=i.forecast_id '
        'WHERE i.game_id=? AND i.espn_player_id=?',(game['game_id'],player_id))
    if not selections or team not in (game['home_team'],game['away_team']):
        raise ValueError('participation must match a sealed selected player and game team')
    source_ids=set()
    for selection in selections:
        ids=json.loads(selection['source_artifact_ids'])
        if roster_id not in ids:raise ValueError('roster was not captured with the selected forecast')
        source_ids.update(ids)
        rows=json.loads(selection['payload']).get('rows') or []
        matching=[r for r in rows if str(r.get('player_id'))==player_id]
        if rows and (not matching or any(_compact(r.get('player',''))!=_compact(full_name) or r.get('team')!=team for r in matching)):
            raise ValueError('participation player conflicts with the frozen selection identity')
    roster=_artifact(roster_id,now)
    path=f'/apis/site/v2/sports/basketball/nba/teams/{nba_identity.espn_team_id(team)}/roster'
    url=urlparse(roster['url'])
    if roster['kind']!='roster' or url.scheme!='https' or url.netloc!='site.api.espn.com' or url.path!=path or url.query or url.fragment:
        raise ValueError('participation needs the captured ESPN team roster source')
    if roster['received_at']>min(s['created_at'] for s in selections) or roster['received_at']>=game['tipoff_ts']:
        raise ValueError('participation roster was received after the frozen forecast')
    raw=json.loads(roster['body']);parsed=nba_data.parse_roster(raw)
    if parsed['team']!=team or parsed['season_label']!=game['season_label']:
        raise ValueError('participation roster game/season/team mismatch')
    people=raw.get('athletes') or []
    matches=[p for p in people if _compact(p.get('displayName',''))==_compact(full_name)]
    if len(matches)!=1 or str(matches[0].get('id'))!=player_id or sum(str(p.get('id'))==player_id for p in people)!=1:
        raise ValueError('participation full name does not uniquely identify the selected ESPN player')
    last=matches[0].get('lastName') or full_name.split()[-1]
    if sum(_compact(p.get('lastName') or p.get('displayName','').split()[-1])==_compact(last) for p in people)!=1:
        raise ValueError('participation roster surname is ambiguous')
    # An identity appearing on another frozen team roster is contradictory evidence.
    for aid in source_ids-{roster_id}:
        other=_artifact(aid,now)
        if other['kind']=='roster':
            other_raw=json.loads(other['body'])
            if any(str(p.get('id'))==player_id for p in other_raw.get('athletes') or []):
                raise ValueError('participation identity appears on multiple captured rosters')
    return roster,last


def _pdf_proof(source,game,team,full_name,last,page):
    url=urlparse(source['url'])
    if url.scheme!='https' or url.port not in (None,443) or url.username or url.password or url.query or url.fragment:
        raise ValueError('untrusted official participation PDF URL')
    book=re.fullmatch(r'/pdfs/(\d{8})/\1_([A-Z]{3})([A-Z]{3})(?:_book)?\.pdf',url.path)
    report=re.fullmatch(r'/referee/injury/Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf',url.path)
    if not ((url.hostname=='statsdmz.nba.com' and book) or (url.hostname=='ak-static.cms.nba.com' and report)):
        raise ValueError('unsupported official participation PDF host/path')
    raw=source['body']
    if not raw.startswith(b'%PDF-') or len(raw)>10*1024*1024:raise ValueError('official participation source is not a bounded PDF')
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            if not 1<=len(pdf.pages)<=40:raise ValueError('unsupported participation PDF page count')
            pages=[p.extract_text() or '' for p in pdf.pages]
    except Exception as exc:
        raise ValueError('official participation PDF cannot be read') from exc
    if isinstance(page,bool) or not isinstance(page,int) or not 0<=page<len(pages):raise ValueError('invalid proof page')
    day=datetime.strptime(game['game_date_et'],'%Y-%m-%d')
    if book:
        if source['received_at']<game['tipoff_ts']:raise ValueError('final gamebook was received before tipoff')
        if book.groups()!=(day.strftime('%Y%m%d'),game['away_team'],game['home_team']):
            raise ValueError('official gamebook URL game/date mismatch')
        text=pages[page];compact=_compact(text)
        if ('finalbox' not in compact or "nationalbasketballassociationofficialscorer'sreport" not in compact or
                _compact(day.strftime('%A, %B %d, %Y').replace(' 0',' ')) not in compact):
            raise ValueError('official final gamebook game/date header mismatch')
        for label,code,key in [('VISITOR',game['away_team'],'away_score'),('HOME',game['home_team'],'home_score')]:
            name=nba_identity.TEAMS[code][2];nickname=name.split()[-1]
            if _compact(label+':'+name) not in compact:raise ValueError('official gamebook team mismatch')
            scores=re.findall(r'^'+re.escape(nickname)+r'\s+(?:\d+\s+){4,}(\d+)\s*$',text,re.M|re.I)
            if len(scores)!=1 or int(scores[0])!=game[key]:raise ValueError('official gamebook final score mismatch')
        nickname=nba_identity.TEAMS[team][2].split()[-1]
        sections=re.findall(r'Inactive:\s*'+re.escape(nickname)+r'\s*-\s*(.*?)(?=\nInactive:|\nPoints in the Paint:|$)',text,re.I|re.S)
        if len(sections)!=1 or not re.search(r'(?<!\w)'+re.escape(last)+r'(?!\w)',sections[0],re.I):
            raise ValueError('official gamebook does not list this team/player inactive')
        return {'kind':'final_gamebook','page':page,'excerpt':'Inactive: '+nickname+' - '+sections[0]}
    stamp=datetime.strptime(''.join(report.groups()),'%Y-%m-%d%I%M%p').replace(tzinfo=nba_identity.EASTERN)
    if report[1]!=game['game_date_et'] or stamp.timestamp()>source['received_at']:
        raise ValueError('official injury report date/receipt mismatch')
    header='Injury Report: '+stamp.strftime('%m/%d/%y %I:%M %p')
    if _compact(header) not in _compact(pages[page]):raise ValueError('official injury report header mismatch')
    first=full_name.rsplit(' ',1)[0]
    name_pattern=re.escape(first).replace(r'\ ',r'\s*')
    matches=list(re.finditer(r'(?<!\w)'+re.escape(last)+r',\s*'+name_pattern+r'\s+Out(?:\s|$)',pages[page],re.I))
    if len(matches)!=1:raise ValueError('official injury report does not uniquely list the selected player Out')
    text_prefix='\n'.join(pages[:page]+[pages[page][:matches[0].start()]])
    prefix=_compact(text_prefix)
    matchups=re.findall(r'[a-z]{3}@[a-z]{3}',prefix);dates=re.findall(r'\b\d{2}/\d{2}/\d{4}\b',text_prefix)
    teams=[(prefix.rfind(_compact(v[2])),code) for code,v in nba_identity.TEAMS.items()]
    if (not matchups or matchups[-1]!=(game['away_team']+'@'+game['home_team']).casefold() or
            not dates or dates[-1]!=day.strftime('%m/%d/%Y') or max(teams)[1]!=team):
        raise ValueError('official injury report player belongs to another game/date/team')
    return {'kind':'game_day_out','page':page,'excerpt':last+','+first+' Out','matchup':game['away_team']+'@'+game['home_team']}


def _validate(row,now):
    if not isinstance(now,(int,float)) or isinstance(now,bool) or not math.isfinite(now):raise ValueError('invalid review time')
    game=nba_store.get_game(row['game_id'])
    if not game or game['status']!='final' or game['tipoff_ts']>now:raise ValueError('review requires the saved final game')
    if row.get('reviewed_at',now)>now:raise ValueError('participation review is from the future')
    source=_artifact(row['source_artifact_id'],now)
    roster,last=_identity(game,str(row['espn_player_id']),row['team'],row['full_name'],row['roster_artifact_id'],now)
    proof=_pdf_proof(source,game,row['team'],row['full_name'],last,row['page'])
    for field,value in [('source_sha256',source['sha256']),('roster_sha256',roster['sha256']),
                        ('home_score',game['home_score']),('away_score',game['away_score'])]:
        if field in row and row[field]!=value:raise ValueError('reviewed participation evidence changed')
    return dict(row,source_sha256=source['sha256'],roster_sha256=roster['sha256'],
                home_score=game['home_score'],away_score=game['away_score'],proof=json.dumps(proof,sort_keys=True))


def record_reviewed_inactive(game_id,player_id,team,full_name,source_artifact_id,roster_artifact_id,*,page,now=None):
    """Register visually reviewed official proof, rechecking its text and frozen identity."""
    ensure_schema();stamp=time.time() if now is None else now
    row=_validate({'game_id':game_id,'espn_player_id':str(player_id),'team':team,'full_name':full_name,
        'source_artifact_id':source_artifact_id,'roster_artifact_id':roster_artifact_id,'page':page,
        'status':'inactive','reviewed_at':stamp},stamp)
    fields=list(row)
    db.ex('INSERT OR IGNORE INTO nba_reviewed_participation('+','.join(fields)+') VALUES('+','.join('?'*len(fields))+')',tuple(row.values()))
    return db.q1('SELECT * FROM nba_reviewed_participation WHERE game_id=? AND espn_player_id=? AND source_artifact_id=? AND roster_artifact_id=?',
        (game_id,str(player_id),source_artifact_id,roster_artifact_id))


def has_new_review(game_id,last_checked,now):
    """A new receipt/review permits a fresh result read, never a grade by itself."""
    ensure_schema()
    return bool(db.q1('SELECT 1 FROM nba_reviewed_participation r JOIN nba_source_artifacts a '
        'ON a.id=r.source_artifact_id WHERE r.game_id=? AND r.reviewed_at<=? AND a.received_at<=? '
        'AND (r.reviewed_at>? OR a.received_at>?) LIMIT 1',(game_id,now,now,last_checked,last_checked)))


def overrides(game_id,official,*,box_complete,now):
    """Return evidence for absent selections only; a listed playing row is a conflict."""
    ensure_schema()
    rows=db.q('SELECT * FROM nba_reviewed_participation WHERE game_id=? ORDER BY id',(game_id,))
    if not rows or official.get('game_status')!='final' or official.get('verified_final') is not True or box_complete is not True:return {}
    if official.get('game_id')!=game_id:raise ValueError('participation overlay game identity conflict')
    result={}
    for saved in rows:
        row=_validate(saved,now)
        if (official.get('home_score'),official.get('away_score'))!=(row['home_score'],row['away_score']):
            raise ValueError('participation proof and final box scores conflict')
        matches=[p for p in official.get('players',[]) if str(p.get('player_id'))==row['espn_player_id']]
        if matches:
            if (len(matches)!=1 or matches[0].get('status') not in {'inactive','dnp'} or matches[0].get('stats') or
                    matches[0].get('minutes') not in (None,0) or (matches[0].get('participation') or {}).get('played') is True):
                raise ValueError('official participation evidence conflicts with a listed player')
            continue
        result[row['espn_player_id']]=row
    return result
