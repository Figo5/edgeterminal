"""Offline postgame operations fixtures: no NFL or nflverse requests."""
import csv
import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from edgeterminal import db, nfl_prop_lab
from edgeterminal.config import settings
from edgeterminal.nfl_prop_result_ingest import _stats, sync, status_view

GAME = "nfl-2026-reg-03-atl-gb"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("edgeterminal.nfl_prop_result_ingest.FORWARD_REPORT_PATH",
                        tmp_path / "forward-report.json")
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'operations.db'}")
    db.init()
    quotes = []
    for i, gsis in enumerate(("gsis-a", "gsis-b", "gsis-c", "gsis-d"), 1):
        quotes.append({"quote_id": i, "canonical_game_id": GAME,
                       "player_id": f"sgo:{i}", "gsis_player_id": gsis,
                       "player_name": gsis, "market": "receptions", "line": 2.5,
                       "side": "over", "price": -110, "sportsbook": "fanduel",
                       "projection": 3.0, "probability_over": .55,
                       "probability_under": .45, "uncertainty_sigma": 2.0,
                       "model_version": "fixture-v1", "observed_at": 10.0,
                       "kickoff_utc": 100.0})
    payload = json.dumps({"metadata": {}, "projections": quotes}, sort_keys=True)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    with db.tx() as c:
        c.execute("INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)",
                  ("batch", "sportsgameodds", 10, "source", "{}", 1, 1, 4, "{}"))
        c.execute("INSERT INTO nfl_prop_research_freezes VALUES(?,?,?,?)",
                  (sha, "batch", 20, payload))
    db.link_nfl_provider_game("espn", "espn-123", GAME)
    monkeypatch.setattr(nfl_prop_lab, "_schedule", lambda: {
        "2026_03_ATL_GB": {"game_id": "2026_03_ATL_GB", "season": "2026",
                           "game_type": "REG", "week": "3", "away_team": "ATL",
                           "home_team": "GB"}})
    yield sha, tmp_path
    if db._conn is not None:
        db._conn.close(); db._conn = None


def scoreboard(status="STATUS_FINAL", score=21, kickoff="1970-01-01T00:01:40Z"):
    return json.dumps({"season": {"year": 2026, "type": 2}, "week": {"number": 3},
                       "events": [{"id": "123", "date": kickoff,
                                   "status": {"type": {"name": status,
                                                       "completed": status == "STATUS_FINAL"}},
                                   "competitions": [{"competitors": [
                                       {"homeAway": "home", "score": str(score),
                                        "team": {"displayName": "Green Bay Packers"}},
                                       {"homeAway": "away", "score": "17",
                                        "team": {"displayName": "Atlanta Falcons"}}]}]}]}).encode()


def stats(receptions=5):
    fields = ["game_id", "player_id", "season", "week", "season_type", "team",
              "opponent_team", "passing_yards", "rushing_yards", "carries",
              "receptions", "receiving_yards", "targets"]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for i in range(60):
        gsis = "gsis-a" if i == 0 else "gsis-b" if i == 1 else f"other-{i}"
        team, opponent = ("ATL", "GB") if i < 30 else ("GB", "ATL")
        writer.writerow({"game_id": "2026_03_ATL_GB", "player_id": gsis,
                         "season": 2026, "week": 3, "season_type": "REG",
                         "team": team, "opponent_team": opponent,
                         "passing_yards": 0, "rushing_yards": 0, "carries": 0,
                         "receptions": receptions if i == 0 else 0,
                         "receiving_yards": 50 if i == 0 else 0,
                         "targets": 5 if i == 0 else 2 if i == 1 else 0})
    return stream.getvalue().encode()


def edit_stat_bytes(raw, change):
    reader = csv.DictReader(io.StringIO(raw.decode()))
    fields, rows = reader.fieldnames, list(reader)
    for row in rows:
        change(row)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode()


