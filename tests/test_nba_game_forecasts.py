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


def artifact(kind, body, at=NOW - 10, url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261030&limit=100'):
    raw = json.dumps(body).encode()
    return nba_store.save_artifact({'kind': kind, 'body': raw, 'sha256': hashlib.sha256(raw).hexdigest(),
        'received_at': at, 'url': url, 'http_status': 200})


def seed(legacy_market=False):
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
        'market': market if legacy_market else {},
        **({'model': {'version': 'legacy'}} if legacy_market else {}),
        **({'projection': dict(projection, model_version='legacy')} if legacy_market else {})}, [sid, history_source], now=NOW - 9)
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
    sid = artifact('scoreboard', raw, url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261030&limit=100')
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


def test_null_feed_revision_keeps_sealed_spread_for_comparison():
    from edgeterminal import nba_ops
    _, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    second = save(dict(projection, home_margin=8, margin=8, home_score=114, away_score=106,
                       proj_home=114, proj_away=106), {}, manifest, sources, NOW + 600)
    compared = nba_ops.comparison_market(GID, second)
    assert compared.get('home_spread') == -2.5
    assert compared['line_refs']['spread']['origin_forecast_id'] == first['forecast_id']
    assert compared['line_refs']['spread']['observed_at'] == market['observed_at']
    assert second['payload']['market'] == {}
    assert second['payload']['projection']['home_margin'] == 8


def total_market(total=220.5, at=NOW + 100):
    game = nba_store.get_game(GID)
    comp = {'date': iso(TIP), 'status': {'type': {'state': 'pre', 'completed': False}},
            'competitors': [{'homeAway': 'home', 'team': {'id': '2', 'abbreviation': 'BOS'}},
                            {'homeAway': 'away', 'team': {'id': '18', 'abbreviation': 'NY'}}],
            'odds': [{'provider': {'name': 'FanDuel'}, 'total': {
                'over': {'close': {'line': 'o'+str(total), 'odds': '-115'}},
                'under': {'close': {'line': 'u'+str(total), 'odds': '-105'}}}}]}
    sid = artifact('scoreboard', {'events': [{'id': game['espn_event_id'], 'date': iso(TIP), 'competitions': [comp]}]}, at)
    return dict(total=total, over_price=-115, under_price=-105, source='ESPN',
                source_artifact_id=sid, source_sha256=nba_store.get_artifact(sid)['sha256'],
                observed_at=at, sportsbook='FanDuel'), sid


def test_partial_markets_keep_independent_offer_provenance_and_original_clocks():
    from edgeterminal import nba_ops, nba_game_lines
    _, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    total, sid = total_market()
    second = save(projection, total, manifest, sources+[sid], NOW + 200)
    comparison = nba_ops.comparison_market(GID, second)
    assert comparison['home_spread'] == -2.5 and comparison['total'] == 220.5
    assert 'source_artifact_id' not in comparison  # no synthetic common source
    assert comparison['line_refs']['spread']['origin_forecast_id'] == first['forecast_id']
    assert comparison['line_refs']['total']['basis'] == 'fresh'
    assert nba_game_lines.effective_market(second, 'spread')['sportsbook'] == 'DraftKings'
    assert nba_game_lines.effective_market(second, 'total')['sportsbook'] == 'FanDuel'
    assert nba_game_lines.effective_market(second, 'spread')['observed_at'] == NOW - 10
    assert second['payload']['market'] == total
    # Another empty response must retain direct origins, not form a copy chain.
    third = save(projection, {}, manifest, sources+[sid], NOW + 300)
    assert third['payload']['line_context']['groups']['spread']['origin_forecast_id'] == first['forecast_id']
    assert third['payload']['line_context']['groups']['total']['origin_forecast_id'] == second['forecast_id']
    assert nba_game_lines.effective_market(third, 'total')['observed_at'] == NOW + 100
    assert save(projection, {}, manifest, sources+[sid], NOW + 400)['forecast_id'] == third['forecast_id']


def test_new_complete_market_replaces_only_its_group():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    total, sid = total_market()
    second = save(projection, total, manifest, sources+[sid], NOW+200)
    total2, sid2 = total_market(222.5, NOW+300)
    third = save(projection, total2, manifest, sources+[sid, sid2], NOW+400)
    assert nba_game_lines.effective_market(third, 'total')['total'] == 222.5
    assert nba_game_lines.effective_market(third, 'spread')['home_spread'] == -2.5
    assert third['payload']['line_context']['groups']['total']['basis'] == 'fresh'


def test_retained_source_tampering_is_an_error_not_a_search_for_older_quotes():
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    db.ex('DROP TRIGGER nba_source_artifacts_no_update')
    db.ex('UPDATE nba_source_artifacts SET body=? WHERE id=?', (b'{}', market['source_artifact_id']))
    with pytest.raises(ValueError, match='hash'):
        save(projection, {}, manifest, sources, NOW+200)


def test_retention_refuses_changed_kickoff_and_sealed_wrong_identity():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    game = nba_store.get_game(GID)
    with pytest.raises(ValueError, match='identity|chronology'):
        nba_game_lines.verify_origin(first, dict(game, tipoff_ts=TIP+3600), 'spread')
    changed = copy.deepcopy(first);changed['payload']['projection']['home_margin'] = 999
    with pytest.raises(ValueError, match='hash'):
        nba_game_lines.verify_origin(changed, game, 'spread')


def test_retention_refuses_future_origin_and_injected_binding():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    second = save(projection, {}, manifest, sources, NOW+200)
    changed = copy.deepcopy(second)
    changed['payload']['line_context']['groups']['spread']['market']['home_spread'] = -9.5
    with pytest.raises(ValueError, match='hash'):
        nba_game_lines.verify(changed)
    context = nba_game_lines.resolve(nba_store.get_game(GID), {}, NOW+300)
    context['groups']['spread']['origin_as_of'] = NOW+1000
    with pytest.raises(ValueError, match='binding'):
        live.save_revision(GID, projection, {}, manifest, sources, now=NOW+300, line_context=context)


def test_performance_does_not_count_a_grade_for_another_selected_forecast():
    from edgeterminal import nba_ops
    nba_ops.ensure_schema()
    _, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    second = save(dict(projection, total=222, home_score=114, away_score=108, proj_home=114, proj_away=108),
                  {}, manifest, sources, NOW+100)
    payload = json.dumps(dict(winners='won', forecast_id=first['forecast_id'], forecast_sha256=first['payload_sha256'], forecast_policy_id=live.POLICY_ID), sort_keys=True)
    digest = hashlib.sha256((payload+':0').encode()).hexdigest()
    db.ex('INSERT INTO nba_game_grade_revisions(game_id,source_artifact_id,graded_at,payload,sha256) VALUES(?,?,?,?,?)',
          (GID, sources[0], TIP+100, payload, digest))
    assert nba_ops._game_outcome(GID, second) == {}
    assert nba_ops._game_outcome(GID, first)['winners'] == 'won'


def test_total_capture_uses_the_same_retained_line_without_changing_fresh_input():
    from edgeterminal import nba_totals_stage_outer
    _, projection, market, manifest, sources = seed()
    total, sid = total_market()
    first = save(projection, total, manifest, sources+[sid], NOW+200)
    second = save(projection, {}, manifest, sources+[sid], NOW+300)
    record = nba_totals_stage_outer._capture_record(second, dict(game_id=GID, tipoff=TIP, as_of=NOW+300))
    assert record['saved_total_line'] == 220.5
    assert record['market']['source_artifact_id'] == sid
    assert second['payload']['market'] == {}
    assert record['market']['origin_forecast_id'] == first['forecast_id']


def test_incomplete_spread_group_does_not_mix_its_side_with_a_retained_offer():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    total, sid = total_market()
    partial = dict(total, home_spread=-4.5, home_price=-120, away_spread=None)
    second = save(projection, partial, manifest, sources+[sid], NOW+200)
    comparison = nba_game_lines.effective_market(second)
    assert comparison['home_spread'] == -2.5
    assert comparison['away_spread'] == 2.5
    assert comparison['home_price'] == -110
    assert comparison['line_refs']['spread']['origin_forecast_id'] == first['forecast_id']
    assert comparison['total'] == 220.5


def test_explicit_suspension_is_not_silently_declared_an_empty_feed():
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    with pytest.raises(ValueError, match='suspended'):
        save(projection, dict(market, home_spread=None, away_spread=None, suspended=True), manifest, sources, NOW+200)


def test_producer_resolves_comparison_before_inference_but_does_not_launder_fresh_prices(monkeypatch):
    from edgeterminal import nba_ops, nba_game_lines, nba_spread_model, nba_spread_data
    _, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    game = nba_store.get_game(GID)
    calls = []
    real_resolve = nba_game_lines.resolve
    def resolved(*args, **kwargs):
        calls.append('resolve')
        return real_resolve(*args, **kwargs)
    def project(*args, **kwargs):
        assert calls[0] == 'resolve'
        assert kwargs['market'] == {}  # retained comparison is not a fresh numeric input
        calls.append('project')
        return dict(projection, selected_rows=manifest['rows'], feature_cutoff=manifest['feature_cutoff'],
                    parameter_fingerprint='fixture', training_cutoff='2026-10-01T00:00:00Z')
    monkeypatch.setattr(nba_game_lines, 'resolve', resolved)
    monkeypatch.setattr(nba_spread_data, 'enrich_from_boxes', lambda history, stamp: history)
    monkeypatch.setattr(nba_ops.nba_game_context, 'schedules', lambda *args: {})
    monkeypatch.setattr(nba_ops.nba_game_context, 'availability', lambda *args: [])
    monkeypatch.setattr(nba_spread_model, 'project_game', project)
    monkeypatch.setattr(nba_ops, '_totals_projection', lambda game, value, history, stamp: (value, None))
    monkeypatch.setattr(nba_ops.time, 'time', lambda: NOW+300)
    assert nba_ops._refresh_game_projection(game, manifest['rows'], NOW+300)
    newest = live.selected(GID)
    assert newest['payload']['market'] == {}
    assert nba_game_lines.effective_market(newest)['home_spread'] == -2.5
    assert newest['payload']['line_context']['groups']['spread']['origin_forecast_id'] == first['forecast_id']


def test_same_observation_clock_cannot_hide_a_conflicting_offer():
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    raw = json.loads(nba_store.get_artifact(sources[0])['body'])
    raw['events'][0]['competitions'][0]['odds'][0]['pointSpread']['home']['close']['line'] = '-3.5'
    raw['events'][0]['competitions'][0]['odds'][0]['pointSpread']['away']['close']['line'] = '+3.5'
    sid = artifact('scoreboard', raw)
    changed = dict(market, home_spread=-3.5, away_spread=3.5, source_artifact_id=sid,
                   source_sha256=nba_store.get_artifact(sid)['sha256'])
    with pytest.raises(ValueError, match='contradictory'):
        save(projection, changed, manifest, sources+[sid], NOW+200)


def test_unapproved_source_cannot_be_hidden_as_an_empty_response():
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    source = nba_store.get_artifact(sources[0])
    sid = artifact('scoreboard', json.loads(source['body']), NOW+100, 'https://example.test/scoreboard')
    empty = {k: v for k, v in market.items() if k in ('source', 'sportsbook')}
    empty.update(source_artifact_id=sid, source_sha256=nba_store.get_artifact(sid)['sha256'], observed_at=NOW+100)
    with pytest.raises(ValueError, match='approved origin'):
        save(projection, empty, manifest, sources+[sid], NOW+200)


def test_context_cannot_omit_a_new_complete_market():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    context = nba_game_lines.resolve(nba_store.get_game(GID), market, NOW)
    context['groups'] = {}
    with pytest.raises(ValueError, match='omitted'):
        live.save_revision(GID, projection, market, manifest, sources, now=NOW, line_context=context)


def test_parent_advance_during_work_refuses_publication(monkeypatch):
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    real = live._validate_manifest
    fired = []
    def moved(*args, **kwargs):
        if not fired:
            fired.append(True)
            save(dict(projection, total=222, home_score=114, away_score=108,
                      proj_home=114, proj_away=108), market, manifest, sources, NOW+100)
        return real(*args, **kwargs)
    monkeypatch.setattr(live, '_validate_manifest', moved)
    with pytest.raises(ValueError, match='parent changed'):
        save(dict(projection, total=224, home_score=115, away_score=109,
                  proj_home=115, proj_away=109), {}, manifest, sources, NOW+200)


def test_moneyline_retention_is_one_coherent_offer_and_preserves_prices():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    raw = json.loads(nba_store.get_artifact(sources[0])['body'])
    raw['events'][0]['competitions'][0]['odds'][0]['moneyline'] = {
        'home': {'close': {'odds': '-140'}}, 'away': {'close': {'odds': '+120'}}}
    sid = artifact('scoreboard', raw)
    moneyline = dict(market, home_moneyline=-140, away_moneyline=120,
                     source_artifact_id=sid, source_sha256=nba_store.get_artifact(sid)['sha256'])
    first = save(projection, moneyline, manifest, sources+[sid])
    second = save(projection, {}, manifest, sources+[sid], NOW+200)
    effective = nba_game_lines.effective_market(second, 'moneyline')
    assert effective['home_moneyline'] == -140 and effective['away_moneyline'] == 120
    assert effective['observed_at'] == NOW-10
    assert effective['origin_forecast_id'] == first['forecast_id']


@pytest.mark.parametrize('mutation, match', [('unknown_book','named sportsbook'), ('source_provider','approved ESPN'),
                                           ('numeric_bool','finite'), ('missing_total_side','matching source sides|one archived sportsbook'),
                                           ('duplicate_offer','ambiguous'), ('bad_route','route')])
def test_comparison_source_security_rejects_ambiguous_or_untrusted_groups(mutation, match):
    _, projection, market, manifest, sources = seed()
    total, sid = total_market()
    raw = json.loads(nba_store.get_artifact(sid)['body'])
    url = nba_store.get_artifact(sid)['url']
    if mutation == 'unknown_book': total['sportsbook'] = 'Unknown'
    if mutation == 'source_provider': total['source'] = 'untrusted'
    if mutation == 'numeric_bool': total['total'] = True
    if mutation == 'missing_total_side': del raw['events'][0]['competitions'][0]['odds'][0]['total']['under']
    if mutation == 'duplicate_offer':
        offer = copy.deepcopy(raw['events'][0]['competitions'][0]['odds'][0])
        offer['total']['over']['close']['odds'] = '-120'
        raw['events'][0]['competitions'][0]['odds'].append(offer)
    if mutation == 'bad_route': url += '&unexpected=true'
    sid2 = artifact('scoreboard', raw, NOW+100, url)
    total.update(source_artifact_id=sid2, source_sha256=nba_store.get_artifact(sid2)['sha256'])
    with pytest.raises(ValueError, match=match):
        save(projection, total, manifest, sources+[sid2], NOW+200)


def test_passive_context_verification_rejects_unknown_or_missing_contracts():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    context = nba_game_lines.resolve(nba_store.get_game(GID), market, NOW)
    context['groups']['unknown'] = copy.deepcopy(context['groups']['spread'])
    with pytest.raises(ValueError, match='unknown markets'):
        live.save_revision(GID, projection, market, manifest, sources, now=NOW, line_context=context)
    context = nba_game_lines.resolve(nba_store.get_game(GID), market, NOW)
    context['policy_id'] = 'untrusted'
    with pytest.raises(ValueError, match='policy'):
        live.save_revision(GID, projection, market, manifest, sources, now=NOW, line_context=context)


def test_retained_sources_are_bound_in_new_saved_source_catalog():
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    second = save(projection, {}, manifest, [sources[1]], NOW+200)
    assert market['source_artifact_id'] in second['source_artifact_ids']
    original = nba_store.get_artifact(market['source_artifact_id'])
    assert {'id': original['id'], 'sha256': original['sha256'], 'received_at': original['received_at']} in second['payload']['source_artifacts']


def test_summary_source_identity_must_match_original_event():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    raw = json.loads(nba_store.get_artifact(sources[0])['body'])
    summary = {'header': raw['events'][0]}
    sid = artifact('summary', summary, NOW-10,
                   'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401999999')
    market.update(source_artifact_id=sid, source_sha256=nba_store.get_artifact(sid)['sha256'])
    first = save(projection, market, manifest, sources+[sid])
    assert nba_game_lines.verify_origin(first, nba_store.get_game(GID), 'spread')['market']['home_spread'] == -2.5


def test_rescheduled_game_can_publish_new_evidence_without_borrowing_old_tipoff_lines():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    save(projection, market, manifest, sources)
    old_game = nba_store.get_game(GID)
    new_tip = TIP + 3600
    raw = json.loads(nba_store.get_artifact(sources[0])['body'])
    raw['events'][0]['date'] = iso(new_tip)
    raw['events'][0]['competitions'][0]['date'] = iso(new_tip)
    sid = artifact('scoreboard', raw, NOW+100)
    moved = nba_store.upsert_game(dict(old_game, tipoff_utc=iso(new_tip)), sid, now=NOW+100)
    assert nba_game_lines.resolve(moved, {}, NOW+200)['groups'] == {}
    updated = dict(market, source_artifact_id=sid, source_sha256=nba_store.get_artifact(sid)['sha256'], observed_at=NOW+100)
    saved = save(dict(projection, tipoff=new_tip), updated, manifest, sources+[sid], NOW+200)
    assert saved['payload']['projection']['tipoff'] == new_tip
    assert nba_game_lines.effective_market(saved)['home_spread'] == -2.5


@pytest.mark.parametrize('name', ['unknown', ' UNKNOWN ', 'UnKnOwN'])
def test_unknown_sportsbook_placeholders_are_case_insensitive(name):
    _, projection, market, manifest, sources = seed()
    raw = json.loads(nba_store.get_artifact(sources[0])['body'])
    raw['events'][0]['competitions'][0]['odds'][0]['provider']['name'] = name
    sid = artifact('scoreboard', raw)
    market.update(sportsbook=name, source_artifact_id=sid, source_sha256=nba_store.get_artifact(sid)['sha256'])
    with pytest.raises(ValueError, match='named sportsbook'):
        save(projection, market, manifest, sources+[sid])


def test_new_line_proofs_are_select_only_on_fresh_query_only_connection(monkeypatch):
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed()
    first = save(projection, market, manifest, sources)
    second = save(projection, {}, manifest, sources, NOW+200)
    game = nba_store.get_game(GID)
    db.conn().commit()
    path = settings.db_path
    db._conn.close()
    db._conn = sqlite3.connect(str(path))
    db._conn.row_factory = sqlite3.Row
    db._conn.execute('PRAGMA query_only=ON')
    monkeypatch.setattr(nba_store, '_ready', None)
    statements = []
    db._conn.set_trace_callback(statements.append)
    assert nba_game_lines.verify_origin(first, game, 'spread')['market']['home_spread'] == -2.5
    assert nba_game_lines.verify(second)['groups']['spread']['basis'] == 'retained'
    assert nba_game_lines.effective_market(second)['home_spread'] == -2.5
    assert nba_game_lines.resolve(game, {}, NOW+300)['groups']['spread']['basis'] == 'retained'
    assert live.selected(GID)['forecast_id'] == second['forecast_id']
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert db._conn.total_changes == 0


def test_first_automatic_revision_retains_verified_priced_legacy_origin():
    from edgeterminal import nba_game_lines
    _, projection, market, manifest, sources = seed(legacy_market=True)
    second = save(projection, {}, manifest, sources, NOW+200)
    effective = nba_game_lines.effective_market(second)
    assert effective['home_spread'] == -2.5
    entry = second['payload']['line_context']['groups']['spread']
    assert entry['origin_kind'] == 'legacy-game'
    assert entry['origin_forecast_id'] == 'game-'+GID
    assert entry['market']['observed_at'] == NOW-10


@pytest.mark.parametrize('mutation', ['model', 'team', 'source_catalog', 'source_hash', 'payload_hash'])
def test_unsupported_priced_legacy_never_gets_erased_by_first_empty_revision(mutation):
    from edgeterminal import nba_game_lines
    legacy, projection, market, manifest, sources = seed(legacy_market=True)
    changed = copy.deepcopy(legacy)
    if mutation == 'model': changed['payload']['model']['version'] = 'other'
    if mutation == 'team': changed['payload']['projection']['home'] = 'MIA'
    if mutation == 'source_catalog': changed['source_artifact_ids'] = [sources[1]]
    if mutation == 'source_hash': changed['payload']['market']['source_sha256'] = '0'*64
    if mutation == 'payload_hash': changed['payload_sha256'] = '0'*64
    with pytest.raises(ValueError, match='legacy|hash|source'):
        nba_game_lines.verify_legacy_origin(changed, nba_store.get_game(GID), 'spread')
    assert live.selected(GID)['forecast_id'] == legacy['forecast_id']


def test_legacy_retention_is_direct_and_idempotent_after_multiple_empty_revisions():
    from edgeterminal import nba_game_lines
    legacy, projection, market, manifest, sources = seed(legacy_market=True)
    first = save(projection, {}, manifest, sources, NOW+200)
    second = save(dict(projection, total=222, home_score=114, away_score=108, proj_home=114, proj_away=108),
                  {}, manifest, sources, NOW+300)
    entry = second['payload']['line_context']['groups']['spread']
    assert entry['origin_forecast_id'] == legacy['forecast_id']
    assert entry['origin_kind'] == 'legacy-game'
    assert nba_game_lines.effective_market(second)['home_spread'] == -2.5
    assert first['forecast_id'] != second['forecast_id']


def test_unverifiable_priced_legacy_preserves_original_instead_of_publishing_empty_revision():
    legacy, projection, market, manifest, sources = seed(legacy_market=True)
    # Owned fixture simulates an earlier legacy writer without a model seal.
    db.ex('DROP TRIGGER nba_forecasts_no_update')
    payload = copy.deepcopy(legacy['payload']); payload.pop('model')
    encoded = json.dumps(payload, sort_keys=True, separators=(',',':'))
    db.ex('UPDATE nba_forecasts SET payload=?,payload_sha256=? WHERE forecast_id=?',
          (encoded, hashlib.sha256(encoded.encode()).hexdigest(), legacy['forecast_id']))
    with pytest.raises(ValueError, match='unsupported'):
        save(projection, {}, manifest, sources, NOW+200)
    assert live.selected(GID)['forecast_id'] == legacy['forecast_id']
    assert db.q1("SELECT COUNT(*) n FROM nba_forecasts WHERE json_extract(payload,'$.kind')='game-revision'")['n'] == 0


def test_reschedule_does_not_carry_or_get_stuck_on_a_priced_legacy_origin():
    from edgeterminal import nba_game_lines
    legacy, projection, market, manifest, sources = seed(legacy_market=True)
    old = nba_store.get_game(GID)
    shifted = nba_store.upsert_game(dict(old, tipoff_utc=iso(TIP+3600)), sources[0], now=NOW+100)
    assert nba_game_lines.resolve(shifted, {}, NOW+200)['groups'] == {}
    with pytest.raises(ValueError, match='unsupported'):
        nba_game_lines.verify_legacy_origin(legacy, shifted, 'spread')
