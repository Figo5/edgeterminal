"""Automatic legacy weekly research preparation, separate from capture/grading.

Only ``prepare_schedule`` belongs to the network-enabled source child. Dispatch
is cheap; ``run_job`` is called exclusively by the existing offline learner and
uses unchanged legacy fitting/bundle code over verified owned cache copies.
"""
from __future__ import annotations

from contextlib import nullcontext
import csv
from datetime import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from zoneinfo import ZoneInfo

from . import db, nfl_game_sources as sources, learning_publication as publication
from .config import ROOT, settings

SCHEDULES_URL = sources.SCHEDULES_URL
OUTPUT_DIRECTORY = None
ET = ZoneInfo('America/New_York')
KIND = 'nfl-weekly-schedule-v1'


def _schema_present():
    return db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfl_weekly_schedule_artifacts'") is not None


def _schema():
    with db.tx() as con:
        con.execute('CREATE TABLE IF NOT EXISTS nfl_weekly_schedule_artifacts ('
                    'schedule_id TEXT PRIMARY KEY,season INTEGER NOT NULL,completed_week INTEGER NOT NULL,'
                    'received_at REAL NOT NULL,prepared_at REAL NOT NULL,descriptor TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS nfl_weekly_schedule_events ('
                    'id INTEGER PRIMARY KEY,season INTEGER NOT NULL,recorded_at REAL NOT NULL,'
                    'kind TEXT NOT NULL,schedule_id TEXT,detail TEXT NOT NULL)')
        for table in ('nfl_weekly_schedule_artifacts', 'nfl_weekly_schedule_events'):
            for action in ('UPDATE', 'DELETE'):
                con.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                            "BEGIN SELECT RAISE(ABORT,'NFL weekly preparation is append-only'); END")


def _event(season, kind, at, schedule_id=None, detail=None):
    db.ex('INSERT INTO nfl_weekly_schedule_events(season,recorded_at,kind,schedule_id,detail) VALUES(?,?,?,?,?)',
          (season, at, kind, schedule_id, sources.encoded(detail or {}).decode()))


def _source_path(relative):
    return sources._path(relative)


def _parse_schedule(raw, season, completed_week, at):
    required = {'game_id', 'season', 'game_type', 'week', 'gameday', 'gametime',
                'home_team', 'away_team', 'home_score', 'away_score'}
    rows, current, complete = [], {}, True
    old = csv.field_size_limit(sources.MAX_FIELD)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            stream = io.TextIOWrapper(io.BufferedReader(sources._Limited(
                compressed, time.monotonic() + sources.MAX_SECONDS)), encoding='utf-8-sig', newline='')
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not required <= set(fields):
                raise ValueError('Weekly schedule identity columns are missing or duplicated')
            for index, row in enumerate(reader, 1):
                if index > sources.MAX_ROWS:
                    raise ValueError('Weekly schedule row limit exceeded')
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('Weekly schedule row shape mismatch')
                try:
                    year, week = int(row['season']), int(row['week'])
                except ValueError as exc:
                    raise ValueError('Weekly schedule season/week is invalid') from exc
                if not 1900 <= year <= 2100 or not 0 <= week <= 24:
                    raise ValueError('Weekly schedule season/week bounds are invalid')
                if row['game_type'] != 'REG' or not 2019 <= year <= season:
                    continue
                key = sources._game_key(row['game_id'])
                if (key[:2] != (year, week) or key[2] != sources.identity.team_code(row['away_team'])
                        or key[3] != sources.identity.team_code(row['home_team']) or not 1 <= week <= 18):
                    raise ValueError('Weekly schedule game/team identity conflict')
                try:
                    datetime.strptime(row['gameday'], '%Y-%m-%d')
                except ValueError as exc:
                    raise ValueError('Weekly schedule game date is invalid') from exc
                hs = sources._number(row['home_score'], optional=True)
                aws = sources._number(row['away_score'], optional=True)
                if any(score is not None and (score != int(score) or not 0 <= score <= 100) for score in (hs, aws)):
                    raise ValueError('Weekly schedule score is invalid')
                if year == season:
                    if key in current:
                        raise ValueError('Weekly schedule repeats a current-season game')
                    current[key] = (row, hs, aws)
                    if week <= completed_week and (hs is None or aws is None):
                        complete = False
                rows.append(row)
    except (gzip.BadGzipFile, EOFError, UnicodeError, csv.Error) as exc:
        raise ValueError('Weekly schedule gzip/CSV is invalid') from exc
    finally:
        csv.field_size_limit(old)
    expected = sources._known_expected(season, completed_week)
    missing = [gid for gid in expected if sources._game_key(gid) not in current]
    finals = sources._known_finals(at)
    for gid, result in finals.items():
        key = sources._game_key(gid)
        if key[0] == season and key[1] <= completed_week and key in current:
            _, hs, aws = current[key]
            if hs is None or aws is None:
                complete = False
            elif (hs, aws) != (result['home_score'], result['away_score']):
                raise ValueError('Weekly schedule score conflicts with verified official final')
    complete = complete and not missing and any(key[1] == completed_week for key in current)
    summary = dict(rows=len(rows), current_game_ids=sorted(row[0]['game_id'] for row in current.values()),
                   missing_game_ids=sorted(missing), complete=complete)
    return dict(rows=rows, summary=summary)


