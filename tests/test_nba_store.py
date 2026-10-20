"""NBA SQLite store: immutability, sourced pretipoff locks, append-only grades."""
import sqlite3

import pytest

from edgeterminal import db, nba_data, nba_store as store
from edgeterminal.config import settings
from test_nba_data import SCOREBOARD, SUMMARY

TIP = 1792546200.0          # 2026-10-21T01:30:00Z, OKC at SAS
DET_TIP = 1792522800.0      # 2026-10-20T19:00:00Z, BOS at DET
EARLY = 1792500000.0        # morning scoreboard receipt
BEFORE, AFTER = TIP - 3600, TIP + 60
OKC, DET = 'nba-2026-401909090', 'nba-2026-401909088'
PLAYERS = ['4278073', '4433255', '4222252', '2991350', '4395702']
PROPS = ['points', 'rebounds', 'assists', 'threes', 'pra']
NAN, INF = float('nan'), float('inf')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "nba.db"}')
    store.ensure_schema()
    yield
    db._conn.close(); db._conn = None


def artifact(body=b'{"events": []}', at=EARLY, kind='scoreboard', status=200):
    return nba_data.fetch('https://site.api.espn.com/x?' + kind, kind, getter=lambda u: (status, body), now=at)


def seed():
    art = store.save_artifact(artifact())
    for g in nba_data.parse_scoreboard(SCOREBOARD, '2026-10-20')['games']:
        store.upsert_game(g, art)
    return art


def forecast(art, fid='f-okc-sas-1', game=OKC, now=BEFORE - 600):
    return store.save_forecast(fid, game, 'nba-v0', {'home_points': 112.4, 'players': {'4278073': {'points': 30.1}}},
                               [art], now=now)


def quote(art, pid, prop, line, game=OKC, side='over', price=-110, at=BEFORE - 300):
    return store.save_quote_observation(game, store.PROP_MARKETS[prop], pid, side, line, price,
                                        'manual-research', observed_at=at, source_artifact_id=art)


def priced(art, fid='f-okc-sas-1', game=OKC, at=BEFORE - 300, players=PLAYERS):
    """Five sourced main-line selections, one per player, on one game."""
    out = []
    for i, (pid, prop) in enumerate(zip(players, PROPS)):
        q = quote(art, pid, prop, 20.5 + i, game=game, at=at)
        out.append({'forecast_id': fid, 'espn_player_id': pid, 'prop': prop, 'side': 'over',
                    'line': 20.5 + i, 'quote_id': q['quote_id']})
    return out


def ready():
    art = seed()
    forecast(art)
    return art, priced(art)


