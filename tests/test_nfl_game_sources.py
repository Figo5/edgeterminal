"""Verified NFL game input refresh is immutable, bounded and independent of fitting."""
import copy
from contextlib import contextmanager
import csv
import gzip
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

from edgeterminal import db, nfl_game_sources as sources
from edgeterminal.config import settings

NOW = 1794232800.0
URL = 'https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2026.csv.gz'
EVENT = dict(id='nfl-2026-reg-09-buf-min', sport='NFL', home_team='Minnesota Vikings',
             away_team='Buffalo Bills', start_time='2026-11-10T01:15:00Z',
             status='STATUS_SCHEDULED', meta=dict(season=2026, season_type=2, week=9))
FIELDS = ('game_id', 'season', 'season_type', 'week', 'home_team', 'away_team',
          'game_date', 'home_score', 'away_score', 'game_seconds_remaining', 'qtr',
          'desc', 'posteam', 'defteam', 'epa', 'success', 'qb_dropback', 'rush_attempt',
          'yards_gained', 'cpoe', 'pass_oe', 'no_huddle')


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(settings, 'nflverse_cache_dir', tmp_path / 'nflverse/pbp')
    monkeypatch.setattr(sources.time, 'time', lambda: NOW)
    monkeypatch.setattr(sources, '_prepare_weekly_schedule', lambda *a, **k: dict(status='ready'))
    db.init()
    db.upsert_event(dict(EVENT, id='nfl-2026-reg-08-buf-min', start_time='2026-11-01T18:00:00Z',
                         status='STATUS_FINAL', meta=dict(season=2026, season_type=2, week=8)))
    yield tmp_path
    db._conn.close()
    db._conn = None


def game(week=8, home='MIN', away='BUF', date='2026-11-01', complete=True):
    base = dict(game_id=f'2026_{week:02d}_{away}_{home}', season='2026', season_type='REG',
                week=str(week), home_team=home, away_team=away, game_date=date,
                home_score='21', away_score='24', game_seconds_remaining='3600', qtr='1')
    rows = [dict(base, desc='GAME')]
    for team, opp, epa in [(home, away, '.2'), (away, home, '.1')]:
        rows.append(dict(base, desc='play', posteam=team, defteam=opp, epa=epa,
                         success='1', qb_dropback='1', rush_attempt='0', yards_gained='8'))
    if complete:
        rows.append(dict(base, desc='END GAME', game_seconds_remaining='0', qtr='4'))
    return rows


def archive(tmp_path, rows=None, *, received=NOW - 1000, metadata_changes=None):
    rows = game() if rows is None else rows
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    raw = gzip.compress(stream.getvalue().encode(), mtime=0)
    path = tmp_path / 'input.csv.gz'
    path.write_bytes(raw)
    meta = dict(source_url=URL, retrieved_at=received,
                sha256=hashlib.sha256(raw).hexdigest(), license='CC-BY-4.0')
    meta.update(metadata_changes or {})
    metadata = tmp_path / 'input.metadata.json'
    metadata.write_text(json.dumps(meta))
    return path, metadata


def adopt(tmp_path, rows=None, **kwargs):
    path, metadata = archive(tmp_path, rows, **kwargs)
    return sources.adopt_archive(path, metadata, season=2026, completed_week=8, now=NOW)


def test_verified_adoption_preserves_input_and_replays_exact_map(isolated):
    path, metadata = archive(isolated)
    before = path.read_bytes(), metadata.read_bytes()
    result = sources.adopt_archive(path, metadata, season=2026, completed_week=8, now=NOW)
    assert result['status'] == 'ready'
    prepared = sources.prepared_stats(EVENT, now=NOW + 1)
    assert prepared['status'] == 'ready'
    assert prepared['stats']['Minnesota Vikings']['feature_games'] == 1
    assert prepared['stats']['Buffalo Bills']['feature_games'] == 1
    saved = prepared['input_map']
    assert saved['original_received_at'] == NOW - 1000
    assert saved['adopted_at'] == NOW
    assert saved['policy_id'] == sources.POLICY_ID
    assert sources.verify_input_map(saved, EVENT, NOW + 1)['stats'] == prepared['stats']
    assert (path.read_bytes(), metadata.read_bytes()) == before
    again = sources.adopt_archive(path, metadata, season=2026, completed_week=8, now=NOW)
    assert again['source_id'] == result['source_id']
    assert len(db.q('SELECT * FROM nfl_game_source_artifacts')) == 1


