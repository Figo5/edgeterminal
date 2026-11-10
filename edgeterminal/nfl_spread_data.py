"""Read existing immutable NFL snapshots and official cached finals, offline.

No provider is called. Old missing PBP provenance is never inferred, and an
already completed game cannot become a new prospective freeze.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
import csv
from datetime import datetime
from functools import wraps
import gzip
import io
import time
from zoneinfo import ZoneInfo
import hashlib
import json
from urllib.parse import urlsplit, parse_qsl

from . import db, nfl_identity as identity
from .integrity import parse_time
from . import nfl_spread_model as model

SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
INACTIVE = {'STATUS_FINAL','FINAL','STATUS_CANCELED','STATUS_CANCELLED','STATUS_POSTPONED'}


def _object(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise ValueError('NFL spread source has duplicate JSON keys')
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('NFL spread source nonfinite value')))


def event_game(raw):
    if not isinstance(raw,dict): raise ValueError('NFL spread event missing')
    event = dict(raw)
    if event.get('kickoff_utc') and not event.get('start_time'): event['start_time'] = event['kickoff_utc']
    if not event.get('id'): event['id'] = event.get('game_id')
    if identity.canonical_id(event) != event.get('id') or '-reg-' not in str(event.get('id')):
        raise ValueError('NFL spread canonical game identity conflict')
    return event


def quote_context(quotes, event, as_of, kickoff):
    """Use the exact first saved home line, as the season record does."""
    from . import nfl_game_lines
    home,away = event['home_team'],event['away_team']; selected=[]
    for raw in quotes or []:
        if raw.get('market') != 'spread': continue
        if raw.get('selection') not in (home,away): raise ValueError('NFL spread quote team mismatch')
        if raw.get('event_id') != event['id'] or raw.get('provider') != 'espn-odds' or raw.get('is_demo'):
            raise ValueError('NFL spread quote provider/event mismatch')
        if raw.get('quote_phase') != 'current' or not nfl_game_lines.valid_source_url(raw.get('source_url'),event):
            raise ValueError('NFL spread quote source/phase mismatch')
        captured = parse_time(raw.get('captured_at'))
        if captured is None or not captured <= as_of < kickoff:
            raise ValueError('NFL spread quote receipt chronology conflict')
        line,price = model.number(raw.get('line')),model.number(raw.get('price'))
        if abs(line)>40 or -100<price<100: raise ValueError('NFL spread quote bounds invalid')
        saved = db.q1('SELECT * FROM odds_snapshots WHERE id=?',(raw.get('id'),))
        fields=('event_id','sport','market','selection','line','price','book','captured_at','provider','source_url','quote_phase','is_demo')
        if not saved or any(saved.get(k) != raw.get(k) for k in fields):
            raise ValueError('NFL spread quote differs from immutable observation')
        selected.append(raw)
    chosen = next((r for r in selected if r['selection']==home),None)
    if chosen is None: return None
    same=[r for r in selected if r['book']==chosen['book'] and r['captured_at']==chosen['captured_at']]
    if any((r['line'] if r['selection']==home else -r['line']) != chosen['line'] for r in same):
        raise ValueError('NFL spread paired quote orientation conflict')
    return dict(line=chosen['line'], quote_id=chosen['id'], captured_at=chosen['captured_at'],
                sha256=model.sha({k:chosen.get(k) for k in fields}))


def observation(snapshot_id, *, now, deep=False, proof_cache=None):
    raw = db.q1('SELECT * FROM nfl_game_snapshots WHERE id=?',(snapshot_id,))
    if not raw: raise ValueError('NFL spread snapshot missing')
    payload = decode(raw['payload'])
    if payload.get('forecast_policy_id')!='nfl-game-source-revisions-v1':
        return None  # Old snapshots without this contract cannot be inferred into sealed evidence.
    digest=hashlib.sha256(raw['payload'].encode()).hexdigest()
    if raw['id']!=snapshot_id or snapshot_id!=digest[:24] or raw['payload']!=model.encoded(payload):
        raise ValueError('NFL spread new-policy snapshot identity/hash conflict')
    game = event_game(payload.get('game'))
    if game['id'] != raw['game_id']: raise ValueError('NFL spread snapshot game mismatch')
    current = db.q1('SELECT * FROM events WHERE id=?',(game['id'],))
    if not current or identity.canonical_id(current) != game['id']:
        raise ValueError('NFL spread current event identity missing')
    if any(current.get(k)!=game.get(k) for k in ('home_team','away_team')):
        raise ValueError('NFL spread current event team conflict')
    kickoff = min(parse_time(game['start_time']),parse_time(current['start_time']))
    at = model.number(payload.get('created_at'))
    if not at <= raw['created_at'] <= now or not at < kickoff:
        raise ValueError('NFL spread snapshot receipt chronology conflict')
    source = payload.get('game_input_map')
    if not source: raise ValueError('NFL spread snapshot has unverifiable PBP input policy')
    from . import nfl_game_sources
    verified_source=nfl_game_sources.verify_input_map(source,game,source['as_of'],deep=deep,proof_cache=proof_cache)
    if source['as_of'] > at: raise ValueError('NFL spread snapshot uses future source context')
    from . import nfl_game_lines
    quote = quote_context(nfl_game_lines.effective_quotes(payload,game),game,at,kickoff)
    if quote is None: return None
    projection = payload.get('projection') or {}; stats = payload.get('team_form') or {}
    home,away=game['home_team'],game['away_team']
    if not stats.get(home) or not stats.get(away): raise ValueError('NFL spread scoring inputs missing')
    if not isinstance(verified_source,dict) or set(verified_source.get('stats',{}))!={home,away}:
        raise ValueError('NFL spread advanced source replay missing')
    for team,expected in verified_source['stats'].items():
        if any(stats[team].get(key)!=value for key,value in expected.items()):
            raise ValueError('NFL spread advanced fields differ from verified source')
    for team in (home,away):
        if not model.number(stats[team].get('retrieved_at')) <= at:
            raise ValueError('NFL spread scoring source receipt conflict')
    from .analytics import football
    meta=game.get('meta') or {}
    if isinstance(meta,str): meta=decode(meta)
    rebuilt=football.project_game('NFL',home,away,stats,neutral=bool(payload.get('neutral_site',meta.get('neutral_site'))))
    for key in ('margin','total','sd_margin','home_win_prob'):
        if rebuilt[key] != projection.get(key):
            raise ValueError('NFL spread saved incumbent formula replay conflict')
    component=projection.get('spread_component') or {}
    incumbent=component['margin'] if component.get('used') else projection['margin']
    teams=source['teams']; h,a=teams.get(home),teams.get(away)
    if not h or not a: raise ValueError('NFL spread advanced coverage identity missing')
    for team,coverage in ((home,h),(away,a)):
        for key in ('games','plays'): model.number(coverage[key])
    parts=game['id'].split('-')
    out=dict(game_id=game['id'],season=int(parts[1]),week=int(parts[3]),snapshot_id=snapshot_id,
        snapshot_sha256=hashlib.sha256(raw['payload'].encode()).hexdigest(),source_sha256=source['sha256'],
        formula_version=raw['model_version'],input_policy_id=source['policy_id'],as_of=at,kickoff=kickoff,
        feature_received_at=max(source['original_received_at'],source['adopted_at'],source.get('published_at',source['adopted_at']),stats[home]['retrieved_at'],stats[away]['retrieved_at']),
        baseline_margin=projection['margin'],baseline_sd=projection['sd_margin'],incumbent_margin=incumbent,
        incumbent_version=component.get('version') if component.get('used') else raw['model_version'],
        incumbent_probability=model.probability(incumbent,component.get('sd_margin',projection['sd_margin']),quote['line']),
        home_spread=quote['line'],quote_id=quote['quote_id'],quote_sha256=quote['sha256'],
        scoring_margin=projection['margin']-(projection.get('feature_groups') or {}).get('efficiency_points',0.),
        efficiency_points=(projection.get('feature_groups') or {}).get('efficiency_points',0.),
        home_games=h['games'],away_games=a['games'],home_plays=h['plays'],away_plays=a['plays'],
        advanced_available=float(min(h['games'],a['games'],h['plays'],a['plays'])>0))
    return model.validate_example(out,label=False)


def observations(*, now, deep=False):
    result=[]; unknown=0; proof_cache={}
    # Old unproven legacy snapshots are reported, never fabricated into examples.
    for raw in db.q("SELECT id,payload FROM nfl_game_snapshots ORDER BY created_at,id"):
        payload=decode(raw['payload'])
        if (payload.get('forecast_policy_id')!='nfl-game-source-revisions-v1'
                or not payload.get('game_input_map') or payload['game_input_map'].get('status')=='legacy_fallback'):
            unknown+=1;continue
        parts=raw.get('id')
        game_id=payload.get('game',{}).get('id','')
        if game_id.startswith('nfl-2026-reg-08-') or game_id.startswith('nfl-2026-reg-09-'): continue
        item=observation(parts,now=now,deep=deep,proof_cache=proof_cache)
        if item: result.append(item)
    return result,unknown


def _score(value):
    if isinstance(value,bool): raise ValueError('NFL spread final score invalid')
    try: result=float(value)
    except (TypeError,ValueError) as exc: raise ValueError('NFL spread final score invalid') from exc
    if not 0<=model.number(result)<=100 or result!=int(result): raise ValueError('NFL spread final score invalid')
    return int(result)


def labels(rows, *, now):
    """Read only these requested games. Outer callers must reserve first."""
    by_week=defaultdict(dict)
    for row in rows: by_week[(row['season'],row['week'])][row['game_id']]=row
    result={}; seen_clock={}; current_semantics={}
    for source in db.q("SELECT sha256,source_file,captured_at FROM nfl_prop_grade_source_artifacts "
            "WHERE source_file LIKE ? AND captured_at<=? ORDER BY captured_at,sha256",(SCOREBOARD+'%',now)):
        url=urlsplit(source['source_file']); pairs=parse_qsl(url.query,keep_blank_values=True); query=dict(pairs)
        if (url.scheme!='https' or url.hostname!='site.api.espn.com' or url.netloc!='site.api.espn.com'
                or url.path!=urlsplit(SCOREBOARD).path or url.fragment or len(query)!=len(pairs)
                or not set(query)<={'dates','seasontype','week','limit'}):
            raise ValueError('NFL spread final source URL conflict')
        try: key=(int(query.get('dates','0')),int(query.get('week','0')))
        except ValueError as exc: raise ValueError('NFL spread final source identity invalid') from exc
        if key not in by_week: continue
        if query.get('seasontype')!='2': raise ValueError('NFL spread final source is not regular season')
        full=db.q1('SELECT payload FROM nfl_prop_grade_source_artifacts WHERE sha256=?',(source['sha256'],))
        raw=bytes(full['payload'])
        if len(raw)>50*1024*1024 or hashlib.sha256(raw).hexdigest()!=source['sha256']:
            raise ValueError('NFL spread final source hash/size conflict')
        body=decode(raw)
        if ((body.get('season') or {}).get('year'),(body.get('week') or {}).get('number'))!=key or (body.get('season') or {}).get('type')!=2:
            raise ValueError('NFL spread final source season/week conflict')
        events=body.get('events')
        if not isinstance(events,list): raise ValueError('NFL spread final source lacks events')
        identities=set(); canonical_ids=set()
        for event in events:
            if not isinstance(event,dict) or str(event.get('id')) in identities:
                raise ValueError('NFL spread final source duplicate event')
            identities.add(str(event.get('id')))
            competitions=event.get('competitions')
            if not isinstance(competitions,list) or len(competitions)!=1: raise ValueError('NFL spread final event competition ambiguous')
            competitors=competitions[0].get('competitors')
            if not isinstance(competitors,list) or len(competitors)!=2: raise ValueError('NFL spread final teams ambiguous')
            h=next((r for r in competitors if r.get('homeAway')=='home'),None)
            a=next((r for r in competitors if r.get('homeAway')=='away'),None)
            if not h or not a: raise ValueError('NFL spread final orientation ambiguous')
            hc=identity.team_code((h.get('team') or {}).get('displayName') or (h.get('team') or {}).get('abbreviation'))
            ac=identity.team_code((a.get('team') or {}).get('displayName') or (a.get('team') or {}).get('abbreviation'))
            if not hc or not ac or hc==ac: raise ValueError('NFL spread final teams invalid')
            gid=f'nfl-{key[0]}-reg-{key[1]:02d}-{ac}-{hc}'
            if gid in canonical_ids:raise ValueError('NFL spread final source ambiguous canonical identity')
            canonical_ids.add(gid)
            if gid not in by_week[key]: continue
            status=(event.get('status') or {}).get('type') or {}
            name=status.get('name');completed=status.get('completed')
            if not isinstance(name,str) or not name.strip() or not isinstance(completed,bool):
                raise ValueError('NFL spread official status evidence is ambiguous')
            name=name.upper();is_final=name in {'STATUS_FINAL','FINAL'}
            if is_final!=completed:
                raise ValueError('NFL spread official final/status conflict')
            kickoff=parse_time(event.get('date')); expected=by_week[key][gid]
            if kickoff is None or abs(kickoff-expected['kickoff'])>4*3600 or source['captured_at']>now:
                raise ValueError('NFL spread final receipt/kickoff conflict')
            if is_final:
                if not kickoff<source['captured_at']:
                    raise ValueError('NFL spread final evidence predates kickoff')
                home,away=_score(h.get('score')),_score(a.get('score'))
                signature=('final',home,away,kickoff)
            else:
                signature=('nonfinal',name,kickoff)
            clock=(gid,source['captured_at'])
            if clock in seen_clock and seen_clock[clock]!=signature:
                raise ValueError('NFL spread equal-clock official status/final conflict')
            seen_clock[clock]=signature
            if not is_final:
                # This is an authenticated withdrawal, not a missing final feed.
                # Never resurrect an older label after a newer explicit status.
                result.pop(gid,None);current_semantics[gid]=signature
                continue
            if gid in result and current_semantics.get(gid)==signature:
                continue  # Identical polls retain the original availability clock.
            current_semantics[gid]=signature
            result[gid]=dict(actual_margin=home-away,final_home=home,final_away=away,
                label_received_at=source['captured_at'],label_sha256=source['sha256'])
    return result


# This context belongs only to one explicit offline learning invocation. Product
# reads and capture never populate it or decompress annual schedule archives.
_SCHEDULE_PROOFS=ContextVar('nfl_spread_verified_week_schedules',default=None)


@contextmanager
def schedule_proofs():
    if _SCHEDULE_PROOFS.get() is not None:
        yield
        return
    token=_SCHEDULE_PROOFS.set({})
    try:yield
    finally:_SCHEDULE_PROOFS.reset(token)


def with_schedule_proofs(action):
    @wraps(action)
    def run(*args,**kwargs):
        with schedule_proofs():return action(*args,**kwargs)
    return run


def _schedule_identities(raw,season):
    """Bounded, label-blind membership reader: no score/status cell is accessed."""
    from . import nfl_game_sources as sources
    required={'game_id','season','game_type','week','gameday','gametime','home_team','away_team'}
    groups=defaultdict(list);seen=set();old=csv.field_size_limit(sources.MAX_FIELD)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            stream=io.TextIOWrapper(io.BufferedReader(sources._Limited(compressed,time.monotonic()+sources.MAX_SECONDS)),encoding='utf-8-sig',newline='')
            reader=csv.DictReader(stream);fields=reader.fieldnames or []
            if len(fields)!=len(set(fields)) or not required<=set(fields):
                raise ValueError('NFL spread schedule identity columns missing or duplicated')
            for index,row in enumerate(reader,1):
                if index>sources.MAX_ROWS:raise ValueError('NFL spread schedule row bound exceeded')
                if None in row or any(row[k] is None for k in required):
                    raise ValueError('NFL spread schedule identity row is malformed')
                try:year,week=int(row['season']),int(row['week'])
                except (TypeError,ValueError) as exc:raise ValueError('NFL spread schedule year/week malformed') from exc
                if not 1900<=year<=2100 or not 0<=week<=24:
                    raise ValueError('NFL spread schedule year/week bounds invalid')
                if row['game_type']!='REG' or year!=season:continue
                key=sources._game_key(row['game_id'])
                away,home=identity.team_code(row['away_team']),identity.team_code(row['home_team'])
                if not 1<=week<=18 or not away or not home or away==home or key!=(year,week,away,home):
                    raise ValueError('NFL spread schedule canonical team/game identity conflict')
                gid=f'nfl-{year}-reg-{week:02d}-{away}-{home}'
                if gid in seen:raise ValueError('NFL spread schedule repeats a canonical game')
                seen.add(gid)
                try:
                    day=datetime.strptime(row['gameday'],'%Y-%m-%d')
                    clock=row['gametime'].strip()
                    when=(datetime.strptime(row['gameday']+' '+clock,'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
                          if clock not in {'','NA','TBD'} else None)
                except ValueError as exc:raise ValueError('NFL spread schedule kickoff is malformed') from exc
                groups[week].append(dict(id=gid,home_team=identity.canonical_team(row['home_team']),
                    away_team=identity.canonical_team(row['away_team']),kickoff=when,gameday=day.date().isoformat()))
    except (gzip.BadGzipFile,EOFError,UnicodeError,csv.Error) as exc:
        raise ValueError('NFL spread schedule gzip/CSV is invalid') from exc
    finally:csv.field_size_limit(old)
    return {week:sorted(rows,key=lambda r:r['id']) for week,rows in groups.items()}


def schedule_head(season,now=None):
    """Shallow authenticated metadata only, including raw/manifest file heads."""
    from . import nfl_weekly_preparation as weekly
    from . import learning_publication as publication
    saved=weekly.read_schedule(season,now=now,deep=False)
    if saved.get('status')!='ready':return dict(status='missing',season=season)
    descriptor=saved['descriptor'];ident=saved['schedule_id']
    paths=(weekly._source_path('weekly_schedules/manifests/'+ident+'.json'),weekly._source_path(descriptor['raw_path']))
    # Include names relative to the data root so relocation only requires an
    # offline recheck; no saved absolute legacy path is trusted.
    root=weekly.sources.root()
    files=[(str(path.relative_to(root)),)+tuple(info[1:]) for path,info in zip(sorted(paths,key=str),publication.file_seals(paths))]
    return dict(status='ready',season=season,schedule_id=ident,descriptor=descriptor,
                published_at=saved['published_at'],publication_event_id=saved['publication_event_id'],files=files)


def schedule_paths(seasons,now=None):
    from . import nfl_weekly_preparation as weekly
    result=set()
    for season in sorted(set(seasons)):
        head=schedule_head(season,now)
        if head['status']=='ready':
            result.update((weekly._source_path('weekly_schedules/manifests/'+head['schedule_id']+'.json'),
                           weekly._source_path(head['descriptor']['raw_path'])))
    return sorted(result,key=str)


def prepare_schedule_proofs(seasons,now=None):
    """Offline only: authenticate raw identity universes without interpreting labels."""
    from . import nfl_weekly_preparation as weekly,nfl_game_sources as sources
    cache=_SCHEDULE_PROOFS.get()
    if cache is None:raise ValueError('NFL spread schedule proof requires an owned offline context')
    ready=True
    for season in sorted(set(seasons)):
        head=schedule_head(season,now)
        if head['status']!='ready' or head['descriptor'].get('complete') is not True:
            cache.pop(season,None);ready=False;continue
        if season in cache and cache[season]['head']==head:continue
        raw=sources._read(weekly._source_path(head['descriptor']['raw_path']),sources.MAX_COMPRESSED)
        if hashlib.sha256(raw).hexdigest()!=head['descriptor']['raw_sha256']:
            raise ValueError('NFL spread authenticated schedule raw hash conflict')
        groups=_schedule_identities(raw,season)
        if not groups:ready=False;cache.pop(season,None);continue
        if head!=schedule_head(season,now):raise ValueError('NFL spread schedule head changed during identity verification')
        cache[season]=dict(head=head,weeks=groups)
    return ready


def week_universe(season,week):
    cache=_SCHEDULE_PROOFS.get() or {};proof=cache.get(season)
    if not proof or proof['head']!=schedule_head(season):return []
    expected=proof['weeks'].get(week)
    if not expected:return []
    stored=db.q("SELECT * FROM events WHERE id LIKE ? ORDER BY id",(f'nfl-{season}-reg-{week:02d}-%',))
    actual={event_game(event)['id']:event for event in stored}
    wanted={row['id']:row for row in expected}
    if set(actual)-set(wanted):raise ValueError('NFL spread stored week conflicts with authenticated schedule universe')
    if set(actual)!=set(wanted):return []
    for gid,event in actual.items():
        match=wanted[gid]
        if match['kickoff'] is None:return []
        if any(event[k]!=match[k] for k in ('home_team','away_team')) or parse_time(event['start_time'])!=match['kickoff']:
            raise ValueError('NFL spread stored week kickoff/team conflicts with authenticated schedule')
    return [actual[gid] for gid in sorted(actual)]


def complete_week(season,week,now):
    universe=week_universe(season,week)
    return bool(universe) and all(str(e['status']).upper() in {'STATUS_FINAL','FINAL'} and parse_time(e['start_time'])<now for e in universe)
