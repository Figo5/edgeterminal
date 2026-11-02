"""Absent NBA selections need exact official participation proof, never a zero."""
import copy
import hashlib
import json

import httpx
import pytest

from edgeterminal import db, nba_data, nba_grading, nba_participation, nba_store
from edgeterminal.config import settings
from test_nba_participation import BOOK, BOOK_URL, REPORT_URL, artifact, case, pdf_bytes


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "official.db"}')
    db.init()
    nba_store.ensure_schema()
    nba_participation.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def collector():
    from edgeterminal import nba_participation_sources
    return nba_participation_sources


def absent_case():
    raw, game, args = case()
    game = nba_store.get_game(game['game_id'])
    lock = nba_store.get_lock('top5-' + game['game_id'])
    official = dict(game_id=game['game_id'], game_status='final', verified_final=True,
        home_score=game['home_score'], away_score=game['away_score'], players=[
            dict(player_id=pid, status='active', stats={'points': 5}, minutes=10.)
            for pid in {'3064514', '3195', '4433255'}])
    return raw, game, lock, official, args


def test_portland_full_nickname_matches_score_and_inactive_section():
    text = BOOK.replace('Minnesota Timberwolves', 'Portland Trail Blazers').replace(
        'Timberwolves', 'Trail Blazers').replace('Ingles', 'Williams III')
    source = dict(url=BOOK_URL.replace('MINOKC', 'POROKC'), body=pdf_bytes(text), received_at=2000000000.)
    game = dict(game_date_et='2026-03-15', tipoff_ts=1773594000., home_team='OKC', away_team='POR',
                home_score=116, away_score=103)
    result = nba_participation._pdf_proof(source, game, 'POR', 'Robert Williams III', 'Williams III', 0)
    assert result['kind'] == 'final_gamebook'
    assert 'Inactive: Trail Blazers' in result['excerpt']


@pytest.mark.parametrize('change', ['wrong_score', 'wrong_inactive_team'])
def test_portland_matching_is_not_a_loose_suffix(change):
    text = BOOK.replace('Minnesota Timberwolves', 'Portland Trail Blazers').replace(
        'Timberwolves', 'Trail Blazers').replace('Ingles', 'Williams III')
    if change == 'wrong_score':
        text = text.replace('28 103', '28 104')
    else:
        text = text.replace('Inactive: Trail Blazers', 'Inactive: Grizzlies')
    source = dict(url=BOOK_URL.replace('MINOKC', 'POROKC'), body=pdf_bytes(text), received_at=2000000000.)
    game = dict(game_date_et='2026-03-15', tipoff_ts=1773594000., home_team='OKC', away_team='POR',
                home_score=116, away_score=103)
    with pytest.raises(ValueError):
        nba_participation._pdf_proof(source, game, 'POR', 'Robert Williams III', 'Williams III', 0)


def test_machine_proof_is_distinct_from_human_review_and_revalidated(isolated):
    _, game, args = case()
    review = nba_participation.record_verified_inactive(**args)
    assert json.loads(review['proof'])['verification_method'] == 'automatic_gamebook_v1'
    official = dict(game_id=game['game_id'], game_status='final', verified_final=True,
                    home_score=game['home_score'], away_score=game['away_score'], players=[])
    overlay = nba_participation.overrides(game['game_id'], official, box_complete=True, now=args['now'])
    assert json.loads(overlay[args['player_id']]['proof'])['verification_method'] == 'automatic_gamebook_v1'
    assert nba_participation.record_verified_inactive(**dict(args, now=args['now']+1))['id'] == review['id']
    assert db.q1('SELECT COUNT(*) n FROM nba_reviewed_participation')['n'] == 1


def test_machine_path_cannot_accept_a_human_reviewed_injury_report(isolated):
    _, _, args = case()
    report = 'Injury Report: 03/15/26 12:00 PM\n03/15/2026 01:00(ET) MIN@OKC MinnesotaTimberwolves Ingles,Joe Out'
    args['source_artifact_id'] = artifact(pdf_bytes(report), REPORT_URL, args['now']-10)
    with pytest.raises(ValueError, match='gamebook'):
        nba_participation.record_verified_inactive(**args)


