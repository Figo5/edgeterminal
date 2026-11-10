"""Known code releases terminate old contexts without fitting or changing evidence."""
from copy import deepcopy
import hashlib
import json

import pytest

from edgeterminal import db, learning_code_context as context
from edgeterminal import nba_totals_stages as nba, nba_totals_stage_store as nstore
from edgeterminal import nfl_spread_learning as nfl, nfl_spread_store as fstore
from edgeterminal.config import settings

NOW = 1800000000.


@pytest.fixture
def owned(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'context.db'))
    root = tmp_path / 'repo'; (root / 'edgeterminal').mkdir(parents=True)
    code = root / 'edgeterminal/component.py'; code.write_bytes(b'old\n')
    for store in (nstore, fstore):
        monkeypatch.setattr(store, 'ROOT', root)
        monkeypatch.setattr(store, 'CODE_NAMES', ('edgeterminal/component.py',))
    monkeypatch.setattr(nstore, 'RESEARCH_ROOT', tmp_path / 'nba-research')
    monkeypatch.setattr(nba, '_physical_now', lambda requested: float(requested))
    monkeypatch.setattr(nba.store.time, 'time', lambda: NOW)
    nba.ensure_schema(); fstore.ensure_schema()
    yield code
    db._conn.close(); db._conn = None


def authorize(monkeypatch, component, before, after):
    manifest = dict(schema=context.CONTRACT, base_commit=context.BASE_COMMIT,
                    target_commit='1' * 40, approved_patch_sha256='2' * 64,
                    components={component: dict(before=before, after=after)})
    monkeypatch.setattr(context, '_verify_release_commit', lambda _: {'edgeterminal/component.py': b'old\n'})
    return context.register_release(manifest, now=NOW)


def nba_stage(monkeypatch):
    old = nstore.code_catalog()
    payload = dict(contract=nba.bank.CONTRACT, stage='development', bank={'fit': {'dates': ['2026-01-01']}, 'cutoff': NOW - 1},
                   code_sha256=old, code_archive=nstore.archive_code(old), sources=[],
                   coverage=dict(fit=660, development=0, calibration=0))
    monkeypatch.setattr(nba.bank, 'verify_bank', lambda bank: bank)
    event = nba.append_event('sealed-bank', 'fit', payload, now=NOW - 20, expected=None)
    return event, old


def test_nba_known_release_closes_context_without_resealing_bank(owned, monkeypatch):
    first, old = nba_stage(monkeypatch)
    original = deepcopy(first)
    owned.write_bytes(b'new\n'); new = nstore.code_catalog()
    auth = authorize(monkeypatch, 'NBA_TOTAL', old, new)
    changed = nba.transition_code_context(now=NOW)
    assert changed['kind'] == 'context_changed'
    assert changed['payload']['bank'] == original['payload']['bank']
    assert changed['payload']['code_sha256'] == old
    assert changed['payload']['context_change']['authorization_id'] == auth['authorization_id']
    assert changed['payload']['context_change']['after_sha256'] == context.sha(new)
    assert changed['previous_sha256'] == first['sha256']
    old_row = db.q1('SELECT payload,sha256 FROM nba_totals_training_stages WHERE id=?', (first['id'],))
    assert json.loads(old_row['payload']) == original['payload'] and old_row['sha256'] == original['sha256']
    assert nba.transition_code_context(now=NOW + 1)['id'] == changed['id']
    monkeypatch.setattr(nba, '_initial', lambda *a, **k: pytest.fail('context cooldown fitted in refresh'))
    result = nba.run_fitting(now=NOW + 1)
    assert result['status'] == 'waiting' and result['stage'] == 'context_changed'
    assert nba.needs_inputs() is False


def test_nba_unknown_catalog_is_not_approved_and_archive_tamper_blocks_transition(owned, monkeypatch):
    first, old = nba_stage(monkeypatch)
    owned.write_bytes(b'new\n'); new = nstore.code_catalog()
    assert nba.transition_code_context(now=NOW)['id'] == first['id']
    authorize(monkeypatch, 'NBA_TOTAL', old, new)
    relative = next(iter(first['payload']['code_archive'].values()))
    nstore._safe_path(nstore._research(), relative).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='archived code'):
        nba.transition_code_context(now=NOW)
    assert nba.current()['id'] == first['id']


