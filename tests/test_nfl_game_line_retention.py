"""Saved NFL lines remain paired research evidence after current quotes expire."""
import json
import hashlib

import pytest

from edgeterminal import db, engine, nfl_game_forecasts, nfl_game_lines, nfl_season_record
from edgeterminal.config import settings
from test_nfl_foundation import isolated_db
from test_nfl_game_forecast_updates import board, GAME, HOME, AWAY

URL = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'


def offer(at, *, spread=-3.5, total=45.5, book='draftkings'):
    rows=[]
    if spread is not None:
        rows += [dict(market='spread',selection=HOME,line=spread),
                 dict(market='spread',selection=AWAY,line=-spread)]
    if total is not None:
        rows += [dict(market='total',selection='Over',line=total),
                 dict(market='total',selection='Under',line=total)]
    db.store_odds([dict(r,event_id=GAME,sport='NFL',price=-110,book=book,
                       captured_at=at,source_url=URL,quote_phase='current') for r in rows],
                  source='espn-odds')


def newest():
    row=db.q1('SELECT * FROM nfl_game_snapshots ORDER BY created_at DESC,id DESC LIMIT 1')
    return dict(row,payload=json.loads(row['payload']))


def test_current_quote_expiry_cannot_erase_a_saved_spread_or_total(board, monkeypatch):
    clock,stats=board
    monkeypatch.setattr(settings,'stale_odds_s',60)
    offer(990.)
    assert engine.ensure_weekly_projections()['ok']
    first=newest()
    clock[0]=1100.;stats[HOME]['points_for']=31.
    assert engine.ensure_weekly_projections()['ok']
    second=newest()
    assert second['id'] != first['id']
    assert second['payload']['market'] == []  # Retention cannot refresh live prices.
    groups=second['payload'].get('line_retention',{}).get('groups',{})
    assert set(groups)=={'spread','total'}
    assert groups['spread']['quotes'][0]['captured_at']==990.
    assert groups['spread']['origin_snapshot_id']==first['id']
    assert db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(first['id'],))['payload']==json.dumps(first['payload'],sort_keys=True,separators=(',',':'))


def test_partial_new_offer_updates_only_its_market(board, monkeypatch):
    clock,_=board
    monkeypatch.setattr(settings,'stale_odds_s',60)
    offer(990.)
    assert engine.ensure_weekly_projections()['ok']
    clock[0]=1100.
    offer(1090.,spread=-4.5,total=None,book='fanduel')
    assert engine.ensure_weekly_projections()['ok']
    groups=newest()['payload'].get('line_retention',{}).get('groups',{})
    assert groups['spread']['quotes'][0]['line']==-4.5
    assert groups['spread']['basis']=='fresh'
    assert groups['total']['quotes'][0]['line']==45.5
    assert groups['total']['basis']=='retained'
    assert groups['total']['quotes'][0]['book']=='draftkings'


def test_retention_has_direct_origin_and_repeat_is_noop(board, monkeypatch):
    clock,stats=board;monkeypatch.setattr(settings,'stale_odds_s',60)
    offer(990.);assert engine.ensure_weekly_projections()['ok'];first=newest()
    clock[0]=1100.;assert engine.ensure_weekly_projections()['ok'];second=newest()
    clock[0]=1200.;assert engine.ensure_weekly_projections()['created']==0
    stats[HOME]['points_for']=33.;assert engine.ensure_weekly_projections()['created']==1
    third=newest();group=third['payload']['line_retention']['groups']['spread']
    assert group['origin_snapshot_id']==first['id']!=second['id']
    assert nfl_game_lines.effective_quotes(third)[0]['captured_at']==990.


def test_incomplete_single_side_cannot_replace_complete_retained_market(board, monkeypatch):
    clock,_=board;monkeypatch.setattr(settings,'stale_odds_s',60)
    offer(990.);engine.ensure_weekly_projections();clock[0]=1100.
    db.store_odds([dict(event_id=GAME,sport='NFL',market='spread',selection=HOME,line=-7.5,
        price=-110,book='fanduel',captured_at=1090.,source_url=URL)],source='espn-odds')
    assert engine.ensure_weekly_projections()['ok']
    group=newest()['payload']['line_retention']['groups']['spread']
    assert group['basis']=='retained' and group['quotes'][0]['line']==-3.5


