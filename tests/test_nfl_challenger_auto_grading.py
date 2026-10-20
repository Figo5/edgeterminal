"""Automatic challenger grading uses archived, verified final evidence offline."""
import hashlib
import json

import pytest

from edgeterminal import db, nfl_prop_challenger_grading as grading
from test_nfl_prop_result_ingest import GAME, challenger_setup, scoreboard, setup


def _unavailable_book(*args):
    raise ValueError('official participation is not available yet')


def _run(challenger_setup, monkeypatch, now=1000., score=None):
    model, raw_stats = challenger_setup
    monkeypatch.setattr(grading.official, 'resolve', _unavailable_book)
    calls = []
    def fetch(url):
        calls.append(url)
        return raw_stats if '.csv' in url else score or scoreboard()
    return model, grading.grade_due(now, fetcher=fetch), calls


def _rewrite_evidence(model, change):
    path = next((model.DIRECTORY / 'results').glob('*.json'))
    data = json.loads(path.read_bytes())
    change(data)
    raw = json.dumps(data, sort_keys=True).encode()
    path.unlink()
    (path.parent / (hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)


def test_verified_final_grades_challenger_before_four_hours(challenger_setup, monkeypatch):
    model, result, calls = _run(challenger_setup, monkeypatch)
    assert result['games_checked'] == 1 and len(calls) == 2
    assert result['record'] == {'won': 2, 'pending': 1}
    assert any(c['status'] == 'needs_review' for c in result['checks'])
    data = json.loads(next((model.DIRECTORY / 'results').glob('*.json')).read_bytes())
    assert data['evidence']['verified_final'] is True
    verified = grading.verified_results(model.selected_rows(), now=1000.)
    assert len(verified) == 2
    assert all(r['actual_stat'] is not None for r in verified.values())
    assert db.q1('SELECT COUNT(*) n FROM nfl_prop_grade_revisions')['n'] == 0


@pytest.mark.parametrize('status', ['STATUS_IN_PROGRESS', 'STATUS_SUSPENDED',
                                    'STATUS_POSTPONED', 'STATUS_CANCELED'])
def test_started_challenger_polls_status_without_grading_nonfinal(challenger_setup, monkeypatch, status):
    model, result, calls = _run(challenger_setup, monkeypatch, score=scoreboard(status))
    assert result['games_checked'] == 1 and len(calls) == 1
    assert result['record'] == {'pending': 3}
    assert result['checks'] == [{'game_id': GAME, 'status': 'waiting_for_final'}]
    assert not list((model.DIRECTORY / 'results').glob('*.json'))


def test_challenger_does_not_fetch_before_saved_kickoff(challenger_setup):
    result = grading.grade_due(99., fetcher=lambda _: pytest.fail('pregame result fetch'))
    assert result['games_checked'] == 0 and result['record'] == {'pending': 3}


def test_challenger_does_not_grade_before_actual_kickoff(challenger_setup, monkeypatch):
    model, result, calls = _run(challenger_setup, monkeypatch, now=150.,
                               score=scoreboard(kickoff='1970-01-01T00:03:20Z'))
    assert len(calls) == 1 and result['record'] == {'pending': 3}
    assert not list((model.DIRECTORY / 'results').glob('*.json'))


def test_challenger_checks_a_game_rescheduled_earlier(challenger_setup, monkeypatch):
    db.ex('INSERT INTO events(id,sport,league,start_time) VALUES(?,?,?,?)',
          (GAME, 'NFL', 'NFL', '1970-01-01T00:00:50Z'))
    _, result, calls = _run(challenger_setup, monkeypatch, now=75.,
                           score=scoreboard(kickoff='1970-01-01T00:00:50Z'))
    assert len(calls) == 2 and result['record'] == {'won': 2, 'pending': 1}


def test_early_challenger_evidence_requires_explicit_verified_flag(challenger_setup, monkeypatch):
    model, _, _ = _run(challenger_setup, monkeypatch)
    _rewrite_evidence(model, lambda d: d['evidence'].pop('verified_final', None))
    with pytest.raises(ValueError, match='postgame wait'):
        grading.verified_results(model.selected_rows(), now=1000.)
    assert len(grading.verified_results(model.selected_rows(), now=20000.)) == 2


def test_early_verified_challenger_evidence_still_requires_source_hash(challenger_setup, monkeypatch):
    model, _, _ = _run(challenger_setup, monkeypatch)
    _rewrite_evidence(model, lambda d: d['evidence']['final_game_source'].update(sha256='0'*64))
    with pytest.raises(ValueError, match='source file/hash mismatch'):
        grading.verified_results(model.selected_rows(), now=1000.)


def test_early_challenger_still_requires_original_quote(challenger_setup, monkeypatch):
    model, _ = challenger_setup
    path = next((model.DIRECTORY / 'model').glob('shadow-*.json'))
    data = json.loads(path.read_bytes())
    data['top_five'][GAME][0]['line'] = 99.5
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    path.with_suffix('.sha256').write_text(hashlib.sha256(raw).hexdigest())
    _, result, _ = _run(challenger_setup, monkeypatch)
    assert result['record'] == {'pending': 3}
    assert any('original quote' in c.get('reason', '') for c in result['checks'])
    assert not list((model.DIRECTORY / 'results').glob('*.json'))


def test_public_grade_due_passes_optional_time_to_challenger(monkeypatch):
    from edgeterminal import nfl_prop_challenger as model
    calls = []
    monkeypatch.setattr(grading, 'grade_due', lambda now=None: calls.append(now) or {'checks': []})
    assert model.grade_due(now=1234.) == {'checks': []}
    assert calls == [1234.]


def test_default_challenger_reads_shared_cache_and_keeps_source_time(challenger_setup, monkeypatch):
    from edgeterminal import nfl_result_cache
    model, raw_stats = challenger_setup
    calls = []
    def cached(url, fetcher, *, now):
        calls.append((url, now))
        assert fetcher is grading.ingest._fetch
        return (raw_stats if '.csv' in url else scoreboard()), 900.
    monkeypatch.setattr(nfl_result_cache, 'read', cached)
    monkeypatch.setattr(grading.ingest, '_fetch', lambda _: pytest.fail('cache should read this feed'))
    monkeypatch.setattr(grading.official, 'resolve', _unavailable_book)
    result = grading.grade_due(now=1000.)
    assert result['record'] == {'won': 2, 'pending': 1} and len(calls) == 2
    data = json.loads(next((model.DIRECTORY / 'results').glob('*.json')).read_bytes())
    assert data['evidence']['final_game_source']['retrieved_at'] == 900.
    assert data['evidence']['player_stat_source']['retrieved_at'] == 900.
    assert data['evidence']['result_retrieved_at'] == 900.


def _official_inactive(game, quotes, raw, kickoff, score, now):
    ref = grading._archive(b'fixture: official inactive', 'fixture://gamebook', now)
    return {'players': {'gsis-c': {'active_roster': False, 'snap_played': False, 'official_stats': {}}},
            'reviewer': 'fixture', 'reviewed_at': now}, {**ref, 'supporting_sources': []}


def _corrected_stats(raw, value):
    import csv
    import io
    reader = csv.DictReader(io.StringIO(raw.decode()))
    fields, rows = reader.fieldnames, list(reader)
    for row in rows:
        if row['player_id'] == 'gsis-a' and row['week'] == '3':
            row['receptions'] = str(value)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def test_challenger_audits_settled_results_daily_and_appends_correction(challenger_setup, monkeypatch):
    model, raw_stats = challenger_setup
    selected_file = next((model.DIRECTORY / 'model').glob('shadow-*.json'))
    selected_bytes = selected_file.read_bytes()
    monkeypatch.setattr(grading.official, 'resolve', _official_inactive)
    first = grading.grade_due(1000., fetcher=lambda u: raw_stats if '.csv' in u else scoreboard())
    assert first['record'] == {'won': 2, 'void': 1}
    original = {p.name: p.read_bytes() for p in (model.DIRECTORY / 'results').glob('*.json')}
    assert grading.grade_due(1300., fetcher=lambda _: pytest.fail('settled result is not due'))['games_checked'] == 0
    corrected = _corrected_stats(raw_stats, 1)
    second = grading.grade_due(1000.+86400, fetcher=lambda u: corrected if '.csv' in u else scoreboard())
    assert second['games_checked'] == 1 and second['record'] == {'lost': 1, 'won': 1, 'void': 1}
    paths = list((model.DIRECTORY / 'results').glob('*.json'))
    assert len(paths) == 2
    assert all((model.DIRECTORY / 'results' / name).read_bytes() == raw for name, raw in original.items())
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == p.stem for p in paths)
    assert selected_file.read_bytes() == selected_bytes
    current = grading.verified_results(model.selected_rows(), now=1000.+86400)
    assert current[(GAME, 'sgo:challenger-a', 'receptions')]['actual_stat'] == 1.