def _schedule_head(season):
    return db.q1("SELECT a.*,e.id AS publication_event_id,e.recorded_at AS published_at "
                 "FROM nfl_weekly_schedule_events e JOIN nfl_weekly_schedule_artifacts a "
                 "ON a.schedule_id=e.schedule_id WHERE e.season=? AND e.kind='published' "
                 'ORDER BY e.id DESC LIMIT 1', (season,))


def _descriptor(schedule_id):
    sources._digest(schedule_id)
    if not _schema_present():
        raise ValueError('Prepared weekly schedule is unavailable')
    row = db.q1('SELECT descriptor FROM nfl_weekly_schedule_artifacts WHERE schedule_id=?', (schedule_id,))
    if not row:
        raise ValueError('Prepared weekly schedule descriptor is unavailable')
    body = sources._read(_source_path('weekly_schedules/manifests/' + schedule_id + '.json'), sources.MAX_METADATA)
    if body != row['descriptor'].encode() or hashlib.sha256(body).hexdigest() != schedule_id:
        raise ValueError('Prepared weekly schedule descriptor hash conflict')
    descriptor = sources._json(body)
    if descriptor['kind'] != KIND or descriptor['source_url'] != SCHEDULES_URL:
        raise ValueError('Prepared weekly schedule contract mismatch')
    return descriptor


def read_schedule(season, now=None, *, deep=False):
    at = sources._stamp(now)
    if not _schema_present():
        return dict(status='missing', reason='verified_schedule_required')
    head = _schedule_head(sources._season(season))
    if not head:
        return dict(status='missing', reason='verified_schedule_required')
    descriptor = _descriptor(head['schedule_id'])
    if max(descriptor['original_received_at'], descriptor['prepared_at'], head['published_at']) > at:
        return dict(status='missing', reason='schedule_not_available_by_asof')
    result = dict(status='ready', schedule_id=head['schedule_id'], descriptor=descriptor,
                  published_at=head['published_at'], publication_event_id=head['publication_event_id'])
    if deep:
        raw = sources._read(_source_path(descriptor['raw_path']), sources.MAX_COMPRESSED)
        if hashlib.sha256(raw).hexdigest() != descriptor['raw_sha256']:
            raise ValueError('Prepared weekly schedule raw hash conflict')
        parsed = _parse_schedule(raw, season, descriptor['completed_week'], descriptor['prepared_at'])
        if sources.sha(parsed['summary']) != descriptor['summary_sha256']:
            raise ValueError('Prepared weekly schedule replay conflict')
        result['rows'] = parsed['rows']
    return result


def _publish_schedule(schedule_id, descriptor, *, wait_seconds=sources.MAX_SECONDS):
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with publication.publish_lock(allow_near=True):
                head = _schedule_head(descriptor['season'])
                if not head or (head['received_at'], head['completed_week']) <= (
                        descriptor['original_received_at'], descriptor['completed_week']):
                    if not head or head['schedule_id'] != schedule_id:
                        _event(descriptor['season'], 'published', time.time(), schedule_id)
                return dict(status='ready', schedule_id=schedule_id)
        except publication.PublicationDeferred as exc:
            if str(exc) != 'refresh_busy' or time.monotonic() >= deadline:
                raise
            time.sleep(min(1., max(.01, deadline-time.monotonic())))


