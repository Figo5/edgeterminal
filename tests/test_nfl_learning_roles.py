"""Composite replay keeps source universes, cohorts and retained scorers honest."""
import copy
import json
from types import SimpleNamespace

import pytest

from edgeterminal import nfl_learning as learning, nfl_prop_roles as roles
from edgeterminal import nfl_prop_lab as lab, nfl_prop_model as baseline
from edgeterminal import nfl_top_five, model_learning_store as store


def features():
    return {name: 1. for name in roles.FEATURES}


def role_config(point=8.):
    return dict(kind=roles.SCHEMA, features=list(roles.FEATURES), alpha=100.,
                coefficients=[0.] * len(roles.FEATURES), intercept=point,
                residuals=[-1.] * 20 + [0.] * 20 + [1.] * 20)


def offers(game='nfl-2026-reg-07-GB-DET', count=6):
    rows = []
    for player in range(count):
        for side in ('over', 'under'):
            rows.append(dict(quote_id=2 * player + (1 if side == 'over' else 2),
                game_id=game, canonical_game_id=game, player_id=f'p{player}', gsis_player_id=f'g{player}',
                player_name=f'Player {player}', market='receptions', sportsbook='book', side=side,
                price=-110, line=4.5, observed_at=100., kickoff_at=200., kickoff_utc=200.,
                result_observed_at=300., actual=8., projection=3., team='GB', features=features(),
                probability_over=.1 + player * .01, probability_under=.9 - player * .01,
                probability_push=0.))
    return rows


def test_new_install_waits_without_reading_schedule_or_model(monkeypatch):
    monkeypatch.setattr(learning.db, 'q', lambda *a: [])
    monkeypatch.setattr(lab, '_schedule', lambda: pytest.fail('schedule on empty install'))
    monkeypatch.setattr(baseline, 'load_artifact', lambda: pytest.fail('model on empty install'))
    assert learning.build_candidate(400.)['status'] == 'waiting'
    monkeypatch.setattr(nfl_top_five, 'view', lambda: {'games': []})
    assert learning._validation_records(400.) == ([], {}, [])


def test_consumed_outer_preflight_happens_before_reading_any_outer_grade(monkeypatch):
    monkeypatch.setattr(learning, '_quality_window', lambda now: ([{'game_id': 'used'}],
        [(2026, 5), (2026, 6), (2026, 7)], {}))
    monkeypatch.setattr(store, 'holdout_available', lambda *args: False)
    monkeypatch.setattr(learning, '_graded_universe', lambda *a: pytest.fail('read consumed labels'))
    monkeypatch.setattr(roles, 'fit_market', lambda *a: pytest.fail('fit consumed labels'))
    result = learning.build_candidate(400.)
    assert result['status'] == 'waiting'
    assert 'already evaluated' in result['reason']


def test_full_policy_replay_can_select_a_player_outside_the_old_five(monkeypatch):
    rows = offers()
    group = dict(game_id=rows[0]['game_id'], freeze_sha='a' * 64, selected_at=150.,
        universe_quote_ids=[r['quote_id'] for r in rows], eligible_quote_ids=[r['quote_id'] for r in rows],
        exclusions={}, rows=rows)
    def score(values, artifact=None):
        return [dict(r, projection=8., probability_over=(.6 + int(r['player_id'][1:]) * .05) if artifact else r['probability_over'],
                     probability_under=(.4 - int(r['player_id'][1:]) * .05) if artifact else r['probability_under']) for r in values]
    monkeypatch.setattr(learning, '_score', score)
    policy = learning._policy(group, None, {'configurations': {}})
    assert {r['player_id'] for r in policy['baseline']} == {'p0', 'p1', 'p2', 'p3', 'p4'}
    assert {r['player_id'] for r in policy['candidate']} == {'p1', 'p2', 'p3', 'p4', 'p5'}
    assert all(r['side'] == 'under' for r in policy['baseline'])
    assert all(r['side'] == 'over' for r in policy['candidate'])
    assert group['rows'] == rows