def test_collector_reuses_exact_source_and_appends_only_participation(isolated):
    _, game, lock, official, args = absent_case()
    before = db.q('SELECT * FROM nba_selection_lock_items')
    result = collector().collect_missing(game, lock, official, box_complete=True, now=args['now'],
        getter=lambda url: pytest.fail('verified archived gamebook should be reused'))
    assert result['ok'] and result['new_evidence'] == 1 and result['requests'] == 0
    assert result['pending_player_ids'] == []
    assert db.q('SELECT * FROM nba_grade_revisions') == []
    assert db.q('SELECT * FROM nba_selection_lock_items') == before
    second = collector().collect_missing(game, lock, official, box_complete=True, now=args['now']+1,
        getter=lambda url: pytest.fail('already resolved proof must not fetch'))
    assert second['ok'] and second['new_evidence'] == 0 and second['requests'] == 0


def test_collector_falls_back_to_book_route_and_caps_actual_requests(isolated, monkeypatch):
    _, game, lock, official, args = absent_case()
    # Existing fixture proof is for a different URL variant so remove it only
    # from the source lookup, without mutating append-only evidence.
    module = collector()
    calls = []
    monkeypatch.setattr(module, '_cached_sources', lambda *a: [])
    def get(url):
        calls.append(url)
        return (404, b'not found') if not url.endswith('_book.pdf') else (200, pdf_bytes(BOOK))
    result = module.collect_missing(game, lock, official, box_complete=True, now=args['now'], getter=get)
    assert result['ok'] and result['new_evidence'] == 1 and result['requests'] == 2
    assert calls == [BOOK_URL, BOOK_URL.replace('.pdf', '_book.pdf')]
    row = db.q1('SELECT proof FROM nba_reviewed_participation')
    assert json.loads(row['proof'])['verification_method'] == 'automatic_gamebook_v1'


@pytest.mark.parametrize('change', ['not_final', 'unverified', 'incomplete', 'no_lock', 'all_listed'])
def test_nonapplicable_games_never_fetch_or_add_evidence(isolated, change):
    _, game, lock, official, args = absent_case()
    if change == 'not_final': official['game_status'] = 'postponed'
    if change == 'unverified': official['verified_final'] = False
    if change == 'no_lock': lock = None
    if change == 'all_listed': official['players'].append(dict(player_id='2968436', status='active'))
    result = collector().collect_missing(game, lock, official, box_complete=change != 'incomplete', now=args['now'],
        getter=lambda url: pytest.fail('nonapplicable game must not fetch'))
    assert result['requests'] == 0 and result['new_evidence'] == 0
    assert db.q('SELECT * FROM nba_reviewed_participation') == []


def test_source_failure_is_visible_and_cooldown_does_not_fabricate_success(isolated, monkeypatch):
    _, game, lock, official, args = absent_case()
    module = collector()
    monkeypatch.setattr(module, '_cached_sources', lambda *a: [])
    calls = []
    def fail(url):
        calls.append(url)
        raise TimeoutError('provider detail must not appear in public errors')
    first = module.collect_missing(game, lock, official, box_complete=True, now=args['now'], getter=fail)
    assert not first['ok'] and first['errors'] and first['pending_player_ids'] == ['2968436']
    second = module.collect_missing(game, lock, official, box_complete=True, now=args['now']+1, getter=fail)
    assert not second['ok'] and second['status'] == 'retry_wait' and second['requests'] == 0
    assert len(calls) <= 2 and db.q('SELECT * FROM nba_reviewed_participation') == []
    assert not any('provider detail' in str(e) for e in first['errors'])


def test_zero_request_budget_defers_without_calling_a_provider(isolated, monkeypatch):
    _, game, lock, official, args = absent_case()
    module = collector()
    monkeypatch.setattr(module, '_cached_sources', lambda *a: [])
    result = module.collect_missing(game, lock, official, box_complete=True, now=args['now'], max_requests=0,
        getter=lambda url: pytest.fail('no request budget'))
    assert result['ok'] and result['status'] == 'deferred' and result['requests'] == 0
    assert result['pending_player_ids'] == ['2968436']


