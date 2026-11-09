"""Weekly legacy research is prepared offline; network work stays in source child."""
import csv
import gzip
import hashlib
import io
import json
import os
import fcntl
from contextlib import contextmanager
import threading
from pathlib import Path
import numpy as np

import pytest

from edgeterminal import db, learning_worker, nfl_game_sources as sources, nfl_weekly_preparation as weekly
from edgeterminal.config import settings
from test_nfl_game_sources import NOW, EVENT, archive, game, Client, Response

SCHEDULE_FIELDS = ['game_id', 'season', 'game_type', 'week', 'gameday', 'gametime',
                   'home_team', 'away_team', 'home_score', 'away_score', 'home_rest', 'away_rest',
                   'location', 'spread_line', 'total_line', 'home_spread_odds', 'away_spread_odds',
                   'home_moneyline', 'away_moneyline', 'over_odds', 'under_odds']


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(settings, 'nflverse_cache_dir', tmp_path / 'nflverse/pbp')
    monkeypatch.setattr(weekly, 'OUTPUT_DIRECTORY', tmp_path / 'reports')
    monkeypatch.setattr(sources.time, 'time', lambda: NOW)
    db.init()
    db.upsert_event(dict(EVENT, start_time='2026-11-09T20:00:00Z'))
    db.upsert_event(dict(EVENT, id='nfl-2026-reg-08-buf-min', start_time='2026-11-01T18:00:00Z',
                         status='STATUS_FINAL', meta=dict(season=2026, season_type=2, week=8)))
    yield tmp_path
    db._conn.close(); db._conn = None


def schedule_rows():
    common = dict(season='2026', game_type='REG', home_team='MIN', away_team='BUF',
                  home_rest='7', away_rest='7', location='Home')
    return [dict(common, game_id='2026_08_BUF_MIN', week='8', gameday='2026-11-01', gametime='13:00',
                 home_score='21', away_score='24'),
            dict(common, game_id='2026_09_BUF_MIN', week='9', gameday='2026-11-09', gametime='15:00',
                 home_score='', away_score='')]


def schedule_archive(tmp_path, rows=None, received=NOW - 1000):
    stream = io.StringIO(newline=''); writer = csv.DictWriter(stream, SCHEDULE_FIELDS)
    writer.writeheader(); writer.writerows(schedule_rows() if rows is None else rows)
    raw = gzip.compress(stream.getvalue().encode(), mtime=0)
    directory = settings.nflverse_cache_dir.parent / 'forward_2026'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'games.csv.gz'; path.write_bytes(raw)
    meta = dict(source_url=weekly.SCHEDULES_URL, retrieved_at=received,
                sha256=hashlib.sha256(raw).hexdigest(), license='CC-BY-4.0')
    (directory / 'games.metadata.json').write_text(json.dumps(meta))
    return raw


def test_dispatch_only_queues_offline_component_never_fits_or_fetches(fixture, monkeypatch):
    monkeypatch.setattr(weekly, '_freeze', lambda *a: pytest.fail('dispatch never fits'))
    monkeypatch.setattr(sources, '_download', lambda *a, **k: pytest.fail('dispatch never downloads'))
    result = weekly.dispatch(2026, now=NOW)
    assert result['status'] == 'queued'
    job = db.q1('SELECT * FROM learning_jobs WHERE id=?', (result['job_id'],))
    assert job['component'] == 'NFL_WEEKLY' and job['kind'] == 'retuning'
    assert json.loads(job['detail'])['week'] == 9


def test_past_first_kickoff_does_not_retroactively_choose_remaining_game(fixture):
    db.upsert_event(dict(EVENT, id='nfl-2026-reg-09-nyj-ne', home_team='New England Patriots',
                         away_team='New York Jets', start_time='2026-11-06T01:15:00Z',
                         status='STATUS_FINAL'))
    result = weekly.dispatch(2026, now=NOW)
    assert result['status'] == 'waiting' and result['reason'] == 'no_whole_week_before_first_kickoff'