def _adopt_schedule(path, metadata, season, completed_week, now):
    info = sources._json(sources._read(metadata, sources.MAX_METADATA))
    if (not isinstance(info, dict) or info.get('source_url') != SCHEDULES_URL
            or info.get('license') != 'CC-BY-4.0' or info.get('retrieved_at') is None
            or not 0 < sources._stamp(info['retrieved_at']) <= now):
        raise ValueError('Weekly schedule source endpoint/receipt/license is invalid')
    sources._digest(info.get('sha256'))
    raw = sources._read(path, sources.MAX_COMPRESSED)
    if hashlib.sha256(raw).hexdigest() != info['sha256']:
        raise ValueError('Weekly schedule raw hash conflicts with its receipt')
    parsed = _parse_schedule(raw, season, completed_week, now)
    descriptor = dict(kind=KIND, schema=1, season=season, completed_week=completed_week,
                      source_url=SCHEDULES_URL, original_received_at=info['retrieved_at'], prepared_at=now,
                      raw_sha256=info['sha256'], raw_path='weekly_schedules/raw/' + info['sha256'] + '.csv.gz',
                      summary_sha256=sources.sha(parsed['summary']), complete=parsed['summary']['complete'])
    schedule_id = sources.sha(descriptor)
    sources._immutable(descriptor['raw_path'], raw)
    sources._immutable('weekly_schedules/manifests/' + schedule_id + '.json', sources.encoded(descriptor))
    db.ex('INSERT OR IGNORE INTO nfl_weekly_schedule_artifacts VALUES(?,?,?,?,?,?)',
          (schedule_id, season, completed_week, info['retrieved_at'], now, sources.encoded(descriptor).decode()))
    if not descriptor['complete']:
        _event(season, 'pending', now, schedule_id, dict(reason='completed_week_schedule_incomplete'))
        return dict(status='pending', reason='completed_week_schedule_incomplete', schedule_id=schedule_id)
    return _publish_schedule(schedule_id, descriptor)


def prepare_schedule(season, completed_week, now=None, *, allow_network=True, client=None, lease_owned=False):
    """Network-enabled source child only; never called by capture or the fitter."""
    season, completed_week, at = sources._season(season), sources._week(completed_week), sources._stamp(now)
    _schema()
    with (nullcontext(True) if lease_owned else sources.source_lease(season)) as owned:
        if not owned:
            return dict(status='pending', reason='source_lease_busy')
        result = dict(status='pending', reason='verified_schedule_required')
        cache = settings.nflverse_cache_dir.absolute()
        candidates = []
        for folder in (cache.parent / f'forward_{season}', cache):
            path, metadata = folder / 'games.csv.gz', folder / 'games.metadata.json'
            if path.exists() or metadata.exists():
                if not path.exists() or not metadata.exists():
                    raise ValueError('Weekly schedule archive/receipt pair is incomplete')
                info = sources._json(sources._read(metadata, sources.MAX_METADATA))
                candidates.append((sources._stamp(info.get('retrieved_at')), path, metadata))
        for _, path, metadata in sorted(candidates, reverse=True):
            result = _adopt_schedule(path, metadata, season, completed_week, at)
            if result['status'] == 'ready':
                return result
        last = db.q1("SELECT recorded_at FROM nfl_weekly_schedule_events WHERE season=? AND kind='network_attempt' "
                      'ORDER BY id DESC LIMIT 1', (season,))
        if not allow_network or (last and at < last['recorded_at'] + sources.DAY):
            return {**result, 'next_due_at': last['recorded_at'] + sources.DAY if last else at + sources.DAY}
        _event(season, 'network_attempt', at)
        raw = sources._download(season, client=client, asset_url=SCHEDULES_URL)
        received = time.time(); digest = hashlib.sha256(raw).hexdigest()
        path = sources._path(sources._immutable('weekly_schedules/downloads/' + digest + '.csv.gz', raw))
        receipt = dict(source_url=SCHEDULES_URL, retrieved_at=received, sha256=digest, license='CC-BY-4.0')
        metadata = sources._path(sources._immutable('weekly_schedules/receipts/' + sources.sha(receipt) + '.json',
                                                   sources.encoded(receipt)))
        return _adopt_schedule(path, metadata, season, completed_week, received)


