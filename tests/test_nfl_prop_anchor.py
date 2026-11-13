"""NFL price anchor: retained scorers, policy ranking, whole-week blocks and gates."""
import copy

import pytest

from edgeterminal import nba_prop_anchor as fit, nfl_learning as learning, nfl_prop_anchor as anchor
from edgeterminal import nfl_prop_lab as lab, nfl_prop_model as baseline, nfl_prop_roles as roles
from edgeterminal import nfl_top_five, model_learning_store as store
from test_nfl_learning_roles import features, offers, role_config


def config(intercept=0., model=0., price=1.):
    return dict(schema=fit.SCHEMA, method=fit.METHOD, intercept=intercept, model_slope=model, price_slope=price)


def anchored(changed=('receptions',), **coefficients):
    return dict(configurations={m: role_config() for m in lab.MARKETS}, price_anchor=config(**coefficients),
                changed_markets=list(changed))


def row(market='receptions', side='over', price=-200, over=.3, push=0., **extra):
    return dict(offers(count=1)[0], market=market, side=side, price=price, probability_over=over,
                probability_under=round(1. - push - over, 6), probability_push=push, **extra)


def test_blend_is_displayed_only_in_chosen_markets_and_ranks_every_market():
    chosen, retained = anchor.apply(row(), anchored()), anchor.apply(row('rushing_yards'), anchored())
    implied = round(fit._implied(-200), 6)
    assert (chosen['probability_over'], chosen['probability_under']) == (implied, round(1. - implied, 6))
    assert (retained['probability_over'], retained['probability_under']) == (.3, .7)
    for value in (chosen, retained):
        assert (value['policy_probability_over'], value['policy_probability_under']) == (implied, round(1. - implied, 6))
    under = anchor.apply(row(side='under', price=150), anchored())
    assert under['probability_under'] == round(fit._implied(150), 6)
    pushed = anchor.apply(row(push=.1, over=.3), anchored())
    assert pushed['probability_push'] == .1 and pushed['probability_over'] == round(.9 * fit._implied(-200), 6)
    assert abs(pushed['probability_over'] + pushed['probability_under'] + .1 - 1.) < 1e-6
    plain = row()
    assert anchor.apply(dict(plain), None) == plain and anchor.apply(dict(plain), dict(configurations={})) == plain


def test_ranking_follows_the_policy_score_and_old_rows_rank_exactly_as_before():
    rows = [dict(r, price=-110) for r in offers()]
    assert all(r['side'] == 'under' for r in nfl_top_five.rank(rows))
    flipped = [dict(r, policy_probability_over=.6 + int(r['player_id'][1:]) * .01,
                    policy_probability_under=.4 - int(r['player_id'][1:]) * .01) for r in rows]
    chosen = nfl_top_five.rank(flipped)
    assert [r['player_id'] for r in chosen] == ['p5', 'p4', 'p3', 'p2', 'p1'] and all(r['side'] == 'over' for r in chosen)


def test_replay_scoring_applies_the_blend_after_the_retained_json_scorer():
    plain = learning._score([row(features=features())], dict(configurations={m: role_config() for m in lab.MARKETS}))[0]
    scored = learning._score([row(features=features())], anchored())[0]
    assert scored['projection'] == plain['projection'] and 'policy_probability_over' not in plain
    mass = 1. - plain['probability_push']
    assert scored['probability_over'] == round(mass * fit._implied(-200), 6)
    assert learning._conditional_side(scored, 'over') == pytest.approx(fit._implied(-200), abs=1e-5)


