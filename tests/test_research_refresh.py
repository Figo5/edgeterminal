"""Refresh cadence and quota guards without external requests."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from edgeterminal import db, research_refresh, research_scheduler
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    # Refresh writes derived reports relative to the working directory.
    monkeypatch.chdir(tmp_path)
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'refresh.db'}")
    monkeypatch.setattr(research_refresh, "LOCK", tmp_path / "refresh.lock")
    db.init()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_idle_refresh_is_idempotent_and_does_not_call_paid_provider(isolated, monkeypatch):
    calls = {"schedule": 0, "projections": 0}
    def schedule(_sport):
        calls["schedule"] += 1
        return {"ok": True, "events": 0}
    monkeypatch.setattr(research_refresh.engine, "refresh_schedule", schedule)
    def projections():
        calls["projections"] += 1
        return {"ok": True, "needed": 0, "created": 0, "missing": []}
    monkeypatch.setattr(research_refresh.engine, "ensure_weekly_projections", projections)
    monkeypatch.setattr(research_refresh, "_events", lambda _now: [])
    monkeypatch.setattr(research_refresh, "_availability", lambda *_args: {"games": 0, "observations": 0})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, "capture",
                        lambda *_args, **_kwargs: pytest.fail("paid provider called"))
    first = research_refresh.run(now=1000)
    second = research_refresh.run(now=1001)
    assert first["success"] and second["success"]
    assert calls == {"schedule": 1, "projections": 1}
    assert second["status"] == "no_work_due"
    assert db.q1("SELECT COUNT(*) n FROM research_refresh_runs")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM research_refresh_ticks")["n"] == 2
    assert db.q1("SELECT COUNT(*) n FROM paper_accounts")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM paper_positions")["n"] == 0
    assert research_refresh.status(now=1001)["last_successful_research_refresh"] is not None


@pytest.mark.parametrize('name', ['top_five_grading', 'challenger_grading', 'game_grading'])
def test_result_checks_and_source_failures_retry_after_five_minutes(isolated, name):
    def save(stamp, ok, error=''):
        db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) '
              'VALUES(?,?,?,?,?)', (str(stamp), name, stamp, int(ok), json.dumps({'error': error})))
    save(1000., False, 'ValueError: freeze: ConnectError')
    assert not research_refresh._result_due(name, 1299.)
    assert research_refresh._result_due(name, 1300.)
    save(1900., True)
    assert not research_refresh._result_due(name, 2199.)
    assert research_refresh._result_due(name, 2200.)
    save(24000., False, 'ValueError: source identity mismatch')
    assert not research_refresh._result_due(name, 24299.)
    assert research_refresh._result_due(name, 24300.)


@pytest.mark.parametrize('name,cadence', [('schedule', 7200.), ('availability', 21600.)])
def test_failed_public_sources_retry_after_five_minutes_then_resume_normal_cadence(isolated, name, cadence):
    def save(stamp, success):
        db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) VALUES(?,?,?,?,?)',
              (str(stamp), name, stamp, int(success), json.dumps({'ok': success})))
    save(1000., False)
    assert not research_refresh._due(name, 1299., cadence)
    assert research_refresh._due(name, 1300., cadence)
    save(1300., False)
    assert not research_refresh._due(name, 1599., cadence)
    assert research_refresh._due(name, 1600., cadence)
    save(1600., True)
    assert not research_refresh._due(name, 1900., cadence)
    assert not research_refresh._due(name, 1600. + cadence - 1, cadence)
    assert research_refresh._due(name, 1600. + cadence, cadence)


@pytest.mark.parametrize('name', ['top_five_capture', 'current_game_odds', 'weekly_freeze_2026_07'])
def test_other_failed_steps_keep_their_existing_cadence(isolated, name):
    db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) VALUES(?,?,?,?,?)',
          ('failure', name, 1000., 0, '{"ok":false}'))
    assert not research_refresh._due(name, 1300., 3600.)
    assert research_refresh._due(name, 4600., 3600.)


def test_worker_automatically_recovers_schedule_and_availability_without_paid_requests(isolated, monkeypatch):
    event = {'id': 'nfl-2026-reg-07-dal-phi', 'kickoff_utc': 100000.,
             'start_time': '1970-01-02T03:46:40Z', 'meta': {}}
    monkeypatch.setattr(research_refresh, '_events', lambda now: [event])
    calls = {'schedule': 0, 'availability': 0}
    def schedule(sport):
        calls['schedule'] += 1
        return {'ok': calls['schedule'] > 1, 'events': 1}
    def availability(*args):
        calls['availability'] += 1
        if calls['availability'] == 1:
            raise ValueError('timestamped live ESPN availability source unavailable')
        return {'games': 1, 'observations': 0}
    monkeypatch.setattr(research_refresh.engine, 'refresh_schedule', schedule)
    monkeypatch.setattr(research_refresh.engine, 'ensure_weekly_projections', lambda: {'ok': True})
    monkeypatch.setattr(research_refresh, '_availability', availability)
    monkeypatch.setattr(research_refresh.nfl_top_five, 'capture_missing', lambda *args, **kwargs: {})
    monkeypatch.setattr(research_refresh.nfl_top_five, 'grade_due', lambda now: {})
    monkeypatch.setattr(research_refresh, '_weekly', lambda *args: {'freeze': 'already_frozen'})
    monkeypatch.setattr(research_refresh.nfl_market_capture, 'capture_week', lambda *args, **kwargs: {})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'plan', lambda *args, **kwargs: {'due': {}})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'capture',
                        lambda *args, **kwargs: pytest.fail('paid source requested'))
    first = research_refresh.run(now=1000.)
    assert not first['steps']['schedule']['ok'] and not first['steps']['availability']['ok']
    research_refresh.run(now=1299.)
    assert calls == {'schedule': 1, 'availability': 1}
    recovered = research_refresh.run(now=1300.)
    assert recovered['steps']['schedule']['ok'] and recovered['steps']['availability']['ok']
    research_refresh.run(now=1600.)
    assert calls == {'schedule': 2, 'availability': 2}


def test_game_grading_keeps_connection_cause_for_retry_policy(monkeypatch):
    monkeypatch.setattr(research_refresh.nfl_forward_grading, 'sync_all',
                        lambda **k: {'sync': [{'error': 'ProviderError: ESPN: ConnectTimeout'}]})
    with pytest.raises(ValueError, match='ConnectTimeout'):
        research_refresh._game_grading(1000.)


@pytest.fixture
def pairing_worker(isolated, monkeypatch):
    game = 'nfl-2026-reg-07-atl-gb'
    clock = [1005.]
    monkeypatch.setattr(research_refresh.time, 'time', lambda: clock[0])
    monkeypatch.setattr(research_refresh, '_events', lambda now: [])
    monkeypatch.setattr(research_refresh, '_due', lambda *args: False)
    monkeypatch.setattr(research_refresh.nfl_top_five, 'grade_due', lambda now: {})
    monkeypatch.setattr(research_refresh.research_scheduler, 'status', lambda: {'loaded': True})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'health',
                        lambda *args: {'warnings': [], 'upcoming_capture_windows': []})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'plan', lambda *args, **kwargs: {'due': {}})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'capture',
                        lambda *args: pytest.fail('retry requested new paid quotes'))
    policy = {**research_refresh.paper_research.example_policy(), 'version': 'pairing-fixture',
              'enabled': True, 'min_model_line_difference': 0., 'min_probability_difference': 0.}
    monkeypatch.setattr(research_refresh.paper_research, 'active_policy', lambda: policy)
    db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
          ('original', 'sportsgameodds', 100., 'fixture', '{}', 1, 1, 1, '{}'))
    quote = dict(quote_id=1, canonical_game_id=game, player_id='sgo:a', player_name='A',
                 market='receptions', side='over', line=2.5, price=-110, sportsbook='fanduel',
                 observed_at=100., kickoff_utc=1200., model_version='fixture-v1',
                 projection=2.5, uncertainty_sigma=2., probability_over=.5, probability_under=.5)
    payload = json.dumps({'metadata': {'created_at': 101., 'model_hash': 'fixture-model'},
                          'projections': [quote]}, sort_keys=True)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    db.ex('INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)', (sha, 'original', 101., payload))

    def batch(receipt=1005.):
        db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
              ('captured', 'sportsgameodds', receipt, 'fixture', '{}', 1, 1, 1, '{}'))
        db.ex('INSERT INTO nfl_prop_snapshots(id,batch_id,canonical_game_id,provider_event_id,player_id,'
              'provider_player_id,player_name,team,opponent,market,line,side,price,sportsbook,retrieved_at,'
              'kickoff_utc,provider,source,provider_updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (2, 'captured', game, 'fixture-event', 'sgo:a', 'a', 'A', 'ATL', 'GB', 'receptions',
               1.5, 'over', -110, 'fanduel', receipt, 1200., 'sportsgameodds', 'fixture', receipt))
        return {'batch_id': 'captured'}

    def failed_pair(capture_detail=None):
        source = capture_detail if capture_detail is not None else {'ok': True, 'result': {'batch_id': 'captured'}}
        db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) VALUES(?,?,?,?,?)',
              ('origin', 'prop_near_game_2026_07', 1000., 1, json.dumps(source)))
        db.ex('INSERT INTO research_refresh_steps(run_id,name,attempted_at,success,detail) VALUES(?,?,?,?,?)',
              ('origin', 'prop_pair_2026_07', 1000., 0,
               json.dumps({'ok': False, 'error': 'ValueError: prop batch is missing or not yet observed'})))

    return clock, batch, failed_pair, payload


def test_prop_pairing_uses_observed_capture_receipt_after_worker_start(pairing_worker, monkeypatch):
    _, batch, _, frozen = pairing_worker
    due = {(2026, 7): ['nfl-2026-reg-07-atl-gb']}
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'plan', lambda *args, **kwargs: {'due': due})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, 'capture', lambda *args: batch())
    result = research_refresh.run(now=1000.)
    assert result['steps']['prop_pair_2026_07']['ok'], result['steps']
    assert result['steps']['prop_pair_2026_07']['result']['paired'] == 1
    assert db.q1('SELECT created_at FROM paper_prop_quote_pairings')['created_at'] == 1005.
    assert db.q1('SELECT payload FROM nfl_prop_research_freezes')['payload'] == frozen
    assert result['readiness'] == 'RESEARCH_ONLY' and result['designation'] == 'PASS'


def test_failed_pair_retries_saved_capture_after_five_minutes_and_clears_health(pairing_worker):
    clock, batch, failed_pair, frozen = pairing_worker
    batch(); failed_pair()
    assert research_refresh.status(now=1299.)['errors'][0]['step'] == 'prop_pair_2026_07'
    clock[0] = 1299.
    assert 'prop_pair_2026_07' not in research_refresh.run(now=1299.)['steps']
    clock[0] = 1300.
    result = research_refresh.run(now=1300.)
    assert result['steps']['prop_pair_2026_07']['result']['paired'] == 1
    assert research_refresh.status(now=1300.)['errors'] == []
    assert db.q1('SELECT payload FROM nfl_prop_research_freezes')['payload'] == frozen
    assert db.q1('SELECT COUNT(*) n FROM nfl_prop_capture_batches')['n'] == 2
    assert db.q1('SELECT COUNT(*) n FROM nfl_top_five_selections')['n'] == 0
    assert db.q1('SELECT COUNT(*) n FROM paper_positions')['n'] == 0
    clock[0] = 1600.
    assert 'prop_pair_2026_07' not in research_refresh.run(now=1600.)['steps']


def test_pair_retry_keeps_original_capture_provenance_across_repeated_failures(pairing_worker, monkeypatch):
    clock, batch, failed_pair, _ = pairing_worker
    batch(); failed_pair()
    original_pair = research_refresh.paper_research.pair_batch
    calls = []
    def intermittent(batch_id, *, now):
        calls.append((batch_id, now))
        if len(calls) < 3:
            raise ValueError('fixture pairing temporarily unavailable')
        return original_pair(batch_id, now=now)
    monkeypatch.setattr(research_refresh.paper_research, 'pair_batch', intermittent)
    for stamp in (1300., 1600., 1900.):
        clock[0] = stamp
        outcome = research_refresh.run(now=stamp)['steps']['prop_pair_2026_07']
        assert outcome['ok'] is (stamp == 1900.)
    assert calls == [('captured', 1300.), ('captured', 1600.), ('captured', 1900.)]
    assert research_refresh.status(now=1900.)['errors'] == []
    assert db.q1('SELECT COUNT(*) n FROM paper_prop_quote_pairings')['n'] == 1


def test_pair_retry_does_not_advance_past_the_actual_processing_clock(pairing_worker):
    clock, batch, failed_pair, _ = pairing_worker
    batch(receipt=5000.); failed_pair()
    clock[0] = 1300.
    result = research_refresh.run(now=1300.)
    step = result['steps']['prop_pair_2026_07']
    assert not step['ok'] and 'not yet observed' in step['error']
    assert research_refresh.status(now=1300.)['errors'][0]['step'] == 'prop_pair_2026_07'
    assert db.q1('SELECT COUNT(*) n FROM paper_prop_quote_pairings')['n'] == 0


@pytest.mark.parametrize('source', [{}, {'ok': True, 'result': {'batch_id': 'missing'}}])
def test_pair_retry_keeps_missing_capture_evidence_unresolved(pairing_worker, source):
    clock, batch, failed_pair, _ = pairing_worker
    batch(); failed_pair(capture_detail=source)
    clock[0] = 1300.
    research_refresh.run(now=1300.)
    assert research_refresh.status(now=1300.)['errors'][0]['step'] == 'prop_pair_2026_07'
    assert db.q1('SELECT COUNT(*) n FROM paper_prop_quote_pairings')['n'] == 0


def test_prop_capture_waits_for_frozen_projection(isolated, monkeypatch):
    event = {"id": "nfl-2026-reg-04-atl-gb", "kickoff_utc": 100000.0,
             "start_time": "1970-01-02T03:46:40Z", "meta": {}}
    monkeypatch.setattr(research_refresh, "_events", lambda _now: [event])
    monkeypatch.setattr(research_refresh, "_availability", lambda *_args: {"games": 1, "observations": 0})
    monkeypatch.setattr(research_refresh.engine, "refresh_schedule", lambda _sport: {"ok": True})
    monkeypatch.setattr(settings, "sportsgameodds_api_key", "fixture-key")
    calls = []
    def fail_capture(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("provider called without frozen projection")
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, "capture", fail_capture)
    monkeypatch.setattr(research_refresh.nfl_forward_weekly, "run", lambda **_kwargs: {"freeze": "fixture"})
    monkeypatch.setattr(research_refresh.nfl_market_capture, "capture_week",
                        lambda *_args, **_kwargs: {"target_games": 1, "rows_stored": 2})
    game_odds_calls = []
    monkeypatch.setattr(research_refresh.nfl_market_capture, "capture_current",
                        lambda *, now: game_odds_calls.append(now) or
                        {"target_games": 1, "rows_stored": 2})
    first = research_refresh.run(now=20000)
    second = research_refresh.run(now=20001)
    assert first["success"] and second["success"]
    assert calls == []
    assert game_odds_calls == [20000]
    assert research_refresh.interval(99900, [event]) == 300
    assert research_refresh._game_odds_interval(99900, [event]) == 300
    assert research_refresh._game_odds_interval(20000, [event]) == 900


def test_concurrent_read_endpoints_share_sqlite_safely(isolated):
    with ThreadPoolExecutor(max_workers=4) as workers:
        outputs = list(workers.map(lambda _: (research_refresh.status(now=1000),
                                               db.q1("SELECT COUNT(*) AS n FROM events")),
                                   range(80)))
    assert all(status["designation"] == "PASS" and row["n"] == 0
               for status, row in outputs)


def test_worker_gap_warning_requires_a_recorded_tick(isolated, monkeypatch):
    monkeypatch.setattr(research_refresh, "_events", lambda _now: [])
    monkeypatch.setattr(research_refresh.research_scheduler, "status",
                        lambda: {"loaded": True, "installed": True})
    monkeypatch.setattr(research_refresh.nfl_prop_capture_ops, "health",
                        lambda *_args, **_kwargs: {"warnings": [],
                            "upcoming_capture_windows": [{"game_id": "fixture"}]})
    first = research_refresh.status(now=1000)
    assert first["operations_warnings"] == ["worker_tick_history_not_yet_available"]
    db.ex("INSERT INTO research_refresh_ticks VALUES(?,?,?,?)", ("tick", 1000, 1001, 1))
    assert research_refresh.status(now=1300)["operations_warnings"] == []
    late = research_refresh.status(now=1800)
    assert any("sleep" in warning for warning in late["operations_warnings"])


def test_launchagent_runs_background_worker_without_terminal():
    spec = research_scheduler.specification()
    assert spec["ProgramArguments"][1:] == ["-m", "edgeterminal.research_refresh", "run"]
    assert spec["RunAtLoad"] is True and spec["StartInterval"] == 300
    assert spec["WorkingDirectory"].endswith("/edgeterminal")
    assert spec["StandardErrorPath"].endswith("research-refresh.err.log")