def test_unchanged_challenger_audit_does_not_append_duplicate_evidence(challenger_setup, monkeypatch):
    model, raw_stats = challenger_setup
    monkeypatch.setattr(grading.official, 'resolve', _official_inactive)
    fetch = lambda u: raw_stats if '.csv' in u else scoreboard()
    grading.grade_due(1000., fetcher=fetch)
    original = {p.name: p.read_bytes() for p in (model.DIRECTORY / 'results').glob('*.json')}
    result = grading.grade_due(1000.+86400, fetcher=fetch)
    assert result['games_checked'] == 1 and result['record'] == {'won': 2, 'void': 1}
    assert {p.name: p.read_bytes() for p in (model.DIRECTORY / 'results').glob('*.json')} == original
    assert grading.grade_due(1300.+86400, fetcher=lambda _: pytest.fail('duplicate daily audit'))['games_checked'] == 0


@pytest.mark.parametrize('age,cadence', [(1000., 300), (8*86400., 86400)])
def test_automatic_challenger_pending_poll_slows_for_old_games(challenger_setup, monkeypatch, age, cadence):
    from edgeterminal import nfl_result_cache
    calls = []
    def cached(url, fetcher, *, now):
        calls.append((url, now))
        return scoreboard('STATUS_IN_PROGRESS'), now
    monkeypatch.setattr(nfl_result_cache, 'read', cached)
    first = grading.grade_due(now=100.+age)
    assert first['games_checked'] == 1 and len(calls) == 1
    assert grading.grade_due(now=100.+age+cadence-1)['games_checked'] == 0
    assert len(calls) == 1
    assert grading.grade_due(now=100.+age+cadence)['games_checked'] == 1
    assert len(calls) == 2