@pytest.mark.parametrize('field,value',[('source_url','https://untrusted.invalid/scoreboard'),
    ('quote_phase','in_play'),('provider','untrusted'),('is_demo',1),('event_id','wrong'),
    ('book','Unknown'),('price',90),('line',float('nan')),('captured_at',1200.)])
def test_forged_or_unsafe_quote_cannot_enter_binding(board,field,value):
    offer(990.);rows=db.q('SELECT * FROM odds_snapshots ORDER BY id');rows[0][field]=value
    with pytest.raises(ValueError):
        nfl_game_lines.resolve(db.q1('SELECT * FROM events WHERE id=?',(GAME,)),rows,1000.)


def test_changed_kickoff_cannot_inherit_previous_quote(board):
    offer(990.);engine.ensure_weekly_projections();old=nfl_game_forecasts.selected(db.q1('SELECT * FROM events WHERE id=?',(GAME,)))
    event=db.q1('SELECT * FROM events WHERE id=?',(GAME,));event['start_time']='1970-01-01T02:00:00Z'
    with pytest.raises(ValueError,match='(identity|kickoff)'):
        nfl_game_lines.resolve(event,[],1100.,old)


def test_rescheduled_game_can_publish_new_verified_quotes_without_old_lines(board):
    clock,_=board
    offer(990.);assert engine.ensure_weekly_projections()['ok'];old=newest()
    db.ex('UPDATE events SET start_time=? WHERE id=?',('1970-01-01T02:00:00Z',GAME))
    clock[0]=1100.;offer(1090.,spread=-4.5,total=46.5)
    result=engine.ensure_weekly_projections()
    assert result['ok'] and result['created']==1
    current=newest()
    assert current['payload']['game']['kickoff_utc']=='1970-01-01T02:00:00Z'
    assert all(g['basis']=='fresh' for g in current['payload']['line_retention']['groups'].values())
    assert current['payload']['line_retention']['groups']['total']['quotes'][0]['line']==46.5
    assert db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(old['id'],))['payload']==json.dumps(old['payload'],sort_keys=True,separators=(',',':'))


def test_equal_clock_contradictory_offer_refuses_instead_of_choosing_id(board):
    offer(990.)
    db.store_odds([dict(event_id=GAME,sport='NFL',market='spread',selection=HOME,line=-7.5,
        price=-110,book='draftkings',captured_at=990.,source_url=URL)],source='espn-odds')
    with pytest.raises(ValueError,match='equal-clock'):
        nfl_game_lines.resolve(db.q1('SELECT * FROM events WHERE id=?',(GAME,)),
                               db.q('SELECT * FROM odds_snapshots ORDER BY id'),1000.)


def test_contract_tamper_refuses_without_searching_other_snapshots(board):
    offer(990.);engine.ensure_weekly_projections();saved=newest()
    saved['payload']['line_retention']['groups']['spread']['quotes'][0]['line']=-30.
    with pytest.raises(ValueError,match='contract hash'):
        nfl_game_lines.effective_quotes(saved)


def test_season_record_uses_the_exact_retained_comparison_pair(board,monkeypatch):
    clock,_=board;monkeypatch.setattr(settings,'stale_odds_s',60)
    offer(990.);engine.ensure_weekly_projections();clock[0]=1100.;engine.ensure_weekly_projections()
    monkeypatch.setattr(nfl_season_record,'_final_scores',lambda _: {GAME:(30.,20.)})
    monkeypatch.setattr(nfl_season_record,'_legacy_games',lambda *_:[])
    row=nfl_season_record.games()['rows'][0]
    assert (row['spread_line'],row['total_line'])==(-3.5,45.5)
    assert row['results']=={'winner':'won','spread':'won','total':'lost'}
    assert row['line_provenance']['spread']['basis']=='retained'


def test_verified_origin_audit_has_original_projection_and_never_uses_scores(board):
    offer(990.);engine.ensure_weekly_projections();saved=newest()
    event=db.q1('SELECT * FROM events WHERE id=?',(GAME,))
    pair=nfl_game_lines.verify_origin(saved,event,'spread')
    assert pair['snapshot_id']==saved['id']
    assert pair['projection']==saved['payload']['projection']
    assert pair['quotes'][0]['line']==-3.5
    assert 'result' not in pair