# ---------------------------------------------------------------- schema / artifacts / games
def test_schema_is_nba_only_and_does_not_touch_shared_tables():
    names = {r['name'] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
    ours = {n for n in names if n.startswith('nba_')}
    assert ours == {'nba_games', 'nba_source_artifacts', 'nba_forecasts', 'nba_quote_observations',
                    'nba_selection_locks', 'nba_selection_lock_items', 'nba_grade_revisions', 'nba_cadences'}
    store.ensure_schema()   # idempotent


def test_artifacts_are_immutable_and_hash_checked():
    a = store.save_artifact(artifact())
    assert store.save_artifact(artifact()) == a                     # same receipt is idempotent
    assert store.save_artifact(artifact(at=BEFORE)) != a            # a later receipt is a new record
    row = store.get_artifact(a)
    assert row['body'] == b'{"events": []}' and row['kind'] == 'scoreboard'
    with pytest.raises(sqlite3.DatabaseError):
        db.ex('UPDATE nba_source_artifacts SET body=? WHERE id=?', (b'x', a))
    with pytest.raises(sqlite3.DatabaseError):
        db.ex('DELETE FROM nba_source_artifacts WHERE id=?', (a,))
    with pytest.raises(ValueError):
        store.save_artifact(dict(artifact(), sha256='0' * 64))


def test_games_by_eastern_date_and_identity_conflicts():
    art = seed()
    games = store.list_games('2026-10-20')
    assert [g['game_id'] for g in games] == [DET, OKC]
    assert games[1]['tipoff_ts'] == TIP and games[1]['source_artifact_id'] == art
    g = dict(nba_data.parse_scoreboard(SCOREBOARD, '2026-10-20')['games'][1])
    with pytest.raises(ValueError):
        store.upsert_game(dict(g, home_team='DET'), art)
    with pytest.raises(ValueError):
        store.upsert_game(dict(g, game_id='nba-2027-401909090'), art)
    with pytest.raises(ValueError):
        store.upsert_game(g, 999)          # untraceable source
    with pytest.raises(ValueError):
        store.upsert_game(g, store.save_artifact(artifact(status=403, body=b'denied')))


def test_final_result_is_idempotent_and_never_rewritten():
    art = store.save_artifact(artifact(kind='summary'))
    final = nba_data.parse_summary(SUMMARY)['game']
    store.upsert_game(final, art)
    store.upsert_game(final, art)
    assert store.get_game(final['game_id'])['home_score'] == 116
    with pytest.raises(ValueError):
        store.upsert_game(dict(final, home_score=117), art)
    with pytest.raises(ValueError):
        store.upsert_game(dict(final, status='scheduled', home_score=None, away_score=None), art)


# ---------------------------------------------------------------- forecasts
def test_forecast_saves_before_lines_and_is_immutable():
    art = seed()
    f = forecast(art)
    assert f['forecast_id'] == 'f-okc-sas-1' and f['source_artifact_ids'] == [art]
    assert store.list_quote_observations(OKC) == []                  # no line needed
    assert forecast(art) == f                                        # identical replay is idempotent
    with pytest.raises(ValueError):
        store.save_forecast('f-okc-sas-1', OKC, 'nba-v0', {'home_points': 1}, [art], now=BEFORE)
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("UPDATE nba_forecasts SET payload='{}'")
    with pytest.raises(ValueError):
        forecast(art, fid='late', now=AFTER)
    with pytest.raises(ValueError):
        store.save_forecast('nosrc', OKC, 'nba-v0', {}, [], now=BEFORE)
    with pytest.raises(ValueError):
        store.save_forecast('badsrc', OKC, 'nba-v0', {}, [12345], now=BEFORE)
    with pytest.raises(ValueError):
        store.save_forecast('nan', OKC, 'nba-v0', {'home_points': NAN}, [art], now=BEFORE)


def test_forecast_sources_must_be_successful_and_not_from_the_future():
    art = seed()
    denied = store.save_artifact(artifact(status=403, body=b'denied', at=EARLY + 1))
    future = store.save_artifact(artifact(body=b'{"later": 1}', at=BEFORE + 1))
    with pytest.raises(ValueError):
        store.save_forecast('f403', OKC, 'nba-v0', {}, [art, denied], now=BEFORE)
    with pytest.raises(ValueError):
        store.save_forecast('ffuture', OKC, 'nba-v0', {}, [art, future], now=BEFORE)
    # direct SQL is refused by the trigger as well
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("INSERT INTO nba_forecasts(forecast_id,game_id,model_version,created_at,payload,payload_sha256,"
              "source_artifact_ids) VALUES('raw',?,'v',?,'{}','x',?)", (OKC, BEFORE, f'[{future}]'))
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("INSERT INTO nba_forecasts(forecast_id,game_id,model_version,created_at,payload,payload_sha256,"
              "source_artifact_ids) VALUES('raw',?,'v',?,'{}','x','[]')", (OKC, BEFORE))
    assert store.list_forecasts(OKC) == []


# ---------------------------------------------------------------- quotes
def test_later_quote_is_separate_and_leaves_forecast_unchanged():
    art = seed()
    f = forecast(art)
    q = quote(art, '4278073', 'points', 29.5, price=-115)
    assert q['quote_id'] >= 1 and q['line'] == 29.5 and q['market'] == 'player_points'
    assert store.get_forecast('f-okc-sas-1') == f
    assert store.list_quote_observations(OKC) == [q]


@pytest.mark.parametrize('case', ['line_none', 'line_nan', 'line_inf', 'line_negative', 'price_none', 'price_nan',
                                  'price_inf', 'price_not_american', 'price_fraction', 'price_bool', 'no_source',
                                  'source_403', 'source_after_observation', 'observed_after_tipoff',
                                  'unknown_market', 'bad_side', 'no_player'])
def test_quote_observation_requires_sourced_finite_main_line(case):
    art = seed()
    kw = dict(game_id=OKC, market='player_points', espn_player_id='4278073', side='over', line=29.5, price=-115,
              source='manual-research', observed_at=BEFORE - 300, source_artifact_id=art)
    kw.update({
        'line_none': {'line': None}, 'line_nan': {'line': NAN}, 'line_inf': {'line': INF},
        'line_negative': {'line': -0.5}, 'price_none': {'price': None}, 'price_nan': {'price': NAN},
        'price_inf': {'price': INF}, 'price_not_american': {'price': -50}, 'price_fraction': {'price': -112.5},
        'price_bool': {'price': True}, 'no_source': {'source_artifact_id': None},
        'source_403': {'source_artifact_id': store.save_artifact(artifact(status=403, body=b'x', at=EARLY + 1))},
        'source_after_observation': {'source_artifact_id': store.save_artifact(artifact(body=b'{}', at=BEFORE))},
        'observed_after_tipoff': {'observed_at': TIP},
        'unknown_market': {'market': 'player_steals'}, 'bad_side': {'side': 'yes'},
        'no_player': {'espn_player_id': None}}[case])
    with pytest.raises(ValueError):
        store.save_quote_observation(**kw)
    assert store.list_quote_observations(OKC) == []


def test_quote_table_refuses_non_finite_values_in_sql():
    art = seed()
    for line, price in ((INF, -110), (NAN, -110), (20.5, INF), (20.5, -50), (-1.0, -110)):
        with pytest.raises(sqlite3.DatabaseError):
            db.ex("INSERT INTO nba_quote_observations(game_id,market,espn_player_id,side,line,price,source,"
                  "observed_at,source_artifact_id,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (OKC, 'player_points', '4278073', 'over', line, price, 'raw', BEFORE, art, BEFORE))


# ---------------------------------------------------------------- five-prop locks
def test_five_prop_lock_is_sealed_and_cannot_extend_or_mutate():
    art, sel = ready()
    lock = store.lock_selections('2026-10-20-top5', sel, now=BEFORE)
    assert lock['lock_id'] == '2026-10-20-top5' and lock['game_id'] == OKC and len(lock['items']) == 5
    first = lock['items'][0]
    assert first == {'rank': 1, 'forecast_id': 'f-okc-sas-1', 'game_id': OKC, 'espn_player_id': '4278073',
                     'prop': 'points', 'side': 'over', 'line': 20.5, 'quote_id': sel[0]['quote_id'],
                     'price': -110.0, 'quote_observed_at': BEFORE - 300, 'quote_source_artifact_id': art}
    assert store.lock_selections('2026-10-20-top5', sel, now=BEFORE) == lock
    with pytest.raises(ValueError):
        store.lock_selections('2026-10-20-top5', sel[::-1], now=BEFORE)
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("INSERT INTO nba_selection_lock_items(lock_id,rank,forecast_id,game_id,espn_player_id,prop,side,line,"
              "quote_id) VALUES('2026-10-20-top5',6,'f-okc-sas-1',?,'1','points','over',1.5,?)",
              (OKC, sel[0]['quote_id']))
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("UPDATE nba_selection_lock_items SET side='under'")
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("DELETE FROM nba_selection_locks")
    assert store.get_lock('2026-10-20-top5') == lock
    assert store.get_game_lock(OKC) == lock and store.get_game_lock(DET) is None


def test_second_lock_for_same_game_is_refused_even_with_another_id():
    art, sel = ready()
    store.lock_selections('first', sel, now=BEFORE)
    other = priced(art, players=['4683692', '4683778', '4432823', '4397020', '4900671'], at=BEFORE - 100)
    with pytest.raises(ValueError):
        store.lock_selections('second', other, now=BEFORE)
    assert store.get_lock('second') is None
    # direct SQL cannot bypass it either
    with pytest.raises(sqlite3.DatabaseError):
        with db.tx() as c:
            for rank, s in enumerate(other, 1):
                c.execute("INSERT INTO nba_selection_lock_items(lock_id,rank,forecast_id,game_id,espn_player_id,"
                          "prop,side,line,quote_id) VALUES('raw',?,?,?,?,?,?,?,?)",
                          (rank, s['forecast_id'], OKC, s['espn_player_id'], s['prop'], s['side'], s['line'],
                           s['quote_id']))
            c.execute("INSERT INTO nba_selection_locks(lock_id,game_id,locked_at,sha256) VALUES('raw',?,?,'x')",
                      (OKC, BEFORE))


def test_lock_must_hold_five_props_from_one_game():
    art = seed()
    forecast(art, fid='f-det', game=DET, now=DET_TIP - 7200)
    forecast(art, now=DET_TIP - 7200)
    sel = priced(art, at=DET_TIP - 7000)
    q = quote(art, '3917376', 'points', 24.5, game=DET, at=DET_TIP - 7000)
    sel[4] = {'forecast_id': 'f-det', 'espn_player_id': '3917376', 'prop': 'points', 'side': 'over',
              'line': 24.5, 'quote_id': q['quote_id']}
    with pytest.raises(ValueError):
        store.lock_selections('mixed', sel, now=DET_TIP - 3600)
    assert store.get_lock('mixed') is None


def _mutate(case, art, sel):
    s0 = sel[0]
    if case == 'four': return sel[:4], BEFORE
    if case == 'six':
        extra = quote(art, '4683692', 'points', 9.5)
        return sel + [dict(s0, espn_player_id='4683692', line=9.5, quote_id=extra['quote_id'])], BEFORE
    if case == 'duplicate': sel[1] = dict(s0)
    if case == 'bad_prop': s0['prop'] = 'steals'
    if case == 'bad_side': s0['side'] = 'yes'
    if case == 'post_tipoff': return sel, TIP
    if case == 'no_forecast': s0['forecast_id'] = 'missing'
    if case == 'line_none': s0['line'] = None
    if case == 'line_missing': s0.pop('line')
    if case == 'line_nan': s0['line'] = NAN
    if case == 'line_inf': s0['line'] = INF
    if case == 'line_negative': s0['line'] = -20.5
    if case == 'line_string': s0['line'] = '20.5'
    if case == 'no_quote': s0.pop('quote_id')
    if case == 'unknown_quote': s0['quote_id'] = 99999
    if case == 'quote_line_mismatch': s0['line'] = 21.5
    if case == 'quote_side_mismatch': s0['side'] = 'under'
    if case == 'quote_market_mismatch': s0['prop'] = 'rebounds'; sel[1]['prop'] = 'points'
    if case == 'quote_player_mismatch': s0['espn_player_id'] = '9999999'
    if case == 'quote_after_lock':
        late = quote(art, s0['espn_player_id'], 'points', 20.5, at=BEFORE + 1)
        s0['quote_id'] = late['quote_id']
    if case == 'stale_quote':
        quote(art, s0['espn_player_id'], 'points', 22.5, at=BEFORE - 60)    # line moved before lock
    return sel, BEFORE


@pytest.mark.parametrize('case', ['four', 'six', 'duplicate', 'bad_prop', 'bad_side', 'post_tipoff', 'no_forecast',
                                  'line_none', 'line_missing', 'line_nan', 'line_inf', 'line_negative', 'line_string',
                                  'no_quote', 'unknown_quote', 'quote_line_mismatch', 'quote_side_mismatch',
                                  'quote_market_mismatch', 'quote_player_mismatch', 'quote_after_lock',
                                  'stale_quote'])
def test_lock_refuses_unsourced_mismatched_or_post_tipoff_selections(case):
    art, sel = ready()
    sel, now = _mutate(case, art, sel)
    with pytest.raises(ValueError):
        store.lock_selections('lock', sel, now=now)
    assert store.get_lock('lock') is None and store.get_game_lock(OKC) is None


def test_lock_refuses_forecast_saved_after_lock_time():
    art = seed()
    forecast(art, now=BEFORE + 100)
    with pytest.raises(ValueError):
        store.lock_selections('early', priced(art, at=BEFORE - 300), now=BEFORE)


def test_lock_refuses_when_stored_tipoff_moved_earlier():
    art, sel = ready()
    g = store.get_game(OKC)
    moved = store.save_artifact(artifact(body=b'{"moved": 1}', at=BEFORE - 200))
    store.upsert_game(dict(g, tipoff_utc='2026-10-21T00:00:00Z', game_date_et=None), moved)
    with pytest.raises(ValueError):
        store.lock_selections('moved', sel, now=BEFORE)


def test_lock_items_refuse_unmatched_quote_in_sql():
    art, sel = ready()
    s = sel[0]
    for line, side, quote_id in ((21.5, 'over', s['quote_id']), (20.5, 'under', s['quote_id']),
                                 (20.5, 'over', None), (NAN, 'over', s['quote_id'])):
        with pytest.raises(sqlite3.DatabaseError):
            db.ex("INSERT INTO nba_selection_lock_items(lock_id,rank,forecast_id,game_id,espn_player_id,prop,side,"
                  "line,quote_id) VALUES('raw',1,?,?,?,?,?,?,?)",
                  (s['forecast_id'], OKC, s['espn_player_id'], 'points', side, line, quote_id))


# ---------------------------------------------------------------- grades
def test_grades_are_append_only_revisions_and_idempotent():
    art, sel = ready()
    store.lock_selections('L', sel, now=BEFORE)
    box = store.save_artifact(artifact(kind='summary', at=AFTER + 9000))
    with pytest.raises(ValueError):          # game not final yet
        store.add_grade_revision('L', 1, 'win', 31, box, now=AFTER + 9000)
    store.upsert_game(dict(store.get_game(OKC), status='final', home_score=110, away_score=118), box)
    r1 = store.add_grade_revision('L', 1, 'win', 31, box, now=AFTER + 9000)
    assert r1['revision'] == 1 and r1['source_artifact_id'] == box
    assert store.add_grade_revision('L', 1, 'win', 31, box, now=AFTER + 9999) == r1
    box2 = store.save_artifact(artifact(body=b'{"corrected": 1}', kind='summary', at=AFTER + 20000))
    r2 = store.add_grade_revision('L', 1, 'loss', 19, box2, detail={'note': 'stat correction'}, now=AFTER + 20000)
    assert r2['revision'] == 2
    assert [g['revision'] for g in store.list_grade_revisions('L')] == [1, 2]
    assert store.latest_grades('L')[1]['result'] == 'loss'
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("UPDATE nba_grade_revisions SET result='win'")
    for bad in (dict(result='won'), dict(rank=9), dict(source=4242), dict(value=NAN), dict(value=INF)):
        with pytest.raises(ValueError):
            store.add_grade_revision('L', bad.get('rank', 2), bad.get('result', 'win'), bad.get('value', 1),
                                     bad.get('source', box), now=AFTER + 9000)
    # void (DNP/inactive) carries no statistic
    assert store.add_grade_revision('L', 3, 'void', None, box, now=AFTER + 9000)['actual_value'] is None


# ---------------------------------------------------------------- cadences
def test_cadences_persist_and_record_runs():
    store.set_cadence('nba-scoreboard', 900, now=1000.0)
    st = store.record_run('nba-scoreboard', 'ok', {'games': 2}, now=2000.0)
    assert st == {'name': 'nba-scoreboard', 'cadence_seconds': 900, 'last_run_at': 2000.0,
                  'last_status': 'ok', 'last_detail': {'games': 2}, 'next_due_at': 2900.0}
    store.set_cadence('nba-scoreboard', 600, now=2100.0)
    assert store.get_cadence('nba-scoreboard')['next_due_at'] == 2600.0
    assert [c['name'] for c in store.list_cadences()] == ['nba-scoreboard']
    with pytest.raises(ValueError):
        store.record_run('nba-unknown', 'ok', {}, now=1.0)
    with pytest.raises(ValueError):
        store.record_run('nba-scoreboard', 'great', {}, now=1.0)
