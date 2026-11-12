"""Price-anchored prop policy: pooled fit, frozen point forecasts, full-offer gates."""
import copy
import math

import pytest

from edgeterminal import nba_learning as learning, nba_model as baseline
from edgeterminal import nba_prop_anchor as anchor, nba_prop_quality as quality, nba_prop_role as role
from test_nba_learning import TIP, history, quote
from test_nba_prop_quality import install_role_build, serving


def config(intercept=0., model=0., price=1.):
    return dict(schema=anchor.SCHEMA, method=anchor.METHOD, intercept=intercept, model_slope=model, price_slope=price)


def artifact(changed=('points',), retained=None, **coefficients):
    return dict(schema=2, kind=anchor.KIND, version='anchor-fixture', training_cutoff=TIP - 1000,
        retained_artifact=retained, selector_sha256=quality.selector_hash(retained), ranking_changed=True,
        anchor=config(**coefficients), changed_markets=list(changed))


def current(a):
    return dict(candidate_id='anchor-fixture-id', artifact=a, version='anchor-fixture',
                training_cutoff=a['training_cutoff'], fingerprint='f' * 64)


def offers():
    return [quote('1', 'points', price=-110.), quote('2', 'points', price=-250.), quote('3', 'points', price=+150.),
            quote('2', 'assists', line=2.5, price=-300.), quote('3', 'rebounds', line=4.5, price=-120.),
            quote('1', 'threes', line=.5, price=-400.), quote('1', 'points', side='under', price=-105.)]


def sample(i, model, price, won, at=TIP - 5000):
    return dict(game_id=f'g{i % 40}', quote_id=i + 1, model_probability=model, price=price, won=won, result_observed_at=at)


def priced_rows(n=800):
    """Outcomes follow the price; the model's own probability is noise."""
    rows = []
    for i in range(n):
        price = (-300., -150., 120., 250.)[i % 4]
        implied = anchor._implied(price)
        rows.append(sample(i, (.9, .6, .75, .55, .8)[i % 5], price, (i * 37 % 100) / 100. < implied))
    return rows


def test_fit_moves_weight_from_an_uninformative_model_to_the_price():
    fitted = anchor.fit_anchor(priced_rows(), cutoff=TIP)
    assert fitted['method'] == anchor.METHOD and fitted['fit_rows'] == 800 and fitted['fit_games'] == 40
    assert fitted['price_slope'] > .6 and fitted['model_slope'] < .4
    favourite, underdog = (anchor.apply_anchor(.9, p, fitted) for p in (-300., 250.))
    assert favourite > .6 > .45 > underdog


def test_sparse_or_one_sided_fits_keep_the_model_probability_exactly():
    few = anchor.fit_anchor(priced_rows(50), cutoff=TIP)
    assert few['method'] == 'identity' and few['reason'] == 'insufficient_full_offer_coverage'
    one = anchor.fit_anchor([dict(r, won=True) for r in priced_rows(400)], cutoff=TIP)
    assert one['method'] == 'identity' and one['reason'] == 'both_decided_outcomes_required'
    assert anchor.apply_anchor(.8123, -110., few) == .8123


def test_fit_and_recalibration_refuse_labels_after_their_cutoff_and_bad_contracts():
    rows = priced_rows(400)
    with pytest.raises(ValueError, match='post-cutoff label'):
        anchor.fit_anchor(rows, cutoff=TIP - 6000)
    with pytest.raises(ValueError, match='post-cutoff label'):
        anchor.recalibrate(config(), rows, cutoff=TIP - 6000)
    for bad in (dict(config(), price_slope=-.1), dict(config(), schema='other'), dict(config(), method='identity')):
        with pytest.raises(ValueError):
            anchor.apply_anchor(.6, -110., bad)
    with pytest.raises(ValueError):
        anchor.apply_anchor(.6, 0., config())