def test_racing_parent_change_prevents_losing_a_concurrent_revision(board,monkeypatch):
    offer(990.)
    real=nfl_game_forecasts.selected;calls=[0]
    def racing(event):
        result=real(event);calls[0]+=1
        if calls[0]>=3 and result is None:return {'snapshot_id':'concurrent','payload':{}}
        return result
    monkeypatch.setattr(nfl_game_forecasts,'selected',racing)
    result=engine.ensure_weekly_projections()
    assert result['created']==0


@pytest.mark.parametrize('changes,provider,is_demo',[
    ({'source_url':'https://untrusted.invalid'},'espn-odds',False),
    ({},'other-provider',False),({},'espn-odds',True),
    ({'book':'Unknown'},'espn-odds',False),
    ({'price':90},'espn-odds',False),
    ({'price':-110.5},'espn-odds',False),
    ({'line':50},'espn-odds',False),
    ({'captured_at':1100.},'espn-odds',False),
    ({'selection':'wrong team'},'espn-odds',False),
])
def test_unsafe_sourced_observation_is_visible_error(board,changes,provider,is_demo):
    r=dict(event_id=GAME,sport='NFL',market='spread',selection=HOME,line=-3.5,
           price=-110,book='draftkings',captured_at=990.,source_url=URL)
    r.update(changes)
    db.store_odds([r],source=provider,is_demo=is_demo)
    with pytest.raises(ValueError):
        nfl_game_lines.resolve(db.q1('SELECT * FROM events WHERE id=?',(GAME,)),
                               db.q('SELECT * FROM odds_snapshots'),1000.)


def test_paired_total_and_spread_contradictions_fail_closed(board):
    offer(990.,spread=None)
    db.store_odds([dict(event_id=GAME,sport='NFL',market='total',selection='Under',line=46.5,
        price=-110,book='draftkings',captured_at=990.,source_url=URL)],source='espn-odds')
    with pytest.raises(ValueError,match='equal-clock'):
        nfl_game_lines.resolve(db.q1('SELECT * FROM events WHERE id=?',(GAME,)),
                               db.q('SELECT * FROM odds_snapshots'),1000.)


def test_reforged_contract_cannot_change_retained_origin(board,monkeypatch):
    clock,_=board;monkeypatch.setattr(settings,'stale_odds_s',60)
    offer(990.);engine.ensure_weekly_projections();clock[0]=1100.;engine.ensure_weekly_projections()
    saved=newest();contract=saved['payload']['line_retention'];contract['groups']['spread']['origin_sha256']='0'*64
    contract['sha256']=nfl_game_lines._sha({k:v for k,v in contract.items() if k!='sha256'})
    with pytest.raises(ValueError,match='origin hash'):
        nfl_game_lines.effective_quotes(saved)


def test_resolver_refuses_postgame_creation(board):
    with pytest.raises(ValueError,match='pregame'):
        nfl_game_lines.resolve(db.q1('SELECT * FROM events WHERE id=?',(GAME,)),[],3600.)


def test_quarantined_observation_cannot_be_retained(board,monkeypatch):
    clock,_=board;monkeypatch.setattr(settings,'stale_odds_s',60)
    offer(990.);engine.ensure_weekly_projections();saved=nfl_game_forecasts.selected(db.q1('SELECT * FROM events WHERE id=?',(GAME,)))
    quote_id=saved['payload']['line_retention']['groups']['spread']['quotes'][0]['id']
    db.quarantine('odds',str(quote_id),event_id=GAME,reason='wrong matchup')
    with pytest.raises(ValueError,match='quarantined'):
        nfl_game_lines.effective_quotes(saved)


def test_client_parent_metadata_cannot_redefine_the_sealed_origin(board):
    offer(990.);engine.ensure_weekly_projections();saved=nfl_game_forecasts.selected(db.q1('SELECT * FROM events WHERE id=?',(GAME,)))
    saved['payload']['projection']['margin']=70.
    with pytest.raises(ValueError,match='parent payload'):
        nfl_game_lines.resolve(db.q1('SELECT * FROM events WHERE id=?',(GAME,)),[],1100.,saved)


def test_older_fresh_group_does_not_replace_newer_original_receipt(board):
    offer(990.);engine.ensure_weekly_projections();saved=nfl_game_forecasts.selected(db.q1('SELECT * FROM events WHERE id=?',(GAME,)))
    offer(980.,spread=-7.5,total=None,book='fanduel')
    older=db.q("SELECT * FROM odds_snapshots WHERE book='fanduel'")
    group=nfl_game_lines.resolve(db.q1('SELECT * FROM events WHERE id=?',(GAME,)),older,1100.,saved)['groups']['spread']
    assert group['quotes'][0]['line']==-3.5 and group['captured_at']==990.