def _events(season, week=None):
    result = []
    for event in db.q("SELECT * FROM events WHERE sport='NFL' AND id LIKE ?", (f'nfl-{season}-reg-%',)):
        _, year, number = sources._event_identity(event)
        if year == season and (week is None or number == week):
            result.append(event)
    return sorted(result, key=lambda event: event['id'])


def _output(season, week):
    directory = Path(OUTPUT_DIRECTORY) if OUTPUT_DIRECTORY is not None else ROOT / 'reports/nfl_model_v2'
    path = directory / f'forward_{season}_week{week:02d}.json'
    sources._ancestors(path.absolute())
    return path


def dispatch(season=2026, week=None, now=None):
    """Queue only. No source load, model fit, capture, grading or provider call."""
    season, at = sources._season(season), sources._stamp(now)
    if week is None:
        groups = {}
        for event in _events(season):
            number = sources._event_identity(event)[2]
            tip = sources.identity.kickoff_utc(event['start_time']).timestamp()
            groups[number] = min(tip, groups.get(number, float('inf')))
        eligible = [(tip, number) for number, tip in groups.items() if at < tip <= at + 36 * 3600]
        if not eligible:
            return dict(status='waiting', reason='no_whole_week_before_first_kickoff', freeze='no eligible pregame week')
        _, week = min(eligible)
    week = sources._week(week)
    events = _events(season, week)
    if not events or min(sources.identity.kickoff_utc(e['start_time']).timestamp() for e in events) <= at:
        return dict(status='waiting', reason='target_week_started_or_missing', freeze='refused: no full future week')
    if _output(season, week).exists():
        return dict(status='retained', season=season, week=week, freeze='already exists; unchanged')
    from . import learning_worker
    learning_worker._catalog('NFL_WEEKLY', 'retuning')
    learning_worker.ensure_schema()
    with db.tx() as con:
        con.execute('BEGIN IMMEDIATE')
        current = con.execute("SELECT * FROM learning_jobs WHERE component='NFL_WEEKLY' AND kind='retuning' "
                              "AND state IN ('queued','running')").fetchone()
        if current:
            return dict(status=current['state'], job_id=current['id'], reason='existing_weekly_job_pending',
                        freeze='queued for offline preparation')
        detail = sources.encoded(dict(season=season, week=week)).decode()
        cursor = con.execute("INSERT INTO learning_jobs(component,kind,queued_at,state,detail) "
                             "VALUES('NFL_WEEKLY','retuning',?,'queued',?)", (at, detail))
        job_id = cursor.lastrowid
        con.execute('INSERT INTO learning_job_events(job_id,recorded_at,state,detail) VALUES(?,?,?,?)',
                    (job_id, at, 'queued', detail))
    return dict(status='queued', job_id=job_id, season=season, week=week, freeze='queued for offline preparation')


def _freeze(cache, out, season, week):
    from .nfl_forward_research import freeze
    return freeze(cache, out, season, week, 'linear')


def _history_asset(year, now):
    # Use genuine prepared full-season data after a prior season rolls over;
    # the legacy mutable production cache may intentionally remain Week 1.
    if sources._has_schema():
        head = sources._head(year)
        if head and head['completed_week'] >= (17 if year < 2021 else 18):
            descriptor = sources.read_manifest(head['source_id'])
            sources._aggregate(descriptor, deep=True)
            return _source_path(descriptor['raw_path']), descriptor['raw_sha256'], dict(
                kind='prepared_history', source_id=head['source_id'], received_at=descriptor['original_received_at'])
    path = settings.nflverse_cache_dir / f'play_by_play_{year}.csv.gz'
    metadata = path.with_name(f'play_by_play_{year}.metadata.json')
    if not path.exists() or not metadata.exists():
        return None
    info = sources._metadata(metadata, year, now)
    raw = sources._read(path, sources.MAX_COMPRESSED)
    if hashlib.sha256(raw).hexdigest() != info['sha256']:
        raise ValueError('Weekly historical source hash conflict')
    parsed = sources._parse(raw, year, 17 if year < 2021 else 18, now)
    if not parsed['complete']:
        return None
    return path, info['sha256'], dict(kind='cached_history', receipt=info,
        metadata_sha256=hashlib.sha256(sources._read(metadata, sources.MAX_METADATA)).hexdigest())