def test_nfl_known_context_transition_preserves_staged_weeks_and_protocol(owned, monkeypatch):
    old = fstore.code_catalog()
    state = dict(stage='development', protocol_sha256=nfl.model.sha(nfl.model.protocol()), code_sha256=old,
                 fit_rows=[], development_rows=[], calibration_rows=[], last_week=[2027, 4], fit_cutoff=NOW - 1)
    first = fstore.append_stage(None, state, now=NOW - 20)
    owned.write_bytes(b'new\n'); new = fstore.code_catalog()
    authorize(monkeypatch, 'NFL_SPREAD', old, new)
    changed = nfl.transition_code_context(now=NOW)
    assert changed['state']['stage'] == 'context_changed'
    assert changed['state']['context_after_week'] == [2027, 4]
    assert changed['state']['code_sha256'] == old
    assert changed['state']['protocol_sha256'] == state['protocol_sha256']
    assert changed['parent_id'] == first['id']
    assert fstore.latest_stage()['id'] == nfl.transition_code_context(now=NOW + 1)['id']
    assert json.loads(db.q1('SELECT state FROM nfl_spread_stages WHERE id=?', (first['id'],))['state']) == state


def test_unknown_nfl_protocol_or_archive_cannot_be_blessed(owned, monkeypatch):
    old = fstore.code_catalog()
    state = dict(stage='fit', protocol_sha256='0' * 64, code_sha256=old,
                 fit_rows=[], development_rows=[], calibration_rows=[])
    first = fstore.append_stage(None, state, now=NOW - 20)
    owned.write_bytes(b'new\n'); new = fstore.code_catalog()
    authorize(monkeypatch, 'NFL_SPREAD', old, new)
    with pytest.raises(ValueError, match='protocol'):
        nfl.transition_code_context(now=NOW)
    assert fstore.latest_stage()['id'] == first['id']


def test_nba_transition_checks_bank_before_writing_a_terminal_event(owned, monkeypatch):
    first, old = nba_stage(monkeypatch)
    owned.write_bytes(b'new\n'); new = nstore.code_catalog()
    authorize(monkeypatch, 'NBA_TOTAL', old, new)
    monkeypatch.setattr(nba.bank, 'verify_bank', lambda _: (_ for _ in ()).throw(ValueError('bank seal changed')))
    with pytest.raises(ValueError, match='bank seal'):
        nba.transition_code_context(now=NOW)
    assert nba.current()['id'] == first['id']


def test_nba_authorization_cannot_bypass_old_source_byte_verification(owned, monkeypatch):
    first, old = nba_stage(monkeypatch)
    db.ex('CREATE TABLE IF NOT EXISTS nba_source_artifacts(id INTEGER PRIMARY KEY,sha256 TEXT)')
    db.ex('INSERT INTO nba_source_artifacts(id,sha256) VALUES(?,?)', (1, 'a' * 64))
    payload = dict(first['payload'], sources=[dict(id=1, sha256='a' * 64)])
    current = nba.append_event(first['chain_id'], 'development', payload, now=NOW - 10, expected=first['sha256'])
    owned.write_bytes(b'new\n'); new = nstore.code_catalog()
    authorize(monkeypatch, 'NBA_TOTAL', old, new)
    monkeypatch.setattr(nba.data, '_source', lambda *a: (_ for _ in ()).throw(ValueError('source bytes changed')))
    with pytest.raises(ValueError, match='source bytes changed'):
        nba.transition_code_context(now=NOW)
    assert nba.current()['id'] == current['id']


def test_known_nfl_context_still_verifies_old_source_and_result_evidence(owned, monkeypatch):
    old = fstore.code_catalog()
    state = dict(stage='development', protocol_sha256=nfl.model.sha(nfl.model.protocol()), code_sha256=old,
                 fit_rows=[], development_rows=[], calibration_rows=[])
    first = fstore.append_stage(None, state, now=NOW - 20)
    owned.write_bytes(b'new\n'); new = fstore.code_catalog()
    authorize(monkeypatch, 'NFL_SPREAD', old, new)
    monkeypatch.setattr(nfl, '_verify_stage_sources', lambda _: (_ for _ in ()).throw(ValueError('source changed')))
    with pytest.raises(ValueError, match='source changed'):
        nfl.transition_code_context(now=NOW)
    assert fstore.latest_stage()['id'] == first['id']


