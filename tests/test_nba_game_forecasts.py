"""Future-only NBA game revisions: immutable inputs and deterministic scoring."""
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from edgeterminal import db, nba_data, nba_game_forecasts as live, nba_store
from edgeterminal.config import settings


TIP = datetime(2026, 10, 30, 23, tzinfo=timezone.utc).timestamp()
NOW = TIP - 12 * 3600
GID = 'nba-2026-401999999'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "nba.db"}')
    monkeypatch.setattr(db, 'now', lambda: NOW)
    nba_store.ensure_schema()
    yield
    db._conn.close()
    db._conn = None


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def artifact(kind, body, at=NOW - 10, url='https://site.api.espn.com/fixture'):
    raw = json.dumps(body).encode()
    return nba_store.save_artifact({'kind': kind, 'body': raw, 'sha256': hashlib.sha256(raw).hexdigest(),
        'received_at': at, 'url': url, 'http_status': 200})


def seed():
    comp = {'date': iso(TIP), 'status': {'type': {'state': 'pre', 'completed': False}},
            'competitors': [{'homeAway': 'home', 'team': {'id': '2', 'abbreviation': 'BOS'}},
                            {'homeAway': 'away', 'team': {'id': '18', 'abbreviation': 'NY'}}],
            'odds': [{'provider': {'name': 'DraftKings'}, 'pointSpread': {
                'home': {'close': {'line': '-2.5', 'odds': '-110'}},
                'away': {'close': {'line': '+2.5', 'odds': '-110'}}}}]}
    sid = artifact('scoreboard', {'events': [{'id': '401999999', 'date': iso(TIP), 'competitions': [comp]}]})
    game = {'game_id': GID, 'espn_event_id': '401999999', 'season_start_year': 2026,
            'season_label': '2026-27', 'season_type': 'regular', 'competition': 'NBA',
            'home_team': 'BOS', 'away_team': 'NYK', 'tipoff_utc': iso(TIP), 'status': 'scheduled',
            'home_score': None, 'away_score': None}
    nba_store.upsert_game(game, sid, now=NOW - 10)
    events, rows = [], []
    for i in range(20):
        stamp = TIP - (i + 2) * 86400
        events.append({'id': str(401800000 + i), 'date': iso(stamp), 'seasonType': {'type': 2},
            'competitions': [{'status': {'type': {'completed': True}}, 'competitors': [
                {'homeAway': 'home', 'team': {'abbreviation': 'BOS'}, 'score': {'value': 110}},
                {'homeAway': 'away', 'team': {'abbreviation': 'NY'}, 'score': {'value': 100}}]}]})
        for team, opp, points, conceded, home in [('BOS', 'NYK', 110, 100, True), ('NYK', 'BOS', 100, 110, False)]:
            rows.append({'team': team, 'opponent': opp, 'game_id': str(401800000 + i),
                         'played_at': iso(stamp), 'points': points, 'opponent_points': conceded, 'home': home})
    history_source = artifact('team-history', {'events': events}, url='https://site.api.espn.com/history')
    for row in rows:
        row['source_artifact_id'] = history_source
    projection = {'status': 'ok', 'home': 'BOS', 'away': 'NYK', 'tipoff': TIP, 'home_margin': 6,
                  'margin': 6, 'total': 220, 'home_score': 113, 'away_score': 107,
                  'proj_home': 113, 'proj_away': 107, 'model_version': 'fixture-game-v1',
                  'observations': {'home': 20, 'away': 20}}
    market = {'home_spread': -2.5, 'away_spread': 2.5, 'home_price': -110, 'away_price': -110,
              'source': 'ESPN', 'observed_at': NOW - 10, 'source_artifact_id': sid,
              'source_sha256': nba_store.get_artifact(sid)['sha256'], 'sportsbook': 'DraftKings'}
    manifest = {'rows': rows, 'feature_cutoff': NOW - 4 * 3600, 'coverage': {'complete': True}}
    legacy = nba_store.save_forecast('game-' + GID, GID, 'legacy', {'kind': 'game', 'projection': projection,
        'market': {}}, [sid, history_source], now=NOW - 9)
    return legacy, projection, market, manifest, [sid, history_source]