def test_score_retained_baseline_uses_pinned_nonlinear_estimator_without_conversion(monkeypatch):
    rows = offers(count=1)
    monkeypatch.setattr(baseline, 'load_artifact', lambda: (
        {'configurations': {'receptions': {'family': 'boosted_role', 'probability_method': 'empirical'}}},
        {'receptions': 'NONLINEAR'}))
    def nonlinear(model, values, market, family):
        assert model == 'NONLINEAR' and family == 'boosted_role'
        return [9.123456]
    monkeypatch.setattr(baseline, '_predict', nonlinear)
    monkeypatch.setattr(baseline, 'probabilities', lambda *a: dict(over=.81234567, under=.18765433, push=0.))
    artifact = {'configurations': {'receptions': learning._baseline_config()}}
    before = learning._score(rows)
    assert learning._score(rows, artifact) == before
    assert before[0]['projection'] == 9.1235
    assert before[0]['probability_over'] == .812346
    altered = copy.deepcopy(artifact)
    altered['configurations']['receptions']['manifest_sha256'] = 'x'
    with pytest.raises(ValueError, match='pin'):
        learning._score(rows, altered)


def test_actual_quote_unit_is_chosen_without_outcomes_and_deduplicates_books():
    rows = offers(count=1)
    rows += [dict(rows[0], quote_id=50, sportsbook='zzz', actual=-999.),
             dict(rows[0], quote_id=51, sportsbook='aaa', actual=999.)]
    assert learning._quote_units([{'rows': rows}])[0]['quote_id'] == 51
    for row in rows:
        row['actual'] *= -1
    assert learning._quote_units([{'rows': rows}])[0]['quote_id'] == 51


def source_fixture(monkeypatch):
    rows = offers()
    clean = [{k: v for k, v in r.items() if k not in ('features', 'team', 'game_id', 'actual',
            'result_observed_at', 'kickoff_at')} for r in rows]
    selected = nfl_top_five.rank(clean)
    payload = dict(policy=nfl_top_five.POLICY, game_id=rows[0]['game_id'], selected_at=150.,
                   freeze_sha='freeze', selections=selected)
    body = json.dumps(payload)
    record = dict(payload=body, sha256=learning._digest(body.encode()), **{k: payload[k] for k in
                  ('game_id', 'selected_at', 'freeze_sha')})
    quotes = {r['quote_id']: dict(r, id=r['quote_id'], retrieved_at=r['observed_at'], line_type='main') for r in rows}
    def q1(sql, args=()):
        if 'nfl_top_five_selections' in sql:
            return record
        if 'frozen_at' in sql:
            return {'frozen_at': 120.}
        if 'nfl_prop_snapshots' in sql:
            return quotes[args[0]]
        raise AssertionError(sql)
    monkeypatch.setattr(learning.db, 'q1', q1)
    monkeypatch.setattr(learning.grading, '_freeze', lambda sha: {'projections': clean,
        'metadata': {'source_batch_id': 'batch'}})
    monkeypatch.setattr(lab, '_pair_quotes', lambda batch: (list(quotes.values()), {}))
    from edgeterminal import nfl_prop_availability
    calls = []
    def available(player, game, kickoff, market, now):
        calls.append(now)
        return {'blocked': False, 'status': 'unresolved'}
    monkeypatch.setattr(nfl_prop_availability, 'view', available)
    return rows, clean, record, calls


def test_universe_reconstructs_all_offers_and_uses_selection_time_availability(monkeypatch):
    rows, clean, record, calls = source_fixture(monkeypatch)
    universe, eligible, excluded, _ = learning._pregame_universe(record)
    assert len(universe) == len(eligible) == 12
    assert excluded == {} and set(calls) == {150.}
    clean[-1]['line'] = 999.
    with pytest.raises(ValueError, match='differs'):
        learning._pregame_universe(record)


def test_one_nonselected_unresolved_offer_excludes_entire_common_game(monkeypatch):
    rows = offers()
    monkeypatch.setattr(learning, '_pregame_universe', lambda group: (rows, rows, {}, {}))
    # Even when every selected forecast has a grade, a nonselected unresolved offer
    # disqualifies the full policy comparison for both models.
    revisions = [dict(quote_id=r['quote_id'], payload=json.dumps(dict(research_statistical_result='won')))
                 for r in rows[:-1]]
    monkeypatch.setattr(learning.db, 'q', lambda *a: revisions)
    # Put the unknown offer first to avoid touching unrelated evidence in this test.
    rows.insert(0, rows.pop())
    group, sources, reason = learning._graded_universe({'freeze_sha': 'sha'}, 400.)
    assert group is None and sources == [] and reason == 'eligible_universe_unresolved_or_void'


