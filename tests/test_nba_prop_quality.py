"""Future-only workload policy and fixed-membership confidence contracts."""
import copy
from contextlib import contextmanager

import pytest

from edgeterminal import nba_learning as learning, nba_model as baseline
from edgeterminal import nba_prop_features as workload, nba_prop_quality as quality
from edgeterminal import nba_prop_role as role, prop_calibration
from test_nba_learning import TIP, artifact, history, quote


@contextmanager
def serving(value):
    token = learning._SNAPSHOT.set(value)
    try:
        yield
    finally:
        learning._SNAPSHOT.reset(token)


def calibrated(base=None, intercept=-5.):
    config = dict(prop_calibration.fit_calibrator([], cutoff=TIP - 1000),
                  method='shrunk_logit', intercept=intercept, slope=1.,
                  selector_sha256=quality.selector_hash(base))
    return dict(schema=2, kind=quality.KIND, training_cutoff=TIP - 1000,
        base_artifact=base, selector_sha256=quality.selector_hash(base),
        ranking_changed=False, changed_markets=['points'],
        calibration_layers={m: [config] if m == 'points' else [] for m in baseline.MARKETS})


def current(a):
    return dict(candidate_id='quality-fixture-id', artifact=a, version='quality-fixture',
                training_cutoff=a['training_cutoff'], fingerprint='f' * 64)


def offers():
    return [quote('1', 'points'), quote('2', 'points'), quote('3', 'points'),
            quote('2', 'assists', line=2.5), quote('3', 'rebounds', line=4.5),
            quote('1', 'threes', line=.5)]


def test_calibration_keeps_exact_membership_order_and_side_below_half():
    base = artifact()
    with serving(current(base)):
        before = learning.select_props(offers(), history(), TIP, TIP - 100)
    upgraded = calibrated(base)
    with serving(current(upgraded)):
        after = learning.select_props(offers(), history(), TIP, TIP - 100)
    identity = lambda rows: [(r['player_id'], r['market'], r['side'], r['line'], r['sportsbook']) for r in rows]
    assert identity(after['selections']) == identity(before['selections'])
    assert identity(after['candidates']) == identity(before['candidates'])
    assert len(after['selections']) == 5
    assert any(r['probability'] < .5 for r in after['selections'] if r['market'] == 'points')


def test_retained_market_and_points_are_exact_and_schema_two_can_replay_itself():
    base = artifact()
    with serving(current(base)):
        old = learning.project_prop('1', 'rebounds', history(), TIP, 4.)
        old_points = learning.project_prop('1', 'points', history(), TIP, 20.5)
    a = calibrated(base)
    with serving(current(a)):
        new = learning.project_prop('1', 'rebounds', history(), TIP, 4.)
        new_points = learning.project_prop('1', 'points', history(), TIP, 20.5)
        repeated = learning._project('1', 'points', history(), TIP, 20.5, a, learning.model_info())
    for key in ('raw_projection', 'projection', 'raw_sd', 'probability_over', 'probability_under', 'probability_push'):
        assert new[key] == old[key]
    assert new_points['raw_projection'] == old_points['raw_projection']
    assert repeated == new_points


def test_selector_binding_and_large_mass_errors_fail_closed():
    a = calibrated(artifact())
    a['base_artifact']['minutes_model']['intercept'] += 1
    with pytest.raises(ValueError, match='binding'):
        quality.validate(a)
    with pytest.raises(ValueError, match='mass'):
        quality.coherent_probabilities(dict(over=.9, under=.2, push=0))
    p = quality.coherent_probabilities(dict(over=.3333, under=.3333, push=.3333))
    assert sum(p.values()) == pytest.approx(1) and p['push'] == .3333


def test_later_calibration_layer_replays_incumbent_serialization_boundary():
    a = calibrated(artifact(), intercept=-.4)
    b = copy.deepcopy(a)
    second = dict(a['calibration_layers']['points'][0], intercept=.2, slope=.8)
    b['calibration_layers']['points'].append(second)
    info = dict(version='fixture', fingerprint='f' * 64)
    before = quality.project('1', 'points', history(), TIP, 20., a, info)
    probabilities = quality.coherent_probabilities({s: before['probability_' + s] for s in ('over','under','push')})
    expected = prop_calibration.apply_calibrator(probabilities, second)
    after = quality.project('1', 'points', history(), TIP, 20., b, info)
    assert {s: after['probability_' + s] for s in expected} == {s: round(v, 4) for s, v in expected.items()}