def test_context_transition_cannot_hide_a_broken_nfl_parent_chain(owned, monkeypatch):
    old = fstore.code_catalog()
    state = dict(stage='fit', protocol_sha256=nfl.model.sha(nfl.model.protocol()), code_sha256=old,
                 fit_rows=[], development_rows=[], calibration_rows=[])
    first = fstore.append_stage(None, state, now=NOW - 20)
    # Simulate a stored predecessor corruption, not an authorized application write.
    db.ex('DROP TRIGGER nfl_spread_stages_no_update')
    db.ex('UPDATE nfl_spread_stages SET parent_id=99 WHERE id=?', (first['id'],))
    owned.write_bytes(b'new\n'); new = fstore.code_catalog()
    authorize(monkeypatch, 'NFL_SPREAD', old, new)
    with pytest.raises(ValueError, match='parent chain'):
        nfl.transition_code_context(now=NOW)
    assert fstore.latest_stage()['id'] == first['id']


def test_terminal_nfl_context_rechecks_the_predecessor_chain_on_repeated_capture(owned, monkeypatch):
    old=fstore.code_catalog()
    first=fstore.append_stage(None,dict(stage='fit',protocol_sha256=nfl.model.sha(nfl.model.protocol()),
        code_sha256=old,fit_rows=[],development_rows=[],calibration_rows=[]),now=NOW-20)
    owned.write_bytes(b'new\n'); new=fstore.code_catalog()
    authorize(monkeypatch,'NFL_SPREAD',old,new)
    changed=nfl.transition_code_context(now=NOW)
    assert changed['state']['stage']=='context_changed'
    db.ex('DROP TRIGGER nfl_spread_stages_no_update')
    db.ex('UPDATE nfl_spread_stages SET parent_id=99 WHERE id=?',(first['id'],))
    with pytest.raises(ValueError,match='parent chain'):
        nfl.transition_code_context(now=NOW+1)


def test_transition_preserves_reserved_nba_date_boundary(owned, monkeypatch):
    first, old = nba_stage(monkeypatch)
    reservation = dict(inner_stage_reservation=True, chain_id=first['chain_id'], phase='development',
                       game_ids=['g'], date_ids=['2026-02-02'], universe_game_ids_by_date={'2026-02-02': ['g']}, games=[])
    ident = nstore.sha(reservation)
    db.ex('INSERT INTO nba_totals_inner_reservations VALUES(?,?,?,?,?,?)',
          (ident, first['chain_id'], 'development', NOW - 5, nstore.encoded(reservation), ident))
    original = db.q('SELECT * FROM nba_totals_inner_reservations')
    owned.write_bytes(b'new\n'); new = nstore.code_catalog()
    authorize(monkeypatch, 'NBA_TOTAL', old, new)
    changed = nba.transition_code_context(now=NOW)
    assert changed['payload']['context_after_day'] == '2026-02-02'
    assert db.q('SELECT * FROM nba_totals_inner_reservations') == original
    stamp=NOW+nba.WEEK+1
    monkeypatch.setattr(db,'now',lambda:stamp)
    monkeypatch.setattr(nba.store.time,'time',lambda:stamp)
    calls=[]
    def initial(*args,**kwargs):
        assert kwargs['after_day']=='2026-02-02'
        calls.append(kwargs['after_day'])
        return None,0
    monkeypatch.setattr(nba,'_initial',initial)
    result = nba.run_fitting(now=stamp)
    assert result['status'] == 'waiting'
    assert calls==['2026-02-02']


@pytest.mark.parametrize('component', ['NBA_TOTAL', 'NFL_SPREAD'])
def test_later_unknown_code_cannot_piggyback_on_a_past_release_authorization(owned, monkeypatch, component):
    if component == 'NBA_TOTAL':
        first, old = nba_stage(monkeypatch)
        transition = nba.transition_code_context; store = nstore
    else:
        old = fstore.code_catalog()
        fstore.append_stage(None, dict(stage='fit', protocol_sha256=nfl.model.sha(nfl.model.protocol()),
            code_sha256=old, fit_rows=[], development_rows=[], calibration_rows=[]), now=NOW - 20)
        transition = nfl.transition_code_context; store = fstore
    owned.write_bytes(b'new\n'); new = store.code_catalog()
    authorize(monkeypatch, component, old, new)
    transition(now=NOW)
    owned.write_bytes(b'another unreviewed change\n')
    with pytest.raises(ValueError, match='after transition'):
        transition(now=NOW + 1)