def test_recalibration_moves_only_the_intercept():
    fitted = anchor.fit_anchor(priced_rows(), cutoff=TIP)
    later = [dict(r, won=False, quote_id=r['quote_id'] + 10000) for r in priced_rows(400)]
    shifted = anchor.recalibrate(fitted, later, cutoff=TIP)
    assert shifted['intercept'] < fitted['intercept'] and shifted['calibration_rows'] == 400
    assert (shifted['model_slope'], shifted['price_slope']) == (fitted['model_slope'], fitted['price_slope'])
    assert math.isclose(shifted['intercept'], fitted['intercept'] + shifted['calibration_shift'])


def test_price_free_projection_is_the_retained_scorer_and_validation_binds_the_selector():
    a = artifact()
    with serving(current(a)):
        new = learning.project_prop('1', 'points', history(), TIP, 20.5)
    old = baseline.project_prop('1', 'points', history(), TIP, 20.5)
    same = lambda d: {k: v for k, v in d.items() if k not in ('model_version', 'parameter_fingerprint')}
    assert same(new) == same(old) and new['model_version'] == 'anchor-fixture'
    assert learning._validate_artifact(a) is a
    for broken in (dict(a, selector_sha256='0' * 64), dict(a, changed_markets=['steals']),
                   dict(a, ranking_changed=False), dict(a, retained_artifact=a, selector_sha256=quality.selector_hash(a))):
        with pytest.raises(ValueError):
            anchor.validate(broken)


def test_selection_ranks_by_price_anchor_and_only_changed_markets_display_it():
    with serving(None):
        before = learning.select_props(offers(), history(), TIP, TIP - 100)
    with serving(current(artifact())):
        after = learning.select_props(offers(), history(), TIP, TIP - 100)
    assert after['status'] == 'ok' and len(after['selections']) == 5
    scores = [r['policy_score'] for r in after['candidates']]
    assert scores == sorted(scores, reverse=True)
    # With all weight on the price, the heaviest favourite leads regardless of the model.
    assert (after['candidates'][0]['market'], after['candidates'][0]['price']) == ('threes', -400.)
    old = {(r['player_id'], r['market'], r['side']): r for r in before['candidates']}
    for row in after['candidates']:
        mass = 1. - row['probability_push']
        assert row['projection'] == baseline.project_prop(row['player_id'], row['market'], history(), TIP, row['line'])['projection']
        if row['market'] == 'points':
            assert row['probability_method'] == anchor.METHOD
            assert row['probability'] == round(mass * anchor._implied(row['price']), 4)
        else:
            assert row['probability'] == old[(row['player_id'], row['market'], row['side'])]['probability']
        assert row['policy_conditional_probability'] == min(1., row['probability'] / mass)
    assert max(sum(r['player_id'] == pid for r in after['selections'][:5]) for pid in '123') <= baseline.MAX_MARKETS_PER_PLAYER + 1


def test_saved_selection_rows_replay_the_price_dependent_probability():
    from edgeterminal import nba_policy_evidence as evidence
    quotes = [dict(q, quote_id=n + 1) for n, q in enumerate(offers())]
    with serving(current(artifact())):
        selected = learning.select_props(quotes, history(), TIP, TIP - 100)
        rows = evidence._selection_rows(selected, quotes, history(), TIP)
    for row, chosen in zip(rows, selected['selections']):
        assert row['conditional_probability'] == chosen['policy_conditional_probability']
        assert row['probability'] == chosen['probability']