@pytest.fixture
def challenger_setup(setup, monkeypatch):
    from edgeterminal import nfl_prop_challenger as model
    root = model.DIRECTORY
    artifact = root / 'model'
    artifact.mkdir(parents=True)
    (root / 'active.json').write_text(json.dumps({'path': str(artifact), 'sha256': 'manifest'}))
    db.ex('INSERT INTO nfl_prop_capture_batches VALUES(?,?,?,?,?,?,?,?,?)',
          ('challenger-batch', 'sportsgameodds', 10., 'source', '{}', 1, 1, 3, '{}'))
    rows = []
    for i, letter in enumerate('abc', 11):
        row = dict(game_id=GAME, quote_id=i, player_id=f'sgo:challenger-{letter}', player=f'Player {letter}',
                   market='receptions', line=2.5 if letter != 'b' else .5,
                   side='over' if letter == 'a' else 'under', price=-110, sportsbook='fanduel',
                   observed_at=10., kickoff_utc=100., projection=3., baseline_projection=4., confidence=.7)
        rows.append(row)
        fields = dict(id=i, batch_id='challenger-batch', canonical_game_id=GAME, provider_event_id='sgo-game',
                      player_id=row['player_id'], provider_player_id=letter, player_name=row['player'],
                      team='Atlanta Falcons', opponent='Green Bay Packers', market=row['market'], line=row['line'],
                      side=row['side'], price=row['price'], sportsbook=row['sportsbook'], retrieved_at=10.,
                      kickoff_utc=100., provider='sportsgameodds', source='fixture://source')
        db.ex('INSERT INTO nfl_prop_snapshots (' + ','.join(fields) + ') VALUES (' +
              ','.join('?' for _ in fields) + ')', fields.values())
    raw = json.dumps({'created_at': 30., 'model_manifest_sha256': 'manifest', 'top_five': {GAME: rows}}).encode()
    (artifact / 'shadow-fixture.json').write_bytes(raw)
    (artifact / 'shadow-fixture.sha256').write_text(hashlib.sha256(raw).hexdigest())
    reader = csv.DictReader(io.StringIO(stats().decode()))
    fields, current = [*reader.fieldnames, 'player_display_name'], list(reader)
    prior = []
    for letter in 'abc':
        prior.append({**current[0], 'player_id': f'gsis-{letter}', 'player_display_name': f'Player {letter}',
                      'week': 2, 'game_id': 'prior-game'})
    for row in current:
        row['player_display_name'] = 'Player a' if row['player_id'] == 'gsis-a' else 'Player b' if row['player_id'] == 'gsis-b' else 'Other'
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader(); writer.writerows([*prior, *current])
    return model, output.getvalue().encode()


def test_challenger_grades_saved_picks_without_creating_main_freezes(challenger_setup, monkeypatch):
    from edgeterminal import nfl_prop_challenger_grading as challenger_grading
    model, raw_stats = challenger_setup
    original = db.q('SELECT * FROM nfl_prop_research_freezes')
    calls = []
    def fetch(url):
        calls.append(url)
        return raw_stats if '.csv' in url else scoreboard()
    def official(game, quotes, raw, kickoff, score, now):
        assert {q['gsis_player_id'] for q in quotes} == {'gsis-c'}
        ref = challenger_grading._archive(b'fixture: official inactive', 'fixture://gamebook', now)
        return {'players': {'gsis-c': {'active_roster': False, 'snap_played': False, 'official_stats': {}}},
                'reviewer': 'fixture', 'reviewed_at': now}, {**ref, 'supporting_sources': []}
    monkeypatch.setattr(challenger_grading.official, 'resolve', official)
    result = challenger_grading.grade_due(20000., fetcher=fetch)
    assert result['record'] == {'won': 2, 'void': 1}
    assert len(calls) == 2
    assert db.q('SELECT * FROM nfl_prop_research_freezes') == original
    assert db.q1('SELECT COUNT(*) n FROM nfl_prop_grade_revisions')['n'] == 0
    assert challenger_grading.grade_due(20001., fetcher=lambda u: pytest.fail('repeat fetch'))['games_checked'] == 0
    path = next((model.DIRECTORY / 'results').glob('*.json'))
    path.chmod(0o644); path.write_text('{}')
    with pytest.raises(ValueError, match='evidence changed'):
        model.report()