def test_suspension_is_not_a_missing_feed_carry_signal(board):
    event=db.q1('SELECT * FROM events WHERE id=?',(GAME,));event['status']='STATUS_SUSPENDED'
    with pytest.raises(ValueError,match='schedule'):
        nfl_game_lines.resolve(event,[],1000.)


def test_official_week_parameter_source_is_valid_comparison_evidence(board):
    source=URL+'?dates=2026&seasontype=2&week=10&limit=100'
    db.store_odds([dict(event_id=GAME,sport='NFL',market='spread',selection=side,line=line,
        price=-110,book='draftkings',captured_at=990.,source_url=source)
        for side,line in ((HOME,-3.5),(AWAY,3.5))],source='espn-odds')
    result=engine.ensure_weekly_projections()
    assert result['ok'],result
    assert newest()['payload']['line_retention']['groups']['spread']['quotes'][0]['source_url']==source


@pytest.mark.parametrize('suffix',[
    '?dates=2026&seasontype=2&week=9&limit=100',
    '?dates=2025&seasontype=2&week=10&limit=100',
    '?dates=2026&seasontype=3&week=10&limit=100',
    '?dates=2026&seasontype=2&week=10&limit=101',
    '?dates=2026&seasontype=2&week=10&limit=0',
    '?dates=2026&seasontype=2&week=10&limit=0100',
    '?dates=2026&seasontype=2&week=10&limit=100&week=9',
    '?dates=2026&seasontype=2&week=10&limit=100&evil=1',
    '?dates=2026&seasontype=2&week=10',
    '?dates=2026&seasontype=2&week=10&limit=100#fragment',
    '?',
])
def test_parameter_source_wrong_or_ambiguous_query_refused(board,suffix):
    assert not nfl_game_lines.valid_source_url(URL+suffix,db.q1('SELECT * FROM events WHERE id=?',(GAME,)))


@pytest.mark.parametrize('url',[
    'http://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
    'https://site.api.espn.com.evil.invalid/apis/site/v2/sports/football/nfl/scoreboard',
    'https://user:pass@site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
    'https://site.api.espn.com:444/apis/site/v2/sports/football/nfl/scoreboard',
    'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard/../summary',
    None, 42,
])
def test_parameter_source_unsafe_origin_refused(board,url):
    assert not nfl_game_lines.valid_source_url(url,db.q1('SELECT * FROM events WHERE id=?',(GAME,)))


def test_actual_mnf_provider_url_has_exact_canonical_week_identity():
    event={'id':'nfl-2026-reg-09-buf-min','home_team':'Minnesota Vikings','away_team':'Buffalo Bills'}
    assert nfl_game_lines.valid_source_url(URL+'?dates=2026&seasontype=2&week=9&limit=100',event)
    assert nfl_game_lines.valid_source_url(URL,event)


def _reseal_contract(saved):
    contract=saved['payload']['line_retention']
    contract['sha256']=nfl_game_lines._sha({k:v for k,v in contract.items() if k!='sha256'})


def test_semantically_resealed_binding_cannot_omit_complete_fresh_market(board):
    offer(990.);engine.ensure_weekly_projections();saved=newest()
    del saved['payload']['line_retention']['groups']['total'];_reseal_contract(saved)
    with pytest.raises(ValueError,match='fresh market'):
        nfl_game_lines.effective_quotes(saved)


def test_semantically_resealed_retention_cannot_override_newer_fresh_market(board):
    clock,_=board;offer(990.);engine.ensure_weekly_projections();old=newest()
    clock[0]=1100.;offer(1090.,spread=-4.5);engine.ensure_weekly_projections();saved=newest()
    group=old['payload']['line_retention']['groups']['spread']
    group=dict(group,basis='retained',origin_snapshot_id=old['id'],
               origin_sha256=nfl_game_lines._sha(old['payload']),origin_saved_at=old['created_at'])
    saved['payload']['line_retention']['groups']['spread']=group;_reseal_contract(saved)
    with pytest.raises(ValueError,match='fresh market'):
        nfl_game_lines.effective_quotes(saved)


