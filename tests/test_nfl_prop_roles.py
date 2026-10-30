"""Role/opportunity transforms use earlier whole weeks and archived columns."""
import copy

import pytest

from edgeterminal import nfl_prop_lab as lab
from edgeterminal import nfl_prop_roles as roles


def player(pid, week, targets, *, team='GB', air=40., **extra):
    row = dict(player_id=pid, game_id=f'2025_{week:02d}_DET_GB', team=team,
               season='2025', week=str(week), season_type='REG', position='WR',
               targets=str(targets), carries='0', attempts='0', receiving_air_yards=str(air),
               passing_air_yards='0', air_yards_share='.5')
    row.update(extra)
    return row


def target(week=4, *, team='GB', market='receiving_yards'):
    return dict(player_id='p', game_id=f'2025_{week:02d}_DET_GB', season=2025,
                week=week, team=team, market=market)


def base():
    return {name: 1. for name in lab.FEATURES}


def test_prior_only_features_and_game_aligned_share():
    raw = [player('p', w, v) for w, v in [(1, 2), (2, 4), (3, 8), (4, 999)]]
    raw += [player('other', w, 2) for w in (1, 2, 3, 4)]
    result = roles.build_features(raw, [target()])[roles.key(target())]
    assert result['opportunity_last1'] == 8
    assert result['opportunity_share_last1'] == .8
    assert result['opportunity_share_last3'] == pytest.approx((.5 + 4/6 + .8) / 3)
    assert result['current_team_prior_games'] == 3
    assert result['role_share_observations'] == 3
    changed = copy.deepcopy(raw)
    for row in changed:
        if int(row['week']) >= 4:
            row['targets'] = '10000000'
            row['receiving_air_yards'] = '1000000000'
    assert roles.build_features(changed, [target()]) == roles.build_features(raw, [target()])


def test_transfer_has_missing_current_team_role_instead_of_invented_share():
    raw = [player('p', w, 8) for w in (1, 2, 3)]
    result = roles.build_features(raw, [target(team='DET')])[roles.key(target(team='DET'))]
    assert result['opportunity_last1'] == 8
    assert result['current_team_prior_games'] == result['role_share_observations'] == 0
    assert result['opportunity_share_last3'] == 0


def test_missing_air_yards_stays_explicitly_missing():
    rows = [player('p', w, 4, receiving_air_yards='', air_yards_share='') for w in (1, 2, 3)]
    result = roles.build_features(rows, [target()])[roles.key(target())]
    assert result['air_yards_observations'] == 0
    assert result['air_yards_per_opportunity_last3'] == 0


def test_conflicting_duplicate_or_nonfinite_prior_data_rejected():
    raw = [player('p', 1, 4), player('p', 1, 6)]
    with pytest.raises(ValueError, match='conflict'):
        roles.build_features(raw, [target()])
    with pytest.raises(ValueError, match='finite'):
        roles.build_features([player('p', 1, 'nan')], [target()])


def test_all_markets_use_their_sourced_opportunity_unit():
    raw = [player('p', w, 4, attempts='20', carries='8') for w in (1, 2, 3)]
    for market, expected in [('passing_yards', 20), ('rushing_yards', 8),
                             ('rushing_attempts', 8), ('receptions', 4), ('receiving_yards', 4)]:
        t = target(market=market)
        assert roles.build_features(raw, [t])[roles.key(t)]['opportunity_last1'] == expected


def test_augment_preserves_baseline_rows_and_never_reads_target_as_feature():
    t = target()
    datasets = {market: [] for market in lab.MARKETS}
    datasets[t['market']] = [dict(t, target=40., features=base())]
    original = copy.deepcopy(datasets)
    augmented = roles.augment_datasets(datasets, [player('p', w, 4) for w in (1, 2, 3)])
    assert datasets == original
    assert set(roles.ROLE_FEATURES) <= augmented[t['market']][0]['features'].keys()


def test_json_fit_does_not_refit_point_coefficients_on_calibration_labels():
    example = {**base(), **{name: 1. for name in roles.ROLE_FEATURES}}
    fit = [dict(features={**example, 'opportunity_last1': float(n)}, target=float(n * 2)) for n in range(1, 50)]
    cal = [dict(features={**example, 'opportunity_last1': float(n)}, target=float(n * 2 + 1)) for n in range(50, 85)]
    config = roles.fit_market(fit, cal, 'receiving_yards')
    changed = roles.fit_market(fit, [dict(row, target=999.) for row in cal], 'receiving_yards')
    assert config['coefficients'] == changed['coefficients']
    assert config['intercept'] == changed['intercept']
    assert config['residuals'] != changed['residuals']
    point, probability = roles.predict(config, example, 'receiving_yards', 4.)
    assert point >= 0 and sum(probability.values()) == pytest.approx(1.)


def test_scoring_rejects_incomplete_or_changed_feature_contract():
    config = dict(kind=roles.SCHEMA, features=list(roles.FEATURES),
                  coefficients=[0.] * len(roles.FEATURES), intercept=4., residuals=[0.] * 30)
    example = {**base(), **{name: 1. for name in roles.ROLE_FEATURES}}
    assert roles.predict(config, example, 'receptions', 4.)[1]['push'] > .9
    with pytest.raises(ValueError, match='role'):
        roles.predict(config, base(), 'receptions', 4.)
    with pytest.raises(ValueError, match='contract'):
        roles.predict({**config, 'features': []}, example, 'receptions', 4.)