def test_nfl_successor_preserves_old_week_boundary_across_repeated_waiting_fits(owned, monkeypatch):
    old=fstore.code_catalog()
    state=dict(stage='fit',protocol_sha256=nfl.model.sha(nfl.model.protocol()),code_sha256=old,
               fit_rows=[],development_rows=[],calibration_rows=[],last_week=[2027,4])
    fstore.append_stage(None,state,now=NOW-20)
    owned.write_bytes(b'new\n'); new=fstore.code_catalog()
    authorize(monkeypatch,'NFL_SPREAD',old,new)
    nfl.transition_code_context(now=NOW)
    rows=[dict(game_id='old',season=2027,week=4,as_of=NOW-200,
               formula_version=nfl.model.FORMULA_VERSION,input_policy_id='fixture'),
          dict(game_id='new',season=2027,week=5,as_of=NOW-100,
               formula_version=nfl.model.FORMULA_VERSION,input_policy_id='fixture')]
    monkeypatch.setattr(nfl,'_verify_stage_sources',lambda state:None)
    monkeypatch.setattr(nfl.data,'observations',lambda **kw:(rows,0))
    monkeypatch.setattr(nfl,'_groups',lambda rs,stamp:{(r['season'],r['week']):[r] for r in rs})
    monkeypatch.setattr(nfl.data,'labels',lambda rs,**kw:{r['game_id']:{} for r in rs})
    first=nfl.run_fitting(now=NOW+nfl.WEEK+1)
    second=nfl.run_fitting(now=NOW+nfl.WEEK+2)
    assert first['status']==second['status']=='waiting'
    for result in (first,second):
        assert result['detail']['context_after_week']==[2027,4]
        assert [r['game_id'] for r in result['detail']['fit_rows']]==['new']


def test_nba_context_marker_cannot_overwrite_a_post_cooldown_waiting_fit(owned, monkeypatch):
    from edgeterminal import learning_worker
    first,old=nba_stage(monkeypatch)
    owned.write_bytes(b'new\n'); new=nstore.code_catalog()
    authorize(monkeypatch,'NBA_TOTAL',old,new)
    nba.transition_code_context(now=NOW)
    stamp=NOW+nba.WEEK+1
    monkeypatch.setattr(db,'now',lambda:stamp+1)
    monkeypatch.setattr(nba.store.time,'time',lambda:stamp+1)
    called=[]
    monkeypatch.setattr(nba,'_initial',lambda *a,**kw:called.append(kw['after_day']) or (None,0))
    result=nba.run_fitting(now=stamp)
    assert called==['2026-01-01'] and result['status']=='waiting'
    before=nba._last_retuning()
    assert nba._next_due(before,stamp+1)==stamp+nba.DAY
    queued=[]
    monkeypatch.setattr(learning_worker,'enqueue',lambda component,kind,**kw:queued.append(kind) or {'id':999})
    nba.refresh_inputs(now=stamp+1)
    assert 'retuning' not in queued
    assert nba._last_retuning()==before


def test_nba_same_clock_post_transition_retry_is_not_replaced_by_context_marker(owned, monkeypatch):
    from edgeterminal import learning_worker
    first,old=nba_stage(monkeypatch)
    owned.write_bytes(b'new\n'); new=nstore.code_catalog()
    authorize(monkeypatch,'NBA_TOTAL',old,new)
    nba.transition_code_context(now=NOW)
    monkeypatch.setattr(db,'now',lambda:NOW)
    nba.store.record_run(NOW,'waiting',dict(kind='retuning',reason='normal retry',next_due_at=NOW+2*nba.WEEK))
    before=nba._last_retuning()
    monkeypatch.setattr(learning_worker,'enqueue',lambda *a,**kw:{'id':999})
    nba.refresh_inputs(now=NOW)
    assert nba._last_retuning()==before
    assert nba._next_due(before,NOW)==NOW+2*nba.WEEK


@pytest.mark.parametrize('same_clock',[False,True])
def test_nfl_context_marker_preserves_post_transition_pending_retry(owned, monkeypatch, same_clock):
    from edgeterminal import learning_worker
    old=fstore.code_catalog()
    state=dict(stage='fit',protocol_sha256=nfl.model.sha(nfl.model.protocol()),code_sha256=old,
               fit_rows=[],development_rows=[],calibration_rows=[])
    fstore.append_stage(None,state,now=NOW-20)
    owned.write_bytes(b'new\n'); new=fstore.code_catalog()
    authorize(monkeypatch,'NFL_SPREAD',old,new)
    nfl.transition_code_context(now=NOW)
    stamp=NOW if same_clock else NOW+nfl.WEEK+1
    monkeypatch.setattr(nfl.time,'time',lambda:stamp)
    monkeypatch.setattr(db,'now',lambda:stamp)
    monkeypatch.setattr(nfl,'_integrity',lambda stamp:'pending')
    assert nfl.run_fitting(now=stamp)['status']=='waiting'
    before=db.q1('SELECT * FROM nfl_spread_runs ORDER BY id DESC LIMIT 1')
    assert json.loads(before['detail'])['next_at']==stamp+300
    queued=[]
    monkeypatch.setattr(learning_worker,'enqueue',lambda *a,**kw:queued.append(kw) or {'id':999,'state':'queued'})
    refreshed=nfl.refresh_checks(now=stamp)
    assert not queued
    assert refreshed['next_at']==stamp+300
    assert db.q1('SELECT * FROM nfl_spread_runs ORDER BY id DESC LIMIT 1')==before