def test_challenger_waits_for_final_and_does_not_assume_missing_means_zero(challenger_setup, monkeypatch):
    from edgeterminal import nfl_prop_challenger_grading as challenger_grading
    model, raw_stats = challenger_setup
    assert challenger_grading.grade_due(101., fetcher=lambda u: pytest.fail('before delay'))['record'] == {'pending': 3}
    result = challenger_grading.grade_due(20000., fetcher=lambda u: scoreboard('STATUS_IN_PROGRESS'))
    assert result['record'] == {'pending': 3}
    monkeypatch.setattr(challenger_grading.official, 'resolve', lambda *a: (_ for _ in ()).throw(ValueError('no book')))
    result = challenger_grading.grade_due(20001., fetcher=lambda u: raw_stats if '.csv' in u else scoreboard())
    assert result['record'] == {'won': 2, 'pending': 1}
    assert any(c['status'] == 'needs_review' for c in result['checks'])


def test_challenger_rejects_a_pick_that_disagrees_with_its_source_quote(challenger_setup):
    from edgeterminal import nfl_prop_challenger_grading as challenger_grading
    model, raw_stats = challenger_setup
    path = next((model.DIRECTORY / 'model').glob('shadow-*.json'))
    snapshot = json.loads(path.read_bytes())
    snapshot['top_five'][GAME][0]['line'] = 99.5
    raw = json.dumps(snapshot).encode(); path.write_bytes(raw)
    path.with_suffix('.sha256').write_text(hashlib.sha256(raw).hexdigest())
    result = challenger_grading.grade_due(20000., fetcher=lambda u: raw_stats if '.csv' in u else scoreboard())
    assert result['record'] == {'pending': 3}
    assert any('original quote' in c.get('reason', '') for c in result['checks'])
    assert not list((model.DIRECTORY / 'results').glob('*.json'))


@pytest.mark.parametrize('field', ['receiving_yards', 'rushing_yards', 'passing_yards'])
def test_signed_official_yardage_is_valid(field):
    raw = edit_stat_bytes(stats(), lambda r: r.update({field: '-3'}) if r['player_id']=='gsis-a' else None)
    assert _stats(raw, '2026_03_ATL_GB', GAME)['gsis-a'][field] == '-3'


@pytest.mark.parametrize('field', ['receptions', 'carries'])
def test_negative_count_stats_remain_invalid(field):
    raw = edit_stat_bytes(stats(), lambda r: r.update({field: '-1'}) if r['player_id']=='gsis-a' else None)
    with pytest.raises(ValueError, match='official stat'):
        _stats(raw, '2026_03_ATL_GB', GAME)


def test_nflverse_rams_alias_matches_canonical_game():
    raw = edit_stat_bytes(stats(), lambda r: r.update(team='LA' if r['team']=='ATL' else 'DEN',
                         opponent_team='DEN' if r['team']=='ATL' else 'LA'))
    assert len(_stats(raw, '2026_03_ATL_GB', 'nfl-2026-reg-03-lar-den')) == 60


def test_anonymous_zero_stat_aggregate_is_ignored():
    raw = stats().decode()
    reader = csv.DictReader(io.StringIO(raw))
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(reader)
    aggregate = {name: "0" for name in reader.fieldnames}
    aggregate.update(game_id="2026_03_ATL_GB", player_id="", season="2026",
                     week="3", season_type="REG", team="ATL", opponent_team="GB")
    writer.writerow(aggregate)
    assert len(_stats(stream.getvalue().encode(), "2026_03_ATL_GB", GAME)) == 60
    aggregate["receptions"] = "2"
    writer.writerow(aggregate)
    with pytest.raises(ValueError, match="unidentified"):
        _stats(stream.getvalue().encode(), "2026_03_ATL_GB", GAME)