def policy_contexts(games=160):
    """Forty whole dates of four games: three earlier ten-date blocks and ten untouched outer dates."""
    from edgeterminal import nba_policy_evidence as evidence
    contexts = []
    for i in range(games):
        tip = TIP + (20 + i // 4) * 86400
        gid = f'nba-2026-{401900000 + i}'
        quotes = [dict(q, game_id=gid, observed_at=tip - 300, quote_id=i * 10 + n + 1) for n, q in enumerate(offers())]
        with serving(None):
            selected = learning.select_props(quotes, history(), tip, tip - 100)
            incumbent = evidence._selection_rows(selected, quotes, history(), tip)
        contexts.append(dict(context_id='c' + str(i), as_of=tip - 100,
            game=dict(game_id=gid, tipoff_ts=tip, tipoff_utc=tip), history=history(), roster=[],
            sources=[dict(id=1000 + i, sha256='b' * 64, received_at=tip - 400, kind='player-quotes', url='u')],
            players={pid: dict(status='played', stats=dict(points=15. + 10 * (i % 2), rebounds=4. + i % 3, assists=3., threes=float(i % 2)))
                     for pid in ('1', '2', '3')},
            final_source=dict(id=i + 1, received_at=tip + 10800, sha256='a' * 64, kind='summary', url='u'),
            payload=dict(incumbent=incumbent, quotes=quotes, observed_at=tip - 300,
                selector=dict(version=baseline.MODEL_VERSION, fingerprint=baseline.PARAMETER_FINGERPRINT))))
    return contexts


def install(monkeypatch, contexts):
    install_role_build(monkeypatch, contexts)
    from edgeterminal import nba_learning_data
    monkeypatch.setattr(nba_learning_data, 'descriptor', lambda source, role: dict(
        kind='nba_source_artifact', id=source['id'], sha256=source['sha256'], received_at=source['received_at'], role=role))


NOW = TIP + 100 * 86400


def test_full_builder_keeps_blocks_disjoint_and_outer_labels_never_move_the_fit(monkeypatch):
    contexts = policy_contexts()
    install(monkeypatch, contexts)
    with serving(None):
        first = anchor.build(NOW)
    assert first['status'] == 'ready' and first['version'].startswith('nba-props-anchor-')
    a, evaluation = first['artifact'], first['evaluation']
    assert a['kind'] == anchor.KIND and a['ranking_changed'] is True and a['evaluation_family'] == anchor.FAMILY
    assert a['probability_contract'] == 'conditional_side_given_no_push' and a['anchor']['method'] == anchor.METHOD
    assert 'edgeterminal/nba_prop_anchor.py' in a['code_sha256'] and learning._validate_artifact(a)
    training = a['training']
    blocks = [set(training[k]) for k in ('fit_game_ids', 'calibration_game_ids', 'selection_game_ids', 'validation_game_ids')]
    assert [len(b) for b in blocks] == [40, 40, 40, 40] and len(set().union(*blocks)) == 160
    assert training['fit_label_max_at'] < a['training_cutoff'] < evaluation['validation_start']
    assert training['calibration_label_max_at'] < a['market_selection']['selection_cutoff'] == evaluation['calibration_cutoff']
    assert {s['role'] for s in first['sources']} == {'training', 'calibration', 'validation'}
    assert all(s['received_at'] <= a['training_cutoff'] for s in first['sources'] if s['role'] == 'training')
    games = evaluation['selection_policy']['games']
    assert len(games) == 40 and all(len(g['baseline']) == len(g['candidate']) == 5 for g in games)
    for row in evaluation['rows']:
        assert row['candidate_prediction'] == row['baseline_prediction']
        if row['market'] in a['changed_markets']:
            assert 0 < row['candidate_probability'] < 1
        else:
            assert row['candidate_probability'] == row['baseline_probability']
    # The registry recomputes its decision from these rows; only performance reasons may remain.
    from edgeterminal import model_learning_store
    decision = model_learning_store.gate(evaluation, first['training_cutoff'], artifact=a, sport='NBA', now=NOW)
    structural = ('catalog', 'overlap', 'after_cutoff', 'selection_required', 'chronological', 'policy_evidence',
                  'retained_market_changed', 'invalid')
    assert not [r for r in decision['reasons'] if any(word in r for word in structural)], decision['reasons']
    # The registry's replay of the candidate five must reproduce these exact fields.
    from edgeterminal import nba_policy_evidence as evidence
    context = next(c for c in contexts if c['game']['game_id'] == games[0]['game_id'])
    with serving(dict(artifact=a, version=a['version'], training_cutoff=a['training_cutoff'], fingerprint='f' * 64)):
        selected = learning.select_props(context['payload']['quotes'], context['history'], context['game']['tipoff_utc'], context['as_of'])
        replayed = evidence._selection_rows(selected, context['payload']['quotes'], context['history'], context['game']['tipoff_utc'])
    assert [r['quote_id'] for r in replayed] == [r['quote_id'] for r in games[0]['candidate']]
    for mine, theirs in zip(games[0]['candidate'], replayed):
        assert mine['candidate_probability'] == theirs['conditional_probability']
        assert mine['candidate_prediction'] == theirs['prediction']
    outer = set(training['validation_game_ids'])
    for context in contexts:
        if context['game']['game_id'] in outer:
            for player in context['players'].values():
                player['stats'].update(points=100., rebounds=100., assists=100., threes=100.)
    with serving(None):
        second = anchor.build(NOW)
    assert second['status'] == 'ready'
    for key in ('anchor', 'changed_markets', 'market_selection', 'training'):
        assert first['artifact'][key] == second['artifact'][key]


def test_lane_waits_for_contexts_blocks_declaration_and_an_unspent_holdout(monkeypatch):
    contexts = policy_contexts()
    install(monkeypatch, contexts)
    from edgeterminal import model_learning_store
    with serving(None):
        assert anchor.build(NOW, contexts=contexts[:99])['reason'] == 'full_same_selector_policy_contexts_required'
        assert anchor.build(NOW, contexts=contexts[:120])['reason'] == \
            'separate_thirty_game_anchor_fit_calibration_and_choice_blocks_required'
    outer_start = min(c['game']['tipoff_ts'] for c in contexts[-40:])
    monkeypatch.setattr(anchor, 'DECLARED_AT', outer_start + 1)
    monkeypatch.setattr(anchor, 'fit_anchor', lambda *a, **k: pytest.fail('fit before an eligible holdout'))
    with serving(None):
        waiting = anchor.build(NOW)
    assert waiting['reason'] == 'price_anchor_validation_must_follow_its_declaration' and 0 < waiting['validation_games'] < 40
    monkeypatch.setattr(anchor, 'DECLARED_AT', outer_start)
    monkeypatch.setattr(model_learning_store, 'holdout_available', lambda *a: False)
    with serving(None):
        assert anchor.build(NOW)['reason'] == 'fresh_untouched_price_anchor_holdout_required'


def test_unknown_selected_participation_blocks_the_whole_outer_policy(monkeypatch):
    contexts = policy_contexts()
    install(monkeypatch, contexts)
    contexts[-1]['players']['1']['status'] = 'unverified'
    with serving(None):
        result = anchor.build(NOW)
    assert result['status'] == 'waiting' and result['reason'] == 'common_price_anchor_validation_participation_unknown'


def test_an_active_anchor_is_never_stacked_or_replayed_price_free(monkeypatch):
    contexts = policy_contexts()
    active = current(artifact())
    for context in contexts:
        context['payload']['selector'] = dict(version=active['version'], fingerprint=active['fingerprint'])
    install(monkeypatch, contexts)
    with serving(active):
        assert anchor.build(NOW)['reason'] == 'price_anchor_incumbent_already_active'
        assert role.build(NOW)['reason'] == 'price_anchor_incumbent_requires_price_aware_baseline'


def test_declaration_follows_every_selection_inspected_before_it():
    assert anchor.DECLARED_AT == baseline.parse_time('2026-11-13T05:00:00Z')
    assert anchor.FAMILY not in role.FAMILIES.values() and anchor.KIND not in role.KINDS