@pytest.mark.parametrize('component',['NBA_TOTAL','NFL_SPREAD'])
def test_same_clock_pretransition_retry_cannot_bypass_weekly_cooldown(owned, monkeypatch, component):
    from edgeterminal import learning_worker
    if component=='NBA_TOTAL':
        first,old=nba_stage(monkeypatch)
        nba.store.record_run(NOW,'waiting',dict(kind='retuning',next_due_at=NOW+nba.DAY))
        transition=nba.transition_code_context; refresh=nba.refresh_inputs; catalog=nstore.code_catalog
    else:
        old=fstore.code_catalog()
        fstore.append_stage(None,dict(stage='fit',protocol_sha256=nfl.model.sha(nfl.model.protocol()),
            code_sha256=old,fit_rows=[],development_rows=[],calibration_rows=[]),now=NOW-20)
        nfl._record(NOW,'waiting',dict(next_at=NOW+nfl.DAY))
        transition=nfl.transition_code_context; refresh=nfl.refresh_checks; catalog=fstore.code_catalog
    owned.write_bytes(b'new\n'); new=catalog()
    authorize(monkeypatch,component,old,new)
    transition(now=NOW)
    stamp=NOW+2*nfl.DAY
    monkeypatch.setattr(db,'now',lambda:stamp)
    monkeypatch.setattr(nfl.time,'time',lambda:stamp)
    calls=[]
    monkeypatch.setattr(learning_worker,'enqueue',lambda component,kind,**kw:calls.append(kind) or {'id':999,'state':'queued'})
    refresh(now=stamp)
    assert 'retuning' not in calls


@pytest.mark.parametrize('component',['NBA_TOTAL','NFL_SPREAD'])
def test_context_run_boundary_is_sealed_and_marker_is_idempotent(owned, monkeypatch, component):
    from edgeterminal import learning_worker
    if component=='NBA_TOTAL':
        first,old=nba_stage(monkeypatch)
        nba.store.record_run(NOW,'waiting',dict(kind='retuning',next_due_at=NOW+nba.DAY))
        prior=nba._last_retuning()['id']
        transition=nba.transition_code_context; refresh=nba.refresh_inputs; catalog=nstore.code_catalog
    else:
        old=fstore.code_catalog()
        fstore.append_stage(None,dict(stage='fit',protocol_sha256=nfl.model.sha(nfl.model.protocol()),
            code_sha256=old,fit_rows=[],development_rows=[],calibration_rows=[]),now=NOW-20)
        nfl._record(NOW,'waiting',dict(next_at=NOW+nfl.DAY))
        prior=db.q1('SELECT MAX(id) id FROM nfl_spread_runs')['id']
        transition=nfl.transition_code_context; refresh=nfl.refresh_checks; catalog=fstore.code_catalog
    owned.write_bytes(b'new\n'); new=catalog()
    authorize(monkeypatch,component,old,new)
    changed=transition(now=NOW)
    payload=changed['payload'] if component=='NBA_TOTAL' else changed['state']
    assert payload['context_change']['after_retuning_run_id']==prior
    if component=='NBA_TOTAL':
        nba.store.record_run(NOW,'retained',dict(kind='diagnostics',reason='no training'))
    monkeypatch.setattr(learning_worker,'enqueue',lambda *a,**kw:{'id':999,'state':'queued'})
    refresh(now=NOW)
    if component=='NBA_TOTAL':
        marker=nba._last_retuning()
        # A diagnostic is not an offline retuning attempt and cannot alter its marker.
        nba.store.record_run(NOW,'retained',dict(kind='diagnostics',reason='no training'))
    else:marker=db.q1('SELECT * FROM nfl_spread_runs ORDER BY id DESC LIMIT 1')
    refresh(now=NOW)
    current=nba._last_retuning() if component=='NBA_TOTAL' else db.q1('SELECT * FROM nfl_spread_runs ORDER BY id DESC LIMIT 1')
    assert current==marker and marker['id']>prior
