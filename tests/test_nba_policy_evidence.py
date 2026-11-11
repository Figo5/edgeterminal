"""Capture policy evidence uses all fresh offers and never rewrites old records."""
import copy
import json
import sqlite3

import pytest

from edgeterminal import db, nba_learning, nba_learning_data, nba_ops, nba_policy_evidence as policy, nba_quotes
from edgeterminal import nba_sources, nba_store
from test_nba_prop_identity import isolated, capture_case, finalize, artifact
from test_nba_data import event
from test_nba_prop_quality import calibrated, current, serving


def test_full_offer_context_keeps_unselected_offer_and_reuses_selected_quote_ids(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    odd = next(iter(case['fresh']['odds'].values()))
    extra = copy.deepcopy(odd)
    key = f"rebounds-{extra['statEntityID']}-game-ou-over"
    extra.update(oddID=key, statID='rebounds')
    case['fresh']['odds'][key] = extra
    result = nba_ops._capture_props(case['now'])
    assert result['new_locks'] == 1 and result['quotes'] == 6
    saved = db.q('SELECT * FROM nba_quote_observations')
    assert len(saved) == 6
    context_id = db.q1('SELECT context_id FROM nba_prop_policy_contexts')['context_id']
    context = policy._context(context_id)
    assert len(context['payload']['quotes']) == 6 and len(policy.replay_incumbent(context)) == 5
    locked = nba_store.get_lock('top5-' + case['game']['game_id'])['items']
    assert {r['quote_id'] for r in locked}.issubset({r['quote_id'] for r in saved})
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        db.ex('UPDATE nba_prop_policy_contexts SET sha256=?', ('0' * 64,))


def test_full_offer_capture_can_lock_best_price_before_another_book_in_same_batch(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    for odd in case['fresh']['odds'].values():
        odd['byBookmaker']['fanduel'] = dict(odd['byBookmaker']['draftkings'], odds='-110')
    result = nba_ops._capture_props(case['now'])
    assert result['new_locks'] == 1 and result['quotes'] == 10
    assert len(db.q('SELECT * FROM nba_quote_observations')) == 10
    lock = nba_store.get_lock('top5-' + case['game']['game_id'])
    selected = nba_store.get_forecast('props-' + case['game']['game_id'])['payload']['rows']
    assert len(lock['items']) == 5 and all(r['sportsbook'] == 'draftkings' for r in selected)


def complete_context(monkeypatch):
    case = capture_case(monkeypatch)
    assert nba_ops._capture_props(case['now'])['new_locks'] == 1
    after = finalize(case)
    contexts = policy.ready_contexts(after + 1)
    assert len(contexts) == 1
    return case, contexts[0]


def test_policy_rows_replay_exact_final_and_both_frozen_fives(isolated, monkeypatch):
    from edgeterminal import nba_prop_role as role, nba_prop_quality as quality
    case, context = complete_context(monkeypatch)
    candidate = calibrated()
    candidate['version'] = 'quality-fixture'
    candidate['training_cutoff'] = case['now'] - 1000
    with serving(current(candidate)):
        selected = nba_learning.select_props(context['payload']['quotes'], context['history'],
            context['game']['tipoff_utc'], context['as_of'], count=5)
    group = dict(game_id=case['game']['game_id'], context_id=context['context_id'],
        universe_quote_ids=[q['quote_id'] for q in context['payload']['quotes']], baseline=[], candidate=[])
    frozen = policy.replay_incumbent(context)
    for label, selected_rows in [('baseline', frozen), ('candidate', selected['selections'])]:
        for chosen in selected_rows:
            q = next(q for q in context['payload']['quotes'] if q['player_id'] == chosen['player_id'] and q['market'] == chosen['market'])
            with serving(None):
                before = nba_learning.project_prop(q['player_id'], q['market'], context['history'], case['tip'], q['line'])
            with serving(current(candidate)):
                after = nba_learning.project_prop(q['player_id'], q['market'], context['history'], case['tip'], q['line'])
            row = role._record(context, q, before, after)
            assert policy.verify_policy_row(row)
            group[label].append(row)
    assert policy.verify_policy_universe(group, candidate)
    changed = copy.deepcopy(group)
    changed['universe_quote_ids'].pop()
    with pytest.raises(ValueError, match='coverage'):
        policy.verify_policy_universe(changed, candidate)
    changed = copy.deepcopy(group)
    changed['candidate'][0]['candidate_probability'] += .01
    with pytest.raises(ValueError, match='fields'):
        policy.verify_policy_universe(changed, candidate)
    changed = copy.deepcopy(group)
    changed['baseline'].reverse()
    with pytest.raises(ValueError, match='membership'):
        policy.verify_policy_universe(changed, candidate)
    altered = dict(group['candidate'][0], actual=999.)
    with pytest.raises(ValueError, match='actual'):
        policy.verify_policy_row(altered)


def test_context_detects_archive_corruption(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    nba_ops._capture_props(case['now'])
    ident = db.q1('SELECT context_id FROM nba_prop_policy_contexts')['context_id']
    original = nba_store.get_artifact
    def corrupt(source_id):
        source = original(source_id)
        return dict(source, body=b'{}') if source_id == case['proof'] else source
    monkeypatch.setattr(nba_store, 'get_artifact', corrupt)
    with pytest.raises(ValueError, match='hash'):
        policy._context(ident)


def test_context_clock_crossing_tip_never_persists_context(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    original = policy._selection_rows
    def crossed(*args):
        result = original(*args)
        monkeypatch.setattr(db, 'now', lambda: case['tip'])
        return result
    monkeypatch.setattr(policy, '_selection_rows', crossed)
    with pytest.raises(ValueError, match='after tipoff'):
        nba_ops._capture_props(case['now'])
    assert db.q1('SELECT COUNT(*) n FROM nba_prop_policy_contexts')['n'] == 0
    assert db.q1('SELECT COUNT(*) n FROM nba_quote_observations')['n'] == 0
    assert not nba_store.get_lock('top5-' + case['game']['game_id'])


def test_context_insert_failure_rolls_back_all_offer_observations(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    original = policy._json
    def crossed(value):
        encoded = original(value)
        if isinstance(value, dict) and value.get('quotes'):
            monkeypatch.setattr(db, 'now', lambda: case['tip'])
        return encoded
    monkeypatch.setattr(policy, '_json', crossed)
    with pytest.raises(ValueError, match='after tipoff'):
        nba_ops._capture_props(case['now'])
    assert db.q1('SELECT COUNT(*) n FROM nba_prop_policy_contexts')['n'] == 0
    assert db.q1('SELECT COUNT(*) n FROM nba_quote_observations')['n'] == 0


def test_capture_reconstructs_history_sources_instead_of_using_poisoned_cache(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    roster = nba_ops._rosters([case['game']])
    canonical, _ = nba_learning_data.history_from_sources(
        [r['id'] for r in db.q("SELECT id FROM nba_source_artifacts WHERE kind='player-history'")], {}, case['now'])
    for player in roster:
        key = 'nba_player_history_' + player['player_id']
        db.kv_set(key, [dict(row, points=999.) for row in db.kv_get(key)])
    nba_ops._capture_props(case['now'])
    saved = nba_store.get_forecast('props-' + case['game']['game_id'])['payload']['rows']
    with serving(None):
        assert all(row['projection'] == nba_learning.project_prop(row['player_id'], row['market'],
            canonical, case['tip'], row['line'])['projection'] for row in saved)
    context_id = saved[0]['policy_context_id']
    assert len(policy.replay_incumbent(policy._context(context_id))) == 5


def test_validation_cache_is_bounded_to_one_call_and_rechecks_receipt_limits(isolated, monkeypatch):
    case, context = complete_context(monkeypatch)
    calls = dict(context=0, final=0)
    original_context, original_final = policy._load_context, policy._load_final
    def load_context(*args):
        calls['context'] += 1
        return original_context(*args)
    def load_final(*args):
        calls['final'] += 1
        return original_final(*args)
    monkeypatch.setattr(policy, '_load_context', load_context)
    monkeypatch.setattr(policy, '_load_final', load_final)
    source = context['final_source']
    with policy.validation_scope():
        first = policy._context(context['context_id'])
        with policy.validation_scope():
            assert policy._context(context['context_id']) is first
        policy._final(first, source['id'], source['received_at'])
        policy._final(first, source['id'], source['received_at'] + 1)
        with pytest.raises(ValueError, match='received after'):
            policy._final(first, source['id'], source['received_at'] - 1)
        assert calls == dict(context=1, final=1)
    with policy.validation_scope():
        policy._context(context['context_id'])
        policy._final(first, source['id'], source['received_at'])
    assert calls == dict(context=2, final=2)


def test_one_context_failure_does_not_prevent_other_captured_games_from_locking(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    second = dict(case['game'], game_id='nba-2026-401810832', espn_event_id='401810832',
                  tipoff_utc='2026-10-21T00:00Z')
    sid = nba_store.get_game(case['game']['game_id'])['source_artifact_id']
    nba_store.upsert_game(second, sid, now=case['now'])
    nba_store.save_forecast('game-' + second['game_id'], second['game_id'], 'fixture',
        {'kind':'game','projection':{'home_margin':5.,'total':220.},'market':{}}, [sid], now=case['now'])
    event2 = copy.deepcopy(case['fresh'])
    event2['eventID'] = 'provider-second'
    event2['status']['startsAt'] = second['tipoff_utc']
    monkeypatch.setattr(nba_quotes, 'get_raw_meta', lambda *a, **k:
        (200, json.dumps({'success': True, 'data':[case['fresh'], event2]}).encode(), {}))
    original = policy.capture_context
    def checked(game, *args):
        if game['game_id'] == case['game']['game_id']:
            raise ValueError('fixture per-game integrity failure')
        return original(game, *args)
    monkeypatch.setattr(policy, 'capture_context', checked)
    with pytest.raises(ValueError, match='after processing other targets'):
        nba_ops._capture_props(case['now'])
    assert not nba_store.get_lock('top5-' + case['game']['game_id'])
    assert len(nba_store.get_lock('top5-' + second['game_id'])['items']) == 5


def test_frozen_schedule_replays_original_quote_after_official_delayed_tip(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    scheduled = event(case['game']['espn_event_id'], case['game']['tipoff_utc'], 2027, 2, 'pre', ('OKC', '25'), ('MIN', '16'))
    sid = artifact('scoreboard', nba_sources.route('scoreboard'), {'events': [scheduled]}, case['now'] - 500)
    nba_store.upsert_game(case['game'], sid, now=case['now'] - 400)
    nba_ops._capture_props(case['now'])
    case['final_body']['header']['competitions'][0]['date'] = '2026-10-20T23:20Z'
    from edgeterminal import nba_data
    case['final'] = nba_data.parse_summary(case['final_body'])['game']
    after = finalize(case)
    before_bytes = db.q1('SELECT payload FROM nba_forecasts WHERE forecast_id=?', ('props-' + case['game']['game_id'],))['payload']
    records, _, _ = nba_learning_data.validation_records(after + 1, 1)
    assert len(records) == 5 and all(r['kickoff_at'] == case['tip'] for r in records)
    contexts = policy.ready_contexts(after + 1)
    assert len(contexts) == 1 and contexts[0]['game']['tipoff_ts'] == case['tip']
    assert len(policy.replay_incumbent(contexts[0])) == 5
    assert nba_store.get_game(case['game']['game_id'])['tipoff_ts'] == case['tip'] + 1200
    assert db.q1('SELECT payload FROM nba_forecasts WHERE forecast_id=?', ('props-' + case['game']['game_id'],))['payload'] == before_bytes


def opponent_row(context):
    return next(r for r in context['history'] if r.get('kind') == 'nba-opponent-context')


def test_context_carries_opponent_allowance_from_gamelogs_received_by_the_quote(isolated, monkeypatch):
    case = capture_case(monkeypatch)
    assert nba_ops._capture_props(case['now'])['new_locks'] == 1
    ident = db.q1('SELECT context_id FROM nba_prop_policy_contexts')['context_id']
    context = policy._context(ident)
    row = opponent_row(context)
    assert set(row['teams'].values()) == {'MIN', 'OKC'} and set(row['ratios']) == {'MIN', 'OKC'}
    assert row['ratios']['MIN']['points'] is not None
    assert len(policy.replay_incumbent(context)) == 5
    # A gamelog received after the quote, even one for an archived game, cannot change the replay.
    old = db.q1("SELECT * FROM nba_source_artifacts WHERE kind='player-history' ORDER BY id LIMIT 1")
    body = json.loads(old['body'])
    for entry in body['seasonTypes'][0]['categories'][0]['events']:
        entry['stats'][1] = '90'
    artifact('player-history', old['url'], body, context['payload']['observed_at'] + 1)
    policy._LEAGUE_ROWS.clear()
    assert opponent_row(policy._load_context(ident)) == row


def test_only_a_selector_that_reads_opponent_context_fails_without_it(monkeypatch):
    from edgeterminal import nba_prop_role as role
    monkeypatch.setattr(policy, '_opponent_context', lambda *a: 1 / 0)
    history = [dict(player_id='1')]
    assert policy._with_opponent(history, {}, [], 0, None) == history
    assert policy._with_opponent(history, {}, [], 0, dict(kind=role.KIND)) == history
    for selector in (dict(kind=role.RATE_KIND), dict(kind='quality', base_artifact=dict(kind=role.RATE_KIND))):
        with pytest.raises(ValueError, match='opponent context'):
            policy._with_opponent(history, {}, [], 0, selector)