def save(projection, market, manifest, sources, at=NOW):
    return live.save_revision(GID, projection, market, manifest, sources, now=at)


def test_reads_do_not_activate_or_enroll_games():
    legacy, *_ = seed()
    before = db.conn().total_changes
    assert live.selected(GID, legacy)['forecast_id'] == legacy['forecast_id']
    assert live.policy() is None
    assert db.conn().total_changes == before


def test_fresh_revision_is_append_only_and_selected_without_mutating_original():
    legacy, projection, market, manifest, sources = seed()
    raw = db.q1('SELECT payload,payload_sha256 FROM nba_forecasts WHERE forecast_id=?', (legacy['forecast_id'],))
    saved = save(projection, market, manifest, sources)
    assert saved['payload']['kind'] == 'game-revision'
    assert saved['payload']['policy_id'] == live.POLICY_ID
    assert saved['payload']['market'] == market
    assert saved['payload']['input_manifest'] == manifest
    assert live.selected(GID, legacy)['forecast_id'] == saved['forecast_id']
    assert db.q1('SELECT payload,payload_sha256 FROM nba_forecasts WHERE forecast_id=?', (legacy['forecast_id'],)) == raw
    for table in ('nba_game_forecast_policies', 'nba_game_forecast_enrollments'):
        with pytest.raises(sqlite3.DatabaseError):
            db.ex('DELETE FROM ' + table)
    with pytest.raises(sqlite3.DatabaseError):
        db.ex('UPDATE nba_forecasts SET payload=? WHERE forecast_id=?', ('{}', saved['forecast_id']))


def test_same_snapshot_is_idempotent_and_later_source_snapshot_replaces_live_only():
    legacy, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    assert save(projection, market, manifest, sources, NOW + 301)['forecast_id'] == first['forecast_id']
    second = save(dict(projection, home_margin=7, margin=7, home_score=113.5, away_score=106.5,
                       proj_home=113.5, proj_away=106.5), market, manifest, sources, NOW + 600)
    assert second['forecast_id'] != first['forecast_id']
    assert live.selected(GID, legacy)['forecast_id'] == second['forecast_id']
    assert len(nba_store.list_forecasts(GID)) == 3


def test_no_first_enrollment_after_tipoff_and_legacy_history_stays_selected():
    legacy, projection, market, manifest, sources = seed()
    live.activate(TIP + 1)
    with pytest.raises(ValueError, match='tipoff|started|scheduled'):
        live.enroll(GID, TIP + 1)
    assert live.selected(GID, legacy)['forecast_id'] == legacy['forecast_id']
    with pytest.raises(ValueError, match='tipoff|started|scheduled'):
        save(projection, market, manifest, sources, TIP + 1)


def test_final_result_cannot_change_the_selected_revision():
    legacy, projection, market, manifest, sources = seed()
    chosen = save(projection, market, manifest, sources)
    game = nba_store.get_game(GID)
    nba_store.upsert_game(dict(game, status='final', home_score=90, away_score=130), sources[0], now=TIP + 10000)
    assert live.selected(GID, legacy)['forecast_id'] == chosen['forecast_id']
    with pytest.raises(ValueError, match='tipoff|started|scheduled'):
        save(projection, market, manifest, sources, TIP + 10000)


@pytest.mark.parametrize('mutation,match', [
    ('wrong_team', 'identity|team'), ('future_source', 'source|received'),
    ('future_row', 'cutoff|future'), ('wrong_score', 'row|score|source'),
    ('duplicate_row', 'duplicate'), ('missing_row_source', 'source'),
    ('wrong_market_line', 'market|line|quote'), ('wrong_market_source', 'market|source'),
    ('late_market', 'market|source|observed'), ('future_cutoff', 'cutoff'),
])
def test_revision_rejects_unbound_or_future_inputs(mutation, match):
    _, projection, market, manifest, sources = seed()
    if mutation == 'wrong_team': projection['home'] = 'MIA'
    if mutation == 'future_source':
        sources.append(artifact('roster', {}, NOW + 1))
    if mutation == 'future_row': manifest['rows'][0]['played_at'] = iso(NOW + 1)
    if mutation == 'wrong_score': manifest['rows'][0]['points'] += 1
    if mutation == 'duplicate_row': manifest['rows'].append(copy.deepcopy(manifest['rows'][0]))
    if mutation == 'missing_row_source': manifest['rows'][0]['source_artifact_id'] = 999999
    if mutation == 'wrong_market_line': market['home_spread'], market['away_spread'] = -3.5, 3.5
    if mutation == 'wrong_market_source': market['source_sha256'] = '0' * 64
    if mutation == 'late_market': market['observed_at'] = NOW + 1
    if mutation == 'future_cutoff': manifest['feature_cutoff'] = NOW + 1
    with pytest.raises(ValueError, match=match):
        save(projection, market, manifest, sources)
    assert not [r for r in nba_store.list_forecasts(GID) if r['payload'].get('kind') == 'game-revision']


