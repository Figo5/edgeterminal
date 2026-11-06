"""Actual totals conversion must keep feature and label availability distinct."""
import copy

import pytest

from edgeterminal import nba_totals_learning as learning, nba_totals_model as model
from test_nba_totals_model import DAY, TIP, history


def reconstructed(*, prebuilt=False, feature_receipt=None):
    receipt = TIP + DAY / 2 if feature_receipt is None else feature_receipt
    rows = history(pace=True)
    rows += [dict(team='CHI', opponent='LAL', game_id='opp-' + str(i),
                  played_at=TIP - (375 + i) * DAY, points=110, opponent_points=100,
                  received_at=TIP - (375 + i) * DAY + 4 * 3600,
                  raw_possessions=100., duration_minutes=48.) for i in range(6)]
    rows[0].update(possession_source_artifact_id=1, duration_source_artifact_id=1,
                   possession_received_at=receipt, duration_received_at=receipt)
    row = dict(game_id='target', home='BOS', away='NYK', tipoff=TIP,
               as_of=TIP - 1800, baseline_total=230., actual_total=235.,
               label_received_at=TIP + 4 * 3600, reconstructed=True,
               reconstruction_cutoff=TIP + 2 * DAY, rows=rows)
    if prebuilt:
        row.update(ts=TIP, context=model.features('BOS', 'NYK', rows, TIP,
            as_of=row['as_of'], baseline_total=230., reconstruction_cutoff=TIP + 2 * DAY))
        assert row['context']['coverage']['pace_supported']
        assert row['context']['coverage']['opponent_supported']
    return row, receipt


@pytest.mark.parametrize('prebuilt', [False, True])
def test_conversion_carries_actual_feature_receipt_without_changing_label(prebuilt):
    row, receipt = reconstructed(prebuilt=prebuilt)
    original = copy.deepcopy(row)
    converted, = learning._training_examples([row], TIP + 2 * DAY)
    assert converted['feature_evidence_received_at'] == receipt
    assert converted['label_received_at'] == TIP + 4 * 3600
    assert converted['feature_evidence_received_at'] > converted['label_received_at']
    assert row == original


def test_prebuilt_context_cannot_supply_a_forged_feature_clock():
    row, receipt = reconstructed(prebuilt=True)
    row['feature_evidence_received_at'] = receipt - 1
    with pytest.raises(ValueError, match='feature.*receipt|feature.*evidence'):
        learning._training_examples([row], TIP + 2 * DAY)


def test_prebuilt_context_cannot_bypass_the_current_conversion_cutoff():
    row, receipt = reconstructed(prebuilt=True)
    with pytest.raises(ValueError, match='feature.*receipt|feature.*evidence'):
        learning._training_examples([row], receipt - 1)
