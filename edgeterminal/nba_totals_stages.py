"""Receipt-aware durable NBA totals stages and explicit v2 outer contract.

The source worker captures only pure frozen-bank predictions. The offline worker
owns archive replay, method selection and calibration. Existing v1 ledgers and
their protocol remain separate and are never rewritten or silently resealed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import time

from . import db, nba_totals_data as original_data, nba_totals_model as model
from . import nba_totals_stage_data as data, nba_totals_stage_store as store
from . import nba_totals_stage_outer as outer, nba_totals_fit_bank as bank
from . import nba_totals_learning as legacy, nba_totals_store as legacy_store

FLOORS = dict(bank.FLOORS)
DAY=86400
WEEK=7*DAY
_ready=None


def _physical_now(requested=None):
    return max(store.stamp(requested),db.now())


class StageConflict(ValueError):
    """A competing durable checkpoint won; the earlier stage is still valid."""


def _work_now(requested=None):
    # Caller clocks cannot turn future dates or receipts into completed work.
    return min(_physical_now(requested),db.now())


def _exists(table):
    return db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)) is not None


def ensure_schema():
    global _ready
    store.ensure_schema()
    if _ready is db.conn():
        return
    with db.tx() as con:
        con.execute('CREATE TABLE IF NOT EXISTS nba_totals_training_stages ('
                    'id INTEGER PRIMARY KEY,chain_id TEXT NOT NULL,kind TEXT NOT NULL,created_at REAL NOT NULL,'
                    'payload TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,previous_sha256 TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS nba_totals_inner_reservations ('
                    'reservation_id TEXT PRIMARY KEY,chain_id TEXT NOT NULL,phase TEXT NOT NULL,created_at REAL NOT NULL,'
                    'payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS nba_totals_inner_freezes ('
                    'freeze_id TEXT PRIMARY KEY,chain_id TEXT NOT NULL,forecast_sha256 TEXT NOT NULL,created_at REAL NOT NULL,'
                    'payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(chain_id,forecast_sha256))')
        for table in ('nba_totals_training_stages','nba_totals_inner_reservations','nba_totals_inner_freezes'):
            for action in ('UPDATE','DELETE'):
                con.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                            "BEGIN SELECT RAISE(ABORT,'NBA totals stages are append-only'); END")
    _ready=db.conn()


def current(chain_id=None):
    if not _exists('nba_totals_training_stages'):
        return None
    row=db.q1('SELECT * FROM nba_totals_training_stages '+('WHERE chain_id=? ' if chain_id else '')+'ORDER BY id DESC LIMIT 1',
              (chain_id,) if chain_id else ())
    if row is None:
        return None
    previous=None
    for entry in db.q('SELECT * FROM nba_totals_training_stages WHERE chain_id=? ORDER BY id',(row['chain_id'],)):
        payload=json.loads(entry['payload'])
        expected=store.sha(dict(chain_id=entry['chain_id'],kind=entry['kind'],created_at=entry['created_at'],
                                payload=payload,previous_sha256=previous))
        if entry['previous_sha256']!=previous or entry['sha256']!=expected:
            raise ValueError('NBA totals training stage chain seal mismatch')
        previous=expected
    row['payload']=json.loads(row['payload'])
    return row


def append_event(chain_id,kind,payload,*,now,expected):
    ensure_schema(); stamp=_physical_now(now)
    value=dict(chain_id=chain_id,kind=kind,created_at=stamp,payload=payload,previous_sha256=expected)
    digest=store.sha(value)
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        head=con.execute('SELECT sha256 FROM nba_totals_training_stages WHERE chain_id=? ORDER BY id DESC LIMIT 1',(chain_id,)).fetchone()
        if (head['sha256'] if head else None)!=expected:
            raise StageConflict('NBA totals stage ownership changed')
        if kind=='context_changed':
            boundary=con.execute("SELECT MAX(id) FROM nba_totals_stage_runs WHERE "
                "COALESCE(json_extract(detail,'$.kind'),'retuning')!='diagnostics'").fetchone()[0] or 0
            payload=dict(payload,context_change=dict(payload['context_change'],after_retuning_run_id=boundary))
            value=dict(value,payload=payload); digest=store.sha(value)
        con.execute('INSERT INTO nba_totals_training_stages(chain_id,kind,created_at,payload,sha256,previous_sha256) VALUES(?,?,?,?,?,?)',
                    (chain_id,kind,stamp,store.encoded(payload),digest,expected))
    return current(chain_id)


def _verify(event, *, full=False, expected_code=None):
    if event['kind']=='invalidated':
        raise ValueError('NBA totals stage was explicitly invalidated')
    payload=event['payload']; bank.verify_bank(payload['bank'])
    if payload['code_sha256']!=(store.code_catalog() if expected_code is None else expected_code):
        raise ValueError('NBA totals stage code fingerprint changed')
    if set(payload['code_archive'])!=set(payload['code_sha256']):
        raise ValueError('NBA totals stage code archive catalog mismatch')
    for name,relative in payload['code_archive'].items():
        import hashlib
        if hashlib.sha256(store.read_archived_code(relative)).hexdigest()!=payload['code_sha256'][name]:
            raise ValueError('NBA totals stage archived code changed')
    for descriptor in payload['sources']:
        source=db.q1('SELECT sha256 FROM nba_source_artifacts WHERE id=?',(descriptor['id'],))
        if not source or source['sha256']!=descriptor['sha256']:
            raise ValueError('NBA totals stage source hash changed')
        if full:
            verified,_,_=data._source(descriptor['id'],payload['bank']['cutoff'])
            if verified['sha256']!=descriptor['sha256']:
                raise ValueError('NBA totals stage source bytes changed')
    for phase in ('development','calibration'):
        saved=payload.get(phase+'_state')
        rows=payload.get(phase+'_examples')
        if saved is not None and rows and saved!=data.validation_state([r['game_id'] for r in rows],now=store.stamp()):
            raise ValueError('NBA totals completed inner phase evidence head changed')
    if full:
        for phase in ('development','calibration'):
            rows=payload.get(phase+'_examples')
            if not rows:
                continue
            universe={}
            for row in rows:
                data.verify_input_map(row['input_map'],dict(game_id=row['game_id'],home_team=row['input_map']['home'],
                    away_team=row['input_map']['away'],tipoff_ts=row['tipoff']),row['forecast_at'])
                universe.setdefault(row['date_et'],[]).append(row['game_id'])
            universe={day:sorted(ids) for day,ids in universe.items()}
            reserved=_inner_reserve(event,phase,rows,universe,store.stamp())
            labels=data.inner_labels(reserved,now=store.stamp())
            actual={r['game_id']:r for r in labels['rows']}
            if set(actual)!={r['game_id'] for r in rows} or set(labels['complete_dates'])!=set(universe):
                raise ValueError('NBA totals completed inner phase coverage changed')
            if any(actual[r['game_id']]['label_sha256']!=r['label_sha256'] or
                   actual[r['game_id']]['actual_total']!=r['actual_total'] for r in rows):
                raise ValueError('NBA totals completed inner phase result changed')
    return payload


def transition_code_context(now=None):
    """Close only an explicitly authorized old context; never fit or reseal it."""
    from . import learning_code_context as releases
    event=current()
    if event and event['kind']=='context_changed':
        change=event['payload']['context_change']
        auth=releases.authorization('NBA_TOTAL',event['payload']['code_sha256'],store.code_catalog())
        if not auth or auth['authorization_id']!=change['authorization_id'] or auth['authorization_sha256']!=change['authorization_sha256']:
            raise ValueError('NBA totals approved release context changed after transition')
        return event
    if not event or event['kind'] in ('retained','invalidated','candidate'):
        return event
    before=event['payload'].get('code_sha256'); after=store.code_catalog()
    if before==after:
        return event
    auth=releases.authorization('NBA_TOTAL',before,after)
    if auth is None:
        return event  # Existing strict capture/fit checks keep this mismatch visible.
    if event['payload'].get('contract')!=bank.CONTRACT:
        raise ValueError('NBA totals staged protocol changed before release transition')
    old_candidates=_exists('nba_totals_candidates') and (legacy_store.pending() or (legacy_store.active_event() or {}).get('version'))
    if store.pending() or (store.active_event() or {}).get('version') or old_candidates:
        raise ValueError('NBA totals code transition requires no candidate or active learned model')
    payload=_verify(event,expected_code=auth['components']['NBA_TOTAL']['before'])
    for descriptor in payload['sources']:
        verified,_,_=data._source(descriptor['id'],payload['bank']['cutoff'])
        if verified['sha256']!=descriptor['sha256']:
            raise ValueError('NBA totals context source bytes changed')
    dates=list(payload['bank']['fit']['dates'])
    for phase in ('development','calibration'):
        dates.extend(row['date_et'] for row in payload.get(phase+'_examples',[]))
        dates.extend((payload.get('selected') or {}).get(phase,{}).get('dates',[]))
    for row in db.q('SELECT reservation_id FROM nba_totals_inner_reservations WHERE chain_id=?',(event['chain_id'],)):
        reservation=validate_inner_reservation(row['reservation_id'])
        dates.extend(reservation.get('date_ids',[]))
    if store.code_catalog()!=after:
        raise StageConflict('NBA totals code changed during release context verification')
    stamp=_physical_now(now)
    change=dict(authorization_id=auth['authorization_id'],authorization_sha256=auth['authorization_sha256'],
                before_sha256=releases.sha(before),after_sha256=releases.sha(after),
                base_commit=auth['base_commit'],target_commit=auth['target_commit'])
    return append_event(event['chain_id'],'context_changed',dict(payload,stage='context_changed',
        reason='approved_release_code_context_changed',context_change=change,context_after_day=max(dates),
        next_due_at=stamp+WEEK),now=stamp,expected=event['sha256'])


def _prepare(raw,cutoff):
    return legacy._training_examples(raw,cutoff)


def _initial(stamp,after_day=None):
    catalog=store.code_catalog()
    raw,sources=data.training_examples(stamp-1)
    examples=_prepare(raw,stamp-1)
    prefix=bank.fit_prefix(examples,min_fit=FLOORS['fit'],after_day=after_day)
    if not prefix:
        return None,len(examples)
    # Re-resolve historical corrections at this fixed authentic receipt cutoff.
    cutoff=max(max(r['label_received_at'],r['feature_evidence_received_at']) for r in prefix)
    last_day=max(datetime.fromtimestamp(r['ts'],model.ET).date().isoformat() for r in prefix)
    fixed_raw,fixed_sources=data.training_examples(cutoff)
    fixed=_prepare(fixed_raw,cutoff)
    fixed=[r for r in fixed if datetime.fromtimestamp(r['ts'],model.ET).date().isoformat()<=last_day]
    if len(fixed)<FLOORS['fit'] or len({model._season(r['ts']) for r in fixed})<2:
        return None,len(fixed)
    fitted=bank.fit_bank(fixed,cutoff,min_fit=FLOORS['fit'])
    if catalog!=store.code_catalog():
        raise ValueError('NBA totals stage code changed during fitting')
    archives=store.archive_code(catalog)
    enrolled=_physical_now(stamp)
    payload=dict(contract=bank.CONTRACT,stage='development',enrolled_at=enrolled,bank=fitted,sources=fixed_sources,
                 code_sha256=catalog,code_archive=archives,coverage=dict(fit=len(fixed),development=0,calibration=0))
    chain=store.sha(dict(bank=fitted['sha256'],sources=store.sha(fixed_sources),code=catalog,enrolled_at=enrolled))
    return append_event(chain,'fit',payload,now=enrolled,expected=None),len(fixed)


def inner_context(record):
    """Label-blind own-asof eligibility, before this date can count toward a floor."""
    mapped=record.get('input_map')
    if not mapped:
        return None
    context=model.features(mapped['home'],mapped['away'],mapped['rows'],mapped['tipoff'],
                           as_of=mapped['as_of'],baseline_total=bank.raw_basis(record))
    result=dict(record,context=context,ts=record['tipoff'],as_of=record['forecast_at'],
                model_base_total=bank.raw_basis(record),candidate_basis=bank.CANDIDATE_BASIS,
                comparison_basis=bank.COMPARISON_BASIS,
                availability='received_by_asof')
    return result if bank.inner_eligible(result) else None


def inner_example(record,label):
    context=inner_context(record)
    if context is None:
        return None
    return dict(context,actual_total=label['actual_total'],label_received_at=label['result_at'],
                label_sha256=label.get('label_sha256'))


def _inner_freeze(event,record):
    row=db.q1('SELECT * FROM nba_totals_inner_freezes WHERE chain_id=? AND forecast_sha256=?',
              (event['chain_id'],record['forecast_sha256']))
    if row is None:
        return False
    payload=json.loads(row['payload'])
    if (store.sha(payload)!=row['sha256'] or payload.get('chain_id')!=event['chain_id']
            or payload.get('bank_sha256')!=event['payload']['bank']['sha256']
            or not record['forecast_at']<=row['created_at']<record['tipoff']
            or payload.get('input_map')!=record.get('input_map')):
        raise ValueError('NBA totals inner prediction freeze seal mismatch')
    context=model.features(record['input_map']['home'],record['input_map']['away'],record['input_map']['rows'],
                           record['tipoff'],as_of=record['forecast_at'],baseline_total=bank.raw_basis(record))
    if payload['predictions']!=bank.predictions(event['payload']['bank'],context,bank.raw_basis(record)):
        raise ValueError('NBA totals inner prediction disagrees with frozen bank')
    return True


def _inner_reserve(event,phase,records,universe,stamp):
    payload=dict(inner_stage_reservation=True,chain_id=event['chain_id'],phase=phase,
                 game_ids=sorted(r['game_id'] for r in records),date_ids=sorted(universe),
                 universe_game_ids_by_date=universe,games=records)
    ident=store.sha(payload)
    with db.tx() as con:
        con.execute('INSERT OR IGNORE INTO nba_totals_inner_reservations VALUES(?,?,?,?,?,?)',
                    (ident,event['chain_id'],phase,stamp,store.encoded(payload),ident))
    return validate_inner_reservation(dict(payload,reservation_id=ident))


def validate_inner_reservation(value):
    ident=value.get('reservation_id') if isinstance(value,dict) else value
    row=db.q1('SELECT * FROM nba_totals_inner_reservations WHERE reservation_id=?',(ident,))
    if not row:
        raise ValueError('NBA totals inner reservation unavailable')
    payload=json.loads(row['payload'])
    if store.sha(payload)!=row['sha256'] or row['sha256']!=ident:
        raise ValueError('NBA totals inner reservation seal mismatch')
    current(row['chain_id'])
    result=dict(payload,reservation_id=ident)
    if isinstance(value,dict) and value!=result:
        raise ValueError('NBA totals inner reservation differs from persisted proof')
    return result


def _inner_rows(event,phase,stamp):
    payload=event['payload']; previous=payload['bank']['fit'] if phase=='development' else payload['selected']['development']
    records=data.forecast_inputs(now=stamp,limit=10000,start_date=max(previous['dates']))
    groups=defaultdict(list)
    for record in records:
        groups[record['date_et']].append(record)
    today=datetime.fromtimestamp(stamp,model.ET).date().isoformat(); rows=[]; pending=None
    for day,items in sorted(groups.items()):
        if day>=today or day<=max(previous['dates']):
            continue
        if any(r['forecast_at']<=max(previous['receipt_max'],previous['feature_receipt_max']) or not r.get('input_map') for r in items):
            continue
        # Eligibility cannot be deferred until after a pre-label floor break:
        # an early unsupported date would otherwise permanently cap the prefix.
        if any(inner_context(r) is None for r in items):
            continue
        # Physical prospective freezes are mandatory for games enrolled after
        # the bank seal; earlier genuine saved as-of inputs may serve inner only.
        if any(r['forecast_at']>=payload['enrolled_at'] and not _inner_freeze(event,r) for r in items):
            continue
        universe={day:sorted(r['game_id'] for r in items)}
        reserved=_inner_reserve(event,phase,items,universe,stamp)
        try:
            labels=data.inner_labels(reserved,now=stamp)
        except data.ResultPending as exc:
            pending=exc
            continue
        if day not in labels['complete_dates']:
            continue
        by_game={r['game_id']:r for r in labels['rows']}
        if set(by_game)!=set(universe[day]):
            raise ValueError('NBA totals complete inner date label coverage mismatch')
        converted=[inner_example(r,by_game[r['game_id']]) for r in items]
        if any(r is None for r in converted):
            continue
        rows.extend(converted)
        if len(rows)>=FLOORS[phase]:
            break
    if not rows and pending is not None:
        raise pending
    return bank.select_whole_dates(rows,previous,minimum=FLOORS[phase])


def _waiting(event,phase,count,stamp):
    payload=event['payload']; coverage=dict(payload['coverage'],**{phase:count})
    return store.record_run(stamp,'waiting',dict(stage=phase,chain_id=event['chain_id'],coverage=coverage,
        required=dict(FLOORS),reason='collecting_'+phase+'_whole_dates',
        message=f'Totals {phase}: {count}/{FLOORS[phase]} verified games; incumbent remains live.'))


def _register_ready(event,stamp):
    payload=_verify(event); artifact=dict(payload['artifact']); chain_sha=event['sha256']
    envelope=dict(artifacts=payload['sources'],stage_contract=bank.CONTRACT,
                  candidate_basis=bank.CANDIDATE_BASIS,comparison_basis=bank.COMPARISON_BASIS,
                  stage_chain_id=event['chain_id'],stage_chain_sha256=chain_sha)
    artifact.update(stage_contract=bank.CONTRACT,stage_chain_id=event['chain_id'],stage_chain_sha256=chain_sha,
                    code_sha256=store.code_catalog(),training_sources_sha256=store.sha(envelope),
                    evaluation_protocol_sha256=store.sha(outer.protocol()))
    artifact['version']=model._version(artifact)
    candidate=store.register_candidate(artifact,outer.protocol(),envelope,now=_physical_now(stamp))
    append_event(event['chain_id'],'candidate',dict(payload,stage='validation',candidate_version=candidate['version']),
                 now=stamp,expected=event['sha256'])
    return store.record_run(stamp,'waiting',dict(stage='validation',version=candidate['version'],
                reason='fresh_frozen_whole_dates_required',required_pairs=60,required_dates=7))


def _recover_registered(candidate,stamp):
    """Finish the durable stage checkpoint if killed after candidate insertion."""
    chain=candidate['artifact'].get('stage_chain_id')
    event=current(chain) if chain else None
    if event and event['kind']=='calibration' and event['payload'].get('stage')=='ready':
        if event['sha256']!=candidate['artifact'].get('stage_chain_sha256'):
            raise ValueError('NBA totals registered candidate stage checkpoint changed')
        _verify(event)
        append_event(chain,'candidate',dict(event['payload'],stage='validation',candidate_version=candidate['version']),
                     now=stamp,expected=event['sha256'])


def run_fitting(now=None):
    stamp=_work_now(now); ensure_schema()
    event=None; after_day=None
    try:
        legacy_store.ensure_schema()
        pending=legacy_store.pending()
        if pending:
            return legacy.resume_publication(pending['version'],now=stamp)
        pending=store.pending()
        if pending:
            _recover_registered(pending,stamp)
            return outer.resume_publication(pending['version'],now=stamp)
        event=transition_code_context(now=stamp)
        if event and event['kind'] in ('retained','invalidated','candidate','context_changed'):
            if stamp<event['created_at']+WEEK:
                return store.record_run(stamp,'error' if event['kind']=='invalidated' else 'waiting',dict(stage=event['kind'],
                    reason=event['payload'].get('reason','predeclared_weekly_proposal_cadence'),next_due_at=event['created_at']+WEEK))
            dates=list(event['payload']['bank']['fit']['dates'])
            if event['payload'].get('context_after_day'):
                dates.append(event['payload']['context_after_day'])
            dates.extend((event['payload'].get('selected') or {}).get('development',{}).get('dates',[]))
            if event['payload'].get('candidate_version'):
                reserved=store.reservation_for(event['payload']['candidate_version'])
                if reserved: dates.extend(reserved['date_ids'])
            after_day=max(dates)
            event=None
        if event is None:
            event,count=_initial(stamp,after_day=after_day)
            if event is None:
                return store.record_run(stamp,'waiting',dict(stage='fit',coverage=dict(fit=count,development=0,calibration=0),
                    required=dict(FLOORS),reason='insufficient_receipt_safe_fit_history'))
        payload=_verify(event); phase=payload['stage']
        if phase=='ready':
            return _register_ready(event,stamp)
        rows=_inner_rows(event,phase,stamp)
        if len(rows)<FLOORS[phase]:
            return _waiting(event,phase,len(rows),stamp)
        if phase=='development':
            _verify(event,full=True)
            selected=bank.select_member(payload['bank'],rows,min_games=FLOORS['development'])
            _verify(event)
            payload=dict(payload,selected=selected,development_examples=rows,
                         development_state=data.validation_state([r['game_id'] for r in rows],now=stamp),
                         coverage=dict(payload['coverage'],development=len(rows)))
            if selected['identical_served_incumbent']:
                append_event(event['chain_id'],'retained',dict(payload,stage='retained'),now=stamp,expected=event['sha256'])
                return store.record_run(stamp,'retained',dict(stage='retained',reason='incumbent_won_development_no_outer_consumed',next_due_at=stamp+WEEK))
            payload['stage']='calibration'
            event=append_event(event['chain_id'],'development',payload,now=stamp,expected=event['sha256'])
            return _waiting(event,'calibration',0,stamp)
        _verify(event,full=True)
        artifact=bank.calibrate(payload['bank'],payload['selected'],rows,stamp-1,min_games=FLOORS['calibration'])
        _verify(event)
        payload=dict(payload,stage='ready',artifact=artifact,calibration_examples=rows,
                     calibration_state=data.validation_state([r['game_id'] for r in rows],now=stamp),
                     coverage=dict(payload['coverage'],calibration=len(rows)))
        event=append_event(event['chain_id'],'calibration',payload,now=stamp,expected=event['sha256'])
        return _register_ready(event,stamp)
    except (TimeoutError,):
        raise
    except data.ResultPending as exc:
        return store.record_run(stamp,'waiting',dict(stage=(event or {}).get('payload',{}).get('stage','fit'),
            reason='inner_dates_await_matching_grade',detail=str(exc)[:250],coverage=(event or {}).get('payload',{}).get('coverage',{})))
    except Exception as exc:
        from .learning_worker import LearningDeadlineExceeded
        if isinstance(exc,LearningDeadlineExceeded):
            raise
        conflict=isinstance(exc,StageConflict) or str(exc) in (
            'NBA totals stage ownership changed','NBA totals staged checkpoint ownership changed')
        if conflict:
            return store.record_run(stamp,'deferred',dict(stage=(event or {}).get('payload',{}).get('stage','fit'),
                reason='stage_ownership_changed',detail=str(exc)[:250]))
        invalidated=False
        # Infrastructure faults preserve the bank and complete phase proofs.
        # Only a verified contract failure may invalidate the current owner.
        if isinstance(exc,ValueError) and event and event['kind'] not in ('invalidated','retained','candidate'):
            head=current(event['chain_id'])
            if head['sha256']!=event['sha256']:
                return store.record_run(stamp,'deferred',dict(stage=head['payload'].get('stage','fit'),reason='stage_ownership_changed'))
            try:
                append_event(event['chain_id'],'invalidated',dict(event['payload'],stage='invalidated',reason=str(exc)[:250]),
                             now=stamp,expected=event['sha256'])
                invalidated=True
            except StageConflict:
                return store.record_run(stamp,'deferred',dict(stage=event['payload'].get('stage','fit'),reason='stage_ownership_changed'))
        return store.record_run(stamp,'error',dict(stage='invalidated' if invalidated else (event or {}).get('payload',{}).get('stage','fit'),
            reason=str(exc)[:250],error_type=type(exc).__name__))


def run_diagnostics(now=None):
    stamp=_work_now(now); ensure_schema()
    transition_code_context(now=stamp)
    pending=store.pending()
    if pending:
        _recover_registered(pending,stamp)
        return outer.resume_publication(pending['version'],now=stamp)
    try:
        state=serving_state(); event=current()
        if event and event['kind'] not in ('retained','invalidated','context_changed'):
            _verify(event)
        last=_last_retuning()
        detail=json.loads(last['detail']) if last else {}
        detail.update(kind='diagnostics',version=state['version'] if state else None,
                      stage=(event or {}).get('payload',{}).get('stage','fit'),
                      reason='stage_integrity_checked_no_training')
        return store.record_run(stamp,'retained',detail)
    except Exception as exc:
        from .learning_worker import LearningDeadlineExceeded
        if isinstance(exc,(LearningDeadlineExceeded,TimeoutError)):
            raise
        return store.record_run(stamp,'error',dict(kind='diagnostics',reason=str(exc)[:250]))


def needs_inputs():
    event=current()
    staged=bool(_exists('nba_totals_stage_candidates') and (store.pending() or store.active_event()))
    old=bool(_exists('nba_totals_candidates') and (legacy_store.pending() or legacy_store.active_event()))
    return bool(staged or old
                or event and event['kind'] in ('fit','development'))


def serving_state():
    staged=outer.serving_state() if _exists('nba_totals_stage_model_events') else None
    if staged:
        return staged
    return _legacy_serving_state() if _exists('nba_totals_model_events') else None


def _legacy_serving_state():
    """The exact v1 proof verifier, using its pure readers without schema DDL."""
    event=legacy_store.active_event()
    if not event or not event['version']:
        return None
    candidate=legacy_store.candidate(event['version'],verify_code=True)
    proof=db.q1('SELECT * FROM nba_totals_publication_proofs WHERE version=?',(event['version'],))
    evaluation=legacy_store.evaluation_for(event['version'])
    if not proof or not evaluation or not evaluation['evaluation'].get('passed'):
        raise ValueError('NBA totals active publication proof is unavailable')
    payload=json.loads(proof['payload'])
    if (legacy_store.sha(payload)!=proof['sha256'] or payload['candidate_sha256']!=candidate['sha256']
            or payload['evaluation_id']!=evaluation['evaluation_id']
            or payload['protocol_sha256']!=candidate['protocol_sha256']
            or payload['reservation_id']!=evaluation['reservation_id']
            or json.loads(event['detail']).get('proof_id')!=proof['proof_id']):
        raise ValueError('NBA totals active publication seal mismatch')
    return dict(version=candidate['version'],sha256=candidate['sha256'],artifact=candidate['artifact'],
                activated_at=event['created_at'],event_id=event['id'])


def capture_forecast(saved,input_map,now=None):
    stamp=_physical_now(now); ensure_schema()
    if stamp>=input_map['tipoff']:
        return None
    if input_map.get('sha256')!=store.sha({k:v for k,v in input_map.items() if k!='sha256'}):
        raise ValueError('NBA totals staged input-map seal mismatch')
    event=transition_code_context(now=stamp)
    captured=None
    if event and event['kind'] in ('fit','development'):
        payload=event['payload']
        if payload['code_sha256']!=store.code_catalog():
            raise ValueError('NBA totals stage capture code fingerprint changed')
        record=outer._capture_record(saved,input_map)
        if record['forecast_at']>=payload['enrolled_at']:
            context=model.features(input_map['home'],input_map['away'],input_map['rows'],input_map['tipoff'],
                                   as_of=input_map['as_of'],baseline_total=bank.raw_basis(record))
            if context.get('status')!='ok':
                return outer.capture_forecast(saved,input_map,now=stamp) or legacy.capture_forecast(saved,input_map,now=stamp)
            captured=dict(record,chain_id=event['chain_id'],bank_sha256=payload['bank']['sha256'],
                          candidate_basis=bank.CANDIDATE_BASIS,comparison_basis=bank.COMPARISON_BASIS,
                          predictions=bank.predictions(payload['bank'],context,bank.raw_basis(record)))
            digest=store.sha(captured); ident=store.sha([event['chain_id'],record['forecast_sha256']])
            physical=_physical_now(stamp)
            if physical>=record['tipoff']:
                return None
            with db.tx() as con:
                old=con.execute('SELECT sha256 FROM nba_totals_inner_freezes WHERE freeze_id=?',(ident,)).fetchone()
                if old and old['sha256']!=digest:
                    raise ValueError('NBA totals immutable inner freeze conflict')
                con.execute('INSERT OR IGNORE INTO nba_totals_inner_freezes VALUES(?,?,?,?,?,?)',
                            (ident,event['chain_id'],record['forecast_sha256'],physical,store.encoded(captured),digest))
    return outer.capture_forecast(saved,input_map,now=stamp) or legacy.capture_forecast(saved,input_map,now=stamp) or captured


def status(now=None):
    stamp=store.stamp(now)
    event=current(); last=db.q1('SELECT * FROM nba_totals_stage_runs ORDER BY id DESC LIMIT 1') if _exists('nba_totals_stage_runs') else None
    detail=json.loads(last['detail']) if last else {}
    try:
        active=serving_state()
    except Exception as exc:
        active=None; detail.update(integrity_error=str(exc),message=str(exc))
    detail.setdefault('stage',(event or {}).get('payload',{}).get('stage','fit'))
    detail.setdefault('coverage',(event or {}).get('payload',{}).get('coverage',dict(fit=0,development=0,calibration=0)))
    detail.setdefault('required',dict(FLOORS))
    pending=store.pending() if _exists('nba_totals_stage_candidates') else None
    from . import learning_worker
    result=dict(enabled=True,component='NBA_TOTAL',status='error' if detail.get('integrity_error') else last['status'] if last else 'not_checked',
                active={k:active[k] for k in ('version','sha256','activated_at')} if active else None,
                pending_version=(pending or {}).get('version'),detail=detail,
                coverage=dict(detail['coverage'],frozen_predictions=len(store.freezes(pending['version'])) if pending else 0,
                              required_pairs=60,required_dates=7),checked_at=last['started_at'] if last else None,
                next_due_at=_next_due(_last_retuning(),stamp),
                message=detail.get('message','Totals stages collect verified chronology; incumbent remains live.'))
    result['runner']=learning_worker.status('NBA_TOTAL',now=stamp)
    return result


def _last_retuning():
    if not _exists('nba_totals_stage_runs'):
        return None
    return db.q1("SELECT * FROM nba_totals_stage_runs WHERE COALESCE(json_extract(detail,'$.kind'),'retuning')!='diagnostics' ORDER BY id DESC LIMIT 1")


def _next_due(last,stamp):
    if last is None:
        return stamp
    detail=json.loads(last['detail'])
    if detail.get('next_due_at') is not None:
        return detail['next_due_at']
    interval=300 if last['status'] in ('error','deferred') else WEEK if last['status'] in ('retained','activated','rejected') else DAY
    return last['started_at']+interval


def refresh_inputs(now=None):
    from . import learning_worker
    stamp=store.stamp(now); ensure_schema()
    event=transition_code_context(now=stamp)
    if event and event['kind']=='context_changed':
        detail=dict(kind='retuning',stage='context_changed',status='waiting',ok=True,
            reason='approved_release_code_context_changed',context_change=event['payload']['context_change'],
            next_due_at=event['payload']['next_due_at'],message='approved release code context changed')
        # The boundary was sealed in the context append's writer transaction.
        # IDs distinguish before/after attempts even on an equal physical clock.
        boundary=event['payload']['context_change']['after_retuning_run_id']
        if isinstance(boundary,bool) or not isinstance(boundary,int) or boundary<0:
            raise ValueError('NBA totals context retuning boundary is invalid')
        db.ex('INSERT INTO nba_totals_stage_runs(started_at,status,detail) SELECT ?,?,? '
              'WHERE NOT EXISTS(SELECT 1 FROM nba_totals_stage_runs WHERE id>? '
              "AND COALESCE(json_extract(detail,'$.kind'),'retuning')!='diagnostics')",
              (stamp,'waiting',store.encoded(detail),boundary))
    jobs={}; due=_next_due(_last_retuning(),stamp)
    diagnostic=db.q1("SELECT * FROM nba_totals_stage_runs WHERE json_extract(detail,'$.kind')='diagnostics' ORDER BY id DESC LIMIT 1")
    today=datetime.fromtimestamp(stamp,store.ET).replace(hour=4,minute=0,second=0,microsecond=0).timestamp()
    diagnostic_due=(today if diagnostic is None or diagnostic['started_at']<today else today+DAY)
    if diagnostic and diagnostic['status']=='error':
        diagnostic_due=diagnostic['started_at']+300
    if stamp>=diagnostic_due:
        jobs['diagnostics_job_id']=learning_worker.enqueue('NBA_TOTAL','diagnostics',now=stamp)['id']
    if stamp>=due:
        jobs['retuning_job_id']=learning_worker.enqueue('NBA_TOTAL','retuning',now=stamp)['id']
    return dict(status='queued' if jobs else 'not_due',next_due_at=min(due,diagnostic_due),**jobs)