def test_verified_retention_can_override_an_out_of_order_older_fresh_receipt(board):
    offer(990.);engine.ensure_weekly_projections();saved=nfl_game_forecasts.selected(db.q1('SELECT * FROM events WHERE id=?',(GAME,)))
    offer(980.,spread=-7.5,total=None,book='fanduel')
    older=db.q("SELECT * FROM odds_snapshots WHERE book='fanduel'")
    event=db.q1('SELECT * FROM events WHERE id=?',(GAME,))
    contract=nfl_game_lines.resolve(event,older,1100.,saved)
    candidate={'game':saved['payload']['game'],'forecast_as_of':1100.,'market':older,'line_retention':contract}
    assert nfl_game_lines.effective_quotes(candidate)[0]['line']==-3.5


def legacy_snapshot(*,priced=False,ident=None,compact=False):
    event=db.q1('SELECT * FROM events WHERE id=?',(GAME,))
    projection=dict(proj_home=23.,proj_away=20.,margin=3.,total=43.,home_win_prob=.55,
                    uncertainty=10.,feature_version='legacy-features')
    payload=dict(schema_version=1,game=dict(id=GAME,home_team=HOME,away_team=AWAY,
        kickoff_utc=event['start_time']),projection=projection,market=db.latest_odds(event_id=GAME) if priced else [],
        model_version='legacy-model',created_at=999.9)
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':')) if compact else json.dumps(payload,sort_keys=True,default=str)
    snapshot_id=ident or hashlib.sha256(encoded.encode()).hexdigest()[:24]
    db.save_nfl_snapshot(snapshot_id,GAME,payload,'legacy-model','legacy-features')
    db.save_nfl_game_prediction('legacy-prediction',GAME,snapshot_id,projection,'legacy-model')
    return snapshot_id


def test_real_spaced_hash_legacy_parent_upgrades_with_fresh_source(board):
    offer(990.);parent=legacy_snapshot()
    board[0][0]=1001.
    before=db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(parent,))['payload']
    result=engine.ensure_weekly_projections()
    assert result['ok'],result
    saved=newest();assert saved['id']!=parent
    assert saved['payload']['line_retention']['parent_snapshot_id']==parent
    assert set(saved['payload']['line_retention']['groups'])=={'spread','total'}
    assert db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(parent,))['payload']==before


def test_uuid_unpriced_legacy_parent_does_not_block_fresh_verified_publication(board):
    offer(990.);parent=legacy_snapshot(ident='legacy-unsealed-uuid')
    board[0][0]=1001.
    result=engine.ensure_weekly_projections();assert result['ok'],result
    assert newest()['payload']['line_retention']['parent_snapshot_id']==parent


@pytest.mark.parametrize('compact',[False,True])
def test_strong_legacy_priced_origin_retains_its_original_source_without_resealing(board,monkeypatch,compact):
    offer(990.);parent=legacy_snapshot(priced=True,compact=compact);before=db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(parent,))
    monkeypatch.setattr(settings,'stale_odds_s',60);board[0][0]=1100.
    result=engine.ensure_weekly_projections();assert result['ok'],result
    saved=newest();refs=nfl_game_lines.line_refs(saved)
    assert refs['spread']['origin_snapshot_id']==parent and refs['spread']['basis']=='retained'
    assert nfl_game_lines.effective_quotes(saved)[0]['captured_at']==990.
    assert db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(parent,))==before


def test_unsealed_legacy_priced_market_cannot_be_silently_erased(board,monkeypatch):
    offer(990.);parent=legacy_snapshot(priced=True,ident='unsealed-priced-legacy')
    monkeypatch.setattr(settings,'stale_odds_s',60);board[0][0]=1100.
    result=engine.ensure_weekly_projections()
    assert not result['ok'] and result['created']==0
    assert 'legacy priced market' in result['errors'][0]['error']
    assert newest()['id']==parent


@pytest.mark.parametrize('mutation',[{'game':{'id':'nfl-2026-reg-10-atl-gb','home_team':AWAY,'away_team':HOME,'kickoff_utc':'1970-01-01T01:00:00Z'}},
    {'game':{'id':'nfl-2026-reg-10-atl-gb','home_team':HOME,'away_team':AWAY,'kickoff_utc':'1970-01-01T02:00:00Z'}},
    {'schema_version':99},{'created_at':1100.}])