def test_prepared_schedule_is_immutable_verified_and_original_cache_unchanged(fixture):
    body = schedule_archive(fixture)
    result = weekly.prepare_schedule(2026, 8, now=NOW, allow_network=False)
    assert result['status'] == 'ready'
    saved = weekly.read_schedule(2026, NOW + 1, deep=True)
    assert saved['descriptor']['original_received_at'] == NOW - 1000
    assert saved['descriptor']['raw_sha256'] == hashlib.sha256(body).hexdigest()
    assert weekly._source_path(saved['descriptor']['raw_path']).read_bytes() == body
    assert (settings.nflverse_cache_dir.parent / 'forward_2026/games.csv.gz').read_bytes() == body


def test_missing_cached_schedule_returns_waiting_without_network(fixture):
    result = weekly.prepare_schedule(2026, 8, now=NOW, allow_network=False)
    assert result['status'] == 'pending'
    assert weekly.status()['status'] == 'missing'


def test_schedule_partial_final_scores_retained_pending_and_no_current_pointer(fixture):
    rows = schedule_rows(); rows[0]['home_score'] = ''
    schedule_archive(fixture, rows)
    result = weekly.prepare_schedule(2026, 8, now=NOW, allow_network=False)
    assert result['status'] == 'pending'
    assert weekly.read_schedule(2026, NOW + 1)['status'] == 'missing'


def test_schedule_current_source_rejects_tamper_and_wrong_endpoint(fixture):
    schedule_archive(fixture)
    path = settings.nflverse_cache_dir.parent / 'forward_2026/games.metadata.json'
    meta = json.loads(path.read_text()); meta['source_url'] = 'https://evil.invalid/games.csv.gz'
    path.write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        weekly.prepare_schedule(2026, 8, now=NOW, allow_network=False)


def test_schedule_download_uses_separate_daily_guard_and_same_bounded_raw_stream(fixture):
    body = schedule_archive(fixture)
    folder = settings.nflverse_cache_dir.parent / 'forward_2026'
    (folder / 'games.csv.gz').unlink(); (folder / 'games.metadata.json').unlink()
    client = Client([Response(body)])
    result = weekly.prepare_schedule(2026, 8, now=NOW, client=client)
    assert result['status'] == 'ready' and client.urls == [weekly.SCHEDULES_URL]
    assert not (folder / 'games.csv.gz').exists()


def test_offline_job_missing_prepared_assets_waits_and_never_calls_legacy_runner(fixture, monkeypatch):
    learning_worker.ensure_schema()
    job = dict(id=1, component='NFL_WEEKLY', kind='retuning', detail=json.dumps(dict(season=2026, week=9)))
    monkeypatch.setattr(weekly, '_freeze', lambda *a: pytest.fail('cannot fit without verified prepared assets'))
    result = weekly.run_job(job)
    assert result['status'] == 'waiting' and result['reason'] == 'verified_current_pbp_required'


def test_status_is_query_only_before_weekly_schema_exists(fixture):
    db.conn().execute('PRAGMA query_only=ON')
    assert weekly.status()['status'] == 'missing'


class FixedEstimator:
    def __init__(self, value):
        self.value = value
    def predict(self, values):
        return np.array([self.value] * len(values))
    def predict_proba(self, values):
        return np.array([[.4, .6]] * len(values))
    def get_params(self, deep=True):
        return {'fixture_value': self.value}


def full_assets(fixture):
    path, meta = archive(fixture)
    sources.adopt_archive(path, meta, season=2026, completed_week=8, now=NOW)
    schedule_archive(fixture)
    assert weekly.prepare_schedule(2026, 8, NOW, allow_network=False)['status'] == 'ready'
    settings.nflverse_cache_dir.mkdir(parents=True, exist_ok=True)
    for year in range(2019, 2026):
        folder = fixture / str(year); folder.mkdir()
        week = 17 if year < 2021 else 18
        rows = game(week=week, date=f'{year}-11-01')
        for row in rows:
            row.update(season=str(year), game_id=f'{year}_{week:02d}_BUF_MIN')
        raw_path, raw_meta = archive(folder, rows)
        info = json.loads(raw_meta.read_text())
        info['source_url'] = sources.PBP_URL.format(season=year)
        destination = settings.nflverse_cache_dir / f'play_by_play_{year}.csv.gz'
        destination.write_bytes(raw_path.read_bytes())
        destination.with_name(f'play_by_play_{year}.metadata.json').write_text(json.dumps(info))


