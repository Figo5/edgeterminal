"""Immutable NFL comparison lines, distinct from fresh executable prices.

Only future publications resolve carry-forward. Readers verify the exact
sealed binding and never look for a different historical forecast.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import re
from urllib.parse import urlsplit,parse_qsl

from . import db,nfl_identity
from .integrity import parse_time

POLICY_ID = 'nfl-game-line-retention-v1'
SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
FIELDS = ('id','event_id','sport','market','selection','line','price','book','captured_at',
          'provider','source','source_url','quote_phase','is_demo','fetched_at','kickoff_utc','provider_updated_at')
MARKETS = ('moneyline','spread','total')


def _encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _sha(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('NFL line numeric value is invalid')
    return value


def _event(value):
    game=value.get('game') or value
    parts=re.fullmatch(r'nfl-(20\d{2})-reg-(\d{2})-([a-z]{2,3})-([a-z]{2,3})',str(game.get('id')))
    if (not parts or not 1<=int(parts[2])<=22 or nfl_identity.team_code(game.get('away_team'))!=parts[3]
            or nfl_identity.team_code(game.get('home_team'))!=parts[4] or parts[3]==parts[4]):
        raise ValueError('NFL line canonical game/team identity invalid')
    return dict(id=game['id'],home_team=game['home_team'],away_team=game['away_team'],
                start_time=game.get('kickoff_utc',game.get('start_time')))


def valid_source_url(value,event):
    """Accept only the two URL forms emitted by the official odds provider."""
    if not isinstance(value,str) or len(value)>2048:return False
    try:
        url=urlsplit(value);pairs=parse_qsl(url.query,keep_blank_values=True);query=dict(pairs)
        base=urlsplit(SCOREBOARD);game=_event(event);parts=game['id'].split('-')
        if (url.scheme!='https' or url.netloc!=base.netloc or url.path!=base.path
                or url.username or url.password or url.fragment or len(query)!=len(pairs)):
            return False
        if not url.query:return value==SCOREBOARD
        return (set(query)=={'dates','seasontype','week','limit'}
                and query['dates']==parts[1] and query['seasontype']=='2'
                and query['week']==str(int(parts[3])) and query['limit'].isdigit()
                and str(int(query['limit']))==query['limit'] and 1<=int(query['limit'])<=100)
    except (ValueError,TypeError,KeyError):return False


def _quote(raw,event,as_of):
    if not isinstance(raw,dict):raise ValueError('NFL line quote is invalid')
    if isinstance(raw.get('id'),bool) or not isinstance(raw.get('id'),int) or raw['id']<=0:
        raise ValueError('NFL line quote identity invalid')
    if any(isinstance(raw.get(key),bool) for key in ('captured_at','fetched_at','kickoff_utc')):
        raise ValueError('NFL line quote clock is invalid')
    saved=db.q1('SELECT * FROM odds_snapshots WHERE id=?',(raw.get('id'),))
    if not saved or any(saved.get(k)!=raw.get(k) for k in FIELDS):
        raise ValueError('NFL line quote differs from immutable observation')
    if db.q1("SELECT 1 FROM quarantine WHERE kind='odds' AND CAST(ref_id AS INTEGER)=?",(raw['id'],)):
        raise ValueError('NFL line quote is quarantined')
    if (raw.get('event_id')!=event['id'] or raw.get('sport')!='NFL'
            or raw.get('provider')!='espn-odds' or raw.get('source')!='espn-odds' or raw.get('is_demo')
            or not valid_source_url(raw.get('source_url'),event) or raw.get('quote_phase')!='current'):
        raise ValueError('NFL line source/game/phase is invalid')
    book=raw.get('book')
    if not isinstance(book,str) or book.strip().lower() in ('','unknown'):
        raise ValueError('NFL line sportsbook is invalid')
    tip=parse_time(event['start_time']);captured=parse_time(raw.get('captured_at'))
    fetched=parse_time(raw.get('fetched_at'));original_tip=parse_time(raw.get('kickoff_utc'))
    if not (tip is not None and captured is not None and fetched is not None
            and original_tip==tip and fetched<=captured<=as_of<tip):
        raise ValueError('NFL line quote chronology/kickoff conflict')
    market=raw.get('market');line=raw.get('line');side=raw.get('selection')
    price=_number(raw.get('price'))
    if price!=int(price) or not 100<=abs(price)<=100000:
        raise ValueError('NFL line quote price bounds invalid')
    if market not in MARKETS:raise ValueError('NFL line market invalid')
    sides=('Over','Under') if market=='total' else (event['home_team'],event['away_team'])
    if side not in sides:raise ValueError('NFL line quote selection invalid')
    if market=='moneyline':
        if line is not None:raise ValueError('NFL moneyline has a points line')
    else:
        line=_number(line)
        if (market=='spread' and abs(line)>40) or (market=='total' and not 10<=line<=100):
            raise ValueError('NFL line quote points bounds invalid')
    return {k:raw.get(k) for k in FIELDS}


def _groups(quotes,event,as_of):
    offers=defaultdict(list)
    for raw in quotes or []:
        if raw.get('market') not in MARKETS:continue
        quote=_quote(raw,event,as_of)
        offers[(quote['market'],quote['book'],quote['captured_at'])].append(quote)
    complete=defaultdict(list)
    for (market,book,captured),rows in offers.items():
        sides=('Over','Under') if market=='total' else (event['home_team'],event['away_team'])
        unique={}
        for side in sides:
            matches=[r for r in rows if r['selection']==side]
            if len({(r['line'],r['price']) for r in matches})>1:
                raise ValueError('NFL line equal-clock offer conflict')
            if matches:unique[side]=min(matches,key=lambda r:r['id'])
        if len(unique)!=2:continue
        pair=[unique[side] for side in sides]
        if market=='spread' and pair[0]['line']!=-pair[1]['line']:
            raise ValueError('NFL line paired spread orientation conflict')
        if market=='total' and pair[0]['line']!=pair[1]['line']:
            raise ValueError('NFL line paired total conflict')
        complete[market].append(dict(quotes=pair,captured_at=captured,book=book))
    # Pick one whole book offer, never independently select each side.
    return {market:sorted(rows,key=lambda r:(-r['captured_at'],r['book']))[0]
            for market,rows in complete.items()}


def _snapshot(snapshot_id,event,as_of):
    row=db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(snapshot_id,))
    if not row:raise ValueError('NFL line origin snapshot missing')
    payload=json.loads(row['payload']);game=_event(payload)
    digest=hashlib.sha256(row['payload'].encode()).hexdigest()
    if (row['id']!=digest[:24] or row['payload']!=_encoded(payload)
            or payload.get('forecast_policy_id')!='nfl-game-source-revisions-v1'):
        raise ValueError('NFL line origin snapshot hash/policy conflict')
    if game!=_event(event) or row['game_id']!=event['id']:
        raise ValueError('NFL line origin snapshot identity conflict')
    origin_asof=_number(payload.get('forecast_as_of'));created=_number(row['created_at'])
    prediction=db.q1('SELECT * FROM nfl_game_predictions WHERE snapshot_id=? AND game_id=?',
                      (snapshot_id,event['id']))
    if (not prediction or prediction['created_at']!=created or payload.get('created_at')!=created
            or prediction['model_version']!=row['model_version']
            or not origin_asof<=created<=as_of<parse_time(game['start_time'])):
        raise ValueError('NFL line origin forecast chronology conflict')
    return row,payload,digest


def _legacy_parent(snapshot_id,event,as_of,*,require_seal=False):
    """Verify the actual v1 producer formats without promoting unsealed rows.

    Original analysis used spaced JSON for its ID and compact JSON for storage.
    The later priced-clone producer used compact JSON for both. UUID-style
    unpriced rows may be a CAS parent only, never a retained quote origin.
    """
    row=db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(snapshot_id,))
    if not row:raise ValueError('NFL legacy line parent snapshot missing')
    payload=json.loads(row['payload']);encoded=_encoded(payload);game=_event(payload)
    if (payload.get('schema_version')!=1 or payload.get('forecast_policy_id') is not None
            or payload.get('line_retention') is not None or row['payload']!=encoded):
        raise ValueError('NFL legacy line parent schema conflict')
    if game!=_event(event) or row['game_id']!=event['id']:
        raise ValueError('NFL legacy line parent identity/kickoff conflict')
    digest=hashlib.sha256(encoded.encode()).hexdigest()
    spaced=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
    sealed=snapshot_id in (digest[:24],spaced[:24])
    if (re.fullmatch('[0-9a-f]{24}',str(snapshot_id)) and not sealed) or (require_seal and not sealed):
        raise ValueError('NFL legacy line parent hash is unverified')
    created=_number(row['created_at']);claimed=_number(payload.get('created_at',created))
    prediction=db.q1('SELECT * FROM nfl_game_predictions WHERE snapshot_id=? AND game_id=?',
                      (snapshot_id,event['id']))
    if (not prediction or prediction['model_version']!=row['model_version']
            or payload.get('model_version',row['model_version'])!=row['model_version']
            or not claimed<=created<=prediction['created_at']<=as_of<parse_time(game['start_time'])):
        raise ValueError('NFL legacy line parent chronology conflict')
    return row,payload,digest,sealed


def _origin(snapshot_id,event,as_of):
    row=db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(snapshot_id,))
    if not row:raise ValueError('NFL line origin snapshot missing')
    payload=json.loads(row['payload'])
    if payload.get('forecast_policy_id')=='nfl-game-source-revisions-v1':
        return _snapshot(snapshot_id,event,as_of)
    row,payload,digest,_=_legacy_parent(snapshot_id,event,as_of,require_seal=True)
    return row,payload,digest


def _origin_clock(row,payload):
    return payload.get('forecast_as_of',payload.get('created_at',row['created_at']))


def _verify_group(group,market,event,as_of,fresh):
    if group.get('basis') not in ('fresh','retained'):
        raise ValueError('NFL line binding basis invalid')
    checked=_groups(group.get('quotes'),event,as_of)
    if set(checked)!={market} or len(group.get('quotes',[]))!=2:
        raise ValueError('NFL line binding needs one complete offer')
    offer=checked[market]
    if any(group.get(k)!=offer[k] for k in ('quotes','book','captured_at')):
        raise ValueError('NFL line binding offer conflict')
    if group['basis']=='fresh':
        if group.get('origin_snapshot_id') is not None or group.get('origin_sha256') is not None:
            raise ValueError('NFL fresh line has retained origin')
        candidates=_groups(fresh,event,as_of)
        if candidates.get(market)!=offer:
            raise ValueError('NFL fresh line differs from sealed fresh market')
    else:
        candidate=_groups(fresh,event,as_of).get(market)
        if candidate and candidate['captured_at']>=offer['captured_at']:
            raise ValueError('NFL retained line cannot override equal or newer fresh market')
        row,payload,digest=_origin(group.get('origin_snapshot_id'),event,as_of)
        if digest!=group.get('origin_sha256') or row['created_at']!=group.get('origin_saved_at'):
            raise ValueError('NFL retained line origin hash/clock conflict')
        origin_quotes=payload.get('market')
        if payload.get('forecast_policy_id')!='nfl-game-source-revisions-v1':
            origin_quotes=[q for q in origin_quotes or [] if q.get('market')==market]
        origin=_groups(origin_quotes,event,_origin_clock(row,payload)).get(market)
        if origin!=offer:
            raise ValueError('NFL retained line does not match originating fresh offer')
    return offer


def _payload(saved):
    value=saved.get('payload',saved)
    return json.loads(value) if isinstance(value,str) else value


def effective_quotes(saved,event=None):
    """Verify only the exact saved decision; never search backward on a read."""
    payload=_payload(saved);contract=payload.get('line_retention')
    if contract is None:return list(payload.get('market') or [])
    event=_event(event or payload)
    if contract.get('policy_id')!=POLICY_ID or _sha({k:v for k,v in contract.items() if k!='sha256'})!=contract.get('sha256'):
        raise ValueError('NFL line retention contract hash/policy conflict')
    groups=contract.get('groups')
    if not isinstance(groups,dict) or set(groups)-set(MARKETS):
        raise ValueError('NFL line retention markets invalid')
    as_of=_number(payload.get('forecast_as_of'))
    if set(_groups(payload.get('market'),event,as_of))-set(groups):
        raise ValueError('NFL line binding omits a complete fresh market')
    rows=[]
    for market in MARKETS:
        if market in groups:
            rows.extend(_verify_group(groups[market],market,event,as_of,payload.get('market'))['quotes'])
    return rows


def line_refs(saved,event=None):
    payload=_payload(saved)
    if payload.get('line_retention') is None:return {}
    effective_quotes(saved,event)
    return {market:dict({k:group.get(k) for k in ('basis','book','captured_at','origin_snapshot_id','origin_sha256','origin_saved_at')},
                        quote_ids=[q['id'] for q in group['quotes']],quotes_sha256=_sha(group['quotes']),
                        retained=group['basis']=='retained',comparison_only=group['basis']=='retained')
            for market,group in payload['line_retention']['groups'].items()}


def resolve(event,fresh_quotes,as_of,previous=None):
    """Resolve complete comparison groups while the game is still pregame."""
    if str(event.get('status','')).upper() in {'STATUS_SUSPENDED','SUSPENDED','STATUS_POSTPONED','POSTPONED',
            'STATUS_CANCELED','STATUS_CANCELLED','CANCELED','CANCELLED','STATUS_FINAL','FINAL'}:
        raise ValueError('NFL line retention needs an active pregame schedule')
    event=_event(event);as_of=_number(as_of)
    if as_of>=parse_time(event['start_time']):raise ValueError('NFL line retention is pregame only')
    fresh=_groups(fresh_quotes,event,as_of);old={}
    if previous:
        pp=_payload(previous)
        legacy=pp.get('forecast_policy_id')!='nfl-game-source-revisions-v1'
        if legacy:
            source,sealed,digest,proved=_legacy_parent(previous['snapshot_id'],event,as_of)
        else:
            source,sealed,digest=_snapshot(previous['snapshot_id'],event,as_of)
        if _encoded(pp)!=_encoded(sealed):
            raise ValueError('NFL line parent payload differs from sealed snapshot')
        if legacy:
            markets={q.get('market') for q in pp.get('market') or [] if q.get('market') in MARKETS}
            needed=markets-set(fresh)
            if needed:
                # Fully replaced legacy prices are not evidence in the new
                # decision. Verify every source within each actually used kind.
                prior=_groups([q for q in pp.get('market') or [] if q.get('market') in needed],
                              event,_origin_clock(source,pp))
                if needed-set(prior) or not proved:
                    raise ValueError('NFL unverified legacy priced market cannot be erased')
                for market,offer in prior.items():
                    old[market]=dict(offer,basis='retained',origin_snapshot_id=previous['snapshot_id'],
                                    origin_sha256=digest,origin_saved_at=source['created_at'])
        elif pp.get('line_retention') is not None:
            effective_quotes(previous,event)
            for market,group in pp['line_retention']['groups'].items():
                origin_id=group.get('origin_snapshot_id') or previous['snapshot_id']
                row,origin,digest=_origin(origin_id,event,as_of)
                old[market]=dict(group,basis='retained',origin_snapshot_id=origin_id,
                                origin_sha256=digest,origin_saved_at=row['created_at'])
        else:
            # A future game may adopt already sealed pregame evidence. Historical
            # page reads never execute this lookup or manufacture past picks.
            for row in db.q('SELECT * FROM nfl_game_snapshots WHERE game_id=? AND created_at<=? ORDER BY created_at DESC,id DESC',
                            (event['id'],as_of)):
                p=json.loads(row['payload'])
                if p.get('forecast_policy_id')!='nfl-game-source-revisions-v1':continue
                if not p.get('market'):continue
                source,p,digest=_snapshot(row['id'],event,as_of)
                for market,offer in _groups(p.get('market'),event,p['forecast_as_of']).items():
                    old.setdefault(market,dict(offer,basis='retained',origin_snapshot_id=row['id'],
                                              origin_sha256=digest,origin_saved_at=source['created_at']))
                if set(old)==set(MARKETS):break
    groups={}
    for market in MARKETS:
        offer=fresh.get(market);prior=old.get(market)
        if offer is not None and (prior is None or offer['captured_at']>=prior['captured_at']):
            if prior and offer['captured_at']==prior['captured_at'] and offer['book']==prior['book'] \
                    and offer['quotes']!=prior['quotes']:
                raise ValueError('NFL line equal-clock retained offer conflict')
            groups[market]=dict(offer,basis='fresh',origin_snapshot_id=None,origin_sha256=None,origin_saved_at=None)
        elif prior is not None:groups[market]=prior
    contract=dict(policy_id=POLICY_ID,parent_snapshot_id=previous['snapshot_id'] if previous else None,groups=groups)
    return dict(contract,sha256=_sha(contract))


def verify_origin(saved,event,market):
    """Read-only historical-audit primitive; returns one existing sealed pair."""
    ident=saved.get('snapshot_id',saved.get('id'))
    row,payload,digest=_snapshot(ident,_event(event),parse_time(_event(event)['start_time'])-1e-6)
    group=_groups(payload.get('market'),_event(event),payload['forecast_as_of']).get(market)
    if group is None:return None
    return dict(snapshot_id=ident,snapshot_sha256=digest,model_version=row['model_version'],
                created_at=row['created_at'],as_of=payload['forecast_as_of'],projection=payload.get('projection'),
                market=market,quotes=group['quotes'],captured_at=group['captured_at'],book=group['book'],
                kickoff_utc=_event(event)['start_time'])