def test_reverification_rejects_changed_universe_and_forged_selected_scores(monkeypatch):
    rows = offers()
    detail = dict(game_id=rows[0]['game_id'], freeze_sha='freeze', selected_at=150.,
        universe_quote_ids=[r['quote_id'] for r in rows], eligible_quote_ids=[r['quote_id'] for r in rows],
        exclusions={}, rows=rows)
    monkeypatch.setattr(learning, '_graded_universe', lambda *a: (copy.deepcopy(detail), [], None))
    monkeypatch.setattr(learning, '_catalog', lambda *a: {})
    monkeypatch.setattr(learning, '_attach_features', lambda *a: None)
    monkeypatch.setattr(learning, '_incumbent', lambda candidate_id: None)
    monkeypatch.setattr(learning, '_score', lambda rows, artifact=None: copy.deepcopy(rows))
    group = learning._policy(detail, None, {})
    group['baseline_candidate_id'] = None
    assert learning.verify_policy_universe(group, {}) is True
    changed = copy.deepcopy(group)
    changed['universe_quote_ids'].pop()
    with pytest.raises(ValueError, match='universe'):
        learning.verify_policy_universe(changed, {})
    changed = copy.deepcopy(group)
    changed['candidate'][0]['candidate_prediction'] = 99.
    with pytest.raises(ValueError, match='scorer'):
        learning.verify_policy_universe(changed, {})


def test_composite_serving_keeps_entire_retained_baseline_row(monkeypatch):
    configs = {market: learning._baseline_config() for market in lab.MARKETS}
    configs['receiving_yards'] = role_config(40.)
    artifact = dict(schema=learning.ROLE_SCHEMA, parameters=learning.ROLE_PARAMETERS,
        features=list(roles.FEATURES), configurations=configs, fit_boundary_week=[2026, 3],
        parameter_fingerprint='f' * 64, calibration_cutoff=50., validation_description='untouched full offers')
    active = dict(candidate_id='adaptive', version='role-version', training_cutoff=25.,
                  artifact_sha256='a' * 64, artifact=artifact)
    monkeypatch.setattr(learning, '_active', lambda: active)
    row = offers(count=1)[0]
    row.update(model_version=baseline.VERSION + ':boosted_role',
        parameter_fingerprint=baseline.PARAMETER_FINGERPRINT, model_manifest_sha256=baseline.MANIFEST_SHA256,
        input_context={'probability_method': 'empirical'})
    retained = copy.deepcopy(row)
    changed = dict(row, quote_id=99, market='receiving_yards', input_context={})
    monkeypatch.setattr(baseline, 'project_current', lambda *a, **kw: {'projections': [row, changed]})
    prepared = {'game': {'game_id': 'g'}, 'feature': features(), 'opp_hist': []}
    monkeypatch.setattr(lab, '_week3_features', lambda *a: {('g0', 'gb'): prepared})
    monkeypatch.setattr(lab, '_read_stats', lambda *a: ([], {}))
    monkeypatch.setattr(roles, 'current_features', lambda *a: {('g0', 'gb', m): features() for m in lab.MARKETS})
    monkeypatch.setattr(learning.db, 'q1', lambda *a: {'team': 'GB'})
    monkeypatch.setattr(roles, 'fit_market', lambda *a: pytest.fail('fit during serving'))
    report = learning.project_current({}, {}, {}, 'batch', season=2026, week=8)
    assert row == retained
    assert changed['projection'] == 40.
    assert changed['model_version'] == 'role-version:role_v2'
    assert learning._seal_model_matches(row, report['model_metadata'], active)
    assert learning._seal_model_matches(changed, report['model_metadata'], active)
    tampered = dict(row, model_manifest_sha256='x')
    assert not learning._seal_model_matches(tampered, report['model_metadata'], active)