@pytest.mark.parametrize('change', ['wrong_date', 'wrong_score', 'wrong_team', 'not_inactive', 'oversize'])
def test_invalid_gamebook_never_resolves_an_absent_player(isolated, monkeypatch, change):
    _, game, lock, official, args = absent_case()
    module = collector()
    monkeypatch.setattr(module, '_cached_sources', lambda *a: [])
    text = BOOK
    if change == 'wrong_date': text = text.replace('March 15', 'March 16')
    if change == 'wrong_score': text = text.replace('28 103', '28 104')
    if change == 'wrong_team': text = text.replace('Inactive: Timberwolves', 'Inactive: Thunder')
    if change == 'not_inactive': text = text.replace('Inactive: Timberwolves', 'Active: Timberwolves')
    raw = pdf_bytes(text)
    if change == 'oversize': monkeypatch.setattr(module, 'MAX_PDF_BYTES', len(raw)-1)
    result = module.collect_missing(game, lock, official, box_complete=True, now=args['now'], getter=lambda url: (200, raw))
    assert not result['ok'] and result['pending_player_ids'] == ['2968436']
    assert db.q('SELECT * FROM nba_reviewed_participation') == []


@pytest.mark.parametrize('status', [301, 302, 307, 308])
def test_http_download_never_follows_redirects(monkeypatch, status):
    module = collector()
    seen = []
    class Response:
        status_code = status
        headers = {'location': 'http://127.0.0.1/private'}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_bytes(self): yield b'redirect'
    def stream(method, url, **kwargs):
        seen.append(kwargs)
        return Response()
    monkeypatch.setattr(module.httpx, 'stream', stream)
    with pytest.raises(ValueError, match='redirect'):
        module._download(BOOK_URL)
    assert seen[0]['follow_redirects'] is False


@pytest.mark.parametrize('url', ['http://statsdmz.nba.com/pdfs/20260315/20260315_MINOKC.pdf',
    'https://statsdmz.nba.com.evil.test/pdfs/20260315/20260315_MINOKC.pdf',
    'https://statsdmz.nba.com/pdfs/20260315/20260315_MINOKC.pdf?key=secret',
    'https://statsdmz.nba.com/pdfs/20260315/../20260315_MINOKC.pdf'])
def test_download_refuses_untrusted_routes_before_network(monkeypatch, url):
    module = collector()
    monkeypatch.setattr(module.httpx, 'stream', lambda *a, **k: pytest.fail('untrusted URL requested'))
    with pytest.raises(ValueError): module._download(url)


def test_day_to_day_is_disclosed_without_becoming_a_blocked_status():
    from edgeterminal.nba_game_context import roster_availability
    from edgeterminal.nba_model import BLOCKED_AVAILABILITY
    status = roster_availability(dict(roster_status='active', injuries=[{'status': 'Day-To-Day'}]))
    assert status == 'day-to-day'
    assert status not in BLOCKED_AVAILABILITY


def test_day_to_day_label_preserves_inactive_roster_eligibility():
    from edgeterminal.nba_game_context import roster_availability
    assert roster_availability(dict(roster_status='inactive', injuries=[{'status':'Day-To-Day'}])) == 'inactive'


def test_cached_unsupported_book_also_respects_retry_cooldown(isolated):
    _, game, lock, official, args = absent_case()
    module = collector();calls=[]
    # A valid cached final without the selected inactive name is inconclusive.
    invalid = pdf_bytes(BOOK.replace('Ingles', 'Other'))
    from unittest.mock import patch
    sources = [dict(id=999, body=invalid, sha256=hashlib.sha256(invalid).hexdigest(),
                    url=BOOK_URL, kind='official-participation', received_at=args['now']-1, http_status=200)]
    with patch.object(module, '_cached_sources', lambda url, now: sources if url == BOOK_URL else []):
        def get(url): calls.append(url); return 404, b'not found'
        first = module.collect_missing(game, lock, official, box_complete=True, now=args['now'], getter=get)
        count = len(calls)
        second = module.collect_missing(game, lock, official, box_complete=True, now=args['now']+1, getter=get)
    assert not first['ok'] and not second['ok']
    assert second['status'] == 'retry_wait' and second['requests'] == 0 and len(calls) == count