def fake_only_estimators(monkeypatch):
    from edgeterminal import nfl_forward_research as legacy, nfl_repro_bundle as bundles
    monkeypatch.setattr(legacy, 'code_commit', lambda: 'c' * 40)
    monkeypatch.setattr(bundles, 'code_commit', lambda: 'c' * 40)
    prior = dict(season=2026, week=8, game_id='2026_08_BUF_MIN',
                 home='Minnesota Vikings', away='Buffalo Bills', metrics={
                     'Minnesota Vikings': {'epa': .2}, 'Buffalo Bills': {'epa': .1}})
    monkeypatch.setattr(legacy.lab, 'build_dataset', lambda *a, **k: [prior])
    monkeypatch.setattr(legacy.lab, '_models', lambda *a, **k: (
        FixedEstimator(7.), FixedEstimator(45.), FixedEstimator(.6)))
    monkeypatch.setattr(legacy.lab, '_team_snapshot', lambda *a, **k: dict(opponents=[]))
    monkeypatch.setattr(legacy.lab, '_features_before', lambda *a, **k: (
        {name: 0. for name in legacy.lab.FEATURES}, .5))


def test_real_unchanged_bundle_builder_publishes_final_then_verifies_after_temp_cleanup(fixture, monkeypatch):
    from edgeterminal.nfl_repro_bundle import verify_bundle
    from edgeterminal.learning_worker import offline_runtime
    full_assets(fixture)
    fake_only_estimators(monkeypatch)
    original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in settings.nflverse_cache_dir.glob('*.csv.gz')}
    job = dict(id=1, component='NFL_WEEKLY', kind='retuning', detail=json.dumps(dict(season=2026, week=9)))
    with offline_runtime():
        result = weekly.run_job(job)
    assert result['status'] == 'completed'
    final = Path(result['freeze']['path'])
    assert final == fixture / 'reports/forward_2026_week09.json'
    assert final.exists() and verify_bundle(final, deep=True)['freeze_sha256'] == result['bundle']['freeze_sha256']
    assert not list((sources.root() / 'weekly_jobs').glob('offline-*'))
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in settings.nflverse_cache_dir.glob('*.csv.gz')} == original
    assert weekly.run_job(job)['status'] == 'retained'


def test_fitting_crossing_kickoff_refuses_final_publication(fixture, monkeypatch):
    full_assets(fixture)
    fake_only_estimators(monkeypatch)
    original = weekly._freeze
    def crossing(*args):
        result = original(*args)
        monkeypatch.setattr(sources.time, 'time', lambda: NOW + 12 * 3600)
        return result
    monkeypatch.setattr(weekly, '_freeze', crossing)
    job = dict(detail=json.dumps(dict(season=2026, week=9)))
    result = weekly.run_job(job)
    assert result['reason'] == 'fitting_crossed_kickoff'
    assert not (fixture / 'reports/forward_2026_week09.json').exists()


def test_independent_learner_cannot_claim_weekly_job_before_parameters_exist(fixture, monkeypatch):
    original, observed = learning_worker.enqueue, []
    def interleave(*args, **kwargs):
        job = original(*args, **kwargs)
        observed.append(learning_worker._claim(NOW, 'fixture-racing-learner'))
        return job
    monkeypatch.setattr(learning_worker, 'enqueue', interleave)
    result = weekly.dispatch(2026, now=NOW)
    claimed = observed[0] if observed else learning_worker._claim(NOW, 'fixture-racing-learner')
    assert claimed['id'] == result['job_id']
    assert json.loads(claimed['detail']) == dict(season=2026, week=9)


def test_source_or_result_head_change_during_fit_defers_publication(fixture, monkeypatch):
    full_assets(fixture); fake_only_estimators(monkeypatch)
    original = weekly._freeze
    def mutation(*args):
        result = original(*args)
        source = sources.read_manifest(sources.status(2026)['source_id'])
        path = sources.root() / source['raw_path']
        path.write_bytes(path.read_bytes() + b'changed-after-fit')
        return result
    monkeypatch.setattr(weekly, '_freeze', mutation)
    with pytest.raises(weekly.publication.PublicationDeferred):
        weekly.run_job(dict(detail=json.dumps(dict(season=2026, week=9))))
    assert not (fixture / 'reports/forward_2026_week09.json').exists()