def test_serving_anchors_retained_baseline_rows_without_breaking_their_seal(monkeypatch):
    configs = {market: learning._baseline_config() for market in lab.MARKETS}
    artifact = dict(schema=learning.ROLE_SCHEMA, parameters=learning.ROLE_PARAMETERS, features=list(roles.FEATURES),
        configurations=configs, fit_boundary_week=[2026, 9], parameter_fingerprint='f' * 64, calibration_cutoff=50.,
        validation_description='untouched full offers', price_anchor=config(), changed_markets=['receptions'])
    active = dict(candidate_id='anchor', version='anchor-version', training_cutoff=25., artifact_sha256='a' * 64, artifact=artifact)
    monkeypatch.setattr(learning, '_active', lambda: active)
    pins = dict(model_version=baseline.VERSION + ':boosted_role', parameter_fingerprint=baseline.PARAMETER_FINGERPRINT,
                model_manifest_sha256=baseline.MANIFEST_SHA256, input_context={'probability_method': 'empirical'})
    shown, kept = row(**pins), row('rushing_yards', quote_id=99, **copy.deepcopy(pins))
    monkeypatch.setattr(baseline, 'project_current', lambda *a, **kw: {'projections': [shown, kept]})
    prepared = {'game': {'game_id': 'g'}, 'feature': features(), 'opp_hist': []}
    monkeypatch.setattr(lab, '_week3_features', lambda *a: {('g0', 'gb'): prepared})
    monkeypatch.setattr(lab, '_read_stats', lambda *a: ([], {}))
    monkeypatch.setattr(roles, 'current_features', lambda *a: {})
    monkeypatch.setattr(learning.db, 'q1', lambda *a: {'team': 'GB'})
    report = learning.project_current({}, {}, {}, 'batch', season=2026, week=12)
    assert shown['probability_over'] == round(fit._implied(-200), 6) and shown['projection'] == 3.
    assert kept['probability_over'] == .3 and kept['policy_probability_over'] == round(fit._implied(-200), 6)
    for value in (shown, kept):
        assert value['price_anchor_candidate_id'] == 'anchor'
        assert learning._seal_model_matches(value, report['model_metadata'], active)


def league(weeks=range(1, 14), games=32):
    """Favoured overs win three times in four; the retained scorer always prefers the under."""
    schedule, records, groups = {}, [], {}
    for week in weeks:
        for i in range(games):
            game = f'nfl-2026-reg-{week:02d}-A{i}-B{i}'
            schedule[game] = dict(season=2026, week=week, away_team=f'A{i}', home_team=f'B{i}', kickoff=week * 1000.)
            record = dict(game_id=game, freeze_sha=game, selected_at=week * 1000. - 100, season=2026, week=week)
            records.append(record)
            rows = []
            for n, market in enumerate(lab.MARKETS):
                base = dict(offers(game, count=1)[0], market=market, week=week, season=2026, kickoff_at=week * 1000.,
                    result_observed_at=week * 1000. + 100, observed_at=week * 1000. - 200,
                    actual=3. if (i + n) % 4 == 0 else 8., frozen_metadata={})
                quote = week * 100000 + i * 100 + 2 * n
                rows += [dict(base, quote_id=quote, side='over', price=-200), dict(base, quote_id=quote + 1, side='under', price=160)]
            groups[game] = dict(record, universe_quote_ids=[r['quote_id'] for r in rows],
                eligible_quote_ids=[r['quote_id'] for r in rows], exclusions={}, rows=rows)
    return schedule, records, groups


def install(monkeypatch, schedule, records, groups, outer_weeks=(11, 12, 13), active=None):
    outer = [r for r in records if r['week'] in outer_weeks]
    monkeypatch.setattr(learning, '_quality_window', lambda now: (outer, [(2026, w) for w in outer_weeks], schedule))
    monkeypatch.setattr(learning, '_kickoff', lambda g: g['kickoff'])
    monkeypatch.setattr(store, 'holdout_available', lambda *a: True)
    monkeypatch.setattr(learning, '_validated_active', lambda: active)
    monkeypatch.setattr(learning.db, 'q', lambda *a: records)
    source = dict(kind='file', path='data/nflverse/source.csv', sha256='c' * 64, received_at=1.)
    monkeypatch.setattr(learning, '_graded_universe', lambda r, now: (copy.deepcopy(groups[r['game_id']]), [dict(source, role='validation')], None))
    monkeypatch.setattr(learning, '_catalog', lambda now: {})
    monkeypatch.setattr(learning, '_attach_features', lambda *a: None)
    def score(rows, artifact=None):
        scored = [dict(r, projection=7., probability_over=.3, probability_under=.7, probability_push=0.) for r in rows]
        return [anchor.apply(r, artifact) for r in scored]
    monkeypatch.setattr(learning, '_score', score)


NOW = 20000.