def manifest(tmp_path):
    book = tmp_path / "official-gamebook.pdf"
    book.write_bytes(b"%PDF-1.4\nsynthetic NFL Game Book fixture\n")
    body = {"canonical_game_id": GAME, "reviewer": "fixture-reviewer", "reviewed_at": 20000,
            "source_url": "https://www.nfl.com/games/fixture/gamebook",
            "source_file": str(book),
            "source_sha256": hashlib.sha256(book.read_bytes()).hexdigest(),
            "players": [
                {"gsis_player_id": "gsis-c", "active_roster": False,
                 "snap_played": False, "certified_no_stats": True},
                {"gsis_player_id": "gsis-d", "active_roster": True,
                 "snap_played": False, "certified_no_stats": True}]}
    return json.dumps(body).encode()


@pytest.mark.parametrize('url,accepted', [
    ('https://static.www.nfl.com/image/upload/gamecenter/official.pdf', True),
    ('https://static.www.nfl.com.evil.test/official.pdf', False),
    ('https://static.www.nfl.com@evil.test/official.pdf', False),
    ('http://static.www.nfl.com/official.pdf', False),
    ('https://static.www.nfl.com:444/official.pdf', False),
])
def test_official_gamebook_cdn_requires_exact_https_host(tmp_path, url, accepted):
    from edgeterminal.nfl_prop_result_ingest import _participation
    body = json.loads(manifest(tmp_path))
    body['source_url'] = url
    if accepted:
        assert _participation(json.dumps(body).encode(), GAME, {'gsis-c', 'gsis-d'})[0]['source_url'] == url
    else:
        with pytest.raises(ValueError, match='Game Book URL'):
            _participation(json.dumps(body).encode(), GAME, {'gsis-c', 'gsis-d'})


def test_review_manifest_cannot_inject_arbitrary_official_stats(tmp_path):
    from edgeterminal.nfl_prop_result_ingest import _participation
    body = json.loads(manifest(tmp_path))
    body['players'][0]['official_stats'] = {'receptions': 99}
    with pytest.raises(ValueError, match='deterministic'):
        _participation(json.dumps(body).encode(), GAME, {'gsis-c', 'gsis-d'})


@pytest.mark.parametrize('fails', [False, True])
def test_builtin_official_fallback_grades_without_a_human_manifest_and_repeats_safely(setup, monkeypatch, fails):
    from edgeterminal import nfl_official_participation, nfl_prop_grading
    sha, tmp_path = setup
    frozen_before = db.q1('SELECT payload FROM nfl_prop_research_freezes')['payload']
    selected = json.dumps({'selections': [{'gsis_player_id': 'gsis-c'}, {'gsis_player_id': 'gsis-d'}]})
    db.ex('INSERT INTO nfl_top_five_selections VALUES(?,?,?,?,?)',
          (GAME, sha, 30., selected, hashlib.sha256(selected.encode()).hexdigest()))
    selected_before = db.q1('SELECT payload FROM nfl_top_five_selections')['payload']
    book = tmp_path / 'automatic.pdf'; book.write_bytes(b'%PDF-1.4 official-source fixture')
    source = {'source': 'https://static.www.nfl.com/official.pdf', 'file': str(book),
              'sha256': hashlib.sha256(book.read_bytes()).hexdigest(), 'retrieved_at': 20000.,
              'supporting_sources': []}
    def resolve(*args):
        if fails: raise ValueError('official evidence ambiguous')
        return {'reviewer': 'automatic-fixture', 'reviewed_at': args[-1], 'players': {
            'gsis-c': {'active_roster': False, 'snap_played': False, 'official_stats': {}},
            'gsis-d': {'active_roster': True, 'snap_played': True, 'official_stats': {'receptions': 0.}}}}, {**source, 'supporting_sources': []}
    monkeypatch.setattr(nfl_official_participation, 'resolve', resolve)
    def run(now=20000.):
        return sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                    scoreboard_source='fixture://scoreboard', stats_source='fixture://stats',
                    now=now, automatic_participation=True)
    result = run()
    grades = {r['gsis_player_id']: r for r in nfl_prop_grading._latest(sha)}
    if fails:
        assert not {'gsis-c', 'gsis-d'} & grades.keys()
        assert result['official_checks'][0]['status'] == 'needs_review'
    else:
        assert grades['gsis-c']['result'] == 'void'
        assert grades['gsis-d']['result'] == 'lost' and grades['gsis-d']['actual_stat'] == 0
        assert grades['gsis-d']['participation_evidence']['manifest_sha256']
        assert result['official_checks'][0]['status'] == 'resolved'
    assert run(20001.)['grade_counts'][0]['new_revisions'] == 0
    assert db.q1('SELECT payload FROM nfl_prop_research_freezes')['payload'] == frozen_before
    assert db.q1('SELECT payload FROM nfl_top_five_selections')['payload'] == selected_before


