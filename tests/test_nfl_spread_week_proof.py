"""Whole NFL weeks require a full authenticated, label-blind schedule universe."""
import csv
import gzip
import hashlib
import io
import json
from datetime import datetime,timezone

import pytest

from edgeterminal import db,nfl_spread_data as data,nfl_spread_learning as learning
from edgeterminal import nfl_game_sources as sources,nfl_weekly_preparation as weekly
from test_nfl_spread_component import isolated,row,NOW

FIRST='nfl-2026-reg-10-atl-gb';SECOND='nfl-2026-reg-10-jax-ten'
FIELDS=['game_id','season','game_type','week','gameday','gametime','home_team','away_team','home_score','away_score']


def event(second=False):
    return dict(id=SECOND if second else FIRST,sport='NFL',home_team='Tennessee Titans' if second else 'Green Bay Packers',
        away_team='Jacksonville Jaguars' if second else 'Atlanta Falcons',start_time='2026-11-09T17:00:00Z',
        status='STATUS_FINAL',meta=dict(season=2026,week=10,season_type=2))


def published(tmp_path,monkeypatch,*,second=True,received=None):
    monkeypatch.setattr(sources.time,'time',lambda:NOW)
    common=dict(season='2026',game_type='REG',week='10',gameday='2026-11-09',gametime='12:00',home_score='20',away_score='17')
    rows=[dict(common,game_id='2026_10_ATL_GB',home_team='GB',away_team='ATL')]
    if second:rows.append(dict(common,game_id='2026_10_JAX_TEN',home_team='TEN',away_team='JAX'))
    text=io.StringIO(newline='');writer=csv.DictWriter(text,FIELDS);writer.writeheader();writer.writerows(rows)
    raw=gzip.compress(text.getvalue().encode(),mtime=0)
    path=tmp_path/('schedule-'+str(received or NOW)+'.csv.gz');path.write_bytes(raw)
    metadata=path.with_suffix('.json');metadata.write_text(json.dumps(dict(source_url=weekly.SCHEDULES_URL,
        retrieved_at=received or NOW-200,sha256=hashlib.sha256(raw).hexdigest(),license='CC-BY-4.0')))
    weekly._schema()
    result=weekly._adopt_schedule(path,metadata,2026,10,NOW-100)
    assert result['status']=='ready'
    return weekly.read_schedule(2026,NOW,deep=False)


def test_one_known_final_without_any_authenticated_schedule_is_not_a_complete_week():
    db.upsert_event(event())
    assert data.week_universe(2026,10)==[]
    assert not data.complete_week(2026,10,NOW)


def test_authenticated_two_game_week_waits_for_both_stored_events_without_reading_labels(isolated,monkeypatch):
    db.upsert_event(event())
    published(isolated,monkeypatch)
    monkeypatch.setattr(weekly,'_parse_schedule',lambda *a,**kw:pytest.fail('score-aware schedule parser called'))
    monkeypatch.setattr(sources,'_known_finals',lambda *a,**kw:pytest.fail('official target labels read before reservation'))
    monkeypatch.setattr(data,'labels',lambda *a,**kw:pytest.fail('spread labels read before reservation'))
    with data.schedule_proofs():
        assert data.prepare_schedule_proofs([2026],NOW)
        assert not data.complete_week(2026,10,NOW)
        assert data.week_universe(2026,10)==[]
        db.upsert_event(event(True))
        assert data.complete_week(2026,10,NOW)
        assert {e['id'] for e in data.week_universe(2026,10)}=={FIRST,SECOND}
    # Expensive identity proofs have explicit run ownership, never a process-wide
    # cache accidentally reused by a page read or another learning invocation.
    assert not data.complete_week(2026,10,NOW)


def test_authenticated_schedule_raw_tamper_and_extra_event_fail_closed(isolated,monkeypatch):
    db.upsert_event(event());db.upsert_event(event(True))
    saved=published(isolated,monkeypatch)
    with data.schedule_proofs():
        assert data.prepare_schedule_proofs([2026],NOW)
        assert data.complete_week(2026,10,NOW)
        db.upsert_event(dict(event(),id='nfl-2026-reg-10-nyj-ne',home_team='New England Patriots',away_team='New York Jets'))
        with pytest.raises(ValueError,match='schedule|universe'):
            data.week_universe(2026,10)
    path=weekly._source_path(saved['descriptor']['raw_path']);path.write_bytes(b'tampered')
    with data.schedule_proofs(),pytest.raises(ValueError,match='hash'):
        data.prepare_schedule_proofs([2026],NOW)


def test_cheap_evidence_heads_bind_entire_week_and_schedule_files(isolated,monkeypatch):
    db.upsert_event(event());saved=published(isolated,monkeypatch)
    item=dict(game_id=FIRST,season=2026,week=10,kickoff=NOW-3600)
    before=learning._sql_state([item])
    assert before['schedule_heads']
    db.upsert_event(event(True))
    assert learning._sql_state([item])!=before
    after=learning._sql_state([item])
    path=weekly._source_path(saved['descriptor']['raw_path']);path.write_bytes(path.read_bytes()+b'changed')
    assert learning._sql_state([item])!=after


def test_identity_reader_does_not_interpret_poisoned_outcome_columns():
    text=io.StringIO(newline='');writer=csv.DictWriter(text,FIELDS);writer.writeheader()
    writer.writerow(dict(game_id='2026_10_ATL_GB',season='2026',game_type='REG',week='10',gameday='2026-11-09',gametime='12:00',home_team='GB',away_team='ATL',home_score='POISON',away_score='DO_NOT_READ'))
    result=data._schedule_identities(gzip.compress(text.getvalue().encode(),mtime=0),2026)
    assert {r['id'] for r in result[10]}=={FIRST}


def test_missing_schedule_cannot_start_inner_training_or_read_labels(monkeypatch):
    rows=[row(i,week=w,start=NOW-60*86400) for w in range(1,7) for i in range(20)]
    monkeypatch.setattr(data,'observations',lambda **kw:(rows,0))
    monkeypatch.setattr(data,'labels',lambda *a,**kw:pytest.fail('inner labels read without full schedule'))
    monkeypatch.setattr(learning.model,'fit_bank',lambda *a,**kw:pytest.fail('inner training started without full schedule'))
    result=learning.run_fitting(now=NOW)
    assert result['status']=='waiting' and result['detail']['stage']=='fit'
    assert db.q1('SELECT count(*) n FROM nfl_spread_candidates')['n']==0


def test_status_and_cheap_heads_never_decompress_or_write_schedule(isolated,monkeypatch):
    db.upsert_event(event());published(isolated,monkeypatch)
    learning.store.ensure_schema()
    item=dict(game_id=FIRST,season=2026,week=10,kickoff=NOW-3600)
    monkeypatch.setattr(data,'_schedule_identities',lambda *a,**kw:pytest.fail('page/cheap head parsed annual schedule'))
    monkeypatch.setattr(gzip,'GzipFile',lambda *a,**kw:pytest.fail('page/cheap head decompressed annual schedule'))
    sql=[];db.conn().set_trace_callback(sql.append);db.conn().execute('PRAGMA query_only=ON')
    try:
        assert learning.status(now=NOW)['active'] is None
        assert learning._sql_state([item])['schedule_heads'][0]['status']=='ready'
        assert len(data.schedule_paths([2026]))==2
        assert not any(s.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE','ALTER')) for s in sql)
    finally:
        db.conn().execute('PRAGMA query_only=OFF');db.conn().set_trace_callback(None)