def test_decided_evaluation_conditions_on_nonpush_without_changing_live_mass():
    p = dict(raw_projection=8., probability_over=.4, probability_under=.4, probability_push=.2)
    r = dict(game_id='g', player_id='p', market='rebounds', quote_id=1, line=8., side='over',
             observed_at=1., kickoff_at=2., result_observed_at=3., actual=9.)
    out = quality._paired(r, p, p)
    assert out['baseline_probability'] == out['candidate_probability'] == .5
    assert p['probability_over'] == .4 and p['probability_push'] == .2


def test_quality_historical_dispatch_cannot_use_future_fit():
    a = calibrated(artifact())
    with serving(current(a)):
        assert learning.select_props(offers(), history(), TIP, a['training_cutoff'] - 1) == \
            baseline.select_props(offers(), history(), TIP, a['training_cutoff'] - 1)


def test_workload_features_reject_target_game_and_expose_observed_minute_shift():
    rows = [dict(ts=TIP - (15 - i) * 86400, minutes=30. if i < 10 else 15., stat=8.) for i in range(15)]
    values = workload.features(rows, TIP)
    assert values['minutes_recent_shift'] == -15.
    assert values['minutes_sd5'] == 0
    assert values['current_season_n'] == 15
    with pytest.raises(ValueError, match='prior'):
        workload.features(rows + [dict(ts=TIP, minutes=40, stat=99)], TIP)


def minute_model(residuals=None):
    return dict(schema=1, feature_names=list(workload.MINUTE_FEATURES),
        minutes_model=dict(intercept=30., coefficients=[0.] * len(workload.MINUTE_FEATURES)),
        minute_residuals=residuals or list(range(-15, 15)))


def test_minute_distribution_preserves_predictive_spread_and_physical_limits():
    rows = [dict(ts=TIP - (15 - i) * 86400, minutes=30., stat=8.) for i in range(15)]
    distribution = workload.minute_distribution(rows, TIP, minute_model())
    assert sum(w for _, w in distribution) == pytest.approx(1.)
    assert min(m for m, _ in distribution) < 25 < max(m for m, _ in distribution)
    clipped = workload.minute_distribution(rows, TIP, minute_model(list(range(-60, -30))))
    assert all(1 <= m <= 48 for m, _ in clipped)


def test_workload_coefficients_never_fit_later_uncertainty_labels():
    rows = [dict(ts=TIP - (15 - i) * 86400, minutes=30., stat=8.) for i in range(15)]
    fit = [dict(ts=TIP + i * 86400, game_id='fit' + str(i), minutes=30., rows=rows) for i in range(100)]
    cal = [dict(ts=TIP + (110 + i) * 86400, game_id='cal' + str(i), minutes=20., rows=rows) for i in range(30)]
    first = workload.fit_minutes(fit, cal)
    second = workload.fit_minutes(fit, [dict(r, minutes=45.) for r in cal])
    assert first['minutes_model'] == second['minutes_model']
    assert first['minute_residuals'] != second['minute_residuals']
    with pytest.raises(ValueError, match='precede'):
        workload.fit_minutes(fit, [dict(r, ts=TIP) for r in cal])


def test_whole_date_partition_keeps_seven_untouched_dates():
    rows = [dict(game_id=f'g{i}-{j}', kickoff_at=TIP + i * 86400) for i in range(30) for j in range(6)]
    fit, inner, outer = quality._partition(rows)
    groups = [{r['game_id'] for r in g} for g in (fit, inner, outer)]
    assert len(groups[2]) >= 40 and not groups[0] & groups[1] and not groups[1] & groups[2]
    assert max(r['kickoff_at'] for r in fit) < min(r['kickoff_at'] for r in inner)
    assert max(r['kickoff_at'] for r in inner) < min(r['kickoff_at'] for r in outer)