def test_status_reports_no_revision_fallback_and_freshness():
    legacy, projection, market, manifest, sources = seed()
    live.activate(NOW)
    live.enroll(GID, NOW)
    assert live.status(NOW)['awaiting_revisions'] == 1
    assert live.selected(GID, legacy)['forecast_id'] == legacy['forecast_id']
    save(projection, market, manifest, sources)
    assert live.status(NOW + 1)['awaiting_revisions'] == 0


def test_same_timestamp_tie_is_selected_by_declared_forecast_id_order():
    legacy, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    second = save(dict(projection, home_margin=7, margin=7, home_score=113.5, away_score=106.5,
                       proj_home=113.5, proj_away=106.5), market, manifest, sources)
    assert live.selected(GID, legacy)['forecast_id'] == max(first['forecast_id'], second['forecast_id'])


@pytest.mark.parametrize('corrupt_source', ['history_body', 'quote_body', 'receipt', 'future_receipt'])
def test_final_learning_snapshots_require_exact_revision_reference_and_verified_label(corrupt_source):
    from test_nba_data import SUMMARY
    legacy, projection, market, manifest, sources = seed()
    chosen = save(projection, market, manifest, sources)
    raw = copy.deepcopy(SUMMARY)
    raw['header'].update(id='401999999', season={'year': 2027, 'type': 2})
    comp = raw['header']['competitions'][0]
    comp['date'] = iso(TIP)
    for c in comp['competitors']:
        c['team'] = {'id': '2', 'abbreviation': 'BOS'} if c['homeAway'] == 'home' else {'id': '18', 'abbreviation': 'NY'}
    for team in raw['boxscore']['players']:
        team['team'] = {'id': '2', 'abbreviation': 'BOS'} if team['team']['id'] == '25' else {'id': '18', 'abbreviation': 'NY'}
    final_source = artifact('final-boxscore', raw, TIP + 10000)
    parsed = nba_data.parse_summary(raw)
    assert parsed['box_complete']
    nba_store.upsert_game(parsed['game'], final_source, now=TIP + 10000)
    db.ex('CREATE TABLE nba_game_grade_revisions (id INTEGER PRIMARY KEY,game_id TEXT,source_artifact_id INTEGER,'
          'graded_at REAL,payload TEXT,sha256 TEXT)')
    label = {'home_score': 116, 'away_score': 103, 'spread': 'won'}
    encoded = json.dumps(label, sort_keys=True)
    db.ex('INSERT INTO nba_game_grade_revisions VALUES(1,?,?,?,?,?)',
          (GID, final_source, TIP + 10001, encoded, hashlib.sha256((encoded + ':0').encode()).hexdigest()))
    assert live.validation_snapshots(TIP + 10002) == []
    label.update(forecast_id=chosen['forecast_id'], forecast_sha256=chosen['payload_sha256'], forecast_policy_id=live.POLICY_ID)
    encoded = json.dumps(label, sort_keys=True)
    db.ex('INSERT INTO nba_game_grade_revisions VALUES(2,?,?,?,?,?)',
          (GID, final_source, TIP + 10003, encoded, hashlib.sha256((encoded + ':1').encode()).hexdigest()))
    assert live.validation_snapshots(TIP + 10002) == []
    records = live.validation_snapshots(TIP + 10004)
    assert len(records) == 1 and records[0]['actual_margin'] == 13
    assert records[0]['forecast_id'] == chosen['forecast_id']
    assert records[0]['result_observed_at'] == TIP + 10000
    assert records[0]['input_manifest'] == manifest
    # Simulate corruption below the append-only application boundary. Learning
    # must recheck archived inputs, not trust the already-hashed forecast JSON.
    db.ex('DROP TRIGGER nba_source_artifacts_no_update')
    ident = sources[0] if corrupt_source == 'quote_body' else sources[1]
    if corrupt_source.endswith('_body'):
        db.ex('UPDATE nba_source_artifacts SET body=? WHERE id=?', (b'corrupted archive', ident))
    else:
        db.ex('UPDATE nba_source_artifacts SET received_at=? WHERE id=?',
              (NOW + 1 if corrupt_source == 'future_receipt' else NOW - 1, ident))
    with pytest.raises(ValueError, match='hash|received|descriptors'):
        live.validation_snapshots(TIP + 10004)