def test_incomplete_genuine_archive_is_pending_without_replacing_head(isolated):
    first = adopt(isolated)
    result = adopt(isolated, game(complete=False), received=NOW - 10)
    assert result['status'] == 'pending'
    assert result['reason'] == 'awaiting_completed_week_coverage'
    assert sources.status(2026)['source_id'] == first['source_id']
    assert len(db.q('SELECT * FROM nfl_game_source_artifacts')) == 2


@pytest.mark.parametrize('change', [dict(source_url='https://evil.invalid/pbp.csv.gz'),
                                  dict(retrieved_at=NOW + 1), dict(sha256='0' * 64),
                                  dict(license='unknown')])
def test_rejects_untrusted_metadata_without_publication(isolated, change):
    with pytest.raises(ValueError):
        adopt(isolated, metadata_changes=change)
    assert sources.status(2026)['status'] == 'missing'


@pytest.mark.parametrize('mutate', [
    lambda rows: rows[1].update(home_team='NYJ'),
    lambda rows: rows[1].update(season='2025'),
    lambda rows: rows[1].update(game_id='2026_08_BUF_NYJ'),
    lambda rows: rows[1].update(home_score='22'),
    lambda rows: rows[1].update(epa='nan'),
    lambda rows: rows[1].update(posteam='FROGS'),
])
def test_conflicting_or_malformed_relevant_rows_fail_before_filter(isolated, mutate):
    rows = game()
    mutate(rows)
    with pytest.raises(ValueError):
        adopt(isolated, rows)
    assert sources.status(2026)['status'] == 'missing'


def test_future_target_or_postseason_is_not_used(isolated):
    rows = game() + game(9, date='2026-11-09')
    post = game(20, date='2027-01-10')
    for row in post:
        row['season_type'] = 'POST'
    adopt(isolated, rows + post)
    result = sources.prepared_stats(EVENT, now=NOW + 1)
    assert result['stats']['Buffalo Bills']['feature_games'] == 1
    assert result['input_map']['teams']['Buffalo Bills']['selected_game_ids'] == ['2026_08_BUF_MIN']
    past = dict(EVENT, id='nfl-2026-reg-08-buf-min', meta=dict(season=2026, season_type=2, week=8))
    assert sources.prepared_stats(past, now=NOW + 1)['status'] == 'missing'


def test_input_map_tamper_and_raw_tamper_fail(isolated):
    adopt(isolated)
    prepared = sources.prepared_stats(EVENT, now=NOW + 1)
    for key, value in [('original_received_at', NOW - 2000), ('raw_sha256', 'f' * 64),
                       ('completed_week', 7), ('game_id', 'nfl-2026-reg-09-nyj-min')]:
        bad = copy.deepcopy(prepared['input_map'])
        bad[key] = value
        with pytest.raises(ValueError):
            sources.verify_input_map(bad, EVENT, NOW + 1)
    descriptor = sources.read_manifest(prepared['input_map']['source_id'])
    raw = sources.root() / descriptor['raw_path']
    raw.write_bytes(raw.read_bytes() + b'bad')
    with pytest.raises(ValueError, match='hash'):
        sources.verify_input_map(prepared['input_map'], EVENT, NOW + 1)


def test_relocation_uses_relative_content_identity_not_original_path(isolated, monkeypatch):
    adopt(isolated)
    saved = sources.prepared_stats(EVENT, now=NOW + 1)['input_map']
    old = sources.root()
    destination = isolated / 'app/state'
    destination.mkdir(parents=True)
    old.rename(destination / 'nfl_game_sources')
    monkeypatch.setattr(sources, 'DIRECTORY', destination / 'nfl_game_sources')
    assert sources.verify_input_map(saved, EVENT, NOW + 1)['status'] == 'ready'
    descriptor = sources.read_manifest(saved['source_id'])
    raw = sources.root() / descriptor['raw_path']
    target = isolated / 'outside.gz'
    target.write_bytes(raw.read_bytes())
    raw.unlink()
    raw.symlink_to(target)
    with pytest.raises(ValueError, match='symlink|regular'):
        sources.verify_input_map(saved, EVENT, NOW + 1)