def test_challenger_daily_audit_refuses_stats_conflicting_with_main_grade(challenger_setup, monkeypatch):
    model, raw_stats = challenger_setup
    monkeypatch.setattr(grading.official, 'resolve', _official_inactive)
    grading.grade_due(1000., fetcher=lambda u: raw_stats if '.csv' in u else scoreboard())
    original = {p.name: p.read_bytes() for p in (model.DIRECTORY / 'results').glob('*.json')}
    main = {'canonical_game_id': GAME, 'player_id': 'sgo:challenger-a', 'market': 'receptions',
            'actual_stat': 5., 'research_statistical_result': 'won'}
    monkeypatch.setattr(grading.grading, '_latest', lambda _: [main])
    corrected = _corrected_stats(raw_stats, 1)
    result = grading.grade_due(1000.+86400, fetcher=lambda u: corrected if '.csv' in u else scoreboard())
    assert result['games_checked'] == 1
    assert any(c['status'] == 'needs_review' and 'main result' in c['reason'] for c in result['checks'])
    assert {p.name: p.read_bytes() for p in (model.DIRECTORY / 'results').glob('*.json')} == original
    assert result['record'] == {'won': 2, 'void': 1}


def test_mixed_pending_challenger_checks_keep_one_file_until_stat_correction(challenger_setup, monkeypatch):
    from edgeterminal import nfl_result_cache
    model, raw_stats = challenger_setup
    current = [raw_stats]
    calls = []
    def cached(url, fetcher, *, now):
        calls.append((url, now))
        return (current[0] if '.csv' in url else scoreboard()), now
    monkeypatch.setattr(nfl_result_cache, 'read', cached)
    monkeypatch.setattr(grading.official, 'resolve', _unavailable_book)
    for stamp in (1000., 1300., 1600., 1000.+86400):
        result = grading.grade_due(now=stamp)
        assert result['games_checked'] == 1 and result['record'] == {'won': 2, 'pending': 1}
        assert len(list((model.DIRECTORY / 'results').glob('*.json'))) == 1
    current[0] = _corrected_stats(raw_stats, 1)
    corrected = grading.grade_due(now=1300.+86400)
    assert corrected['record'] == {'lost': 1, 'won': 1, 'pending': 1}
    assert len(list((model.DIRECTORY / 'results').glob('*.json'))) == 2
    assert len(calls) == 10