def test_missing_market_does_not_remove_a_supported_projection():
    legacy, projection, _, manifest, sources = seed()
    revision = save(projection, {}, manifest, sources)
    assert live.selected(GID, legacy)['forecast_id'] == revision['forecast_id']
    assert revision['payload']['market'] == {}


def test_possessions_are_bound_to_verified_historical_boxscore_total_turnovers():
    from test_nba_data import SUMMARY
    _, projection, market, manifest, sources = seed()
    row = manifest['rows'][0]
    raw = copy.deepcopy(SUMMARY)
    raw['header'].update(id=row['game_id'], season={'year': 2027, 'type': 2})
    comp = raw['header']['competitions'][0]
    comp['date'] = row['played_at']
    for c in comp['competitors']:
        c['team'] = {'id': '2', 'abbreviation': 'BOS'} if c['homeAway'] == 'home' else {'id': '18', 'abbreviation': 'NY'}
    for team in raw['boxscore']['players']:
        team['team'] = {'id': '2', 'abbreviation': 'BOS'} if team['team']['id'] == '25' else {'id': '18', 'abbreviation': 'NY'}
    raw['boxscore']['teams'] = [{'team': {'id': ident, 'abbreviation': code}, 'statistics': [
        {'name': 'fieldGoalsMade-fieldGoalsAttempted', 'displayValue': '40-85'},
        {'name': 'freeThrowsMade-freeThrowsAttempted', 'displayValue': '20-25'},
        {'name': 'offensiveRebounds', 'displayValue': '10'},
        {'name': 'totalTurnovers', 'displayValue': '13'},
        {'name': 'turnovers', 'displayValue': '12'}]} for ident, code in [('2', 'BOS'), ('18', 'NY')]]
    sid = artifact('final-boxscore', raw, NOW - 20, url='https://site.api.espn.com/summary?event=' + row['game_id'])
    row.update(points=116, opponent_points=103, source_artifact_id=sid,
               possessions=99.0, possession_method='symmetric_box_estimate_044', possession_source_artifact_id=sid)
    sources.append(sid)
    revision = save(projection, market, manifest, sources)
    assert revision['payload']['input_manifest']['rows'][0]['possessions'] == 99
    row['possessions'] = 98
    with pytest.raises(ValueError, match='possessions'):
        save(projection, market, manifest, sources, NOW + 301)


def test_different_bookmakers_cannot_supply_mixed_line_and_price():
    _, projection, market, manifest, sources = seed()
    source = nba_store.get_artifact(sources[0])
    raw = json.loads(source['body'])
    comp = raw['events'][0]['competitions'][0]
    comp['odds'][0]['total'] = {'over': {'close': {'line': 'o225.5', 'odds': '-105'}}}
    comp['odds'].append({'provider': {'name': 'AnotherBook'}, 'total': {'over': {'close': {'line': 'o220.5', 'odds': '-110'}}}})
    sid = artifact('scoreboard', raw, url='https://site.api.espn.com/multi-book')
    market.update(total=220.5, over_price=-110, source_artifact_id=sid,
                  source_sha256=nba_store.get_artifact(sid)['sha256'])
    sources.append(sid)
    with pytest.raises(ValueError, match='one archived sportsbook'):
        save(projection, market, manifest, sources)


