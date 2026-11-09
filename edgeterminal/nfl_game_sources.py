"""Verified, immutable NFL game inputs and a separate bounded source preparer.

The source tick dispatches only. Network, gzip parsing and aggregation execute in
the child, outside the research lock; serving reads a small sealed aggregate.
This module never changes a numerical model or an existing annual cache.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit
import uuid
from zoneinfo import ZoneInfo


def _environment(environ):
    allowed = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'TZ', 'SSL_CERT_FILE',
               'SSL_CERT_DIR', 'DATABASE_URL', 'NFLVERSE_CACHE_DIR'}
    clean = {key: value for key, value in environ.items() if key in allowed}
    # This existing flag suppresses .env loading, not network access. This is
    # deliberately not the offline learning command/runtime.
    clean.update(EDGETERMINAL_OFFLINE_LEARNER='1', PYTHONDONTWRITEBYTECODE='1')
    return clean


if __name__ == '__main__':
    _clean = _environment(os.environ)
    os.environ.clear()
    os.environ.update(_clean)

from . import db, nfl_identity as identity  # noqa: E402
from .config import settings  # noqa: E402
from .providers.nflverse import aggregate_team_features, PBP_URL  # noqa: E402

SCHEDULES_URL = 'https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv.gz'

POLICY_ID = 'nfl-game-current-inputs-v1'
AGGREGATION_ID = 'nflverse-prior-final-team-means-v1'
DIRECTORY = None
DAY = 86400
MAX_COMPRESSED = 128 * 1024 * 1024
MAX_DECOMPRESSED = 1536 * 1024 * 1024
MAX_ROWS = 250000
MAX_FIELD = 256 * 1024
MAX_SECONDS = 240
MAX_METADATA = 1024 * 1024
MAX_REDIRECTS = 3
STARTUP_GRACE_SECONDS = 30
METRICS = ('epa', 'success', 'qb_dropback', 'rush_attempt', 'yards_gained', 'cpoe',
           'pass_oe', 'no_huddle')
REQUIRED = {'game_id', 'season', 'season_type', 'week', 'home_team', 'away_team',
            'game_date', 'home_score', 'away_score', 'game_seconds_remaining',
            'qtr', 'desc', 'posteam', 'defteam', 'epa', 'success'}
ET = ZoneInfo('America/New_York')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('NFL game source JSON has duplicate keys')
        value[key] = item
    return value


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def _stamp(value=None):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('NFL game source time must be finite')
    return float(value)


def _season(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 2000 <= value <= 2100:
        raise ValueError('NFL game source season is invalid')
    return value


def _week(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 18:
        raise ValueError('NFL game source completed week is invalid')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('NFL game source requires a SHA-256 identity')
    return value


def _ancestors(path):
    for parent in reversed((path,) + tuple(path.parents)):
        if parent.is_symlink():
            raise ValueError('NFL game source symlink is forbidden')
        if parent.exists() and parent != path and not parent.is_dir():
            raise ValueError('NFL game source ancestor is not a directory')


def root():
    path = Path(DIRECTORY) if DIRECTORY is not None else settings.db_path.parent / 'research/nfl_game_sources'
    path = path.absolute()
    _ancestors(path)
    return path


def _path(relative, *, create=False):
    pure = PurePosixPath(str(relative))
    if pure.is_absolute() or any(part in ('', '.', '..') for part in str(relative).split('/')):
        raise ValueError('NFL game source path must be relative and contained')
    base = root()
    path = base.joinpath(*pure.parts)
    _ancestors(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read(path, limit):
    path = Path(path).absolute()
    _ancestors(path)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('NFL game source must be a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            body = handle.read(limit + 1)
        if len(body) > limit:
            raise ValueError('NFL game source exceeds byte limit')
        return body
    finally:
        os.close(fd)


def _immutable(relative, body):
    path = _path(relative, create=True)
    temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(temporary), str(path), follow_symlinks=False)
        except FileExistsError:
            if _read(path, max(len(body), 1)) != body:
                raise ValueError('NFL game source immutable artifact collision')
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return relative


def ensure_schema():
    statements = (
        'CREATE TABLE IF NOT EXISTS nfl_game_source_artifacts ('
        'source_id TEXT PRIMARY KEY,season INTEGER NOT NULL,completed_week INTEGER NOT NULL,'
        'original_received_at REAL NOT NULL,adopted_at REAL NOT NULL,raw_sha256 TEXT NOT NULL,'
        'aggregate_sha256 TEXT NOT NULL,descriptor TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_game_source_events ('
        'id INTEGER PRIMARY KEY,season INTEGER NOT NULL,recorded_at REAL NOT NULL,kind TEXT NOT NULL,'
        'source_id TEXT,detail TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS nfl_game_source_jobs ('
        'id INTEGER PRIMARY KEY,season INTEGER NOT NULL,completed_week INTEGER NOT NULL,'
        'requested_at REAL NOT NULL,state TEXT NOT NULL,started_at REAL,finished_at REAL,'
        'owner TEXT,pid INTEGER,detail TEXT NOT NULL)',
        "CREATE UNIQUE INDEX IF NOT EXISTS nfl_game_source_one_pending ON nfl_game_source_jobs(season) "
        "WHERE state IN ('queued','running')",
    )
    with db.tx() as con:
        for statement in statements:
            con.execute(statement)
        for table in ('nfl_game_source_artifacts', 'nfl_game_source_events'):
            for action in ('UPDATE', 'DELETE'):
                con.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} '
                            "BEGIN SELECT RAISE(ABORT,'NFL game source evidence is append-only'); END")


def _has_schema():
    names = {row['name'] for row in db.q("SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('nfl_game_source_artifacts','nfl_game_source_events','nfl_game_source_jobs')")}
    return len(names) == 3


def _event(con, season, kind, now, detail=None, source_id=None):
    con.execute('INSERT INTO nfl_game_source_events(season,recorded_at,kind,source_id,detail) VALUES(?,?,?,?,?)',
                (season, now, kind, source_id, encoded(detail or {}).decode()))


@contextmanager
def source_lease(season, blocking=False):
    """Shared with the existing weekly producer; heartbeat age cannot steal it."""
    season = _season(season)
    path = _path(f'leases/{season}.lock', create=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('NFL game source lease must be regular')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _url(url, season, *, redirect=False):
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError('NFL game source URL is invalid')
    parsed = urlsplit(url)
    base = PBP_URL.format(season=_season(season))
    valid_origin = (parsed.hostname in {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
                    if redirect else False)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443) or not (url in (base, SCHEDULES_URL) or valid_origin)):
        raise ValueError('NFL game source URL is not an allowed HTTPS asset')
    return url


class _Limited(io.RawIOBase):
    def __init__(self, stream, deadline):
        self.stream, self.deadline, self.count = stream, deadline, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        if time.monotonic() > self.deadline:
            raise TimeoutError('NFL game source preparation deadline exceeded')
        data = self.stream.read(min(len(buffer), MAX_DECOMPRESSED - self.count + 1))
        self.count += len(data)
        if self.count > MAX_DECOMPRESSED:
            raise ValueError('NFL game source decompressed byte limit exceeded')
        buffer[:len(data)] = data
        return len(data)


def _number(value, *, optional=False):
    if optional and value in (None, '', 'NA', 'None'):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('NFL game source numeric field is invalid') from exc
    if not math.isfinite(number):
        raise ValueError('NFL game source numeric field is nonfinite')
    return number


def _source_team_code(value, season):
    """Closed provider-era aliases, confined to the supported historical window.

    Raw game IDs and schedule rows use OAK in 2019 and LV from 2020. This
    resolver does not change global team identity or accept fuzzy/full-name
    legacy aliases. Canonical franchise keys remain ``lv``.
    """
    if value == 'OAK':
        return 'lv' if season == 2019 else None
    code = identity.team_code(value)
    if season == 2019 and code == 'lv':
        return None  # A modern Raiders source spelling is not a 2019 identity.
    return code


def _pbp_team_code(value, season, game_match):
    # The observed nflverse 2019 PBP keeps OAK in game_id but normalizes its
    # team columns to LV. This field-specific representation is allowed only
    # under that exact historical game identity, never in schedule/raw IDs.
    if (value == 'LV' and season == 2019 and game_match and game_match[1] == '2019'
            and 'OAK' in (game_match[3], game_match[4])):
        return 'lv'
    return _source_team_code(value, season)


def _game_key(value):
    match = re.fullmatch(r'(\d{4})_(\d{2})_([A-Z]{2,3})_([A-Z]{2,3})', str(value))
    if match:
        season, week, away, home = match.groups()
        away, home = _source_team_code(away, int(season)), _source_team_code(home, int(season))
    else:
        match = re.fullmatch(r'nfl-(\d{4})-reg-(\d{2})-([a-z]{2,3})-([a-z]{2,3})', str(value))
        if not match:
            raise ValueError('NFL game source expected-game identity is invalid')
        season, week, away, home = match.groups()
        # Canonical IDs use stable franchise codes; only raw provider fields
        # carry the historical city abbreviation contract above.
        away, home = identity.team_code(away), identity.team_code(home)
    if not away or not home or away == home:
        raise ValueError('NFL game source expected-game team is invalid')
    return int(season), int(week), away, home


def _parse(raw, season, completed_week, at, *, expected_games=None, expected_scores=None):
    """Verify every relevant row before selecting completed prior-game inputs."""
    deadline = time.monotonic() + MAX_SECONDS
    groups = {}
    old_limit = csv.field_size_limit(MAX_FIELD)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            text = io.TextIOWrapper(io.BufferedReader(_Limited(compressed, deadline)), encoding='utf-8-sig', newline='')
            reader = csv.DictReader(text)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not REQUIRED <= set(fields):
                raise ValueError('NFL game source CSV identity columns are missing or duplicated')
            for index, row in enumerate(reader, 1):
                if index > MAX_ROWS:
                    raise ValueError('NFL game source CSV row limit exceeded')
                if time.monotonic() > deadline:
                    raise TimeoutError('NFL game source preparation deadline exceeded')
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('NFL game source CSV row shape mismatch')
                match = re.fullmatch(r'(\d{4})_(\d{2})_([A-Z]{2,3})_([A-Z]{2,3})', row['game_id'])
                try:
                    row_season, week = int(row['season']), int(row['week'])
                    date = datetime.strptime(row['game_date'], '%Y-%m-%d').date().isoformat()
                except (TypeError, ValueError) as exc:
                    raise ValueError('NFL game source season/week/date is invalid') from exc
                kind = row['season_type']
                home_code = _pbp_team_code(row['home_team'], row_season, match)
                away_code = _pbp_team_code(row['away_team'], row_season, match)
                home, away = identity.canonical_team(home_code), identity.canonical_team(away_code)
                if (not match or row_season != season or int(match[1]) != season or int(match[2]) != week
                        or not 1 <= week <= 22 or kind not in {'REG', 'POST', 'PRE'} or not home or not away or home == away
                        or _source_team_code(match[3], row_season) != away_code
                        or _source_team_code(match[4], row_season) != home_code):
                    raise ValueError('NFL game source row identity conflict')
                hs, aws = _number(row['home_score']), _number(row['away_score'])
                if hs != int(hs) or aws != int(aws) or not 0 <= hs <= 100 or not 0 <= aws <= 100:
                    raise ValueError('NFL game source final score is invalid')
                remaining = _number(row['game_seconds_remaining'], optional=True)
                if remaining is not None and not 0 <= remaining <= 3600:
                    raise ValueError('NFL game source clock is invalid')
                values = {key: _number(row.get(key), optional=True) for key in METRICS}
                for key in ('success', 'qb_dropback', 'rush_attempt', 'no_huddle'):
                    if values[key] not in (None, 0., 1.):
                        raise ValueError('NFL game source indicator is invalid')
                historical_raiders = row_season == 2019 and 'OAK' in (match[3], match[4])
                raiders_spelling = (row['home_team'] if home_code == 'lv' else row['away_team']) if historical_raiders else None
                for key in ('posteam', 'defteam'):
                    if row.get(key) not in (None, '', 'NA', 'None'):
                        play_code = _pbp_team_code(row[key], row_season, match)
                        if play_code not in (home_code, away_code) or (
                                historical_raiders and play_code == 'lv' and row[key] != raiders_spelling):
                            raise ValueError('NFL game source play team identity conflict')
                info = dict(game_id=row['game_id'], week=week, season_type=kind, home=home, away=away,
                            date=date, home_score=int(hs), away_score=int(aws))
                group = groups.setdefault(row['game_id'], dict(info=info, ended=False, rows=[],
                                                               raiders_spelling=raiders_spelling))
                if group['info'] != info or group['raiders_spelling'] != raiders_spelling:
                    raise ValueError('NFL game source same-game score or identity conflict')
                if str(row['desc']).strip().upper() == 'END GAME':
                    quarter = _number(row['qtr'])
                    # A genuine sudden-death overtime final has an END GAME
                    # marker while the OT clock still has time remaining.
                    # Postseason periods are 15 minutes; REG/PRE stay at 10.
                    overtime_limit = 900 if kind == 'POST' else 600
                    if quarter != int(quarter) or quarter < 4 or quarter > 10 or (
                            remaining != 0 and (quarter < 5 or remaining is None or remaining > overtime_limit)):
                        raise ValueError('NFL game source final clock conflicts with END GAME')
                    group['ended'] = True
                if row.get('posteam') not in (None, '', 'NA', 'None'):
                    play = {key: row.get(key, '') for key in ('game_id', 'posteam') + METRICS}
                    if row_season == 2019 and play['posteam'] == 'OAK':
                        # The unchanged legacy aggregator otherwise leaves an
                        # unknown OAK key. Adapt only this verified in-memory
                        # input; original IDs, bytes and all metrics stay intact.
                        play['posteam'] = 'LV'
                    group['rows'].append(play)
    except (gzip.BadGzipFile, EOFError, UnicodeError, csv.Error) as exc:
        raise ValueError('NFL game source gzip/CSV is invalid') from exc
    finally:
        csv.field_size_limit(old_limit)
    today = datetime.fromtimestamp(at, ET).date().isoformat()
    selected = [g for g in groups.values() if g['info']['season_type'] == 'REG'
                and g['info']['week'] <= completed_week and g['info']['date'] < today and g['ended']]
    games = sorted((g['info'] for g in selected), key=lambda g: g['game_id'])
    chosen = {_game_key(g['game_id']) for g in games}
    expected = set(expected_games or ())
    expected_keys = {_game_key(gid): gid for gid in expected}
    for game_id in expected:
        key = _game_key(game_id)
        if key[0] != season or key[1] > completed_week:
            raise ValueError('NFL game source expected-game week conflict')
    missing = sorted(expected_keys[key] for key in expected_keys.keys() - chosen)
    actual = {_game_key(game['game_id']): game for game in games}
    for gid, result in (expected_scores or {}).items():
        key = _game_key(gid)
        if key[0] != season or key[1] > completed_week or _stamp(result['received_at']) > at:
            raise ValueError('NFL game source official final has invalid season/week/receipt')
        _digest(result['payload_sha256'])
        if key in actual and (actual[key]['home_score'], actual[key]['away_score']) != (
                result['home_score'], result['away_score']):
            raise ValueError('NFL game source final score conflicts with verified official result')
    complete = bool(games and max(g['week'] for g in games) == completed_week
                    and not missing
                    and all(g['ended'] for g in groups.values()
                            if g['info']['season_type'] == 'REG' and g['info']['week'] <= completed_week))
    rows = (row for group in selected for row in group['rows'])
    stats = aggregate_team_features(rows, season, retrieved_at=at)
    teams = {}
    for team, values in stats.items():
        ids = sorted(g['game_id'] for g in games if team in (g['home'], g['away']))
        if values['games'] != len(ids):
            raise ValueError('NFL game source aggregate play/game coverage conflict')
        teams[team] = dict(games=values['games'], plays=values['plays'], season=season, selected_game_ids=ids)
    return dict(schema=1, aggregation_id=AGGREGATION_ID, season=season, completed_week=completed_week,
                games=games, teams=teams, stats=stats, complete=complete,
                missing_game_ids=missing, parsed_games=len(groups))


def _metadata(path, season, now):
    meta = _json(_read(path, MAX_METADATA))
    if not isinstance(meta, dict) or meta.get('license') != 'CC-BY-4.0':
        raise ValueError('NFL game source metadata/license is invalid')
    _url(meta.get('source_url', ''), season)
    if meta['source_url'] != PBP_URL.format(season=season):
        raise ValueError('NFL PBP source metadata has the wrong endpoint')
    _digest(meta.get('sha256'))
    if meta.get('retrieved_at') is None or not 0 < _stamp(meta['retrieved_at']) <= now:
        raise ValueError('NFL game source receipt is after adoption')
    return meta


def _head(season):
    return db.q1("SELECT a.*,e.id AS publication_event_id,e.recorded_at AS published_at "
                 "FROM nfl_game_source_events e JOIN nfl_game_source_artifacts a "
                 "ON a.source_id=e.source_id WHERE e.season=? AND e.kind='published' ORDER BY e.id DESC LIMIT 1",
                 (season,))


def read_manifest(source_id):
    _digest(source_id)
    if not _has_schema():
        raise ValueError('NFL game source schema is not initialized')
    row = db.q1('SELECT descriptor FROM nfl_game_source_artifacts WHERE source_id=?', (source_id,))
    if not row:
        raise ValueError('NFL game source descriptor is unavailable')
    raw = _read(_path('manifests/' + source_id + '.json'), MAX_METADATA)
    if hashlib.sha256(raw).hexdigest() != source_id or raw != row['descriptor'].encode():
        raise ValueError('NFL game source descriptor hash conflict')
    result = _json(raw)
    if result.get('schema') != 1 or result.get('policy_id') != POLICY_ID or result.get('aggregation_id') != AGGREGATION_ID:
        raise ValueError('NFL game source descriptor contract mismatch')
    return result


def _aggregate(descriptor, *, deep=False, proof_cache=None):
    raw = _read(_path(descriptor['aggregate_path']), 4 * MAX_METADATA)
    if hashlib.sha256(raw).hexdigest() != descriptor['aggregate_sha256']:
        raise ValueError('NFL game source aggregate hash conflict')
    value = _json(raw)
    if (value.get('aggregation_id') != AGGREGATION_ID or value.get('season') != descriptor['season']
            or value.get('completed_week') != descriptor['completed_week']):
        raise ValueError('NFL game source aggregate identity conflict')
    if deep:
        paths = [_path(descriptor['raw_path']), _path(descriptor['aggregate_path'])]
        seals = []
        for path in paths:
            _ancestors(path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('NFL game source proof requires regular files')
            seals.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        key = sha(descriptor)
        if proof_cache is not None and key in proof_cache:
            if proof_cache[key] != seals:
                raise ValueError('NFL game source changed during verified replay')
            return value
        body = _read(paths[0], MAX_COMPRESSED)
        if hashlib.sha256(body).hexdigest() != descriptor['raw_sha256']:
            raise ValueError('NFL game source raw hash conflict')
        replay = _parse(body, descriptor['season'], descriptor['completed_week'], descriptor['adopted_at'],
                        expected_games=descriptor['expected_game_ids'], expected_scores=descriptor['expected_scores'])
        if replay != value:
            raise ValueError('NFL game source aggregation replay conflict')
        if proof_cache is not None:
            if len(proof_cache) >= 16:
                proof_cache.pop(next(iter(proof_cache)))
            proof_cache[key] = seals
    return value


def _publish_prepared(source_id, descriptor, aggregate, *, check_current_state=False,
                       publication_wait_seconds=0):
    """Wait only in the independent child; the main source worker never calls this."""
    from .learning_publication import publish_lock, PublicationDeferred
    wait = _stamp(publication_wait_seconds)
    if not 0 <= wait <= MAX_SECONDS:
        raise ValueError('NFL game source publication wait is out of bounds')
    deadline = time.monotonic() + wait
    season, completed_week = descriptor['season'], descriptor['completed_week']
    previous = _head(season)
    while True:
        try:
            with publish_lock(allow_near=True):
                with db.tx() as con:
                    if check_current_state:
                        physical = time.time()
                        targets = maintenance_targets(physical)
                        current_scores = {gid: row for gid, row in _known_finals(physical).items()
                                          if _game_key(gid)[0] == season and _game_key(gid)[1] <= completed_week}
                        if (not any(t['season'] == season and t['completed_week'] >= completed_week for t in targets)
                                or sorted(_known_expected(season, completed_week)) != descriptor['expected_game_ids']
                                or current_scores != descriptor['expected_scores']):
                            raise PublicationDeferred('NFL game source schedule/result heads changed before publication')
                    current = _head(season)
                    if (current or {}).get('source_id') != (previous or {}).get('source_id'):
                        raise PublicationDeferred('NFL game source head changed before publication')
                    can_publish = aggregate['complete'] and (not current or (
                        completed_week >= current['completed_week'] and
                        descriptor['original_received_at'] >= current['original_received_at']))
                    if can_publish and (current or {}).get('source_id') != source_id:
                        _event(con, season, 'published', time.time(), dict(complete=True), source_id)
                    elif not can_publish:
                        _event(con, season, 'pending', time.time(), dict(complete=aggregate['complete'],
                               missing_game_ids=aggregate['missing_game_ids']), source_id)
            return dict(status='ready' if can_publish else 'pending', source_id=source_id,
                        reason=None if can_publish else 'awaiting_completed_week_coverage')
        except PublicationDeferred as exc:
            if str(exc) != 'refresh_busy' or time.monotonic() >= deadline:
                if str(exc) == 'refresh_busy':
                    exc.prepared_source_id = source_id
                raise
            time.sleep(min(1., max(.01, deadline - time.monotonic())))


def resume_prepared(source_id, *, check_current_state=True, publication_wait_seconds=MAX_SECONDS):
    """Recover an already verified preparation without another parse or request."""
    descriptor = read_manifest(source_id)
    aggregate = _aggregate(descriptor)
    raw = _read(_path(descriptor['raw_path']), MAX_COMPRESSED)
    if hashlib.sha256(raw).hexdigest() != descriptor['raw_sha256']:
        raise ValueError('NFL prepared source raw hash conflict')
    return _publish_prepared(source_id, descriptor, aggregate, check_current_state=check_current_state,
                             publication_wait_seconds=publication_wait_seconds)


def adopt_archive(path, metadata_path, *, season, completed_week, now=None, expected_games=None,
                  expected_scores=None, check_current_state=False, publication_wait_seconds=0):
    """Explicit worker-only adoption; old archives and their metadata are untouched."""
    season, completed_week, at = _season(season), _week(completed_week), _stamp(now)
    if at > time.time() + 1:
        raise ValueError('NFL game source adoption cannot use a future clock')
    ensure_schema()
    meta = _metadata(metadata_path, season, at)
    raw = _read(path, MAX_COMPRESSED)
    if hashlib.sha256(raw).hexdigest() != meta['sha256']:
        raise ValueError('NFL game source raw hash differs from metadata')
    expected = sorted(set(expected_games or ()))
    old_rows = db.q('SELECT source_id FROM nfl_game_source_artifacts WHERE season=? AND completed_week=? '
                    'AND raw_sha256=? ORDER BY adopted_at DESC', (season, completed_week, meta['sha256']))
    for old in old_rows:
        descriptor = read_manifest(old['source_id'])
        if descriptor['expected_game_ids'] == expected and descriptor['expected_scores'] == (expected_scores or {}):
            aggregate = _aggregate(descriptor, deep=True)
            return _publish_prepared(old['source_id'], descriptor, aggregate,
                check_current_state=check_current_state, publication_wait_seconds=publication_wait_seconds)
    aggregate = _parse(raw, season, completed_week, at, expected_games=expected, expected_scores=expected_scores)
    aggregate_raw = encoded(aggregate)
    aggregate_sha = hashlib.sha256(aggregate_raw).hexdigest()
    descriptor = dict(schema=1, policy_id=POLICY_ID, aggregation_id=AGGREGATION_ID,
                      season=season, completed_week=completed_week,
                      source_url=meta['source_url'], original_received_at=meta['retrieved_at'], adopted_at=at,
                      raw_sha256=meta['sha256'], raw_path='raw/' + meta['sha256'] + '.csv.gz',
                      aggregate_sha256=aggregate_sha, aggregate_path='aggregates/' + aggregate_sha + '.json',
                      expected_game_ids=expected, expected_scores=expected_scores or {}, complete=aggregate['complete'])
    source_id = sha(descriptor)
    _immutable(descriptor['raw_path'], raw)
    _immutable(descriptor['aggregate_path'], aggregate_raw)
    _immutable('manifests/' + source_id + '.json', encoded(descriptor))
    # Prepared evidence is durable but not served. Only a later 'published'
    # event changes the pointer; no global source lock is needed for this row.
    with db.tx() as con:
        con.execute('INSERT OR IGNORE INTO nfl_game_source_artifacts VALUES(?,?,?,?,?,?,?,?)',
                    (source_id, season, completed_week, meta['retrieved_at'], at,
                     meta['sha256'], aggregate_sha, encoded(descriptor).decode()))
        _event(con, season, 'prepared', at, dict(complete=aggregate['complete']), source_id)
    return _publish_prepared(source_id, descriptor, aggregate,
        check_current_state=check_current_state, publication_wait_seconds=publication_wait_seconds)


def _event_identity(event):
    canonical = identity.canonical_id(event)
    if not canonical or canonical != event.get('id') or canonical.split('-')[2] != 'reg':
        raise ValueError('NFL game input target identity must be canonical regular season')
    parts = canonical.split('-')
    return canonical, int(parts[1]), int(parts[3])


def prepared_stats(event, now=None):
    """Cheap saved-input read. No archive scan, provider work or model fitting."""
    at = _stamp(now)
    game_id, season, week = _event_identity(event)
    if not _has_schema():
        return dict(status='missing', stats={}, input_map=None, reason='awaiting_verified_game_source')
    head = _head(season)
    if not head:
        return dict(status='missing', stats={}, input_map=None, reason='awaiting_verified_game_source')
    descriptor = read_manifest(head['source_id'])
    if max(descriptor['adopted_at'], descriptor['original_received_at'], head['published_at']) > at or descriptor['completed_week'] >= week:
        return dict(status='missing', stats={}, input_map=None, reason='source_not_prior_to_target_asof')
    aggregate = _aggregate(descriptor)
    target_teams = [identity.canonical_team(event['home_team']), identity.canonical_team(event['away_team'])]
    if not aggregate['complete'] or any(team not in aggregate['stats'] for team in target_teams):
        return dict(status='missing', stats={}, input_map=None, reason='awaiting_target_team_coverage')
    stats = {}
    for team in target_teams:
        row = aggregate['stats'][team]
        stats[team] = {key: value for key, value in row.items()
                       if key not in ('games', 'plays', 'season', 'source', 'retrieved_at')}
        stats[team].update(feature_games=row['games'], feature_plays=row['plays'], feature_season=season,
                           prior_season_features=False, feature_source='nflverse-pbp',
                           feature_retrieved_at=max(descriptor['original_received_at'], descriptor['adopted_at'],
                                                    head['published_at']))
    input_map = {key: descriptor[key] for key in ('policy_id', 'season', 'completed_week', 'raw_sha256',
                 'aggregate_sha256', 'original_received_at', 'adopted_at', 'source_url')}
    input_map.update(schema=1, source_id=head['source_id'], as_of=at, game_id=game_id,
                     target_week=week, published_at=head['published_at'],
                     publication_event_id=head['publication_event_id'],
                     teams={team: aggregate['teams'][team] for team in target_teams})
    input_map['sha256'] = sha(input_map)
    return dict(status='ready', stats=stats, input_map=input_map, reason=None)


def verify_input_map(input_map, event, as_of, *, deep=True, proof_cache=None):
    """Reproduce a saved map by contained artifact identity, never by basename."""
    if not isinstance(input_map, dict):
        raise ValueError('NFL game input map is absent')
    saved = dict(input_map)
    digest = saved.pop('sha256', None)
    if _digest(digest) != sha(saved):
        raise ValueError('NFL game input map hash conflict')
    game_id, season, week = _event_identity(event)
    at = _stamp(as_of)
    if (saved.get('schema') != 1 or saved.get('policy_id') != POLICY_ID or saved.get('game_id') != game_id
            or saved.get('as_of') != at or saved.get('season') != season or saved.get('target_week') != week):
        raise ValueError('NFL game input map identity/as-of conflict')
    descriptor = read_manifest(saved['source_id'])
    publication = db.q1("SELECT * FROM nfl_game_source_events WHERE id=? AND kind='published'",
                         (saved.get('publication_event_id'),))
    if (not publication or publication['source_id'] != saved['source_id']
            or publication['recorded_at'] != saved.get('published_at') or publication['recorded_at'] > at):
        raise ValueError('NFL game input map publication receipt conflict')
    for key in ('policy_id', 'season', 'completed_week', 'raw_sha256', 'aggregate_sha256',
                'original_received_at', 'adopted_at', 'source_url'):
        if saved.get(key) != descriptor.get(key):
            raise ValueError('NFL game input map source identity conflict')
    if max(descriptor['adopted_at'], descriptor['original_received_at']) > at or descriptor['completed_week'] >= week:
        raise ValueError('NFL game input map contains future or target evidence')
    aggregate = _aggregate(descriptor, deep=deep, proof_cache=proof_cache)
    teams = [identity.canonical_team(event['home_team']), identity.canonical_team(event['away_team'])]
    if not aggregate['complete'] or saved['teams'] != {team: aggregate['teams'][team] for team in teams}:
        raise ValueError('NFL game input map team coverage conflict')
    # Reuse the same transformation without relying on whichever source is current now.
    stats = {}
    for team in teams:
        row = aggregate['stats'][team]
        stats[team] = {key: value for key, value in row.items()
                       if key not in ('games', 'plays', 'season', 'source', 'retrieved_at')}
        stats[team].update(feature_games=row['games'], feature_plays=row['plays'], feature_season=season,
                           prior_season_features=False, feature_source='nflverse-pbp',
                           feature_retrieved_at=max(descriptor['adopted_at'], descriptor['original_received_at'],
                                                    publication['recorded_at']))
    return dict(status='ready', stats=stats, input_map=input_map, reason=None)


def input_paths(input_map):
    """Exact contained files for a fast metadata recheck after deep verification."""
    descriptor = read_manifest(input_map['source_id'])
    return tuple(_path(name) for name in ('manifests/' + input_map['source_id'] + '.json',
                                         descriptor['raw_path'], descriptor['aggregate_path']))


def status(season=None):
    if not _has_schema():
        return dict(status='missing', initialized=False, source_id=None, job=None,
                    policy_id=POLICY_ID, served_head=dict(status='missing', source_id=None),
                    reason='source_maintenance_not_initialized')
    if season is None:
        row = db.q1('SELECT season FROM nfl_game_source_jobs UNION SELECT season FROM nfl_game_source_artifacts '
                     'ORDER BY season DESC LIMIT 1')
        season = row['season'] if row else None
    if season is None:
        return dict(status='missing', initialized=True, source_id=None, job=None, policy_id=POLICY_ID,
                    served_head=dict(status='missing', source_id=None), reason='awaiting_verified_game_source')
    _season(season)
    head = _head(season)
    job = db.q1('SELECT * FROM nfl_game_source_jobs WHERE season=? ORDER BY id DESC LIMIT 1', (season,))
    event = db.q1('SELECT * FROM nfl_game_source_events WHERE season=? ORDER BY id DESC LIMIT 1', (season,))
    served = dict(status='ready' if head else 'missing', source_id=head['source_id'] if head else None,
                  completed_week=head['completed_week'] if head else None,
                  published_at=head['published_at'] if head else None)
    state, reason = served['status'], None if head else 'awaiting_verified_game_source'
    detail = _json(job['detail']) if job else {}
    if job and job['state'] == 'failed':
        state, reason = 'error', detail.get('reason', 'source_maintenance_failed')
    elif job and (job['state'] in ('queued', 'running', 'deferred') or detail.get('status') == 'pending'):
        state, reason = 'pending', detail.get('reason', 'source_maintenance_' + job['state'])
    return dict(status=state, initialized=True, reason=reason, served_head=served,
                source_id=head['source_id'] if head else None,
                season=season, completed_week=head['completed_week'] if head else None,
                original_received_at=head['original_received_at'] if head else None,
                adopted_at=head['adopted_at'] if head else None, policy_id=POLICY_ID,
                job=job, latest_event=event)


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def dispatch_maintenance(season, completed_week, now=None, *, launch=True):
    """Cheap durable enqueue; no network, source scan, or blocking subprocess wait."""
    season, completed_week, at = _season(season), _week(completed_week), _stamp(now)
    ensure_schema()
    targets = maintenance_targets(at)
    if not any(target['season'] == season and target['completed_week'] >= completed_week for target in targets):
        return dict(status='waiting', job_id=None, reason='awaiting_canonical_completed_week')
    with source_lease(season) as available:
        if not available:
            job = db.q1("SELECT * FROM nfl_game_source_jobs WHERE season=? AND state IN ('queued','running')",
                         (season,))
            return dict(status=job['state'] if job else 'deferred', job_id=job['id'] if job else None,
                        reason='source_lease_busy')
        with db.tx() as con:
            con.execute('BEGIN IMMEDIATE')
            job = con.execute("SELECT * FROM nfl_game_source_jobs WHERE season=? AND state IN ('queued','running')",
                              (season,)).fetchone()
            if job:
                job = dict(job)
                detail = _json(job['detail'])
                age = at - detail.get('launch_requested_at', -DAY)
                startup = (job['state'] == 'queued' and detail.get('startup_nonce')
                           and 0 <= age < STARTUP_GRACE_SECONDS and job['pid'] and _alive(job['pid']))
                if startup:
                    return dict(status='queued', job_id=job['id'])
                # We own the free OS lease. A recycled live PID is not the
                # source owner and cannot suppress recovery indefinitely.
                if job['state'] == 'running' or job['pid']:
                    con.execute("UPDATE nfl_game_source_jobs SET state='queued',owner=NULL,pid=NULL WHERE id=?", (job['id'],))
                    _event(con, season, 'recovered', at, dict(job_id=job['id'], authority='free_source_lease'))
                    job['state'] = 'queued'
            else:
                last = con.execute('SELECT * FROM nfl_game_source_jobs WHERE season=? ORDER BY id DESC LIMIT 1',
                                   (season,)).fetchone()
                cooldown = 300 if last and last['state'] in ('failed', 'deferred') else DAY
                if last and at < last['requested_at'] + cooldown and completed_week <= last['completed_week']:
                    return dict(status=last['state'], job_id=last['id'], next_due_at=last['requested_at'] + cooldown)
                prior = _json(last['detail']) if last and last['completed_week'] == completed_week else {}
                checkpoint = ({'prepared_source_id': prior['prepared_source_id']}
                              if prior.get('prepared_source_id') and last['state'] == 'deferred' else {})
                cur = con.execute("INSERT INTO nfl_game_source_jobs(season,completed_week,requested_at,state,detail) "
                                  "VALUES(?,?,?,'queued',?)", (season, completed_week, at, encoded(checkpoint).decode()))
                job = dict(con.execute('SELECT * FROM nfl_game_source_jobs WHERE id=?', (cur.lastrowid,)).fetchone())
                _event(con, season, 'queued', at, dict(job_id=job['id'], completed_week=completed_week))
            if launch:
                detail = _json(job['detail'])
                nonce = uuid.uuid4().hex
                detail.update(startup_nonce=nonce, launch_requested_at=at)
                con.execute('UPDATE nfl_game_source_jobs SET detail=? WHERE id=?', (encoded(detail).decode(), job['id']))
        if launch:
            env = _environment(os.environ)
            env['DATABASE_URL'] = 'sqlite:///' + str(settings.db_path)
            env['NFLVERSE_CACHE_DIR'] = str(settings.nflverse_cache_dir)
            process = subprocess.Popen([sys.executable, '-m', 'edgeterminal.nfl_game_sources', 'run',
                                        '--job-id', str(job['id']), '--startup-nonce', nonce],
                                       cwd=str(Path(__file__).resolve().parents[1]),
                                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False, close_fds=True, start_new_session=True)
            db.ex("UPDATE nfl_game_source_jobs SET pid=? WHERE id=? AND state='queued'", (process.pid, job['id']))
    return dict(status='queued', job_id=job['id'])


def _local_candidates(season, now):
    cache = settings.nflverse_cache_dir.absolute()
    candidates = []
    for directory in (cache.parent / f'forward_{season}', cache):
        archive = directory / f'play_by_play_{season}.csv.gz'
        metadata = directory / f'play_by_play_{season}.metadata.json'
        if archive.exists() or metadata.exists():
            if not archive.exists() or not metadata.exists():
                raise ValueError('NFL game source archive/metadata pair is incomplete')
            info = _metadata(metadata, season, now)
            candidates.append((info['retrieved_at'], archive, metadata))
    return sorted(candidates, reverse=True)


def _download(season, *, client=None, asset_url=None):
    """Bounded public endpoint retrieval; the caller records the daily attempt first."""
    import httpx
    url = _url(asset_url or PBP_URL.format(season=season), season)
    if url not in (PBP_URL.format(season=season), SCHEDULES_URL):
        raise ValueError('NFL source download must start at its fixed endpoint')
    owned = client is None
    client = httpx.Client(timeout=30., follow_redirects=False, trust_env=False) if owned else client
    start = time.monotonic()
    try:
        for redirect in range(MAX_REDIRECTS + 1):
            with client.stream('GET', url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if redirect == MAX_REDIRECTS:
                        raise ValueError('NFL game source redirect limit exceeded')
                    url = _url(urljoin(url, response.headers.get('location', '')), season, redirect=True)
                    continue
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError('NFL game source requires a complete HTTP 200 response')
                length = response.headers.get('content-length')
                if length and (not length.isdigit() or int(length) > MAX_COMPRESSED):
                    raise ValueError('NFL game source compressed byte limit exceeded')
                chunks, size = [], 0
                for chunk in response.iter_raw(chunk_size=64 * 1024):
                    if time.monotonic() - start > MAX_SECONDS:
                        raise TimeoutError('NFL game source retrieval deadline exceeded')
                    size += len(chunk)
                    if size > MAX_COMPRESSED:
                        raise ValueError('NFL game source compressed byte limit exceeded')
                    chunks.append(chunk)
                body = b''.join(chunks)
                if not body:
                    raise ValueError('NFL game source response is empty')
                return body
        raise ValueError('NFL game source redirect limit exceeded')
    finally:
        if owned:
            client.close()


def _known_expected(season, completed_week):
    """Known canonical schedule identities are a minimum coverage check, not an invented full schedule."""
    expected = []
    for row in db.q("SELECT * FROM events WHERE sport='NFL'"):
        try:
            gid, year, week = _event_identity(row)
        except ValueError:
            continue
        if year == season and week <= completed_week:
            parts = gid.split('-')
            expected.append(gid)
    return expected


def _known_finals(now):
    """Read the existing sourced, append-only final records; never grade or fetch."""
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfl_research_grades'"):
        return {}
    result = {}
    for row in db.q('SELECT * FROM nfl_research_grades WHERE source_retrieved_at<=? '
                    'ORDER BY source_retrieved_at,revision,freeze_sha', (now,)):
        value = _json(row['payload'])
        key = _game_key(row['game_id'])
        request = value.get('result_request') or {}
        if (value.get('freeze_sha256') != row['freeze_sha'] or value.get('game_id') != row['game_id']
                or value.get('final_home_score') != row['home_score']
                or value.get('final_away_score') != row['away_score']
                or value.get('result_provider') != 'espn'
                or row['result_source'] != 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
                or value.get('result_source') != row['result_source']
                or value.get('result_retrieved_at') != row['source_retrieved_at']
                or request != dict(season=key[0], season_type=2, week=key[1])
                or identity.team_code(value.get('home_team')) != key[3]
                or identity.team_code(value.get('away_team')) != key[2]
                or _stamp(value.get('kickoff_utc')) >= row['source_retrieved_at']):
            raise ValueError('NFL game source official final evidence conflict')
        gid = f'nfl-{key[0]}-reg-{key[1]:02d}-{key[2]}-{key[3]}'
        entry = dict(home_score=row['home_score'], away_score=row['away_score'],
                     received_at=row['source_retrieved_at'], payload_sha256=hashlib.sha256(row['payload'].encode()).hexdigest())
        if gid in result and result[gid]['received_at'] == entry['received_at'] and (
                result[gid]['home_score'], result[gid]['away_score']) != (entry['home_score'], entry['away_score']):
            raise ValueError('NFL game source equal-clock official final conflict')
        result[gid] = entry
    return result


def _prepare_weekly_schedule(season, completed_week, now, *, allow_network=True):
    from . import nfl_weekly_preparation
    return nfl_weekly_preparation.prepare_schedule(season, completed_week, now=now,
                                                  allow_network=allow_network, lease_owned=True)


def run_job(job_id, *, allow_network=True, client=None, startup_nonce=None):
    """Source-maintenance child. Never called synchronously by the source tick."""
    ensure_schema()
    job = db.q1('SELECT * FROM nfl_game_source_jobs WHERE id=?', (job_id,))
    if not job:
        raise ValueError('NFL game source job is unavailable')
    season, week = _season(job['season']), _week(job['completed_week'])
    if job['state'] not in ('queued', 'running'):
        return _json(job['detail'])
    owner = uuid.uuid4().hex
    with source_lease(season) as acquired:
        if not acquired:
            return dict(status='deferred', reason='source_lease_busy')
        job = db.q1('SELECT * FROM nfl_game_source_jobs WHERE id=?', (job_id,))
        if startup_nonce is not None and _json(job['detail']).get('startup_nonce') != startup_nonce:
            raise ValueError('NFL game source startup nonce is stale')
        at = time.time()
        db.ex("UPDATE nfl_game_source_jobs SET state='running',started_at=?,owner=?,pid=? WHERE id=?",
              (at, owner, os.getpid(), job_id))
        try:
            checkpoint = _json(job['detail']).get('prepared_source_id')
            result = resume_prepared(checkpoint) if checkpoint else None
            candidates = _local_candidates(season, at)
            expected = _known_expected(season, week)
            finals = {gid: row for gid, row in _known_finals(at).items()
                      if _game_key(gid)[0] == season and _game_key(gid)[1] <= week}
            for _, path, metadata in candidates:
                if result and result['status'] == 'ready':
                    break
                candidate = adopt_archive(path, metadata, season=season, completed_week=week, now=at,
                    expected_games=expected, expected_scores=finals, check_current_state=True,
                    publication_wait_seconds=MAX_SECONDS)
                result = candidate
                if candidate['status'] == 'ready':
                    break
            if not result or result['status'] != 'ready':
                last = db.q1("SELECT recorded_at FROM nfl_game_source_events WHERE season=? AND kind='network_attempt' "
                              'ORDER BY id DESC LIMIT 1', (season,))
                if allow_network and (not last or at >= last['recorded_at'] + DAY):
                    with db.tx() as con:
                        _event(con, season, 'network_attempt', at, dict(job_id=job_id))
                    raw = _download(season, client=client)
                    received = time.time()
                    digest = hashlib.sha256(raw).hexdigest()
                    # Keep a genuine incomplete response and its receipt without
                    # publishing it as current. Never overwrite a source cache.
                    path = _path(_immutable('downloads/' + digest + '.csv.gz', raw))
                    meta = dict(source_url=PBP_URL.format(season=season), retrieved_at=received,
                                sha256=digest, license='CC-BY-4.0')
                    metadata = _path(_immutable('receipts/' + sha(meta) + '.json', encoded(meta)))
                    result = adopt_archive(path, metadata, season=season, completed_week=week, now=received,
                        expected_games=expected, expected_scores=finals, check_current_state=True,
                        publication_wait_seconds=MAX_SECONDS)
                else:
                    result = dict(status='pending', reason='awaiting_completed_week_coverage',
                                  next_due_at=last['recorded_at'] + DAY if last else at + DAY)
            schedule = _prepare_weekly_schedule(season, week, time.time(), allow_network=allow_network)
            result['schedule'] = schedule
            if schedule['status'] != 'ready' and result['status'] == 'ready':
                result.update(status='pending', reason='weekly_schedule_maintenance_pending')
            state = 'completed'
        except Exception as exc:
            from .learning_publication import PublicationDeferred
            state = 'deferred' if isinstance(exc, PublicationDeferred) else 'failed'
            result = dict(status=state, reason=str(exc)[:250], error_type=type(exc).__name__)
            if getattr(exc, 'prepared_source_id', None):
                result['prepared_source_id'] = exc.prepared_source_id
        finished = time.time()
        with db.tx() as con:
            con.execute('UPDATE nfl_game_source_jobs SET state=?,finished_at=?,detail=? WHERE id=? AND owner=?',
                        (state, finished, encoded(result).decode(), job_id, owner))
            _event(con, season, state, finished, dict(job_id=job_id, result=result), result.get('source_id'))
        return result


def maintenance_targets(now=None):
    """Cheap canonical schedule view. No provider request or raw-source scan."""
    at = _stamp(now)
    finals = _known_finals(at)
    grouped = {}
    for event in db.q("SELECT * FROM events WHERE sport='NFL'"):
        try:
            gid, season, week = _event_identity(event)
        except ValueError:
            continue
        kickoff = identity.kickoff_utc(event.get('start_time'))
        grouped.setdefault((season, week), []).append(bool(kickoff and kickoff.timestamp() < at and
            (gid in finals or str(event.get('status', '')).upper() in {'STATUS_FINAL', 'FINAL'})))
    result = {}
    for (season, week), finals in grouped.items():
        if finals and all(finals):
            result[season] = max(week, result.get(season, 0))
    return [dict(season=season, completed_week=week) for season, week in sorted(result.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'status'))
    parser.add_argument('--job-id', type=int)
    parser.add_argument('--startup-nonce')
    args = parser.parse_args()
    if args.action == 'run':
        if args.job_id is None or args.startup_nonce is None:
            parser.error('run requires --job-id and --startup-nonce')
        print(json.dumps(run_job(args.job_id, startup_nonce=args.startup_nonce), sort_keys=True))
    else:
        print(json.dumps(status(), sort_keys=True))


if __name__ == '__main__':
    main()