def test_frozen_forecast_hash_is_verified_before_evidence_append(isolated, monkeypatch):
    _, game, lock, official, args = absent_case()
    original = nba_store.get_forecast
    monkeypatch.setattr(nba_store, 'get_forecast', lambda fid: dict(original(fid), payload_sha256='0'*64))
    result = collector().collect_missing(game, lock, official, box_complete=True, now=args['now'],
        getter=lambda url: pytest.fail('corrupt forecast must fail before network'))
    assert not result['ok'] and db.q('SELECT * FROM nba_reviewed_participation') == []


def test_tampered_cached_gamebook_fails_closed_without_network(isolated, monkeypatch):
    _, game, lock, official, args = absent_case();module = collector()
    row = nba_store.get_artifact(args['source_artifact_id'])
    monkeypatch.setattr(module, '_cached_sources', lambda url, now: [dict(row, sha256='0'*64)] if url == BOOK_URL else [])
    result = module.collect_missing(game, lock, official, box_complete=True, now=args['now'],
        getter=lambda url: pytest.fail('tampered archive must not be hidden by another fetch'))
    assert not result['ok'] and result['requests'] == 0
    assert db.q('SELECT * FROM nba_reviewed_participation') == []


@pytest.mark.parametrize('failure', ['length', 'bytes', 'deadline', 'invalid_length'])
def test_streaming_download_enforces_bounds(monkeypatch, failure):
    module = collector()
    monkeypatch.setattr(module, 'MAX_PDF_BYTES', 4)
    class Response:
        status_code = 200
        headers = {'content-length': '5' if failure == 'length' else 'bad' if failure == 'invalid_length' else '3'}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_bytes(self): yield b'123'; yield b'45' if failure == 'bytes' else b''
    monkeypatch.setattr(module.httpx, 'stream', lambda *a, **kw: Response())
    if failure == 'deadline':
        values = iter([0, module.STREAM_DEADLINE + 1])
        monkeypatch.setattr(module.time, 'monotonic', lambda: next(values))
    with pytest.raises((ValueError, TimeoutError)):
        module._download(BOOK_URL)


def test_streaming_success_returns_exact_bytes(monkeypatch):
    module = collector()
    class Response:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_bytes(self): yield b'%PDF'; yield b'-test'
    monkeypatch.setattr(module.httpx, 'stream', lambda *a, **kw: Response())
    assert module._download(BOOK_URL) == (200, b'%PDF-test')


def test_tampered_sealed_lock_is_rejected_before_source_work(isolated):
    _, game, lock, official, args = absent_case()
    lock['sha256'] = '0' * 64
    result = collector().collect_missing(game, lock, official, box_complete=True, now=args['now'],
        getter=lambda url: pytest.fail('corrupt sealed lock must fail before network'))
    assert not result['ok'] and result['requests'] == 0
    assert db.q('SELECT * FROM nba_reviewed_participation') == []


def test_surname_must_be_unique_in_the_frozen_roster_for_machine_evidence(isolated):
    _, _, args = case([{'id':'9999999','displayName':'Other Ingles','lastName':'Ingles'}])
    with pytest.raises(ValueError, match='surname'):
        nba_participation.record_verified_inactive(**args)


def test_collector_final_score_correction_stays_pending_without_source_request(isolated):
    _, game, lock, official, args = absent_case()
    official['home_score'] += 1
    result = collector().collect_missing(game, lock, official, box_complete=True, now=args['now'],
        getter=lambda url: pytest.fail('contradictory final must fail before network'))
    assert not result['ok'] and result['requests'] == 0
    assert result['pending_player_ids'] == ['2968436']
    assert db.q('SELECT * FROM nba_reviewed_participation') == []
