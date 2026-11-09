"""Fixed totals coefficients precede method choice and independent calibration."""
import copy
from datetime import datetime, timezone

import pytest

from edgeterminal import nba_totals_fit_bank as bank
from test_nba_totals_model import examples, context, DAY


def rows():
    return examples()


def test_frozen_family_has_twenty_members_and_fit_only_intercepts():
    source = rows()[:6]
    fitted = bank.fit_bank(source, source[-1]['label_received_at'] + 1, min_fit=6)
    assert len(fitted['members']) == 20
    assert fitted['members'][0]['intercept'] == 0
    assert fitted['members'][1]['intercept'] == pytest.approx(6)
    assert fitted['fit_rows'] == 6
    changed = copy.deepcopy(fitted)
    changed['members'][1]['intercept'] += 1
    with pytest.raises(ValueError, match='seal'):
        bank.verify_bank(changed)


def test_earliest_prefix_is_metadata_driven_and_keeps_whole_date():
    source = rows()
    source[6]['ts'] = source[5]['ts'] + 60
    source[6]['as_of'] = source[6]['ts'] - 1800
    source[6]['context'] = context(tip=source[6]['ts'], baseline_total=source[6]['baseline_total'])
    source[6]['label_received_at'] = source[6]['ts'] + 4*3600
    selected = bank.fit_prefix(source, min_fit=6)
    assert len(selected) == 7
    poison = copy.deepcopy(source)
    for item in poison:
        item['actual_total'] += 100
    assert [r['game_id'] for r in bank.fit_prefix(poison, min_fit=6)] == [r['game_id'] for r in selected]


def test_inner_requires_own_asof_and_original_saved_baseline():
    source = rows()[6]
    source['forecast_sha256'] = 'a'*64
    source['availability'] = 'received_by_asof'
    assert bank.inner_eligible(source)
    for key, value in [('forecast_sha256', None), ('availability', 'reconstructed_at_cutoff')]:
        altered = dict(source, **{key: value})
        assert not bank.inner_eligible(altered)
    altered = copy.deepcopy(source)
    altered['context']['selected_rows'][0]['received_at'] = source['as_of'] + 1
    assert not bank.inner_eligible(altered)


def test_development_and_calibration_do_not_refit_bank():
    source = rows()
    fitted = bank.fit_bank(source[:6], source[5]['label_received_at']+1, min_fit=6)
    untouched = copy.deepcopy(fitted)
    selected = bank.select_member(fitted, source[6:9], min_games=3)
    assert selected['member']['params']['method'] == 'intercept'
    artifact = bank.calibrate(fitted, selected, source[9:], source[-1]['label_received_at']+1,
                              min_games=3)
    assert artifact['intercept'] == pytest.approx(6)
    assert artifact['sd_total'] == 1
    assert fitted == untouched


def test_phase_receipt_boundaries_and_duplicate_games_reject():
    source=rows()
    fitted=bank.fit_bank(source[:6], source[5]['label_received_at']+1, min_fit=6)
    bad=copy.deepcopy(source[6:9]); bad[0]['as_of']=fitted['receipt_max']
    bad[0]['context']['as_of']=bad[0]['as_of']
    with pytest.raises(ValueError, match='chronology'):
        bank.select_member(fitted,bad,min_games=3)
    with pytest.raises(ValueError, match='duplicate'):
        bank.fit_bank(source[:6]+[source[0]],source[5]['label_received_at']+1,min_fit=6)


def test_minimums_and_multiseason_requirement_are_not_lowered():
    source=rows()
    with pytest.raises(ValueError, match='Insufficient'):
        bank.fit_bank(source,source[-1]['label_received_at']+1)
    with pytest.raises(ValueError, match='seasons'):
        bank.fit_bank(source[:2],source[1]['label_received_at']+1,min_fit=2)


def test_staged_numerics_match_existing_fitter_on_valid_chronological_rows():
    from edgeterminal import nba_totals_model as model
    source=rows(); cutoff=source[-1]['label_received_at']+1
    expected=model.fit_candidate(source,cutoff,min_fit=6,min_development=3,min_calibration=3)
    fitted=bank.fit_bank(source[:6],source[5]['label_received_at']+1,min_fit=6)
    selected=bank.select_member(fitted,source[6:9],min_games=3)
    actual=bank.calibrate(fitted,selected,source[9:],cutoff,min_games=3)
    assert actual==expected


def test_later_generation_bank_selection_and_calibration_use_raw_candidate_basis():
    source=rows()
    fitted=bank.fit_bank(source[:6],source[5]['label_received_at']+1,min_fit=6)
    inner=[]
    for row in source[6:]:
        row=copy.deepcopy(row)
        row.update(model_base_total=230.,baseline_total=236.,actual_total=236.,
                   forecast_sha256='a'*64,availability='received_by_asof',
                   context=context(tip=row['ts'],baseline_total=230.))
        assert bank.inner_eligible(row)
        inner.append(row)
    selected=bank.select_member(fitted,inner[:3],min_games=3)
    assert selected['member']['params']['method']=='intercept'
    assert selected['mae']==0
    artifact=bank.calibrate(fitted,selected,inner[3:],inner[-1]['label_received_at']+1,min_games=3)
    assert artifact['intercept']==6 and artifact['sd_total']==1