def test_build_chooses_markets_on_distinct_inner_week_and_retains_exact_configs(tmp_path, monkeypatch):
    schedule = {}
    records, groups = [], {}
    for week in range(1, 8):
        for i in range(30):
            game = f'nfl-2026-reg-{week:02d}-A{i}-B{i}'
            schedule[game] = dict(season=2026, week=week, away_team=f'A{i}', home_team=f'B{i}', kickoff=week * 1000.)
            record = dict(game_id=game, freeze_sha=game, selected_at=week * 1000. - 100,
                          season=2026, week=week)
            records.append(record)
            rows = []
            for market in lab.MARKETS:
                r = offers(game, count=1)[0]
                r.update(market=market, quote_id=week * 10000 + i * 10 + lab.MARKETS.index(market),
                         week=week, season=2026, kickoff_at=week * 1000.,
                         result_observed_at=week * 1000. + 100,
                         observed_at=week * 1000. - 200,
                         actual=8. if i % 2 else 3., frozen_metadata={})
                r['features']['opportunity_last1'] = 8. if i % 2 else 3.
                r['quote_id'] = week * 10000 + i * 100 + 2 * lab.MARKETS.index(market)
                rows.extend([r, dict(r, quote_id=r['quote_id'] + 1, side='under')])
            groups[game] = dict(record, universe_quote_ids=[r['quote_id'] for r in rows],
                eligible_quote_ids=[r['quote_id'] for r in rows], exclusions={}, rows=rows)
    outer = [r for r in records if r['week'] >= 5 and int(r['game_id'].split('-')[4][1:]) < 10]
    monkeypatch.setattr(learning, '_quality_window', lambda now: (outer, [(2026, 5), (2026, 6), (2026, 7)], schedule))
    monkeypatch.setattr(learning, '_kickoff', lambda g: g['kickoff'])
    monkeypatch.setattr(store, 'holdout_available', lambda *a: True)
    retained_config = dict(coefficients=[0.] * len(baseline.FEATURES), intercept=7., residuals=[0.] * 40,
                           retained_extra={'original': ['preserve', 1.]})
    prior_configs = {m: copy.deepcopy(retained_config) for m in lab.MARKETS}
    active = dict(candidate_id='prior', training_cutoff=1., artifact={'configurations': prior_configs})
    monkeypatch.setattr(learning, '_validated_active', lambda: active)
    monkeypatch.setattr(learning.db, 'q', lambda *a: records)
    source = dict(kind='file', path='data/nflverse/source.csv', sha256='c' * 64, received_at=1.)
    monkeypatch.setattr(learning, '_graded_universe', lambda r, now: (copy.deepcopy(groups[r['game_id']]), [dict(source, role='validation')], None))
    monkeypatch.setattr(learning, '_catalog', lambda now: {})
    monkeypatch.setattr(learning, '_current_snapshot', lambda *a: {'body': b'fixture', 'source': source})
    monkeypatch.setattr(learning, '_require_completed_history', lambda *a: None)
    def dataset(body, cutoff):
        return {m: [dict(season=2026, week=w, game_id=f'nfl-2026-reg-{w:02d}-A{i}-B{i}',
                         features=features(), target=8.) for w in range(1, cutoff[1]) for i in range(30)]
                for m in lab.MARKETS}, {}, {}
    monkeypatch.setattr(learning, '_role_dataset', dataset)
    monkeypatch.setattr(learning, '_attach_features', lambda *a: None)
    monkeypatch.setattr(roles, 'fit_market', lambda *a: role_config())
    monkeypatch.setattr(lab, 'SCHEDULE', SimpleNamespace(
        read_bytes=lambda: b'schedule', relative_to=lambda root: 'data/nflverse/schedule.csv',
        stat=lambda: SimpleNamespace(st_mtime=1.)))
    def score(rows, artifact=None):
        out = []
        for r in rows:
            candidate = artifact is not None and artifact is not active['artifact']
            signal = r['features']['opportunity_last1']
            probability = .9 if signal > r['line'] else .1
            out.append(dict(r, projection=signal if candidate else 7.,
                            probability_over=probability if candidate else .5,
                            probability_under=1 - probability if candidate else .5,
                            probability_push=0.))
        return out
    monkeypatch.setattr(learning, '_score', score)
    # Test a predeclared inner choice without making this protocol fixture depend
    # on the helper's separate tested minimum-label or optimizer behavior.
    from edgeterminal import prop_calibration
    def choice(rows, markets, *, cutoff):
        assert {r['game_id'].split('-')[3] for r in rows} == {'04'}
        assert max(r['result_observed_at'] for r in rows) < cutoff == 4999.
        return dict(changed_markets=['receptions'], markets={}, reasons=[],
            selection_game_ids=sorted({r['game_id'] for r in rows}),
            selection_quote_ids=[r['quote_id'] for r in rows], selection_label_max_at=4100., selection_cutoff=cutoff)
    monkeypatch.setattr(prop_calibration, 'choose_market_updates', choice)
    result = learning.build_candidate(10000.)
    assert result['status'] == 'ready'
    artifact = result['artifact']
    assert artifact['changed_markets'] == ['receptions']
    assert artifact['configurations']['passing_yards'] == retained_config
    assert artifact['configurations']['passing_yards'] is not retained_config
    assert len(result['evaluation']['selection_policy']['games']) == 30
    training = artifact['training']
    inner = set(artifact['market_selection']['selection_game_ids'])
    outer_ids = {r['game_id'] for r in result['evaluation']['rows']}
    assert not inner & (set(training['fit_game_ids']) | set(training['calibration_game_ids']) | outer_ids)
    assert all(len(g['baseline']) == len(g['candidate']) == 5 for g in result['evaluation']['selection_policy']['games'])