def test_role_path_waits_for_full_universe_not_saved_five(monkeypatch):
    from edgeterminal import nba_policy_evidence
    monkeypatch.setattr(nba_policy_evidence, 'ready_contexts', lambda *a: [])
    with serving(None):
        result = role.build(TIP)
    assert result['status'] == 'waiting' and result['required_contexts'] == 100


def role_model():
    return dict(minutes=minute_model(), markets={m: dict(recent_weight=.75,
        standardized_residuals=[-1.] * 40 + [0.] * 40 + [1.] * 40,
        calibration_n=120, probability_method='empirical') for m in baseline.MARKETS})


def policy_contexts():
    from edgeterminal import nba_policy_evidence as evidence
    contexts = []
    # Thirty whole dates, four games per date: disjoint earlier choice and
    # calibration blocks plus ten untouched outer dates.
    for i in range(120):
        tip = TIP + (10 + i // 4) * 86400
        gid = f'nba-2026-{401900000+i}'
        quotes = [dict(q, game_id=gid, observed_at=tip - 300, quote_id=i * 10 + n + 1)
                  for n, q in enumerate(offers())]
        with serving(None):
            selected = learning.select_props(quotes, history(), tip, tip - 100)
            incumbent = evidence._selection_rows(selected, quotes, history(), tip)
        contexts.append(dict(context_id='c' + str(i), as_of=tip - 100,
            game=dict(game_id=gid, tipoff_ts=tip, tipoff_utc=tip), history=history(), roster=[], sources=[],
            players={pid:dict(status='played', stats=dict(points=15., rebounds=4., assists=3., threes=0.)) for pid in ('1','2','3')},
            final_source=dict(id=i+1, received_at=tip + 10800, sha256='a' * 64),
            payload=dict(incumbent=incumbent, quotes=quotes, observed_at=tip-300,
                selector=dict(version=baseline.MODEL_VERSION, fingerprint=baseline.PARAMETER_FINGERPRINT))))
    return contexts


def install_role_build(monkeypatch, contexts):
    from edgeterminal import nba_policy_evidence as evidence, model_learning_store
    monkeypatch.setattr(evidence, 'ready_contexts', lambda *a: contexts)
    monkeypatch.setattr(evidence, 'replay_incumbent', lambda context: context['payload']['incumbent'])
    monkeypatch.setattr(model_learning_store, 'holdout_available', lambda *a: True)
    monkeypatch.setattr(learning, 'training_history', lambda *a: ({}, []))
    monkeypatch.setattr(learning, 'examples', lambda *a, **k: {})
    training = dict(fit_game_ids=['nba-2025-old-fit'], development_game_ids=['nba-2025-old-dev'],
        calibration_game_ids=['nba-2025-old-cal'], fit_label_max_at=TIP-100000,
        development_label_max_at=TIP-90000, calibration_label_max_at=TIP-80000)
    monkeypatch.setattr(role, 'fit_role', lambda *a: (role_model(), copy.deepcopy(training)))
    original = prop_calibration.choose_market_updates
    def choose(rows, markets, **kwargs):
        result = original(rows, markets, **kwargs)
        result['changed_markets'] = ['points']
        return result
    monkeypatch.setattr(prop_calibration, 'choose_market_updates', choose)


def test_full_role_builder_is_reachable_and_outer_labels_never_choose_parameters(monkeypatch):
    contexts = policy_contexts()
    install_role_build(monkeypatch, contexts)
    with serving(None):
        first = role.build(TIP + 100 * 86400)
    assert first['status'] == 'ready'
    assert first['artifact']['ranking_changed'] is True
    assert first['artifact']['probability_contract'] == 'conditional_side_given_no_push'
    assert len(first['evaluation']['selection_policy']['games']) == 40
    assert all(len(g['baseline']) == len(g['candidate']) == 5 for g in first['evaluation']['selection_policy']['games'])
    outer_ids = set(first['artifact']['training']['validation_game_ids'])
    for context in contexts:
        if context['game']['game_id'] in outer_ids:
            for player in context['players'].values():
                player['stats']['points'] = 100.
    with serving(None):
        second = role.build(TIP + 100 * 86400)
    assert second['status'] == 'ready'
    for key in ('role_model', 'calibrators', 'changed_markets', 'market_selection'):
        assert first['artifact'][key] == second['artifact'][key]


def test_unknown_selected_participation_blocks_entire_outer_policy(monkeypatch):
    contexts = policy_contexts()
    install_role_build(monkeypatch, contexts)
    contexts[-1]['players']['1']['status'] = 'unverified'
    with serving(None):
        result = role.build(TIP + 100 * 86400)
    assert result['status'] == 'waiting'
    assert result['reason'] == 'common_role_policy_validation_participation_unknown'


def test_consumed_outer_cohort_is_checked_before_any_fit(monkeypatch):
    contexts = policy_contexts()
    install_role_build(monkeypatch, contexts)
    from edgeterminal import model_learning_store
    monkeypatch.setattr(model_learning_store, 'holdout_available', lambda *a: False)
    monkeypatch.setattr(role, 'fit_role', lambda *a: pytest.fail('fit after consumed holdout'))
    with serving(None):
        result = role.build(TIP + 100 * 86400)
    assert result['reason'] == 'fresh_untouched_role_policy_holdout_required'


def test_incomplete_pretip_incumbent_does_not_enter_future_policy_cohort(monkeypatch):
    contexts = policy_contexts()[:1]
    contexts[0]['payload']['incumbent'] = []
    install_role_build(monkeypatch, contexts)
    with serving(None):
        result = role.build(TIP + 100 * 86400)
    assert result['available_contexts'] == 0 and result['incomplete_pretip_contexts'] == 1


def test_calibration_builder_uses_three_disjoint_blocks_and_never_refits_retained_points(monkeypatch):
    contexts = policy_contexts()
    base = artifact()
    incumbent = current(base)
    records = []
    for context in contexts:
        for q in context['payload']['quotes'][:5]:
            records.append(dict(game_id=q['game_id'], player_id=q['player_id'], market=q['market'], quote_id=q['quote_id'],
                line=q['line'], side=q['side'], observed_at=q['observed_at'], kickoff_at=context['game']['tipoff_ts'],
                result_observed_at=context['final_source']['received_at'], actual=15. if q['player_id'] == '1' else 30.,
                history=history(), saved=dict(model_version=incumbent['version']), sources=[]))
    monkeypatch.setattr(learning, 'validation_records', lambda *a: (records, dict(exclusions=[]), []))
    monkeypatch.setattr(quality, '_point_training', lambda *a: (dict(fit_game_ids=['old-fit'],
        fit_label_max_at=TIP-100000, development_game_ids=['old-dev'], development_label_max_at=TIP-90000,
        retained_calibration_game_ids=['old-residual']), [dict(id=1, role='training', sha256='a'*64, received_at=TIP-100000)]))
    from edgeterminal import model_learning_store
    monkeypatch.setattr(model_learning_store, 'holdout_available', lambda *a: True)
    original = prop_calibration.choose_market_updates
    def choose(rows, markets, **kwargs):
        out = original(rows, markets, **kwargs)
        out['changed_markets'] = ['points']
        return out
    monkeypatch.setattr(prop_calibration, 'choose_market_updates', choose)
    with serving(incumbent):
        first = quality.build(TIP + 100 * 86400)
    assert first['status'] == 'ready'
    proposed = first['artifact']
    assert proposed['ranking_changed'] is False and proposed['base_artifact'] == base
    training = proposed['training']
    assert set(training['calibration_fit_game_ids']).isdisjoint(training['selection_game_ids'])
    assert set(training['selection_game_ids']).isdisjoint(training['validation_game_ids'])
    assert 'old-residual' in training['calibration_game_ids']
    for row in records:
        if row['game_id'] in training['validation_game_ids']:
            row['actual'] += 1000
    with serving(incumbent):
        second = quality.build(TIP + 100 * 86400)
    for key in ('calibration_layers', 'base_artifact', 'changed_markets', 'market_selection'):
        assert first['artifact'][key] == second['artifact'][key]
