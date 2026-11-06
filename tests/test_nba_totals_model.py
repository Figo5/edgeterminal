"""Totals uses its own frozen parameters and leaves every spread decision alone."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math

import pytest

from edgeterminal import nba_totals_model as model


DAY = 86400
TIP = datetime(2026, 11, 6, 0, tzinfo=timezone.utc).timestamp()


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def history(tip=TIP, *, pace=False, home_points=116, away_points=108):
    result = []
    for team, points, allowed in [('BOS', home_points, 106), ('NYK', away_points, 112)]:
        for i in range(20):
            played = tip - ((i + 3) if i < 10 else (i + 355)) * DAY
            when = datetime.fromtimestamp(played, timezone.utc)
            row = dict(team=team, opponent='CHI', game_id=f'{team}-{i}', played_at=played,
                       points=points + i % 3, opponent_points=allowed + i % 2,
                       season_start_year=when.year if when.month >= 7 else when.year - 1,
                       received_at=played + 4 * 3600)
            if pace:
                row.update(raw_possessions=100., duration_minutes=48.)
            result.append(row)
    return result


def context(rows=None, *, tip=TIP, baseline_total=230., **kwargs):
    return model.features('BOS', 'NYK', history(tip) if rows is None else rows, tip,
                          as_of=tip - 1800, baseline_total=baseline_total, **kwargs)


def input_map(rows=None):
    value = dict(schema='nba-totals-input-1', game_id='target', home='BOS', away='NYK',
                 tipoff=TIP, as_of=TIP - 1800, rows=history() if rows is None else rows)
    return dict(value, sha256=_sha(value))


def examples():
    # Dates cross season boundaries; all labels precede the next phase's as-of.
    result = []
    start = datetime(2024, 10, 5, 0, tzinfo=timezone.utc).timestamp()
    for i in range(12):
        tip = start + i * 60 * DAY
        base = 220. + i % 4
        ctx = context(tip=tip, baseline_total=base)
        result.append(dict(game_id=str(i), ts=tip, as_of=tip - 1800,
                           label_received_at=tip + 4 * 3600, baseline_total=base,
                           actual_total=base + 6., context=ctx))
    return result


def fit(rows=None):
    rows = examples() if rows is None else rows
    return model.fit_candidate(rows, rows[-1]['ts'] + DAY,
                               min_fit=6, min_development=3, min_calibration=3)


def projection():
    return dict(total=230., proj_home=115., proj_away=115., home_score=115., away_score=115.,
                margin=-.00001, home_margin=-.00001, raw_home_margin=-.00001,
                raw_feature_margin=-.00002, home_win_probability=.499999,
                away_win_probability=.500001, spread_probability_home=.499998,
                spread_probability_away=.500002, spread_probability_push=0.,
                spread_probability_home_given_no_push=.499998, spread_side='away',
                game_component_version='existing-spread', parameter_fingerprint='s' * 64,
                training_cutoff=1., market_margin=0., market_anchor_weight=.25,
                sd_margin=11., sd_total=16.5, uncertainty=dict(sd_margin=11., sd_total=16.5))


def test_features_separate_current_prior_and_recent_rates_without_mutation():
    rows = history()
    original = copy.deepcopy(rows)
    ctx = context(rows)
    assert rows == original
    assert ctx['status'] == 'ok'
    assert len(ctx['values']) == len(model.FEATURE_NAMES)
    assert all(math.isfinite(x) for x in ctx['values'])
    assert ctx['coverage']['home']['current_season_rows'] == 10
    assert ctx['coverage']['home']['prior_season_rows'] == 10
    assert ctx['coverage']['home']['current_weight'] == pytest.approx(.5)
    assert ctx['coverage']['home']['pace_rows'] == 0
    assert not ctx['coverage']['pace_supported']
    assert len(ctx['selected_rows']) == 40


def test_few_current_rows_shrink_to_prior_and_old_seasons_are_not_used():
    rows = history()
    rows = [r for r in rows if r['game_id'].endswith('-0') or int(r['game_id'].split('-')[1]) >= 10]
    old = dict(rows[0], game_id='too-old', played_at=TIP - 800 * DAY,
               received_at=TIP - 799 * DAY, season_start_year=2024)
    ctx = context(rows + [old])
    assert ctx['coverage']['home']['current_season_rows'] == 1
    assert ctx['coverage']['home']['current_weight'] == pytest.approx(1 / 11)
    assert 'too-old' not in {r['game_id'] for r in ctx['selected_rows']}
    thin = context([r for r in rows if r['game_id'].endswith('-0')])
    assert thin['status'] == 'unavailable'


@pytest.mark.parametrize('change', [
    {'points': float('nan')}, {'points': True}, {'opponent_points': 1.2},
    {'played_at': TIP - 100}, {'received_at': TIP}, {'team': 'XXX'},
    {'played_at': TIP - 4 * 3600},
    {'opponent': 'BOS'}, {'season_start_year': 2025},
    {'raw_possessions': 0.}, {'duration_minutes': 49.},
    {'raw_possessions': 100., 'duration_minutes': 48., 'pace48': 105.},
])
def test_corrupt_identity_time_or_numeric_evidence_fails(change):
    rows = history()
    rows[0].update(change)
    with pytest.raises(ValueError):
        context(rows)


def test_conflicting_duplicates_and_mirrored_game_scores_fail():
    rows = history()
    with pytest.raises(ValueError, match='conflict'):
        context(rows + [dict(rows[0], points=120)])
    assert len(context(rows + [dict(rows[0])])['selected_rows']) == 40
    mirror = dict(rows[0], team='CHI', opponent='BOS', points=90, opponent_points=116)
    with pytest.raises(ValueError, match='conflict'):
        context(rows + [mirror])


def test_pace_normalizes_overtime_without_changing_full_game_efficiency():
    rows = history(pace=True)
    for row in rows:
        row.update(raw_possessions=110., duration_minutes=53., pace48=110. * 48 / 53)
    ctx = context(rows)
    home = ctx['team_rates']['BOS']
    assert home['pace'] == pytest.approx(110 * 48 / 53)
    assert home['offense_ppp'] == pytest.approx(sum(r['points'] / 110 for r in rows[:20]) / 20)
    assert ctx['coverage']['pace_supported']
    assert rows[0]['raw_possessions'] == 110.
    assert rows[0]['duration_minutes'] == 53.


def test_partial_missing_duration_withholds_pace_but_keeps_score_coverage():
    rows = history(pace=True)
    for row in rows[:16]:
        row.pop('duration_minutes')
    ctx = context(rows)
    assert ctx['status'] == 'ok'
    assert ctx['coverage']['home']['pace_rows'] == 4
    assert not ctx['coverage']['pace_supported']
    assert ctx['values'][model.FEATURE_NAMES.index('pace_coverage')] == 0.


def test_opponent_context_uses_only_rows_earlier_than_the_historical_game():
    rows = history(pace=True)
    older = [dict(team='CHI', opponent='LAL', game_id=f'opp-{i}',
                  played_at=TIP - (420 + i) * DAY, received_at=TIP - (419 + i) * DAY,
                  points=120, opponent_points=130, raw_possessions=100., duration_minutes=48.)
             for i in range(5)]
    ctx = context(rows + older)
    assert ctx['coverage']['opponent_feature_rows'] == 5
    later = [dict(r, game_id=r['game_id'] + '-late', played_at=TIP - 2 * DAY,
                  received_at=TIP - DAY) for r in older]
    again = context(rows + older + later)
    assert again['values'] == ctx['values']
    assert all(not r['game_id'].endswith('-late') for r in again['selected_rows'])


def test_reconstruction_retains_true_receipts_and_is_explicitly_marked():
    rows = history()
    for row in rows:
        row['received_at'] = TIP + DAY
    with pytest.raises(ValueError, match='receipt'):
        context(rows)
    ctx = context(rows, reconstruction_cutoff=TIP + 2 * DAY)
    assert ctx['reconstructed']
    assert ctx['reconstruction_cutoff'] == TIP + 2 * DAY
    assert all(r['received_at'] == TIP + DAY for r in ctx['selected_rows'])


def test_fitter_uses_earlier_fit_and_development_then_calibrates_sigma_only():
    rows = examples()
    artifact = fit(rows)
    assert model.validate_artifact(artifact) == artifact
    assert artifact['params']['method'] == 'intercept'
    assert artifact['intercept'] == pytest.approx(6.)
    assert artifact['sd_total'] == 1.
    assert set(artifact['phase_dates']) == {'fit', 'development', 'calibration'}
    dates = artifact['phase_dates']
    assert max(dates['fit']) < min(dates['development'])
    assert max(dates['development']) < min(dates['calibration'])
    poisoned = copy.deepcopy(rows)
    for row in poisoned[-3:]:
        row['actual_total'] += 20
    second = fit(poisoned)
    assert second['params'] == artifact['params']
    assert second['intercept'] == artifact['intercept']
    assert second['sd_total'] != artifact['sd_total']
    assert second['version'] != artifact['version']


def test_fitter_never_admits_outer_rows_or_split_dates_or_late_labels():
    rows = examples()
    bad = copy.deepcopy(rows)
    bad[-1]['phase'] = 'outer'
    with pytest.raises(ValueError, match='phase'):
        fit(bad)
    bad = copy.deepcopy(rows)
    for i, row in enumerate(bad):
        row['phase'] = 'fit' if i < 6 else 'development' if i < 9 else 'calibration'
    bad[6]['ts'] = bad[5]['ts'] + 60
    bad[6]['as_of'] = bad[6]['ts'] - 1800
    bad[6]['context'] = context(tip=bad[6]['ts'], baseline_total=bad[6]['baseline_total'])
    with pytest.raises(ValueError, match='date|phase'):
        fit(bad)
    bad = copy.deepcopy(rows)
    bad[0]['label_received_at'] = bad[6]['as_of'] + 1
    with pytest.raises(ValueError, match='receipt|label'):
        fit(bad)
    bad = copy.deepcopy(rows)
    bad[-1]['label_received_at'] = bad[-1]['ts'] + 2 * DAY
    with pytest.raises(ValueError, match='receipt|cutoff'):
        fit(bad)


def test_integer_push_and_conditional_probabilities_match_the_same_total_mean():
    p = model.total_probabilities(230., 16.5, 230.)
    assert p['push'] > 0
    assert p['over'] + p['under'] + p['push'] == pytest.approx(1.)
    assert p['conditional_over'] == pytest.approx(.5)
    assert p['conditional_under'] == pytest.approx(.5)
    half = model.total_probabilities(232., 16.5, 230.5)
    assert half['push'] == 0.
    assert half['over'] == half['conditional_over'] > .5
    for args in [(230., 0., 230.), (float('inf'), 16., 230.), (230., 16., True)]:
        with pytest.raises(ValueError):
            model.total_probabilities(*args)


def test_compose_keeps_all_spread_fields_exact_and_reconciles_both_score_pairs():
    before = projection()
    original = copy.deepcopy(before)
    artifact = fit()
    after = model.compose(before, input_map(), artifact)
    assert before == original
    assert after['total'] == 236.
    changed = {'total', 'proj_home', 'proj_away', 'home_score', 'away_score', 'sd_total', 'uncertainty'}
    for name, value in original.items():
        if name not in changed:
            assert after[name] == value, name
    assert after['uncertainty']['sd_margin'] == original['uncertainty']['sd_margin']
    assert after['home_score'] == after['proj_home']
    assert after['away_score'] == after['proj_away']
    assert after['home_score'] + after['away_score'] == pytest.approx(after['total'], abs=.011)
    assert after['home_score'] - after['away_score'] == pytest.approx(before['home_margin'], abs=.011)
    assert after['total_applied']
    assert after['total_artifact_sha256'] == _sha(artifact)
    assert after['total_input_map_sha256'] == input_map()['sha256']


def test_absent_future_or_unapproved_active_state_retains_every_incumbent_number():
    before = projection()
    no = model.compose(before, input_map())
    assert all(no[k] == value for k, value in before.items())
    assert no['total_component_version'] == model.INCUMBENT_VERSION
    artifact = fit()
    future_state = dict(artifact=artifact, activated_at=TIP, sha256=_sha(artifact))
    future = model.compose(before, input_map(), model_state=future_state)
    assert all(future[k] == value for k, value in before.items())
    assert future['total_fallback'] == 'artifact_not_available_asof'


def test_map_hash_target_rows_and_reconstruction_cannot_bypass_composition():
    bad = input_map()
    bad['rows'][0]['points'] += 1
    with pytest.raises(ValueError, match='hash'):
        model.compose(projection(), bad, fit())
    bad = input_map()
    bad['rows'][0]['game_id'] = 'target'
    bad['sha256'] = _sha({k: v for k, v in bad.items() if k != 'sha256'})
    with pytest.raises(ValueError, match='target'):
        model.compose(projection(), bad, fit())
    bad = input_map()
    bad['reconstructed'] = True
    bad['sha256'] = _sha({k: v for k, v in bad.items() if k != 'sha256'})
    with pytest.raises(ValueError, match='reconstruct'):
        model.compose(projection(), bad, fit())


@pytest.mark.parametrize('change', [
    {'kind': 'nba-spread-residual-v1'}, {'schema': 2}, {'sd_total': float('nan')},
    {'sd_total': 0.}, {'feature_names': ['baseline_total']},
    {'params': {'method': 'season', 'shrinkage': 1, 'recent_weight': .25}},
    {'intercept': float('inf')}, {'version': 'mislabelled'},
])
def test_artifact_contract_rejects_unsealed_or_unsupported_parameters(change):
    artifact = fit()
    artifact.update(change)
    with pytest.raises(ValueError):
        model.validate_artifact(artifact)


def test_envelope_seals_change_the_immutable_candidate_version():
    artifact = fit()
    sealed = model.seal_version(dict(artifact, code_sha256={'edgeterminal/nba_totals_model.py': 'a' * 64},
                                     training_sources_sha256='b' * 64, evaluation_protocol_sha256='c' * 64))
    assert sealed['version'] != artifact['version']
    model.validate_artifact(sealed)
    changed = copy.deepcopy(sealed)
    changed['code_sha256']['edgeterminal/nba_totals_model.py'] = 'd' * 64
    with pytest.raises(ValueError, match='seal'):
        model.validate_artifact(changed)


def test_pace_model_distinguishes_pace_from_efficiency_and_retains_score_fallback():
    artifact = fit()
    artifact['params'] = dict(method='pace', shrinkage=10., recent_weight=.25)
    artifact['intercept'] = 0.
    artifact = model.seal_version(artifact)
    rows = history(pace=True)
    opponent = [dict(team='CHI', opponent='LAL', game_id=f'opp-{i}',
                     played_at=TIP - (420 + i) * DAY, received_at=TIP - (419 + i) * DAY,
                     points=110, opponent_points=110, raw_possessions=100., duration_minutes=48.)
                for i in range(5)]
    initial = model.predict(artifact, context(rows + opponent), 230.)
    assert initial['branch'] == 'pace_efficiency_opponent'
    # More raw possessions at fixed points means lower efficiency, not a
    # proportional points increase from accidentally dividing by pace48 twice.
    changed = [dict(row, raw_possessions=row['raw_possessions'] * 1.1) for row in rows + opponent]
    same = model.predict(artifact, context(changed), 230.)
    assert same['total'] == pytest.approx(initial['total'])
    higher = [dict(row, points=row['points'] + 10, opponent_points=row['opponent_points'] + 10)
              for row in rows + opponent]
    faster = model.predict(artifact, context(higher), 230.)
    assert faster['total'] > initial['total']
    missing = model.predict(artifact, context(history()), 230.)
    assert missing['applied']
    assert missing['branch'] == 'season_score_missing_pace_or_opponent'
    assert missing['fallback'] == 'missing_pace_or_opponent'


def test_prediction_future_training_or_thin_history_keeps_incumbent_total():
    artifact = fit()
    current = context()
    earlier = dict(current, as_of=artifact['training_cutoff'])
    assert model.predict(artifact, earlier, 230.)['total'] == 230.
    assert not model.predict(artifact, earlier, 230.)['applied']
    thin = context(history()[:2])
    output = model.predict(artifact, thin, 230.)
    assert output['total'] == 230.
    assert output['fallback'] == 'insufficient_score_history'


def test_probability_is_defined_for_deterministic_integer_push_limit():
    p = model.total_probabilities(230., 1e-300, 230.)
    assert p['push'] == 1.
    assert p['conditional_over'] == .5
    assert p['conditional_under'] == .5


def test_development_selects_season_weights_and_calibration_cannot_retune_them():
    rows = examples()
    for i, example in enumerate(rows):
        evidence = history(example['ts'])
        for row in evidence:
            index = int(row['game_id'].split('-')[1])
            row['points'] = (100 + (i % 3) * 6 if index < 10 else 110 + (i % 4) * 4)
            row['opponent_points'] = (102 + (i % 4) * 3 if index < 10 else 108 + (i % 3) * 4)
            if index < 5:
                row['points'] += (i % 5) * 3
        example['context'] = context(evidence, tip=example['ts'], baseline_total=example['baseline_total'])
        # Independently generate truth from a 10/(10+5) current-season weight
        # and a half recent-game weight. The model must choose these on dev.
        current = sum(r['points'] + r['opponent_points'] for r in evidence if int(r['game_id'].split('-')[1]) < 10) / 20
        prior = sum(r['points'] + r['opponent_points'] for r in evidence if int(r['game_id'].split('-')[1]) >= 10) / 20
        recent = sum(r['points'] + r['opponent_points'] for r in evidence if int(r['game_id'].split('-')[1]) < 5) / 10
        example['actual_total'] = .5 * ((2 / 3) * current + (1 / 3) * prior) + .5 * recent + 4
    artifact = fit(rows)
    assert artifact['params'] == dict(method='season', shrinkage=5., recent_weight=.5)
    assert artifact['intercept'] == pytest.approx(4.)
    changed = copy.deepcopy(rows)
    for example in changed[-3:]:
        example['actual_total'] = 300.
    second = fit(changed)
    assert second['params'] == artifact['params']
    assert second['intercept'] == artifact['intercept']


def test_target_game_identity_cannot_enter_its_training_context():
    rows = examples()
    rows[0]['context']['selected_rows'][0]['game_id'] = rows[0]['game_id']
    with pytest.raises(ValueError, match='target'):
        fit(rows)


def test_artifact_rechecks_phase_availability_and_integer_counts():
    artifact = fit()
    corrupted = copy.deepcopy(artifact)
    corrupted['phase_rows']['fit'] = 6.5
    with pytest.raises(ValueError, match='count'):
        model.seal_version(corrupted)
    corrupted = copy.deepcopy(artifact)
    corrupted['phase_receipt_max']['fit'] = corrupted['phase_asof_min']['development'] + 1
    with pytest.raises(ValueError, match='receipt|chronology'):
        model.seal_version(corrupted)


def test_current_only_or_prior_only_windows_keep_supported_scoring():
    for offset, year, weight in [(3, 2026, 1.), (355, 2025, 0.)]:
        rows = history()
        for row in rows:
            index = int(row['game_id'].split('-')[1])
            row.update(played_at=TIP - (offset + index) * DAY,
                       received_at=TIP - (offset + index) * DAY + 4 * 3600,
                       season_start_year=year)
        ctx = context(rows)
        assert ctx['status'] == 'ok'
        assert ctx['coverage']['home']['current_weight'] == weight
        assert math.isfinite(model.predict(fit(), ctx, 230.)['total'])


def test_automatic_split_keeps_entire_eastern_dates_together():
    doubled = []
    for row in examples():
        doubled.append(row)
        twin = copy.deepcopy(row)
        twin['game_id'] += '-other'
        doubled.append(twin)
    artifact = model.fit_candidate(doubled, doubled[-1]['ts'] + DAY,
                                   min_fit=6, min_development=3, min_calibration=3)
    assert artifact['phase_rows']['development'] == 4
    assert artifact['phase_rows']['calibration'] == 4
    assert not set(artifact['phase_dates']['fit']) & set(artifact['phase_dates']['development'])
    with pytest.raises(ValueError, match='Insufficient'):
        model.fit_candidate(examples(), examples()[-1]['ts'] + DAY)


def test_invalid_time_and_container_game_identity_fail_as_value_errors():
    for change in [{'played_at': -1e100}, {'game_id': {'invalid': 1}}, {'game_id': '   '}]:
        rows = history()
        rows[0].update(change)
        with pytest.raises(ValueError):
            context(rows)


def test_composed_coverage_reports_the_selected_effective_season_weights():
    artifact = fit()
    artifact['params'] = dict(method='season', shrinkage=5., recent_weight=.5)
    artifact = model.seal_version(artifact)
    result = model.compose(projection(), input_map(), artifact)
    assert result['total_coverage']['home']['current_weight'] == pytest.approx(2 / 3)
    assert result['total_coverage']['away']['current_weight'] == pytest.approx(2 / 3)
    assert result['total_coverage']['season_shrinkage'] == 5.
    assert result['total_coverage']['recent_weight'] == .5


def test_raw_model_base_matches_serving_when_comparator_is_an_active_total():
    raw = projection()
    active_artifact = fit()
    active_public = model.compose(raw, input_map(), active_artifact)
    assert active_public['total'] == 236.
    assert active_public['total_base_total'] == raw['total']
    assert active_public['total_base_sd_total'] == raw['sd_total']
    assert active_artifact['model_base'] == 'pre_totals_spread_projection_v1'

    # A future proposal competes with the saved active total. Its inputs still
    # start from the raw spread component, exactly as serving composition does.
    earlier = examples()
    for row in earlier:
        row['actual_total'] += 2
    pending = fit(earlier)
    replay_context = context(baseline_total=active_public['total_base_total'])
    replay = model.predict(pending, replay_context, active_public['total_base_total'])
    served = model.compose(raw, input_map(), pending)
    assert served['total'] == replay['total'] == 238.
    assert served['sd_total'] == replay['sd_total']
    assert replay['total'] - active_public['total'] == 2.
    with pytest.raises(ValueError, match='already.*compos'):
        model.compose(active_public, input_map(), pending)


def test_raw_base_contract_is_versioned_and_spread_sigma_is_retained_in_metadata():
    artifact = fit()
    corrupted = dict(artifact, model_base='currently_active_total')
    with pytest.raises(ValueError, match='contract|base'):
        model.seal_version(corrupted)
    raw = projection()
    raw['sd_total'] = 18.25
    result = model.compose(raw, input_map(), artifact)
    assert result['total_base_sd_total'] == 18.25
    assert result['sd_total'] == artifact['sd_total']
    retained = model.compose(raw, input_map())
    assert retained['sd_total'] == raw['sd_total']
    assert retained['total_base_total'] == raw['total']
    assert retained['total_base_sd_total'] == raw['sd_total']


def _component_example(rows, index, component, receipt, *, declared=None):
    example = rows[index]
    evidence = history(example['ts'], pace=True)
    prior_start = datetime(model._season(example['ts']) - 1, 7, 1, tzinfo=timezone.utc).timestamp()
    evidence.extend(dict(team='CHI', opponent='LAL', game_id=f'opp-{i}',
                         played_at=prior_start + i * DAY, received_at=prior_start + i * DAY + 4 * 3600,
                         points=110, opponent_points=112, raw_possessions=100., duration_minutes=48.)
                    for i in range(5))
    for row in evidence:
        row.update(possession_source_artifact_id=101, duration_source_artifact_id=101,
                   possession_received_at=row['received_at'], duration_received_at=row['received_at'])
    evidence[0][component + '_received_at'] = receipt
    cutoff = rows[-1]['ts'] + DAY
    example['context'] = context(evidence, tip=example['ts'], baseline_total=example['baseline_total'],
                                 reconstruction_cutoff=max(cutoff, receipt))
    assert example['context']['coverage']['pace_supported']
    assert example['context']['coverage']['opponent_supported']
    if declared is not None:
        example['feature_evidence_received_at'] = declared
    return example


@pytest.mark.parametrize('component', ['possession', 'duration'])
@pytest.mark.parametrize('earlier,later', [(0, 6), (6, 9)])
@pytest.mark.parametrize('declared', ['omitted', 'forged'])
def test_fitting_derives_actual_component_receipts_for_both_phase_boundaries(component, earlier, later, declared):
    rows = examples()
    before = copy.deepcopy(rows)
    _component_example(rows, earlier, component, rows[later]['as_of'] + 1,
                       declared=rows[earlier]['as_of'] - DAY if declared == 'forged' else None)
    with pytest.raises(ValueError, match='receipt|chronology'):
        fit(rows)
    assert [r['label_received_at'] for r in rows] == [r['label_received_at'] for r in before]


@pytest.mark.parametrize('component', ['possession', 'duration'])
def test_fitting_feature_receipt_boundary_is_strict_and_uses_history_waiting_error(component):
    for offset in (0, 1):
        rows = examples()
        _component_example(rows, 0, component, rows[6]['as_of'] + offset)
        with pytest.raises(ValueError) as failure:
            fit(rows)
        assert str(failure.value) == 'NBA totals phase date/label receipt chronology conflict'
    rows = examples()
    _component_example(rows, 0, component, rows[6]['as_of'] - 1)
    assert fit(rows)['params'] == fit()['params']


def test_fitting_checks_feature_receipt_cutoff_independently_of_context_reconstruction_marker():
    rows = examples()
    example = _component_example(rows, 10, 'possession', rows[-1]['ts'] + 2 * DAY)
    example['context']['reconstruction_cutoff'] = rows[-1]['ts'] + DAY
    with pytest.raises(ValueError, match='receipt.*cutoff'):
        fit(rows)


@pytest.mark.parametrize('component', ['possession', 'duration'])
def test_real_component_source_identity_needs_actual_receipt_during_fitting_only(component):
    rows = examples()
    example = _component_example(rows, 0, component, rows[0]['as_of'] - DAY)
    example['context']['selected_rows'][0].pop(component + '_received_at')
    with pytest.raises(ValueError, match='receipt'):
        fit(rows)
    # Ordinary saved serving maps predate these fitting-only receipt fields.
    evidence = history(pace=True)
    for row in evidence:
        row.update(possession_source_artifact_id=101, duration_source_artifact_id=101)
    ctx = context(evidence)
    assert ctx['status'] == 'ok'
    assert model.compose(projection(), input_map(evidence), fit())['total'] == 236.


def test_feature_receipt_helper_uses_selected_actual_evidence_without_mutation():
    rows = examples()
    example = _component_example(rows, 0, 'possession', rows[6]['as_of'] - 1)
    ctx = example['context']
    original = copy.deepcopy(ctx)
    assert model.feature_evidence_received_at(ctx) == rows[6]['as_of'] - 1
    assert ctx == original
    synthetic = context()
    assert model.feature_evidence_received_at(synthetic) == max(r['received_at'] for r in synthetic['selected_rows'])


def test_declared_feature_receipt_must_match_actual_selected_evidence():
    for declared in (1., examples()[-1]['ts']):
        rows = examples()
        rows[0]['feature_evidence_received_at'] = declared
        with pytest.raises(ValueError, match='receipt'):
            fit(rows)
    rows = examples()
    for example in rows:
        example['feature_evidence_received_at'] = model.feature_evidence_received_at(example['context'])
    assert fit(rows)['params'] == fit()['params']


def test_unused_late_opponent_and_unsupported_advanced_evidence_do_not_delay_fitting():
    for unsupported in ('pace', 'opponent'):
        rows = examples()
        example = _component_example(rows, 0, 'possession', rows[6]['as_of'] + 1)
        ctx = example['context']
        ctx['coverage'][unsupported + '_supported'] = False
        for row in ctx['selected_rows']:
            if row['team'] == 'CHI':
                row.update(received_at=rows[-1]['ts'] + 2 * DAY,
                           possession_received_at=rows[-1]['ts'] + 2 * DAY,
                           duration_received_at=rows[-1]['ts'] + 2 * DAY)
        assert model.feature_evidence_received_at(ctx) < rows[6]['as_of']
        assert fit(rows)['params']['method'] == 'intercept'


def test_used_late_opponent_evidence_blocks_phase_boundary():
    rows = examples()
    example = _component_example(rows, 0, 'possession', rows[0]['as_of'] - DAY)
    next(row for row in example['context']['selected_rows'] if row['team'] == 'CHI')[
        'duration_received_at'] = rows[6]['as_of']
    with pytest.raises(ValueError) as failure:
        fit(rows)
    assert str(failure.value) == 'NBA totals phase date/label receipt chronology conflict'


@pytest.mark.parametrize('index,next_phase', [(0, 6), (6, 9)])
def test_late_selected_score_receipt_blocks_phase_boundary(index, next_phase):
    rows = examples()
    example = _component_example(rows, index, 'possession', rows[index]['as_of'] - DAY)
    example['context']['selected_rows'][0]['received_at'] = rows[next_phase]['as_of']
    with pytest.raises(ValueError) as failure:
        fit(rows)
    assert str(failure.value) == 'NBA totals phase date/label receipt chronology conflict'


def test_supported_synthetic_components_use_existing_score_receipts():
    rows = examples()
    example = _component_example(rows, 0, 'possession', rows[0]['as_of'] - DAY)
    for row in example['context']['selected_rows']:
        for name in ('possession', 'duration'):
            row.pop(name + '_source_artifact_id')
            row.pop(name + '_received_at')
    expected = max(row['received_at'] for row in example['context']['selected_rows'])
    assert model.feature_evidence_received_at(example['context']) == expected
    assert fit(rows)['params']['method'] == 'intercept'


def test_helper_uses_target_window_and_paired_components_instead_of_unused_union():
    ctx = context(history(pace=True))
    unused = dict(ctx['selected_rows'][-1], game_id='unused-old-target',
                  played_at=min(row['played_at'] for row in ctx['selected_rows']) - DAY,
                  received_at=TIP + DAY, possession_source_artifact_id=123,
                  raw_possessions=100., duration_minutes=None)
    ctx['selected_rows'].append(unused)
    expected = max(row['received_at'] for row in ctx['selected_rows'] if row is not unused)
    assert model.feature_evidence_received_at(ctx) == expected
    # An unpaired raw-possession value does not contribute to pace/efficiency.
    rows = examples()
    example = _component_example(rows, 0, 'possession', rows[0]['as_of'] - DAY)
    row = example['context']['selected_rows'][0]
    row.update(duration_minutes=None, possession_received_at=rows[-1]['ts'] + 2 * DAY)
    assert model.feature_evidence_received_at(example['context']) < rows[6]['as_of']


def test_new_artifact_binds_feature_receipt_maxima_and_validates_them():
    artifact = fit()
    expected = {
        name: max(model.feature_evidence_received_at(r['context']) for r in examples()[span])
        for name, span in [('fit', slice(0, 6)), ('development', slice(6, 9)), ('calibration', slice(9, 12))]
    }
    assert artifact['phase_feature_receipt_max'] == expected
    tampered = copy.deepcopy(artifact)
    tampered['phase_feature_receipt_max']['fit'] -= 1
    with pytest.raises(ValueError, match='seal'):
        model.validate_artifact(tampered)
    for name, receipt in [('fit', artifact['phase_asof_min']['development']),
                          ('development', artifact['phase_asof_min']['calibration']),
                          ('calibration', artifact['training_cutoff'] + 1)]:
        invalid = copy.deepcopy(artifact)
        invalid['phase_feature_receipt_max'][name] = receipt
        with pytest.raises(ValueError, match='receipt|chronology'):
            model.seal_version(invalid)
    invalid = copy.deepcopy(artifact)
    invalid['phase_feature_receipt_max']['outer'] = 1.
    with pytest.raises(ValueError, match='receipt|contract'):
        model.seal_version(invalid)


def test_old_artifact_version_and_old_serving_maps_validate_without_resealing():
    # The exact v1 seal captured before the feature-receipt implementation.
    legacy = fit()
    legacy.pop('phase_feature_receipt_max', None)
    legacy['version'] = 'nba-total-v1-fa98e38f0f603068edb0'
    encoded = json.dumps(legacy, sort_keys=True, separators=(',', ':'))
    assert model.validate_artifact(legacy) == legacy
    assert json.dumps(legacy, sort_keys=True, separators=(',', ':')) == encoded
    serving_map = input_map()
    original = copy.deepcopy(serving_map)
    before = model.compose(projection(), serving_map, legacy)
    after = model.compose(projection(), serving_map, fit())
    assert serving_map == original
    for name in ('total', 'proj_home', 'proj_away', 'home_score', 'away_score',
                 'sd_total', 'margin', 'home_margin', 'sd_margin', 'uncertainty'):
        assert after[name] == before[name]
    assert (before['total'], before['home_score'], before['away_score'], before['sd_total']) == (236., 118., 118., 1.)