def test_validation_crossing_tipoff_cannot_backdate_a_revision(monkeypatch):
    legacy, projection, market, manifest, sources = seed()
    clock = [NOW]
    monkeypatch.setattr(db, 'now', lambda: clock[0])
    validate = live._validate_manifest
    def delayed(*args, **kwargs):
        validate(*args, **kwargs)
        clock[0] = TIP + 1
    monkeypatch.setattr(live, '_validate_manifest', delayed)
    with pytest.raises(ValueError, match='at or after tipoff'):
        save(projection, market, manifest, sources)
    assert live.selected(GID, legacy)['forecast_id'] == legacy['forecast_id']
    assert not [r for r in nba_store.list_forecasts(GID) if r['payload'].get('kind') == 'game-revision']


def test_persistence_timestamp_is_not_backdated_to_inference_start(monkeypatch):
    legacy, projection, market, manifest, sources = seed()
    clock = [NOW]
    monkeypatch.setattr(db, 'now', lambda: clock[0])
    validate = live._validate_manifest
    def delayed(*args, **kwargs):
        validate(*args, **kwargs)
        clock[0] = NOW + 10
    monkeypatch.setattr(live, '_validate_manifest', delayed)
    chosen = save(projection, market, manifest, sources)
    assert chosen['payload']['as_of'] == NOW
    assert chosen['created_at'] == NOW + 10
    assert live.selected(GID, legacy)['forecast_id'] == chosen['forecast_id']


def test_history_competitor_id_abbreviation_conflict_is_rejected():
    _, projection, market, manifest, sources = seed()
    source = nba_store.get_artifact(sources[1])
    body = json.loads(source['body'])
    body['events'][0]['competitions'][0]['competitors'][0]['team']['id'] = '25'
    sid = artifact('team-history', body, url='https://site.api.espn.com/conflicted-history')
    for row in manifest['rows']:
        row['source_artifact_id'] = sid
    sources.append(sid)
    with pytest.raises(ValueError, match='conflict'):
        save(projection, market, manifest, sources)


def _with_full_schedule(manifest, sources):
    source = nba_store.get_artifact(sources[1])
    raw = json.loads(source['body'])
    for offset in range(10):
        older = copy.deepcopy(raw['events'][0])
        older.update(id=str(401700000 + offset), date=iso(TIP - (22 + offset) * 86400))
        raw['events'].append(older)
    for event in raw['events']:
        for item in event['competitions'][0]['competitors']:
            item['team']['id'] = '2' if item['team']['abbreviation'] == 'BOS' else '18'
    sid = artifact('team-history', raw, url='https://site.api.espn.com/full-schedule')
    sources.append(sid)
    for row in manifest['rows']:
        row['source_artifact_id'] = sid
    manifest['schedule'] = {}
    for team in ('BOS', 'NYK'):
        schedule = live.nba_game_context.parse_schedule(team, raw, sid, NOW - 10)
        coverage = live._source_rows(dict(source, data=raw)) + manifest['rows']
        manifest['schedule'][team] = live.nba_game_context.team_context(schedule, team, TIP, coverage, NOW)
    return manifest


def test_full_schedule_proves_coverage_outside_the_selected_scoring_window():
    _, projection, market, manifest, sources = seed()
    _with_full_schedule(manifest, sources)
    assert all(c['complete'] for c in manifest['schedule'].values())
    assert len(manifest['rows']) == 40
    saved = save(projection, market, manifest, sources)
    assert saved['payload']['input_manifest']['schedule']['BOS']['complete'] is True


@pytest.mark.parametrize('field,bad', [
    ('complete', False), ('missing_game_ids', ['401800000']), ('stale', True),
    ('last_game_id', '401800001'), ('last_game_tipoff', TIP - 100), ('observed_at', NOW - 100),
])
def test_schedule_flags_are_rederived_from_the_full_archived_source(field, bad):
    _, projection, market, manifest, sources = seed()
    _with_full_schedule(manifest, sources)
    manifest['schedule']['BOS'][field] = bad
    with pytest.raises(ValueError, match='schedule context flags'):
        save(projection, market, manifest, sources)