def test_build_fits_relevels_and_chooses_on_separate_weeks_before_untouched_outer_weeks(monkeypatch):
    schedule, records, groups = league()
    install(monkeypatch, schedule, records, groups)
    first = anchor.build(NOW)
    assert first['status'] == 'ready' and first['version'].startswith('nfl-anchor-1.2026w13-')
    artifact, evaluation = first['artifact'], first['evaluation']
    assert artifact['evaluation_family'] == anchor.FAMILY and artifact['ranking_changed'] is True
    assert artifact['price_anchor']['method'] == fit.METHOD and artifact['price_anchor']['price_slope'] > .3
    assert artifact['configurations'] == {m: learning._baseline_config() for m in lab.MARKETS}
    assert set(artifact['changed_markets']) == set(lab.MARKETS)
    week = lambda ids: {int(g.split('-')[3]) for g in ids}
    training = artifact['training']
    assert week(training['fit_game_ids']) == set(range(1, 9)) and week(training['calibration_game_ids']) == {9}
    assert week(artifact['market_selection']['selection_game_ids']) == {10}
    assert week(r['game_id'] for r in evaluation['rows']) == {11, 12, 13}
    assert training['fit_label_max_at'] < first['training_cutoff'] == 8999.
    assert training['calibration_label_max_at'] < artifact['probability_calibration_cutoff'] == 9999.
    assert evaluation['calibration_cutoff'] == 10999. == artifact['market_selection']['selection_cutoff']
    assert {s['role'] for s in first['sources']} >= {'training', 'calibration', 'validation'}
    for name in ('edgeterminal/nfl_prop_anchor.py', 'edgeterminal/nba_prop_anchor.py', 'edgeterminal/nfl_learning.py'):
        assert name in artifact['code_sha256']
    games = evaluation['selection_policy']['games']
    assert len(games) == 96 and all(len(g['baseline']) == len(g['candidate']) == 5 for g in games)
    # The retained scorer picks unders; the anchor follows the favoured overs.
    assert all(r['side'] == 'under' for g in games for r in g['baseline'])
    assert all(r['side'] == 'over' for g in games for r in g['candidate'])
    assert all(r['candidate_prediction'] == r['baseline_prediction'] for r in evaluation['rows'])
    decision = store.gate(evaluation, first['training_cutoff'], artifact=artifact, sport='NFL', now=NOW)
    # Fixture weeks share one kickoff each, so only the registry's distinct-date checks can remain.
    assert set(decision['reasons']) == {'insufficient_validation_dates', 'uncertain_date_group_improvement'}
    for game, group in groups.items():
        if int(game.split('-')[3]) >= 11:
            for r in group['rows']:
                r['actual'] = 100.
    second = anchor.build(NOW)
    for key in ('price_anchor', 'changed_markets', 'market_selection', 'training'):
        assert first['artifact'][key] == second['artifact'][key]


def test_lane_waits_for_declared_weeks_before_reading_any_grade(monkeypatch):
    schedule, records, groups = league()
    install(monkeypatch, schedule, records, groups, outer_weeks=(10, 11, 12))
    monkeypatch.setattr(learning, '_graded_universe', lambda *a: pytest.fail('read grades before declared weeks'))
    result = anchor.build(NOW)
    assert result['status'] == 'waiting' and 'Weeks 11-13' in result['reason']
    install(monkeypatch, schedule, records, groups)
    monkeypatch.setattr(store, 'holdout_available', lambda *a: False)
    monkeypatch.setattr(learning, '_graded_universe', lambda *a: pytest.fail('read consumed labels'))
    assert 'already evaluated' in anchor.build(NOW)['reason']


def test_thin_fit_weeks_and_an_active_anchor_wait(monkeypatch):
    schedule, records, groups = league(weeks=range(9, 14), games=32)
    install(monkeypatch, schedule, records, groups)
    result = anchor.build(NOW)
    assert result['waiting_kind'] == 'coverage_wait' and result['quote_coverage']['fit'] == 0
    schedule, records, groups = league()
    active = dict(candidate_id='prior', training_cutoff=1., artifact=dict(anchored(), calibration_cutoff=2.))
    install(monkeypatch, schedule, records, groups, active=active)
    monkeypatch.setattr(learning, '_graded_universe', lambda *a: pytest.fail('stacked a second anchor'))
    assert anchor.build(NOW)['reason'] == 'A price anchor is already active.'


def test_retuning_tries_the_anchor_first_and_reports_it_when_both_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(anchor, 'build', lambda now: calls.append('anchor') or dict(status='waiting', reason='anchor-waiting', artifact='hidden'))
    monkeypatch.setattr(learning, 'build_candidate', lambda now: calls.append('role') or dict(status='waiting', reason='role-waiting'))
    result = learning.build_quality_candidate(1.)
    assert calls == ['anchor', 'role'] and result['reason'] == 'role-waiting'
    assert result['anchor_proposal'] == dict(status='waiting', reason='anchor-waiting')
    monkeypatch.setattr(anchor, 'build', lambda now: dict(status='ready', version='v'))
    monkeypatch.setattr(learning, 'build_candidate', lambda now: pytest.fail('role lane ran after a ready anchor'))
    assert learning.build_quality_candidate(1.) == dict(status='ready', version='v')