def test_verified_results_parses_unchanged_stat_artifact_once(challenger_setup, monkeypatch):
    model, _, _ = _run(challenger_setup, monkeypatch)
    path = next((model.DIRECTORY / 'results').glob('*.json'))
    data = json.loads(path.read_bytes())
    data['created_at'] = 1100.
    raw = json.dumps(data, sort_keys=True).encode()
    (path.parent / (hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
    reads = []
    original = grading.csv.DictReader
    def reader(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(grading.csv, 'DictReader', reader)
    assert len(grading.verified_results(model.selected_rows(), now=1200.)) == 2
    assert len(reads) == 1


def test_main_stat_correction_does_not_block_pending_challenger_audit(challenger_setup, monkeypatch):
    model, raw_stats = challenger_setup
    main = {'canonical_game_id': GAME, 'player_id': 'sgo:challenger-a', 'market': 'receptions',
            'actual_stat': 5., 'research_statistical_result': 'won'}
    monkeypatch.setattr(grading.grading, '_latest', lambda _: [main])
    monkeypatch.setattr(grading.official, 'resolve', _unavailable_book)
    first = grading.grade_due(1000., fetcher=lambda u: raw_stats if '.csv' in u else scoreboard())
    assert first['record'] == {'won': 2, 'pending': 1}
    path = next((model.DIRECTORY / 'results').glob('*.json'))
    original = path.read_bytes()
    main['actual_stat'] = 1.
    main['research_statistical_result'] = 'lost'
    corrected = _corrected_stats(raw_stats, 1)
    second = grading.grade_due(1300., fetcher=lambda u: corrected if '.csv' in u else scoreboard())
    assert second['games_checked'] == 1 and second['record'] == {'lost': 1, 'won': 1, 'pending': 1}
    assert not any('main result' in c.get('reason', '') for c in second['checks'])
    assert json.loads(original)['quote_ids'] == [12, 13]
    own = grading.verified_results(model.selected_rows(), now=1300.)
    assert (GAME, 'sgo:challenger-a', 'receptions') not in own
    assert path.read_bytes() == original
    stale = grading.grade_due(1600., fetcher=lambda u: raw_stats if '.csv' in u else scoreboard())
    assert any('main result' in c.get('reason', '') for c in stale['checks'])
    assert stale['record'] == {'lost': 1, 'won': 1, 'pending': 1}
    assert len(list((model.DIRECTORY / 'results').glob('*.json'))) == 1


@pytest.mark.parametrize('quote_ids', [None, [], [11, 11], [999], ['11'], [True]])
def test_scoped_challenger_evidence_requires_sealed_quote_ids(challenger_setup, monkeypatch, quote_ids):
    model, _, _ = _run(challenger_setup, monkeypatch)
    _rewrite_evidence(model, lambda d: d.update(quote_ids=quote_ids))
    with pytest.raises(ValueError, match='quote IDs'):
        grading.verified_results(model.selected_rows(), now=1000.)


def test_legacy_challenger_evidence_without_quote_ids_keeps_its_contract(challenger_setup, monkeypatch):
    model, _, _ = _run(challenger_setup, monkeypatch)
    _rewrite_evidence(model, lambda d: d.pop('quote_ids', None))
    assert len(grading.verified_results(model.selected_rows(), now=1000.)) == 2