def test_paired_quality_probabilities_condition_on_no_push_without_changing_live_mass():
    row = offers(count=1)[0]
    before = dict(row, probability_over=.4, probability_under=.4, probability_push=.2)
    after = dict(row, probability_over=.6, probability_under=.2, probability_push=.2)
    paired = learning._paired(row, before, after)
    assert paired['baseline_probability'] == .5
    assert paired['candidate_probability'] == pytest.approx(.75)
    assert before['probability_push'] == after['probability_push'] == .2


def test_missing_inner_quotes_wait_before_fit_or_outer_label_reads(monkeypatch):
    schedule = {str(w): dict(season=2026, week=w, kickoff=w * 1000.) for w in range(3, 8)}
    outer = [dict(game_id='outer', week=5, season=2026)]
    monkeypatch.setattr(learning, '_quality_window', lambda now: (outer, [(2026, 5), (2026, 6), (2026, 7)], schedule))
    monkeypatch.setattr(store, 'holdout_available', lambda *a: True)
    monkeypatch.setattr(learning, '_kickoff', lambda game: game['kickoff'])
    monkeypatch.setattr(learning, '_validated_active', lambda: None)
    monkeypatch.setattr(learning.db, 'q', lambda *a: [])
    monkeypatch.setattr(learning, '_graded_universe', lambda *a: pytest.fail('outer labels before inner choice'))
    monkeypatch.setattr(learning, '_role_dataset', lambda *a: pytest.fail('fit without quote coverage'))
    result = learning.build_candidate(10000.)
    assert result['status'] == 'waiting' and result['waiting_kind'] == 'coverage_wait'
    assert result['quote_coverage']['market_selection_rows'] == 0


def test_sparse_qb_week_retains_exact_incumbent_without_blocking_other_markets(monkeypatch):
    fit = {m: [{}] for m in lab.MARKETS}
    calibration = {m: [{}] * 30 for m in lab.MARKETS}
    calibration['passing_yards'] = [{}] * 28
    original = {m: dict(kind='existing-config', exact=[m, 1.234567890123]) for m in lab.MARKETS}
    incumbent = {'configurations': original}
    calls = []
    def fitted(rows, cal, market):
        calls.append(market)
        return role_config()
    monkeypatch.setattr(roles, 'fit_market', fitted)
    proposal, coverage = learning._fit_role_proposals(fit, calibration, incumbent)
    assert 'passing_yards' not in calls and len(calls) == 4
    assert proposal['passing_yards'] == original['passing_yards']
    assert proposal['passing_yards'] is not original['passing_yards']
    assert not coverage['passing_yards']['supported']
    assert coverage['receptions']['supported']
