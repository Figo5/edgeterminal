"""Reviewed official PDFs may resolve only a uniquely identified absent player."""
import hashlib
import json
import sqlite3

import pytest

from edgeterminal import db, nba_data, nba_ops, nba_store
from test_nba_ops import isolated, _grading_case, _summary_feed


def pdf_bytes(*pages):
    """Small valid PDFs keep these parser/security tests independent of live sources."""
    objects=[b'',b'<< /Type /Catalog /Pages 2 0 R >>',b'',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'];kids=[]
    for text in pages:
        page_id=len(objects);kids.append(f'{page_id} 0 R')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id+1} 0 R >>'.encode())
        content='BT /F1 10 Tf 20 770 Td\n'+'\n'.join(
            '('+line.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')+') Tj 0 -12 Td'
            for line in text.splitlines())+'\nET'
        raw=content.encode();objects.append(f'<< /Length {len(raw)} >>\nstream\n'.encode()+raw+b'\nendstream')
    objects[2]=f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'.encode()
    raw=bytearray(b'%PDF-1.4\n');offsets=[0]
    for i,obj in enumerate(objects[1:],1):
        offsets.append(len(raw));raw.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    start=len(raw);raw.extend(f'xref\n0 {len(objects)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:raw.extend(f'{offset:010} 00000 n \n'.encode())
    raw.extend(f'trailer << /Size {len(objects)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode())
    return bytes(raw)


BOOK="""NATIONAL BASKETBALL ASSOCIATION OFFICIAL SCORER'S REPORT
FINAL BOX
Sunday, March 15, 2026 Fixture Arena
VISITOR: Minnesota Timberwolves (1-1)
HOME: Oklahoma City Thunder (1-1)
SCORE BY PERIOD 1 2 3 4 FINAL
Timberwolves 25 25 25 28 103
Thunder 29 29 29 29 116
Inactive: Timberwolves - Ingles (Injury/Illness - Ankle)
Inactive: Thunder - Other
Points in the Paint: Timberwolves 40, Thunder 40"""
BOOK_URL='https://statsdmz.nba.com/pdfs/20260315/20260315_MINOKC.pdf'
REPORT_URL='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-03-15_12_00PM.pdf'


def artifact(raw,url,received,status=200,kind='official-participation'):
    return nba_store.save_artifact({'kind':kind,'url':url,'received_at':received,
        'http_status':status,'sha256':hashlib.sha256(raw).hexdigest(),'body':raw})


def case(extra_roster=()):
    from test_nba_data import SUMMARY
    tip=nba_ops.parse_time(nba_data.parse_summary(SUMMARY)['game']['tipoff_utc'])
    roster={'season':{'year':2026},'team':{'id':'16','abbreviation':'MIN'},
            'athletes':[{'id':'2968436','displayName':'Joe Ingles','firstName':'Joe','lastName':'Ingles'}]}
    roster['athletes'].extend(extra_roster)
    rid=artifact(json.dumps(roster).encode(),nba_data.roster_url('MIN'),tip-4000,kind='roster')
    raw,game,_,tip=_grading_case(roster_artifact=rid);now=tip+5*3600
    source=artifact(pdf_bytes(BOOK),BOOK_URL,now-10)
    args={'game_id':game['game_id'],'player_id':'2968436','team':'MIN','full_name':'Joe Ingles',
          'source_artifact_id':source,'roster_artifact_id':rid,'page':0,'now':now}
    return raw,game,args


def test_review_is_immutable_idempotent_and_bound_to_captured_identity(isolated):
    from edgeterminal import nba_participation as official
    _,_,args=case();first=official.record_reviewed_inactive(**args)
    assert first['status']=='inactive' and first['source_artifact_id']==args['source_artifact_id']
    assert official.record_reviewed_inactive(**dict(args,now=args['now']+1))['id']==first['id']
    assert db.q1('SELECT COUNT(*) n FROM nba_reviewed_participation')['n']==1
    for statement in ['UPDATE nba_reviewed_participation SET team=\'OKC\'','DELETE FROM nba_reviewed_participation']:
        with pytest.raises(sqlite3.IntegrityError):db.ex(statement)


@pytest.mark.parametrize('change',['game','player','team','name','roster','future','host','path','status','date','score','not_out','tamper'])
def test_invalid_proof_never_creates_review(isolated,monkeypatch,change):
    from edgeterminal import nba_participation as official
    _,_,args=case()
    if change=='game':args['game_id']='nba-2025-401810832'
    elif change=='player':args['player_id']='3064514'
    elif change=='team':args['team']='OKC'
    elif change=='name':args['full_name']='Other Ingles'
    elif change=='roster':args['roster_artifact_id']=args['source_artifact_id']
    elif change=='future':args['now']-=20
    elif change in {'host','path','status','date','score','not_out'}:
        url=BOOK_URL;raw=pdf_bytes(BOOK);status=200
        if change=='host':url=url.replace('statsdmz.nba.com','statsdmz.nba.com.evil.test')
        elif change=='path':url='https://www.nba.com/player/joe-injury.pdf'
        elif change=='status':status=403
        elif change=='date':raw=pdf_bytes(BOOK.replace('March 15','March 16'))
        elif change=='score':raw=pdf_bytes(BOOK.replace('28 103','28 104'))
        elif change=='not_out':raw=pdf_bytes(BOOK.replace('Inactive: Timberwolves - Ingles','Active: Timberwolves - Ingles'))
        args['source_artifact_id']=artifact(raw,url,args['now']-9,status)
    else:
        get=nba_store.get_artifact
        monkeypatch.setattr(nba_store,'get_artifact',lambda aid:dict(get(aid),body=b'%PDF-tampered') if aid==args['source_artifact_id'] else get(aid))
    with pytest.raises(ValueError):official.record_reviewed_inactive(**args)
    assert db.q1('SELECT COUNT(*) n FROM nba_reviewed_participation')['n']==0


def test_gameday_out_report_carries_matchup_and_team_across_pages(isolated):
    from edgeterminal import nba_participation as official
    _,_,args=case()
    raw=pdf_bytes('Injury Report: 03/15/26 12:00 PM\n03/15/2026 01:00(ET) MIN@OKC MinnesotaTimberwolves',
                  'Injury Report: 03/15/26 12:00 PM\nIngles,Joe Out Injury/Illness-Ankle\nOklahomaCityThunder Other,Player Out')
    args.update(source_artifact_id=artifact(raw,REPORT_URL,args['now']-10),page=1)
    assert official.record_reviewed_inactive(**args)['status']=='inactive'


@pytest.mark.parametrize('change',['wrong_matchup','wrong_team','wrong_date','questionable','current_feed'])
def test_non_game_specific_injury_or_wrong_report_cannot_void(isolated,change):
    from edgeterminal import nba_participation as official
    _,_,args=case()
    text='Injury Report: 03/15/26 12:00 PM\n03/15/2026 01:00(ET) MIN@OKC MinnesotaTimberwolves Ingles,Joe Out Injury/Illness-Ankle'
    url=REPORT_URL
    if change=='wrong_matchup':text=text.replace('MIN@OKC','MIN@BOS')
    elif change=='wrong_team':text=text.replace('MinnesotaTimberwolves','OklahomaCityThunder')
    elif change=='wrong_date':text=text.replace('03/15/2026','03/16/2026')
    elif change=='questionable':text=text.replace(' Out ',' Questionable ')
    else:url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries'
    args['source_artifact_id']=artifact(pdf_bytes(text),url,args['now']-10)
    with pytest.raises(ValueError):official.record_reviewed_inactive(**args)


@pytest.mark.parametrize('days',[0,8])
def test_reviewed_void_survives_fresh_missing_player_checks_without_churn(isolated,monkeypatch,days):
    from edgeterminal import nba_participation as official
    from edgeterminal import nba_participation_sources
    # Exercise the separately reviewed path; automatic gamebooks have their
    # own tests and would otherwise resolve this archived fixture immediately.
    monkeypatch.setattr(nba_participation_sources,'collect_missing',lambda *a,**kw:
        {'ok':True,'requests':0,'checked_at':kw['now']})
    raw,game,args=case()
    raw['boxscore']['players'][0]['statistics'][0]['athletes']=[r for r in raw['boxscore']['players'][0]['statistics'][0]['athletes'] if r['athlete']['id']!='2968436']
    calls=[];_summary_feed(monkeypatch,{game['espn_event_id']:raw},calls)
    now=args['now']+days*86400
    assert nba_ops.grade_due(now)['pending_props']==1
    review=official.record_reviewed_inactive(**dict(args,now=now+1))
    assert nba_ops.grade_due(now+2)['new_grades']==1
    grade=nba_store.latest_grades('top5-'+game['game_id'])[3]
    assert grade['result']=='void' and grade['actual_value'] is None
    assert grade['source_artifact_id']==args['source_artifact_id']
    assert grade['detail']['participation_evidence_id']==review['id']
    assert grade['detail']['box_source_artifact_id']!=args['source_artifact_id']
    assert nba_ops.grade_due(now+86402)['new_grades']==0
    assert nba_store.latest_grades('top5-'+game['game_id'])[3]['revision']==2


def test_present_playing_player_conflicts_with_reviewed_inactivity(isolated,monkeypatch):
    from edgeterminal import nba_participation as official
    raw,game,args=case();official.record_reviewed_inactive(**args)
    _summary_feed(monkeypatch,{game['espn_event_id']:raw},[])
    result=nba_ops.grade_due(args['now']+1)
    assert result['ok'] is False and result['new_grades']==0
    assert nba_store.latest_grades('top5-'+game['game_id'])=={}


def test_duplicate_espn_identity_in_frozen_roster_is_rejected(isolated):
    from edgeterminal import nba_participation as official
    _,_,args=case([{'id':'2968436','displayName':'Other Player','firstName':'Other','lastName':'Player'}])
    with pytest.raises(ValueError):official.record_reviewed_inactive(**args)


def test_final_gamebook_cannot_have_been_received_before_tipoff(isolated):
    from edgeterminal import nba_participation as official
    _,game,args=case()
    args['source_artifact_id']=artifact(pdf_bytes(BOOK),BOOK_URL,nba_ops.parse_time(game['tipoff_utc'])-1)
    with pytest.raises(ValueError):official.record_reviewed_inactive(**args)


@pytest.mark.parametrize('complete',[False,1,'true'])
def test_overlay_requires_literal_verified_complete_box(isolated,complete):
    from edgeterminal import nba_participation as official
    _,game,args=case();official.record_reviewed_inactive(**args)
    result={'game_id':game['game_id'],'game_status':'final','verified_final':True,
            'home_score':game['home_score'],'away_score':game['away_score'],'players':[]}
    assert official.overrides(game['game_id'],result,box_complete=complete,now=args['now'])=={}