@pytest.mark.parametrize('status,now', [('STATUS_IN_PROGRESS', 20000.), ('STATUS_FINAL', 200.)])
def test_official_fallback_never_runs_before_completed_game_delay(setup, monkeypatch, status, now):
    from edgeterminal import nfl_official_participation
    sha, _ = setup
    monkeypatch.setattr(nfl_official_participation, 'resolve', lambda *args: pytest.fail('premature official lookup'))
    result = sync(sha, scoreboard_bytes=scoreboard(status), scoreboard_source='fixture://scoreboard',
                  stats_bytes=stats(), stats_source='fixture://stats', now=now, automatic_participation=True)
    assert result['grade_counts'] == []
    assert db.q1('SELECT COUNT(*) n FROM nfl_prop_grade_revisions')['n'] == 0


def test_result_sync_wait_pending_participation_revision_and_idempotency(setup):
    sha, tmp_path = setup
    assert status_view(sha, now=99)["counts"] == {"not_started": 1}
    before = db.q1("SELECT payload FROM nfl_prop_research_freezes")["payload"]
    in_play = sync(sha, scoreboard_bytes=scoreboard("STATUS_IN_PROGRESS"),
                   scoreboard_source="fixture://scoreboard",
                   now=101)
    assert in_play["status"]["counts"] == {"in_progress": 1}
    assert in_play["network_requests"] == 0
    waiting = sync(sha, scoreboard_bytes=scoreboard(),
                   scoreboard_source="fixture://scoreboard", now=200)
    assert waiting["status"]["counts"] == {"final_wait": 1}
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"] == 0
    first = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                 scoreboard_source="fixture://scoreboard", stats_source="fixture://stats", now=20000)
    assert first["grade_counts"][0]["new_revisions"] == 2
    report = json.loads((tmp_path / "forward-report.json").read_text())
    assert report["prospective_2026"]["quote_sides_graded"] == 2
    assert "retrospective_validation" in report
    assert first["status"]["counts"] == {"participation_pending": 1}
    assert first["status"]["players_pending_participation"] == 2
    assert sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                scoreboard_source="fixture://scoreboard", stats_source="fixture://stats",
                now=20001)["grade_counts"][0]["new_revisions"] == 0
    filled = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                  participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                  stats_source="fixture://stats", now=20002)
    assert filled["grade_counts"][0]["new_revisions"] == 2
    assert filled["status"]["counts"] == {"graded": 1}
    grades = [json.loads(r["payload"]) for r in db.q("SELECT payload FROM nfl_prop_grade_revisions")]
    assert {r["player_id"]: r["player_status"] for r in grades} == {
        "sgo:1": "active", "sgo:2": "active", "sgo:3": "inactive", "sgo:4": "active"}
    assert next(r for r in grades if r["player_id"] == "sgo:4")["actual_stat"] == 0
    assert next(r for r in grades if r["player_id"] == "sgo:4")["participation_evidence"]["manifest_sha256"]
    assert next(r for r in grades if r["player_id"] == "sgo:4")["participation_evidence"]["snap_played"] is False
    assert next(r for r in grades if r["player_id"] == "sgo:2")["participation_evidence"]["snap_played"] is True
    assert {r["player_id"]: r["participation_class"] for r in grades} == {
        "sgo:1": "participated_stat_recorded", "sgo:2": "participated_target_stat_zero",
        "sgo:3": "confirmed_inactive", "sgo:4": "active_zero_no_snap"}
    ingestions = db.q1("SELECT COUNT(*) n FROM nfl_prop_result_ingestions")["n"]
    assert sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
                participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                stats_source="fixture://stats", now=20003)["grade_counts"][0]["new_revisions"] == 0
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_result_ingestions")["n"] == ingestions
    corrected = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(6),
                     participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                     stats_source="fixture://stats", now=20004)
    assert corrected["grade_counts"][0]["corrections"] == 1
    assert corrected["status"]["source_corrections_detected"] == 1
    assert sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(6),
                participation_bytes=manifest(tmp_path), scoreboard_source="fixture://scoreboard",
                stats_source="fixture://stats", now=20005)["grade_counts"][0]["new_revisions"] == 0
    score_corrected = sync(sha, scoreboard_bytes=scoreboard(score=24),
                           stats_bytes=stats(6), participation_bytes=manifest(tmp_path),
                           scoreboard_source="fixture://scoreboard", stats_source="fixture://stats",
                           now=20006)
    assert score_corrected["grade_counts"][0]["corrections"] == 4
    assert score_corrected["status"]["source_corrections_detected"] == 2
    assert score_corrected["status"]["games"][0]["corrected_quote_revisions"] == 5
    history = [json.loads(r["payload"]) for r in db.q(
        "SELECT payload FROM nfl_prop_grade_revisions WHERE quote_id=1 ORDER BY revision")]
    assert [r["actual_stat"] for r in history] == [5, 6, 6]
    assert history[1]["previous_revision"] == 1
    assert history[1]["previous_evidence_sha"] == history[0]["evidence_sha"]
    assert db.q1("SELECT payload FROM nfl_prop_research_freezes")["payload"] == before
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_source_artifacts")["n"] >= 4
    with pytest.raises(sqlite3.IntegrityError):
        db.ex("DELETE FROM nfl_prop_result_ingestions")


