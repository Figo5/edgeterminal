"""Short publication locks and metadata guards for separately fitted models.

Evidence is hashed/replayed outside the research and SQLite writer locks.
Append-only source/forecast rows are identified here by their stored hashes;
mutable result heads, game eligibility and active events are compared again
inside the short publication transaction. No fitting or providers run here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os
import re
import stat
import time

from . import db
from .config import settings

_OWNED = ContextVar('learning_publication_research_lock_owned', default=False)


class PublicationDeferred(ValueError):
    """A verified proposal must wait rather than publish stale dependencies."""


@contextmanager
def research_owned():
    """Mark a research lock already acquired by the ordinary refresh worker."""
    token = _OWNED.set(True)
    try:
        yield
    finally:
        _OWNED.reset(token)


@contextmanager
def publish_lock(*,allow_near=False):
    """Never wait behind capture/grading, and never acquire the learner lock."""
    if _OWNED.get():
        if not allow_near and near_kickoff():raise PublicationDeferred('near_tipoff_capture_priority')
        yield
        return
    path = settings.db_path.with_name(settings.db_path.name + '-research.lock')
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('research publication lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublicationDeferred('refresh_busy') from exc
        if not allow_near and near_kickoff():raise PublicationDeferred('near_tipoff_capture_priority')
        with research_owned():
            yield
    finally:
        os.close(fd)


def same(before, after):
    if before != after:
        raise PublicationDeferred('publication_dependencies_changed')


def file_seals(paths):
    """Detect replacement/edits after the expensive content-hash verification."""
    result = []
    for path in sorted(set(paths), key=str):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('verified model source is no longer a regular file')
        result.append((str(path), info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns))
    return result


def _exists(table):
    return db.q1('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)) is not None


def near_kickoff():
    """Recheck actual publication time, including a fit crossing into tipoff hour."""
    if _exists('nba_games'):
        from .model_learning import _near_kickoff
        return _near_kickoff(time.time())
    from .integrity import parse_time
    now=time.time()
    return any(0<(parse_time(row['start_time']) or 0)-now<=3600
        for row in db.q("SELECT start_time FROM events WHERE sport='NFL' AND status NOT IN "
                       "('STATUS_FINAL','STATUS_CANCELED','STATUS_CANCELLED')"))


def _game(game_id):
    row = db.q1('SELECT * FROM nba_games WHERE game_id=?', (game_id,))
    keys = ('game_id', 'espn_event_id', 'home_team', 'away_team', 'tipoff_utc',
            'tipoff_ts', 'status', 'home_score', 'away_score')
    return {key: row.get(key) for key in keys} if row else None


def _final_head(url):
    # The existing unique index starts with URL. Do not parse every raw body
    # while holding the publication lock, or invalidate on unrelated games.
    return db.q1('SELECT sha256 FROM nba_source_artifacts WHERE url=? AND '
                  'kind="final-boxscore" AND http_status=200 '
                  'ORDER BY received_at DESC,id DESC LIMIT 1', (url,))


def _quote_heads(sport, quotes):
    identities=sorted(quotes,key=str);saved={};grades={}
    for start in range(0,len(identities),400):
        subset=identities[start:start+400];marks=','.join('?' for _ in subset)
        if sport=='NBA':
            rows=db.q('SELECT * FROM nba_quote_observations WHERE quote_id IN ('+marks+')',subset)
            saved.update((str(row['quote_id']),row) for row in rows)
            latest=db.q('SELECT * FROM (SELECT i.quote_id,r.*,ROW_NUMBER() OVER '
                '(PARTITION BY i.quote_id ORDER BY r.revision DESC,r.id DESC) AS _head '
                'FROM nba_grade_revisions r JOIN nba_selection_lock_items i '
                'ON r.lock_id=i.lock_id AND r.rank=i.rank WHERE i.quote_id IN ('+marks+')) WHERE _head=1',subset)
        else:
            rows=db.q('SELECT * FROM nfl_prop_snapshots WHERE id IN ('+marks+')',subset)
            saved.update((str(row['id']),row) for row in rows)
            latest=db.q('SELECT * FROM (SELECT r.*,ROW_NUMBER() OVER '
                '(PARTITION BY quote_id ORDER BY graded_at DESC,revision DESC) AS _head '
                'FROM nfl_prop_grade_revisions r WHERE quote_id IN ('+marks+')) WHERE _head=1',subset)
        for row in latest:
            row.pop('_head');grades[str(row['quote_id'])]=row
    return [(ident,saved.get(str(ident)),grades.get(str(ident))) for ident in identities]


def _legacy_final_sources(rows, heads, games, *, require_current):
    """Bind evaluated grades to their own canonical final-source URL and head.

    Policy universes also contain unselected quotes without a grade. Only
    evaluated non-policy rows belong here; their full source bytes are checked
    by the slow verifier outside the publication lock.
    """
    evaluated={str(row['quote_id']) for row in rows if not row.get('policy_context_id')}
    prefix='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='
    sources={};latest_by_url={};result=[]
    for ident,quote,grade in heads:
        if str(ident) not in evaluated or not grade:continue
        source_id=grade.get('source_artifact_id')
        if source_id not in sources:
            sources[source_id]=db.q1('SELECT id,url,kind,http_status,received_at,sha256 '
                'FROM nba_source_artifacts WHERE id=?',(source_id,))
        source=sources[source_id]
        if require_current and not source:raise ValueError('NBA grade result source is unavailable')
        url=source['url'] if source else None
        if url not in latest_by_url:latest_by_url[url]=_final_head(url) if url else None
        latest=latest_by_url[url]
        if source:
            match=re.fullmatch(re.escape(prefix)+r'([0-9]+)',str(url))
            game=games.get(str(quote['game_id'])) if quote else None
            expected={str(game['espn_event_id'])} if game and game.get('espn_event_id') else set()
            canonical=re.fullmatch(r'nba-[0-9]{4}-([0-9]{6,12})',str(quote['game_id'])) if quote else None
            if canonical:expected.add(canonical[1])
            if require_current and (source['kind']!='final-boxscore' or source['http_status']!=200 or not match or
                    len(expected)>1 or (expected and match[1] not in expected)):
                raise ValueError('NBA grade final source identity mismatch')
            if require_current and (not latest or latest['sha256']!=source['sha256']):
                raise ValueError('NBA grade result source has been superseded or is unavailable')
        result.append((ident,source,latest))
    return result


def evidence_state(sport, evaluation, sources=(), *, require_current=False):
    rows = list(evaluation.get('rows') or [])
    groups = (evaluation.get('selection_policy') or {}).get('games') or []
    for group in groups:
        rows.extend(group.get('baseline') or [])
        rows.extend(group.get('candidate') or [])
    quotes = {row['quote_id'] for row in rows}
    for group in groups:
        quotes.update(group.get('universe_quote_ids') or [])
    games = {str(row['game_id']) for row in rows}
    games.update(str(group['game_id']) for group in groups)
    state = {'schema': db.q1('PRAGMA schema_version'), 'quotes': [], 'games': [],
             'sources': [], 'contexts': [], 'finals': []}
    if _exists('model_learning_events'):
        state['active'] = db.q1('SELECT id,candidate_id,event FROM model_learning_events '
            'WHERE sport=? AND event IN ("activated","rolled_back") ORDER BY id DESC LIMIT 1', (sport,))
    state['quotes']=_quote_heads(sport,quotes)
    if sport == 'NBA':
        for game_id in sorted(games):
            game = _game(game_id)
            state['games'].append(game)
            if game and game.get('espn_event_id'):
                url = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=' + str(game['espn_event_id'])
                state['finals'].append((game_id, _final_head(url)))
        state['grade_final_sources']=_legacy_final_sources(rows,state['quotes'],
            {str(game['game_id']):game for game in state['games'] if game},require_current=require_current)
        for row in rows:
            context = row.get('policy_context_id')
            if not context:
                continue
            state['contexts'].append(db.q1('SELECT context_id,game_id,as_of,tipoff_ts,sha256 '
                'FROM nba_prop_policy_contexts WHERE context_id=?', (context,)))
            source = db.q1('SELECT id,url,kind,http_status,received_at,sha256 '
                'FROM nba_source_artifacts WHERE id=?', (row.get('result_source_artifact_id'),))
            latest = _final_head(source['url']) if source else None
            if require_current and (not source or not latest or latest['sha256'] != source['sha256']):
                raise ValueError('NBA policy result source has been superseded or is unavailable')
            state['finals'].append((context, source, latest))
    for source in sources:
        kind = source.get('kind')
        if kind in ('nba_source_artifact', 'nba_source_artifacts'):
            row = db.q1('SELECT id,kind,url,received_at,http_status,sha256 '
                'FROM nba_source_artifacts WHERE id=?', (source.get('id'),))
        elif kind == 'nfl_prop_grade_source_artifact':
            row = db.q1('SELECT sha256,source_file,captured_at FROM nfl_prop_grade_source_artifacts WHERE sha256=?',
                        (source.get('sha256'),))
        elif kind == 'nfl_prop_raw_response':
            row = db.q1('SELECT batch_id,page_number,fetched_at,http_status,body_sha256 '
                'FROM nfl_prop_raw_responses WHERE batch_id=? AND page_number=?',
                (source.get('batch_id'), source.get('page_number')))
        else:
            row = None  # Archived files have their separately verified inode/time seals.
        state['sources'].append((kind, row))
    return state


def spread_state(game_ids=None):
    """Cheap exact game/grade heads; no full validation replay or raw-body reads."""
    ids = sorted(set(game_ids)) if game_ids is not None else [
        row['game_id'] for row in db.q('SELECT game_id FROM nba_games')]
    result = {}
    for ident in ids:
        game = _game(ident)
        grade = (db.q1('SELECT id,game_id,source_artifact_id,graded_at,payload,sha256 '
            'FROM nba_game_grade_revisions WHERE game_id=? ORDER BY id DESC LIMIT 1', (ident,))
            if _exists('nba_game_grade_revisions') else None)
        final = None
        if game and game.get('espn_event_id'):
            final = _final_head('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=' +
                                str(game['espn_event_id']))
        result[ident] = (game, grade, final)
    active = (db.q1('SELECT id,version,kind FROM nba_spread_model_events '
        'WHERE kind IN ("activated","rollback") ORDER BY id DESC LIMIT 1')
        if _exists('nba_spread_model_events') else None)
    return {'games': result, 'active': active, 'schema': db.q1('PRAGMA schema_version')}