def test_local_symlink_and_duplicate_metadata_keys_rejected(isolated):
    path, metadata = archive(isolated)
    alias = isolated / 'alias.gz'
    alias.symlink_to(path)
    with pytest.raises(ValueError, match='symlink|regular'):
        sources.adopt_archive(alias, metadata, season=2026, completed_week=8, now=NOW)
    metadata.write_text('{"sha256":"a","sha256":"b"}')
    with pytest.raises(ValueError, match='duplicate'):
        sources.adopt_archive(path, metadata, season=2026, completed_week=8, now=NOW)


def test_archive_limits_and_traversal_are_explicit(isolated, monkeypatch):
    monkeypatch.setattr(sources, 'MAX_ROWS', 2)
    with pytest.raises(ValueError, match='row'):
        adopt(isolated)
    with pytest.raises(ValueError):
        sources.read_manifest('../outside')


def test_os_lease_is_nonblocking_and_not_heartbeat_driven(isolated):
    with sources.source_lease(2026) as first:
        assert first
        with sources.source_lease(2026) as second:
            assert not second
    with sources.source_lease(2026) as recovered:
        assert recovered


def test_dispatch_is_durable_idempotent_and_credential_free(isolated, monkeypatch):
    monkeypatch.setenv('SPORTSGAMEODDS_API_KEY', 'must-never-inherit')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-never-inherit-either')
    calls = []
    class Process:
        pid = 123456
    def launch(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs['close_fds'] is True and kwargs.get('shell', False) is False
        assert not any('KEY' in key or 'TOKEN' in key or 'PASSWORD' in key for key in kwargs['env'])
        return Process()
    monkeypatch.setattr(sources.subprocess, 'Popen', launch)
    monkeypatch.setattr(sources, '_alive', lambda pid: True)
    first = sources.dispatch_maintenance(2026, 8, NOW)
    second = sources.dispatch_maintenance(2026, 8, NOW)
    assert first['job_id'] == second['job_id'] and len(calls) == 1
    assert sources.status(2026)['job']['state'] == 'queued'


def test_jobs_recover_dead_owner_and_keep_event_history(isolated, monkeypatch):
    first = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    db.ex("UPDATE nfl_game_source_jobs SET state='running',pid=999999,owner='dead' WHERE id=?", (first['job_id'],))
    monkeypatch.setattr(sources, '_alive', lambda pid: False)
    again = sources.dispatch_maintenance(2026, 8, NOW + 10, launch=False)
    assert again['job_id'] == first['job_id']
    assert sources.status(2026)['job']['state'] == 'queued'
    with pytest.raises(Exception, match='append-only'):
        db.ex('DELETE FROM nfl_game_source_events')


def test_dispatch_refuses_unknown_or_unfinished_schedule_without_a_child(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(sources.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    result = sources.dispatch_maintenance(2026, 9, NOW)
    assert result['status'] == 'waiting' and not calls
    assert sources.maintenance_targets(NOW) == [dict(season=2026, completed_week=8)]


def test_child_adopts_existing_local_archive_without_network_and_rechecks_hash(isolated, monkeypatch):
    folder = settings.nflverse_cache_dir.parent / 'forward_2026'
    folder.mkdir(parents=True)
    path, meta = archive(isolated)
    (folder / 'play_by_play_2026.csv.gz').write_bytes(path.read_bytes())
    (folder / 'play_by_play_2026.metadata.json').write_bytes(meta.read_bytes())
    monkeypatch.setattr(sources, '_download', lambda *a, **k: pytest.fail('fresh local data must prevent download'))
    job = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    result = sources.run_job(job['job_id'])
    assert result['status'] == 'ready'
    assert sources.status(2026)['job']['state'] == 'completed'
    assert sources.prepared_stats(EVENT, NOW + 1)['status'] == 'ready'


def test_child_checks_known_schedule_coverage_before_publishing(isolated, monkeypatch):
    db.upsert_event(dict(EVENT, id='nfl-2026-reg-08-nyj-ne', home_team='New England Patriots',
                        away_team='New York Jets', start_time='2026-11-01T18:00:00Z',
                        status='STATUS_FINAL', meta=dict(season=2026, season_type=2, week=8)))
    folder = settings.nflverse_cache_dir.parent / 'forward_2026'
    folder.mkdir(parents=True)
    path, meta = archive(isolated)
    (folder / 'play_by_play_2026.csv.gz').write_bytes(path.read_bytes())
    (folder / 'play_by_play_2026.metadata.json').write_bytes(meta.read_bytes())
    job = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    result = sources.run_job(job['job_id'], allow_network=False)
    assert result['status'] == 'pending'
    assert sources.status(2026)['source_id'] is None


class Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code != 200:
            raise ValueError('HTTP failure')
    def iter_raw(self, chunk_size):
        yield self.body
    def iter_bytes(self, chunk_size):
        pytest.fail('raw gzip bytes must not be decoded by HTTP client')


class Client:
    def __init__(self, responses):
        self.responses, self.urls = list(responses), []
    def stream(self, method, url):
        assert method == 'GET'
        self.urls.append(url)
        return self.responses.pop(0)


def test_download_redirects_are_https_allowlisted_and_streamed_raw(isolated):
    client = Client([Response(status=302, headers={'location': 'https://release-assets.githubusercontent.com/asset?sig=x'}),
                     Response(b'raw-gzip', headers={'content-length': '8'})])
    assert sources._download(2026, client=client) == b'raw-gzip'
    assert len(client.urls) == 2
    bad = Client([Response(status=302, headers={'location': 'https://evil.invalid/asset'})])
    with pytest.raises(ValueError, match='HTTPS'):
        sources._download(2026, client=bad)
    with pytest.raises(ValueError, match='HTTPS'):
        sources._url('https://github.com/other/repo/asset', 2026)


def test_download_byte_redirect_and_timeout_limits(isolated, monkeypatch):
    monkeypatch.setattr(sources, 'MAX_COMPRESSED', 2)
    with pytest.raises(ValueError, match='byte'):
        sources._download(2026, client=Client([Response(b'abc')]))
    with pytest.raises(ValueError, match='byte'):
        sources._download(2026, client=Client([Response(b'x', headers={'content-length': '100'})]))
    redirects = [Response(status=302, headers={'location': 'https://objects.githubusercontent.com/x'})] * 4
    with pytest.raises(ValueError, match='redirect'):
        sources._download(2026, client=Client(redirects))
    ticks = iter([0., sources.MAX_SECONDS + 1])
    monkeypatch.setattr(sources.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError):
        sources._download(2026, client=Client([Response(b'x')]))


def test_network_attempt_is_durable_and_daily_even_when_incomplete(isolated, monkeypatch):
    path, _ = archive(isolated, game(complete=False))
    body = path.read_bytes()
    job = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    result = sources.run_job(job['job_id'], client=Client([Response(body)]))
    assert result['status'] == 'pending'
    assert len(db.q("SELECT * FROM nfl_game_source_events WHERE kind='network_attempt'")) == 1
    assert len(list((sources.root() / 'downloads').glob('*.csv.gz'))) == 1
    assert sources.dispatch_maintenance(2026, 8, NOW + 60, launch=False)['job_id'] == job['job_id']


def test_decompressed_and_csv_field_limits_fail_without_publication(isolated, monkeypatch):
    monkeypatch.setattr(sources, 'MAX_DECOMPRESSED', 10)
    with pytest.raises(ValueError, match='decompressed'):
        adopt(isolated)


def test_prior_map_verifies_after_newer_head_and_requires_real_receipt(isolated):
    adopt(isolated)
    saved = sources.prepared_stats(EVENT, NOW + 1)['input_map']
    rows = game()
    rows[1]['epa'] = '.3'
    adopt(isolated, rows, received=NOW - 1)
    assert sources.verify_input_map(saved, EVENT, NOW + 1)['stats']['Minnesota Vikings']['epa_play'] == .2
    with pytest.raises(ValueError, match='as-of'):
        sources.verify_input_map(saved, EVENT, NOW - 2)
    assert sources.prepared_stats(EVENT, NOW - 2)['status'] == 'missing'


def final_record(*, score=21, retrieved=NOW - 100):
    value = dict(freeze_sha256='a' * 64, game_id='2026_08_BUF_MIN',
                 final_home_score=score, final_away_score=24, result_provider='espn',
                 home_team='Minnesota Vikings', away_team='Buffalo Bills',
                 result_request=dict(season=2026, season_type=2, week=8),
                 result_source='https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
                 result_retrieved_at=retrieved, kickoff_utc=1793556000.)
    return ('a' * 64, '2026_08_BUF_MIN', 1, score, 24, value['result_source'], retrieved,
            retrieved + 1, json.dumps(value))


def install_final(row):
    db.ex('CREATE TABLE IF NOT EXISTS nfl_research_grades (freeze_sha TEXT,game_id TEXT,revision INTEGER,'
          'home_score INTEGER,away_score INTEGER,result_source TEXT,source_retrieved_at REAL,'
          'graded_at REAL,payload TEXT)')
    db.ex('INSERT INTO nfl_research_grades VALUES(?,?,?,?,?,?,?,?,?)', row)


def test_verified_final_overrides_stale_schedule_without_force_grading(isolated):
    db.ex("UPDATE events SET status='STATUS_SCHEDULED' WHERE id='nfl-2026-reg-08-buf-min'")
    assert sources.maintenance_targets(NOW) == []
    install_final(final_record())
    before = db.q('SELECT * FROM nfl_research_grades')
    assert sources.maintenance_targets(NOW) == [dict(season=2026, completed_week=8)]
    assert db.q('SELECT * FROM nfl_research_grades') == before
    assert sources.maintenance_targets(NOW - 101) == []


def test_final_score_conflict_with_independent_official_record_is_visible(isolated):
    install_final(final_record(score=22))
    path, meta = archive(isolated)
    with pytest.raises(ValueError, match='official result'):
        sources.adopt_archive(path, meta, season=2026, completed_week=8, now=NOW,
                              expected_scores=sources._known_finals(NOW))
    assert sources.status(2026)['source_id'] is None


def test_bad_final_receipt_or_identity_is_not_hidden_as_missing(isolated):
    row = list(final_record())
    body = json.loads(row[-1])
    body['home_team'] = 'New York Jets'
    row[-1] = json.dumps(body)
    install_final(tuple(row))
    with pytest.raises(ValueError, match='official final'):
        sources.maintenance_targets(NOW)


def test_per_run_deep_cache_rechecks_files_and_avoids_redecompression(isolated, monkeypatch):
    adopt(isolated)
    saved = sources.prepared_stats(EVENT, NOW + 1)['input_map']
    original, calls = sources._parse, []
    def parse(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(sources, '_parse', parse)
    cache = {}
    sources.verify_input_map(saved, EVENT, NOW + 1, proof_cache=cache)
    sources.verify_input_map(saved, EVENT, NOW + 1, proof_cache=cache)
    assert len(calls) == 1
    descriptor = sources.read_manifest(saved['source_id'])
    raw = sources.root() / descriptor['raw_path']
    raw.write_bytes(raw.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='changed'):
        sources.verify_input_map(saved, EVENT, NOW + 1, proof_cache=cache)


def test_http_failure_is_visible_and_retry_does_not_spend_another_daily_request(isolated):
    job = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    first = sources.run_job(job['job_id'], client=Client([Response(status=503)]))
    assert first['status'] == 'failed' and sources.status(2026)['job']['state'] == 'failed'
    retry = sources.dispatch_maintenance(2026, 8, NOW + 301, launch=False)
    assert retry['job_id'] != job['job_id']
    second = sources.run_job(retry['job_id'], client=Client([]))
    assert second['status'] == 'pending'
    assert len(db.q("SELECT * FROM nfl_game_source_events WHERE kind='network_attempt'")) == 1


def test_busy_source_owner_defers_child_without_releasing_job(isolated):
    job = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    with sources.source_lease(2026) as held:
        assert held
        result = sources.run_job(job['job_id'], allow_network=False)
        assert result == dict(status='deferred', reason='source_lease_busy')
    assert sources.status(2026)['job']['state'] == 'queued'


@pytest.mark.parametrize('bad', [True, 0, 1999, 2101, '2026'])
def test_invalid_source_season_refused(isolated, bad):
    with pytest.raises(ValueError):
        sources.dispatch_maintenance(bad, 8, NOW, launch=False)


def test_missing_receipt_and_invalid_end_clock_are_rejected(isolated):
    with pytest.raises(ValueError):
        adopt(isolated, metadata_changes={'retrieved_at': None})
    rows = game()
    rows[-1]['game_seconds_remaining'] = '2'
    with pytest.raises(ValueError, match='final clock'):
        adopt(isolated, rows)


def test_real_nfl_overtime_end_marker_may_have_time_remaining(isolated):
    rows = game()
    rows[-1].update(qtr='5', game_seconds_remaining='94')
    assert adopt(isolated, rows)['status'] == 'ready'
    rows[-1]['qtr'] = '3'
    with pytest.raises(ValueError, match='final clock'):
        adopt(isolated, rows, received=NOW - 10)


@pytest.mark.parametrize('remaining', ['640', '900'])
def test_postseason_end_marker_uses_verified_fifteen_minute_period(isolated, remaining):
    regular = game()
    postseason = game(week=18, date='2026-10-01')
    for row in postseason:
        row['season_type'] = 'POST'
    postseason[-1].update(qtr='5', game_seconds_remaining=remaining)
    path, _ = archive(isolated, regular + postseason)
    parsed = sources._parse(path.read_bytes(), 2026, 8, NOW)
    assert parsed['complete'] and parsed['parsed_games'] == 2
    regular_path, _ = archive(isolated, regular)
    assert parsed['games'] == sources._parse(regular_path.read_bytes(), 2026, 8, NOW)['games']
    assert parsed['stats'] == sources.aggregate_team_features(regular, 2026, retrieved_at=NOW)


@pytest.mark.parametrize('kind,quarter,remaining', [
    ('REG', '5', '601'), ('PRE', '5', '601'), ('POST', '5', '901'),
    ('POST', '4', '640'), ('POST', '5', ''), ('POST', '5.5', '640')])
def test_overtime_clock_limit_does_not_relax_other_final_checks(isolated, kind, quarter, remaining):
    rows = game()
    for row in rows:
        row['season_type'] = kind
    rows[-1].update(qtr=quarter, game_seconds_remaining=remaining)
    path, _ = archive(isolated, rows)
    with pytest.raises(ValueError, match='final clock'):
        sources._parse(path.read_bytes(), 2026, 8, NOW)


def test_independent_child_waits_for_capture_release_without_reparsing_or_network(isolated, monkeypatch):
    from edgeterminal import learning_publication
    calls, parses = [], []
    original = sources._parse
    def parse(*args, **kwargs):
        parses.append(True)
        return original(*args, **kwargs)
    @contextmanager
    def publication(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise learning_publication.PublicationDeferred('refresh_busy')
        yield
    monkeypatch.setattr(sources, '_parse', parse)
    monkeypatch.setattr(learning_publication, 'publish_lock', publication)
    monkeypatch.setattr(sources.time, 'sleep', lambda _: None)
    path, meta = archive(isolated)
    result = sources.adopt_archive(path, meta, season=2026, completed_week=8, now=NOW,
                                   publication_wait_seconds=1)
    assert result['status'] == 'ready' and len(calls) == 2 and len(parses) == 1


def test_publication_deadline_durably_resumes_next_tick_without_parse_or_provider(isolated, monkeypatch):
    from edgeterminal import learning_publication
    folder = settings.nflverse_cache_dir.parent / 'forward_2026'
    folder.mkdir(parents=True)
    path, meta = archive(isolated)
    (folder / 'play_by_play_2026.csv.gz').write_bytes(path.read_bytes())
    (folder / 'play_by_play_2026.metadata.json').write_bytes(meta.read_bytes())
    @contextmanager
    def busy(**kwargs):
        raise learning_publication.PublicationDeferred('refresh_busy')
        yield
    original = sources.adopt_archive
    def immediate(*args, **kwargs):
        kwargs['publication_wait_seconds'] = 0
        return original(*args, **kwargs)
    monkeypatch.setattr(sources, 'adopt_archive', immediate)
    monkeypatch.setattr(learning_publication, 'publish_lock', busy)
    first = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    result = sources.run_job(first['job_id'], allow_network=False)
    assert result['status'] == 'deferred' and result['prepared_source_id']
    assert sources.status(2026)['source_id'] is None
    assert len(db.q('SELECT * FROM nfl_game_source_artifacts')) == 1
    @contextmanager
    def available(**kwargs):
        yield
    monkeypatch.setattr(learning_publication, 'publish_lock', available)
    monkeypatch.setattr(sources, '_parse', lambda *a, **k: pytest.fail('sealed preparation must not be parsed again'))
    monkeypatch.setattr(sources, '_download', lambda *a, **k: pytest.fail('publication retry must not request a source'))
    monkeypatch.setattr(sources.time, 'time', lambda: NOW + 302)
    second = sources.dispatch_maintenance(2026, 8, NOW + 301, launch=False)
    assert second['job_id'] != first['job_id']
    assert sources.run_job(second['job_id'], allow_network=False)['status'] == 'ready'
    assert sources.prepared_stats(EVENT, NOW + 1)['status'] == 'missing'
    prepared = sources.prepared_stats(EVENT, NOW + 303)
    assert prepared['stats']['Minnesota Vikings']['feature_retrieved_at'] == NOW + 302
    assert sources.verify_input_map(prepared['input_map'], EVENT, NOW + 303, deep=False)['status'] == 'ready'
    bad = copy.deepcopy(prepared['input_map'])
    bad['published_at'] = NOW
    bad['sha256'] = sources.sha({k: v for k, v in bad.items() if k != 'sha256'})
    with pytest.raises(ValueError, match='publication'):
        sources.verify_input_map(bad, EVENT, NOW + 303, deep=False)


def test_status_on_query_only_database_with_absent_source_schema_never_writes(isolated):
    db.conn().execute('PRAGMA query_only=ON')
    before = db.q("SELECT name,type FROM sqlite_master ORDER BY name")
    result = sources.status(2026)
    assert result['status'] == 'missing' and result['initialized'] is False
    assert db.q("SELECT name,type FROM sqlite_master ORDER BY name") == before


def test_failed_and_pending_maintenance_are_visible_with_served_head(isolated):
    adopt(isolated)
    first = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    assert sources.status(2026)['status'] == 'pending'
    db.ex("UPDATE nfl_game_source_jobs SET state='failed',detail=? WHERE id=?",
          (json.dumps(dict(status='failed', reason='source hash conflict')), first['job_id']))
    result = sources.status(2026)
    assert result['status'] == 'error' and result['reason'] == 'source hash conflict'
    assert result['served_head']['status'] == 'ready' and result['served_head']['source_id']


def test_free_running_lease_recovers_even_if_dead_child_pid_was_recycled(isolated, monkeypatch):
    first = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    db.ex("UPDATE nfl_game_source_jobs SET state='running',pid=?,owner='crashed-owner' WHERE id=?",
          (os.getpid(), first['job_id']))
    monkeypatch.setattr(sources, '_alive', lambda _: True)
    recovered = sources.dispatch_maintenance(2026, 8, NOW + 1000000, launch=False)
    assert recovered['job_id'] == first['job_id']
    assert sources.status(2026)['job']['state'] == 'queued'
    assert sources.status(2026)['job']['owner'] is None


def test_held_os_lease_is_not_stolen_by_pid_or_age(isolated, monkeypatch):
    first = sources.dispatch_maintenance(2026, 8, NOW, launch=False)
    db.ex("UPDATE nfl_game_source_jobs SET state='running',pid=999999,owner='actual-owner' WHERE id=?",
          (first['job_id'],))
    monkeypatch.setattr(sources, '_alive', lambda _: False)
    with sources.source_lease(2026) as held:
        assert held
        result = sources.dispatch_maintenance(2026, 8, NOW + 1000000, launch=False)
        assert result['job_id'] == first['job_id']
        assert sources.status(2026)['job']['owner'] == 'actual-owner'


def test_queued_live_pid_only_gets_bounded_nonce_startup_grace(isolated, monkeypatch):
    calls = []
    class Process:
        pid = os.getpid()
    monkeypatch.setattr(sources.subprocess, 'Popen', lambda *a, **k: calls.append(a) or Process())
    monkeypatch.setattr(sources, '_alive', lambda _: True)
    first = sources.dispatch_maintenance(2026, 8, NOW)
    first_nonce = json.loads(sources.status(2026)['job']['detail'])['startup_nonce']
    sources.dispatch_maintenance(2026, 8, NOW + 1)
    assert len(calls) == 1
    sources.dispatch_maintenance(2026, 8, NOW + 61)
    assert len(calls) == 2
    assert json.loads(sources.status(2026)['job']['detail'])['startup_nonce'] != first_nonce
    with pytest.raises(ValueError, match='nonce'):
        sources.run_job(first['job_id'], allow_network=False, startup_nonce=first_nonce)


def historical_oakland_rows():
    rows = game(week=17, home='OAK', away='DEN', date='2019-12-29')
    for row in rows:
        row.update(season='2019', game_id='2019_17_DEN_OAK')
    return rows


def normalized_2019_pbp_rows():
    rows = historical_oakland_rows()
    for row in rows:
        for key in ('home_team', 'away_team', 'posteam', 'defteam'):
            if row.get(key) == 'OAK':
                row[key] = 'LV'
    return rows


def test_observed_2019_pbp_normalized_fields_require_historical_oakland_game_id(isolated):
    rows = normalized_2019_pbp_rows()
    path, _ = archive(isolated, rows)
    parsed = sources._parse(path.read_bytes(), 2019, 17, NOW)
    assert parsed['games'][0]['game_id'] == '2019_17_DEN_OAK'
    assert parsed['stats']['Las Vegas Raiders']['games'] == 1
    assert parsed['stats'] == sources.aggregate_team_features(rows, 2019, retrieved_at=NOW)
    assert sources.identity.team_code('OAK') is None  # global/pinned identity is untouched


@pytest.mark.parametrize('index,key', [(1, 'home_team'), (1, 'posteam'), (2, 'defteam')])
def test_pbp_2019_does_not_mix_raw_and_normalized_raidership_spelling(isolated, index, key):
    rows = normalized_2019_pbp_rows()
    rows[index][key] = 'OAK'
    path, _ = archive(isolated, rows)
    with pytest.raises(ValueError):
        sources._parse(path.read_bytes(), 2019, 17, NOW)


def test_verified_2019_oakland_source_preserves_raw_ids_and_aggregates_same_franchise(isolated):
    rows = historical_oakland_rows()
    path, _ = archive(isolated, rows)
    raw = path.read_bytes()
    parsed = sources._parse(raw, 2019, 17, NOW)
    assert sources._game_key('2019_17_DEN_OAK') == (2019, 17, 'den', 'lv')
    assert parsed['complete'] and parsed['parsed_games'] == 1
    assert parsed['games'][0]['game_id'] == '2019_17_DEN_OAK'
    assert parsed['games'][0]['home'] == 'Las Vegas Raiders'  # stable franchise identity
    assert parsed['teams']['Las Vegas Raiders']['selected_game_ids'] == ['2019_17_DEN_OAK']
    assert 'OAK' not in parsed['stats'] and parsed['stats']['Las Vegas Raiders']['games'] == 1
    original = sources.aggregate_team_features(rows, 2019, retrieved_at=NOW)
    assert parsed['stats']['Las Vegas Raiders'] == original['OAK']
    assert parsed['stats']['Denver Broncos'] == original['Denver Broncos']
    assert path.read_bytes() == raw


@pytest.mark.parametrize('game_id', ['2018_17_DEN_OAK', '2020_17_DEN_OAK', '2026_17_DEN_OAK',
                                  '2019_17_DEN_LV', '2019_17_DEN_RAIDERS', '2019_17_DEN_O.A.K'])
def test_historical_alias_is_exact_and_rejects_wrong_era_or_fuzzy_raw_id(isolated, game_id):
    with pytest.raises(ValueError):
        sources._game_key(game_id)


@pytest.mark.parametrize('field,alias', [('home_team', 'LV'), ('home_team', 'Las Vegas Raiders'),
                                        ('home_team', 'Oakland Raiders'), ('posteam', 'LV'),
                                        ('posteam', 'Raiders'), ('defteam', 'LV')])
def test_2019_alias_conflicts_or_unverified_spelling_fail_before_aggregation(isolated, field, alias):
    rows = historical_oakland_rows()
    if field == 'defteam':
        rows[2][field] = alias
    else:
        rows[1][field] = alias
    path, _ = archive(isolated, rows)
    with pytest.raises(ValueError):
        sources._parse(path.read_bytes(), 2019, 17, NOW)


def test_future_era_oakland_play_team_is_not_accepted(isolated):
    rows = game(home='LV', away='DEN')
    rows[1]['posteam'] = 'OAK'
    path, _ = archive(isolated, rows)
    with pytest.raises(ValueError):
        sources._parse(path.read_bytes(), 2026, 8, NOW)
