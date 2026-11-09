"""NFL spread quality: finite models, genuine chronology and immutable ledgers."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from edgeterminal import db
from edgeterminal.config import settings

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, 'database_url', 'sqlite:///' + str(tmp_path / 'spread.db'))
    db.init()
    yield tmp_path
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def row(i, week=1, season=2027, start=NOW):
    at = start + (week - 1) * 7 * 86400 + i * 5
    return dict(game_id=f'nfl-{season}-reg-{week:02d}-x{i}-y{i}', season=season, week=week,
        snapshot_id=f'snapshot-{season}-{week}-{i}', as_of=at, kickoff=at + 3600, feature_received_at=at - 10,
        label_received_at=at + 18000, actual_margin=2. * i + 2,
        baseline_margin=1. * i, incumbent_margin=1. * i, baseline_sd=13.2,
        home_spread=-2.5, scoring_margin=1. * i, efficiency_points=0., home_games=7.,
        away_games=7., home_plays=400., away_plays=400., advanced_available=1.,
        formula_version='et-model-0.4.0', input_policy_id='nfl-game-current-inputs-v1',
        source_sha256='a' * 64, snapshot_sha256='b' * 64, quote_sha256='d' * 64, label_sha256='c' * 64)


def test_finite_bank_only_fits_earlier_rows_and_keeps_incumbent():
    from edgeterminal import nfl_spread_model as model
    fit = [row(i, week=1) for i in range(40)]
    bank = model.fit_bank(fit, cutoff=fit[-1]['label_received_at'] + 1)
    assert [m['method'] for m in bank['members']] == ['incumbent', 'affine', 'market_residual']
    assert bank['alpha'] == 100.
    future = row(45, week=2)
    old = deepcopy(bank)
    assert model.predict(bank['members'][1], future) > future['baseline_margin']
    future['actual_margin'] = -9999
    assert bank == old
    assert model.predict(bank['members'][0], future) == future['incumbent_margin']
    with pytest.raises(ValueError, match='receipt|cutoff'):
        model.fit_bank(fit, cutoff=fit[0]['as_of'])


def test_phase_selection_never_splits_weeks_or_reuses_inspected_week():
    from edgeterminal import nfl_spread_model as model
    rows = [row(i, w) for w in range(1, 6) for i in range(20)]
    phases = model.partition(rows, minimums={'fit':40, 'development':20, 'calibration':20})
    assert {r['week'] for r in phases['fit']} == {1, 2}
    assert {r['week'] for r in phases['development']} == {3}
    assert {r['week'] for r in phases['calibration']} == {4}
    bad = row(0, week=8, season=2026)
    with pytest.raises(ValueError, match='inspected'):
        model.validate_example(bad)
    late = deepcopy(rows)
    late[0]['label_received_at'] = row(0, week=5)['as_of']
    with pytest.raises(ValueError, match='chronolog|coverage'):
        model.partition(late, minimums={'fit':40, 'development':20, 'calibration':20})


def test_phase_rejects_mixed_policy_and_uses_fit_only_scaling():
    from edgeterminal import nfl_spread_model as model
    fit = [row(i, week=1) for i in range(40)]
    bank = model.fit_bank(fit, cutoff=fit[-1]['label_received_at'] + 1)
    assert bank['members'][1]['means'] == [19.5]
    fit[-1]['input_policy_id'] = 'incompatible-policy'
    with pytest.raises(ValueError, match='policy'):
        model.fit_bank(fit, cutoff=fit[-1]['label_received_at'] + 1)


@pytest.mark.parametrize('minimums', [
    {'fit': 40}, {'fit': 40, 'development': 20, 'calibration': 20, 'extra': 1},
    {'fit': 0, 'development': 20, 'calibration': 20},
    {'fit': True, 'development': 20, 'calibration': 20},
    {'fit': 40.5, 'development': 20, 'calibration': 20},
])
def test_phase_minimums_reject_incomplete_or_invalid_contracts(minimums):
    from edgeterminal import nfl_spread_model as model
    with pytest.raises(ValueError, match='minimums'):
        model.partition([row(i, w) for w in range(1, 6) for i in range(20)], minimums=minimums)


def test_stage_ledger_is_append_only_and_compare_and_swap():
    from edgeterminal import nfl_spread_store as store
    store.ensure_schema()
    state = dict(stage='fit', input_policy_id='p', formula_version='f', fit_cutoff=NOW)
    a = store.append_stage(None, state, now=NOW)
    assert store.latest_stage()['sha256'] == store.sha(state)
    b = store.append_stage(a['id'], dict(state, stage='development'), now=NOW + 1)
    assert b['id'] > a['id']
    with pytest.raises(ValueError, match='ownership|head'):
        store.append_stage(a['id'], state, now=NOW + 2)
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        db.ex('UPDATE nfl_spread_stages SET created_at=0')
    assert store.latest_stage()['state']['fit_cutoff'] == NOW


def test_code_archive_is_contained_hash_verified_and_portable(isolated, monkeypatch):
    from edgeterminal import nfl_spread_store as store
    root = isolated / 'repo'
    (root / 'edgeterminal').mkdir(parents=True)
    (root / 'edgeterminal/a.py').write_text('same code\n')
    monkeypatch.setattr(store, 'ROOT', root)
    monkeypatch.setattr(store, 'CODE_NAMES', ('edgeterminal/a.py',))
    catalog = store.code_catalog()
    archive = store.archive_code(catalog)
    assert store.read_code(archive['edgeterminal/a.py'], catalog['edgeterminal/a.py']) == b'same code\n'
    with pytest.raises(ValueError):
        store.read_code('../escape.py', 'a' * 64)
    moved = isolated / 'app'
    (moved / 'edgeterminal').mkdir(parents=True)
    (moved / 'edgeterminal/a.py').write_text('same code\n')
    monkeypatch.setattr(store, 'ROOT', moved)
    assert store.code_catalog() == catalog
    (moved / 'edgeterminal/a.py').unlink()
    (moved / 'edgeterminal/a.py').symlink_to(root / 'edgeterminal/a.py')
    with pytest.raises(ValueError, match='symlink|regular'):
        store.code_catalog()


def test_unproved_direct_activation_fails_at_database_boundary():
    from edgeterminal import nfl_spread_store as store
    store.ensure_schema()
    with pytest.raises(sqlite3.DatabaseError, match='proof'):
        db.ex('INSERT INTO nfl_spread_events(version,created_at,kind,detail) VALUES(?,?,?,?)',
              ('forged', NOW, 'activated', '{}'))


@pytest.mark.parametrize('field,value', [('as_of',float('nan')),('baseline_sd',0.),('home_spread',41.),
    ('home_games',-1.),('advanced_available',.5),('season',True),('week',19),
    ('source_sha256','untrusted'),('feature_received_at',NOW+1)])
def test_invalid_inputs_never_reach_a_fitted_family(field,value):
    from edgeterminal import nfl_spread_model as model
    sample=row(0); sample[field]=value
    with pytest.raises(ValueError):model.validate_example(sample)


def test_scaling_and_ridge_coefficients_equal_the_installed_reference():
    import numpy as np
    from sklearn.linear_model import Ridge
    from edgeterminal import nfl_spread_model as model
    rows=[row(i) for i in range(40)]
    bank=model.fit_bank(rows,cutoff=rows[-1]['label_received_at']+1)
    member=bank['members'][2]
    x=np.array([model.feature_values('market_residual',r) for r in rows]);means=x.mean(0);scales=x.std(0);scales[scales==0]=1
    y=np.array([r['actual_margin']+r['home_spread'] for r in rows])
    reference=Ridge(alpha=100.).fit((x-means)/scales,y)
    assert member['coefficients']==pytest.approx(reference.coef_.tolist())
    assert member['intercept']==pytest.approx(reference.intercept_)


def test_calibration_cannot_refit_the_bank_or_change_the_selected_member():
    from edgeterminal import nfl_spread_model as model
    fit=[row(i,1) for i in range(40)]
    bank=model.fit_bank(fit,cutoff=fit[-1]['label_received_at']+1)
    dev=[row(i,2) for i in range(20)]
    chosen=model.select(bank,dev,previous_receipt=bank['fit_cutoff'])
    before=deepcopy((bank,chosen));cal=[row(i,3) for i in range(20)]
    sigma=model.calibrate(bank,chosen,cal,previous_receipt=max(r['label_received_at'] for r in dev))
    assert 1<=sigma<=100
    assert (bank,chosen)==before
    with pytest.raises(ValueError,match='chronology'):
        model.calibrate(bank,chosen,cal,previous_receipt=cal[0]['as_of'])
    with pytest.raises(ValueError,match='coverage'):
        model.calibrate(bank,chosen,cal[:19],previous_receipt=bank['fit_cutoff'])
    with pytest.raises(ValueError,match='coverage'):
        model.fit_bank(fit[:39],cutoff=bank['fit_cutoff'])


def test_duplicate_games_and_future_own_features_cannot_make_phase_coverage():
    from edgeterminal import nfl_spread_model as model
    rows=[row(i,w) for w in range(1,5) for i in range(20)]
    with pytest.raises(ValueError,match='duplicate'):
        model.partition(rows+[rows[0]])
    with pytest.raises(ValueError,match='coverage'):
        model.partition([])
    bad=deepcopy(rows[0]);bad['feature_received_at']=bad['as_of']+1
    with pytest.raises(ValueError,match='receipt'):
        model.validate_example(bad)


def test_declared_member_shape_and_nonfinite_values_fail_closed():
    from edgeterminal import nfl_spread_model as model
    good=dict(method='affine',feature_names=['baseline_margin'],alpha=100.,means=[0.],scales=[1.],coefficients=[1.],intercept=0.)
    for key,value in [('method','unbounded_search'),('alpha',1.),('scales',[0.]),('means',[]),('intercept',float('inf'))]:
        with pytest.raises(ValueError):model.validate_member(dict(good,**{key:value}))
    assert model.probability(0.,13.2,0.)==pytest.approx(.5)
    assert model.probability(3.,13.2,-3.)==pytest.approx(.5)
    assert model.probability(3.,13.2,-3.5)<.5