def test_identity_incomplete_stats_and_bad_gamebook_fail_closed(setup):
    sha, tmp_path = setup
    wrong = json.loads(scoreboard())
    wrong["events"][0]["competitions"][0]["competitors"][0]["team"]["displayName"] = "Chicago Bears"
    with pytest.raises(ValueError, match="identity"):
        sync(sha, scoreboard_bytes=json.dumps(wrong).encode(),
             scoreboard_source="fixture://scoreboard", now=20000)
    with pytest.raises(ValueError, match="source URL"):
        sync(sha, scoreboard_bytes=scoreboard(), now=20000)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_source_artifacts")["n"] == 0
    short = stats().splitlines()[:20]
    result = sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=b"\n".join(short),
                  scoreboard_source="fixture://scoreboard", stats_source="fixture://stats", now=20000)
    assert result["grade_counts"] == []
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"] == 0
    bad = json.loads(manifest(tmp_path))
    bad["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        sync(sha, scoreboard_bytes=scoreboard(), stats_bytes=stats(),
             participation_bytes=json.dumps(bad).encode(),
             scoreboard_source="fixture://scoreboard", stats_source="fixture://stats", now=20001)
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_grade_revisions")["n"] == 0


def test_moved_earlier_kickoff_uses_current_schedule_to_start_capture(setup):
    sha, _ = setup
    with db.tx() as c:
        c.execute("INSERT INTO events(id,sport,league,start_time) VALUES(?,?,?,?)",
                  (GAME, "NFL", "NFL", "1970-01-01T00:00:50Z"))
    result = sync(sha, scoreboard_bytes=scoreboard("STATUS_IN_PROGRESS",
                                                 kickoff="1970-01-01T00:00:50Z"),
                  scoreboard_source="fixture://scoreboard", now=75)
    assert result["status"]["counts"] == {"in_progress": 1}
    assert db.q1("SELECT COUNT(*) n FROM nfl_prop_result_ingestions")["n"] == 1