def _copy_verified(source, destination, expected):
    source = Path(source); sources._ancestors(source.absolute())
    with source.open('rb') as handle, destination.open('xb') as target:
        shutil.copyfileobj(handle, target, 1024 * 1024)
        target.flush(); os.fsync(target.fileno())
    if hashlib.sha256(sources._read(destination, sources.MAX_COMPRESSED)).hexdigest() != expected:
        raise ValueError('Weekly immutable input changed during owned copy')


def _publish_final(draft, output, cutoff, recheck):
    """Only the independent learner waits; fitting/serialization are finished."""
    deadline = time.monotonic() + sources.MAX_SECONDS
    while True:
        try:
            with publication.publish_lock(allow_near=True):
                recheck()
                sources._ancestors(output.absolute())
                if time.time() >= cutoff:
                    raise publication.PublicationDeferred('weekly_publication_crossed_kickoff')
                output.parent.mkdir(parents=True, exist_ok=True)
                original = draft.lstat()
                os.link(draft, output)  # O_EXCL semantics: an existing freeze is never replaced.
                fd = os.open(str(output.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                    if time.time() >= cutoff:
                        current = output.lstat()
                        if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                            # Abort only this new, unregistered publication while
                            # the source lock excludes registration/grading. Its
                            # immutable bundle/proof remains as refusal evidence.
                            output.unlink()
                            os.fsync(fd)
                        raise publication.PublicationDeferred('weekly_publication_crossed_kickoff')
                finally:
                    os.close(fd)
                return
        except publication.PublicationDeferred as exc:
            if str(exc) != 'refresh_busy' or time.monotonic() >= deadline or time.time() >= cutoff:
                raise
            time.sleep(min(1., max(.01, deadline-time.monotonic())))


def run_job(job):
    """Offline-only legacy fit; never download, sync grades or capture quotes."""
    detail = json.loads(job.get('detail', '{}')) if isinstance(job.get('detail'), str) else job.get('detail', {})
    season, week = sources._season(detail.get('season')), sources._week(detail.get('week'))
    now = time.time(); events = _events(season, week)
    if not events or min(sources.identity.kickoff_utc(e['start_time']).timestamp() for e in events) <= now:
        return dict(status='waiting', reason='target_week_started_or_missing', freeze='refused: target week started')
    output = _output(season, week)
    if output.exists():
        return dict(status='retained', freeze='already exists; unchanged')
    state = sources.status(season)
    if not state.get('source_id'):
        return dict(status='waiting', reason='verified_current_pbp_required', freeze='waiting for prepared inputs')
    current = sources.read_manifest(state['source_id'])
    if current['completed_week'] != week - 1:
        return dict(status='waiting', reason='complete_prior_week_pbp_required', freeze='waiting for prepared inputs')
    current_aggregate = sources._aggregate(current, deep=True)
    schedule = read_schedule(season, now, deep=True)
    if schedule['status'] != 'ready' or schedule['descriptor']['completed_week'] < week - 1:
        return dict(status='waiting', reason='verified_schedule_required', freeze='waiting for prepared schedule')
    # A source remains valid historical evidence after a correction, but a new
    # fit cannot present its superseded final scores as the current truth.
    latest_finals = sources._known_finals(now)
    current_games = {sources._game_key(row['game_id']): row for row in current_aggregate['games']}
    schedule_games = {sources._game_key(row['game_id']): row for row in schedule['rows']}
    for gid, final in latest_finals.items():
        key = sources._game_key(gid)
        if key[0] != season or key[1] >= week:
            continue
        for game in (current_games.get(key), schedule_games.get(key)):
            if game and (sources._number(game['home_score'], optional=True),
                         sources._number(game['away_score'], optional=True)) != (final['home_score'], final['away_score']):
                raise ValueError('Weekly prepared inputs conflict with the current official final head')
    targets = [r for r in schedule['rows'] if int(r['season']) == season and int(r['week']) == week]
    by_id = {sources._game_key(e['id']): e for e in events}
    if {sources._game_key(r['game_id']) for r in targets} != set(by_id):
        return dict(status='waiting', reason='cached_schedule_target_coverage', freeze='refused: target coverage differs')
    for row in targets:
        event = by_id[sources._game_key(row['game_id'])]
        try:
            tip = datetime.fromisoformat(row['gameday'] + 'T' + row['gametime']).replace(tzinfo=ET).timestamp()
        except ValueError:
            return dict(status='waiting', reason='cached_schedule_target_time', freeze='refused: target time unknown')
        if (tip != sources.identity.kickoff_utc(event['start_time']).timestamp() or tip <= now
                or sources._number(row['home_score'], optional=True) is not None
                or sources._number(row['away_score'], optional=True) is not None):
            return dict(status='waiting', reason='cached_schedule_target_identity_or_result', freeze='refused: stale target')
    assets = {'games.csv.gz': (_source_path(schedule['descriptor']['raw_path']), schedule['descriptor']['raw_sha256'])}
    assets[f'play_by_play_{season}.csv.gz'] = (_source_path(current['raw_path']), current['raw_sha256'])
    receipts = dict(current_source_id=state['source_id'], schedule_id=schedule['schedule_id'], history={})
    for year in range(2019, season):
        asset = _history_asset(year, now)
        if asset is None:
            return dict(status='waiting', reason='verified_history_required', missing_season=year,
                        freeze='waiting for cached historical inputs')
        path, digest, evidence = asset
        assets[f'play_by_play_{year}.csv.gz'] = (path, digest)
        receipts['history'][str(year)] = evidence
    original_paths = [path for path, _ in assets.values()]
    original_paths.extend(sources.input_paths({'source_id': state['source_id']}))
    original_paths.append(_source_path('weekly_schedules/manifests/' + schedule['schedule_id'] + '.json'))
    for year, receipt in receipts['history'].items():
        if receipt['kind'] == 'cached_history':
            original_paths.append(settings.nflverse_cache_dir / f'play_by_play_{year}.metadata.json')
        else:
            original_paths.extend(sources.input_paths({'source_id': receipt['source_id']}))
    seals = publication.file_seals(original_paths)
    identity_state = sources.sha(events)
    result_state = sources.sha(latest_finals)
    directory = sources._path('weekly_jobs/placeholder', create=True).parent
    from .nfl_repro_bundle import verify_bundle, code_commit
    with tempfile.TemporaryDirectory(prefix='offline-', dir=directory) as temporary:
        cache = Path(temporary)
        for name, (path, digest) in assets.items():
            _copy_verified(path, cache / name, digest)
        # The unchanged legacy bundle builder first creates an owned private
        # freeze. Heavy serialization/verification therefore holds no research lock.
        draft = cache / 'draft-freeze.json'
        result = _freeze(cache, draft, season, week)
        proof = verify_bundle(draft, deep=True)
        payload = sources._json(sources._read(draft, 8 * sources.MAX_METADATA))
        if (payload.get('season'), payload.get('week'), payload.get('family')) != (season, week, 'linear'):
            raise ValueError('Weekly draft identity mismatch')
        cutoff = min(sources.identity.kickoff_utc(e['start_time']).timestamp() for e in events)
        if time.time() >= cutoff or payload['frozen_at'] >= cutoff:
            return dict(status='waiting', reason='fitting_crossed_kickoff', freeze='refused: fitting crossed kickoff')
        sources._immutable('weekly_jobs/proofs/' + proof['freeze_sha256'] + '.json',
                           sources.encoded(dict(inputs=receipts, legacy_bundle=proof)))
        def recheck():
            publication.same(seals, publication.file_seals(original_paths))
            if (sources.sha(_events(season, week)) != identity_state
                    or sources._head(season)['source_id'] != state['source_id']
                    or _schedule_head(season)['schedule_id'] != schedule['schedule_id']
                    or sources.sha(sources._known_finals(time.time())) != result_state
                    or code_commit() != payload['code_commit'] or time.time() >= cutoff):
                raise publication.PublicationDeferred('weekly_preparation_dependencies_changed')
        _publish_final(draft, output, cutoff, recheck)
        return dict(status='completed', freeze={**result, 'path': str(output)}, bundle=proof)


def status():
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_jobs'"):
        return dict(status='missing', reason='weekly_preparation_not_dispatched')
    job = db.q1("SELECT * FROM learning_jobs WHERE component='NFL_WEEKLY' ORDER BY id DESC LIMIT 1")
    return dict(status=job['state'] if job else 'missing', job=job)