def test_legacy_parent_wrong_identity_schema_or_clock_refuses_upgrade(board,mutation):
    parent=legacy_snapshot();row=db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(parent,));payload=json.loads(row['payload']);payload.update(mutation)
    # Owned fixture simulates corrupt or unsupported pre-policy storage.
    for t in db.q("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nfl_game_snapshots'"):
        db.ex('DROP TRIGGER "'+t['name'].replace('"','""')+'"')
    db.ex('UPDATE nfl_game_snapshots SET payload=? WHERE id=?',(json.dumps(payload,sort_keys=True,separators=(',',':')),parent))
    offer(990.);board[0][0]=1001.
    result=engine.ensure_weekly_projections();assert not result['ok'] and result['created']==0


def test_legacy_priced_forged_quote_is_not_trusted_even_if_parent_hash_matches(board,monkeypatch):
    offer(990.);event=db.q1('SELECT * FROM events WHERE id=?',(GAME,));projection={'margin':3.}
    quotes=db.latest_odds(event_id=GAME);quotes[0]['price']=-500
    payload=dict(schema_version=1,game=dict(id=GAME,home_team=HOME,away_team=AWAY,kickoff_utc=event['start_time']),
                 market=quotes,projection=projection,created_at=999.9,model_version='legacy-model')
    parent=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()[:24]
    db.save_nfl_snapshot(parent,GAME,payload,'legacy-model','legacy-features')
    db.save_nfl_game_prediction('legacy-prediction',GAME,parent,projection,'legacy-model')
    monkeypatch.setattr(settings,'stale_odds_s',60)
    board[0][0]=1100.;result=engine.ensure_weekly_projections()
    assert not result['ok'] and 'immutable observation' in result['errors'][0]['error']


@pytest.mark.parametrize('ident',[None,'unsealed-priced-legacy'])
def test_complete_fresh_markets_can_replace_unsupported_old_prices_without_using_them(board,ident):
    db.store_odds([dict(event_id=GAME,sport='NFL',market='spread',selection=side,line=line,
        price=-110,book='draftkings',captured_at=980.,source_url=URL)
        for side,line in ((HOME,-2.5),(AWAY,2.5))],source='old-foreign-feed')
    parent=legacy_snapshot(priced=True,ident=ident)
    original=db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(parent,))
    board[0][0]=1010.;offer(1005.)
    result=engine.ensure_weekly_projections();assert result['ok'],result
    saved=newest();assert saved['payload']['line_retention']['parent_snapshot_id']==parent
    assert all(g['basis']=='fresh' and g['origin_snapshot_id'] is None
               for g in saved['payload']['line_retention']['groups'].values())
    assert db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(parent,))==original


def test_legacy_origin_authenticates_only_the_market_actually_carried(board,monkeypatch):
    offer(980.,spread=None)
    db.store_odds([dict(event_id=GAME,sport='NFL',market='spread',selection=side,line=line,
        price=-110,book='draftkings',captured_at=980.,source_url=URL)
        for side,line in ((HOME,-2.5),(AWAY,2.5))],source='old-foreign-feed')
    parent=legacy_snapshot(priced=True)
    board[0][0]=1100.;monkeypatch.setattr(settings,'stale_odds_s',60);offer(1090.,total=None)
    result=engine.ensure_weekly_projections();assert result['ok'],result
    saved=newest();quotes=nfl_game_lines.effective_quotes(saved)
    assert next(q for q in quotes if q['market']=='total')['line']==45.5
    assert saved['payload']['line_retention']['groups']['total']['origin_snapshot_id']==parent
    assert saved['payload']['line_retention']['groups']['spread']['basis']=='fresh'


def test_foreign_and_demo_rows_do_not_block_genuine_official_producer_quotes(board):
    offer(990.)
    for provider,demo,book in [('other-feed',False,'fanduel'),('espn-odds',True,'betmgm')]:
        db.store_odds([dict(event_id=GAME,sport='NFL',market='spread',selection=HOME,line=-7.5,
            price=-110,book=book,captured_at=995.,source_url=URL)],source=provider,is_demo=demo)
    result=engine.ensure_weekly_projections();assert result['ok'],result
    assert newest()['payload']['line_retention']['groups']['spread']['quotes'][0]['book']=='draftkings'


def test_malformed_admitted_official_quote_still_blocks_producer_visibly(board):
    offer(990.)
    db.store_odds([dict(event_id=GAME,sport='NFL',market='spread',selection=HOME,line=-7.5,
        price=-110,book='fanduel',captured_at=995.,source_url='https://untrusted.invalid')],source='espn-odds')
    result=engine.ensure_weekly_projections()
    assert not result['ok'] and result['created']==0 and result['errors']