def test_missing_or_corrupt_history_never_starts_models(fixture, monkeypatch):
    full_assets(fixture)
    first = settings.nflverse_cache_dir / 'play_by_play_2019.csv.gz'
    first.unlink()
    monkeypatch.setattr(weekly, '_freeze', lambda *a: pytest.fail('missing history must wait before fitting'))
    result = weekly.run_job(dict(detail=json.dumps(dict(season=2026, week=9))))
    assert result['reason'] == 'verified_history_required' and result['missing_season'] == 2019


def test_canonical_january_game_uses_its_nfl_season_not_calendar_year(fixture, monkeypatch):
    from datetime import datetime, timezone
    at = datetime(2027, 1, 4, 12, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(sources.time, 'time', lambda: at)
    db.upsert_event(dict(EVENT, id='nfl-2026-reg-18-buf-min', start_time='2027-01-05T01:15:00Z',
                        meta=dict(season=2026, season_type=2, week=18)))
    result = weekly.dispatch(2026, now=at)
    assert result['season'] == 2026 and result['week'] == 18


def test_real_research_lock_does_not_block_offline_fit_and_publication_waits_for_release(fixture, monkeypatch):
    full_assets(fixture); fake_only_estimators(monkeypatch)
    path = settings.db_path.with_name(settings.db_path.name + '-research.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    busy, errors = threading.Event(), []
    original = weekly.publication.publish_lock
    @contextmanager
    def observe(**kwargs):
        try:
            with original(**kwargs):
                yield
        except weekly.publication.PublicationDeferred as exc:
            if str(exc) == 'refresh_busy':
                busy.set()
            raise
    monkeypatch.setattr(weekly.publication, 'publish_lock', observe)
    def release():
        if not busy.wait(5):
            errors.append('publication never reached held research lock')
        fcntl.flock(fd, fcntl.LOCK_UN)
    thread = threading.Thread(target=release); thread.start()
    try:
        result = weekly.run_job(dict(detail=json.dumps(dict(season=2026, week=9))))
        assert result['status'] == 'completed' and busy.is_set() and not errors
    finally:
        thread.join(6); os.close(fd)


def test_crossing_tipoff_during_final_link_aborts_only_owned_new_link(fixture, monkeypatch):
    full_assets(fixture); fake_only_estimators(monkeypatch)
    final = fixture / 'reports/forward_2026_week09.json'
    original = os.link
    def crossing(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination) == final:
            monkeypatch.setattr(sources.time, 'time', lambda: NOW + 12 * 3600)
        return result
    monkeypatch.setattr(os, 'link', crossing)
    with pytest.raises(weekly.publication.PublicationDeferred, match='crossed_kickoff'):
        weekly.run_job(dict(detail=json.dumps(dict(season=2026, week=9))))
    assert not final.exists()
    assert list((settings.nflverse_cache_dir.parent / 'forward_bundles').glob('*/manifest.json'))


def test_cached_schedule_accepts_exact_2019_oakland_without_rewriting_rows(fixture):
    historical = dict(schedule_rows()[0], season='2019', week='1', game_id='2019_01_DEN_OAK',
                      home_team='OAK', away_team='DEN', gameday='2019-09-09')
    raw = schedule_archive(fixture, [historical] + schedule_rows())
    parsed = weekly._parse_schedule(raw, 2026, 8, NOW)
    original_row = next(csv.DictReader(io.StringIO(gzip.decompress(raw).decode())))
    assert parsed['rows'][0] == original_row
    assert parsed['summary']['rows'] == 3
    assert gzip.decompress(raw).decode().find('2019_01_DEN_OAK') >= 0


@pytest.mark.parametrize('change', [dict(season='2020', game_id='2020_01_DEN_OAK'),
                                  dict(home_team='LV'), dict(home_team='Las Vegas Raiders'),
                                  dict(home_team='Oakland Raiders'), dict(game_id='2019_01_DEN_LV')])
def test_cached_schedule_rejects_wrong_era_and_alias_conflicts(fixture, change):
    historical = dict(schedule_rows()[0], season='2019', week='1', game_id='2019_01_DEN_OAK',
                      home_team='OAK', away_team='DEN', gameday='2019-09-09')
    historical.update(change)
    raw = schedule_archive(fixture, [historical] + schedule_rows())
    with pytest.raises(ValueError):
        weekly._parse_schedule(raw, 2026, 8, NOW)
