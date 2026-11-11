"""Read-only NBA prop miss decomposition: exact identity, unavailable rows, clusters, determinism."""
import json
import random

import pytest

from edgeterminal import nba_model as baseline
from edgeterminal import nba_prop_audit as audit

TIP = '2026-10-28T23:00:00Z'


def test_identity_reconciles_at_full_precision():
    terms = audit.decompose(0.4123456789, 31.7, 28.3, 11.0, 13.07)
    assert terms['minutes_deviation'] + terms['rate_residual'] + terms['rounding_residual'] == pytest.approx(
        11.0 - 13.07, abs=1e-12)
    assert terms['minutes_deviation'] == 0.4123456789 * (28.3 - 31.7)
    assert terms['rate_residual'] == 11.0 - 0.4123456789 * 28.3
    assert terms['rounding_residual'] == 0.4123456789 * 31.7 - 13.07


def test_pure_minutes_miss_is_labelled_minutes():
    terms = audit.decompose(0.5, 30.0, 20.0, 10.0, 15.0)
    assert (terms['minutes_deviation'], terms['rate_residual'], terms['rounding_residual']) == (-5.0, 0.0, 0.0)
    assert terms['dominant_component'] == 'minutes_deviation'


def test_pure_rate_miss_is_labelled_rate():
    terms = audit.decompose(0.5, 30.0, 30.0, 8.0, 15.0)
    assert (terms['minutes_deviation'], terms['rate_residual'], terms['rounding_residual']) == (0.0, -7.0, 0.0)
    assert terms['dominant_component'] == 'rate_residual'


def test_mixed_miss_reports_all_terms_and_the_largest():
    terms = audit.decompose(0.5, 30.0, 36.0, 10.0, 15.0)
    assert terms['minutes_deviation'] == 3.0
    assert terms['rate_residual'] == -8.0
    assert terms['dominant_component'] == 'rate_residual'


def test_rounding_residual_is_kept_separate():
    terms = audit.decompose(0.5, 30.0, 30.0, 15.0, 14.97)
    assert terms['rounding_residual'] == pytest.approx(0.03)
    assert terms['dominant_component'] == 'rounding_residual'


@pytest.mark.parametrize('minutes', [None, 0, -3.0, float('nan')])
def test_missing_or_nonpositive_minutes_are_unavailable(minutes):
    assert audit.decompose(0.5, 30.0, minutes, 10.0, 15.0) == dict(unavailable='actual_minutes_missing_or_nonpositive')


def test_rate_or_predicted_minutes_unavailable_is_explicit():
    assert audit.decompose(None, 30.0, 30.0, 10.0, 15.0)['unavailable'] == 'rate_or_predicted_minutes_unavailable'
    assert audit.decompose(0.5, 0.0, 30.0, 10.0, 15.0)['unavailable'] == 'rate_or_predicted_minutes_unavailable'


def _history():
    rows = []
    for i in range(15):
        rows.append(dict(player_id='1', game_id=f'g{i}', played_at=f'2026-03-{i + 1:02d}T19:00:00Z',
                         minutes=28. + i % 5, rebounds=float(4 + i % 7), points=10., assists=3., threes=0.))
    return rows


def _saved(history, projection_shift=0.0):
    features, minutes = audit.nba_prop_model.features(
        baseline._player_rows(history, '1', 'rebounds', baseline.parse_time(TIP))[0],
        baseline.parse_time(TIP), baseline.PROP_PARAMETERS['minutes_model'])
    return dict(model_version=baseline.MODEL_VERSION, parameter_fingerprint=baseline.PARAMETER_FINGERPRINT,
                point_method='opportunity', projection=round(features['rate15'] * minutes, 2) + projection_shift,
                projected_minutes=round(minutes, 1))


def test_replay_reproduces_saved_row_and_rejects_tampering():
    history = _history()
    replayed = audit.replay(history, '1', 'rebounds', TIP, _saved(history))
    assert set(replayed) == {'rate', 'predicted_minutes'}
    assert audit.replay(history, '1', 'rebounds', TIP, _saved(history, 0.5)) == dict(unavailable='replay_mismatch')
    assert audit.replay(history, '1', 'rebounds', TIP, dict(_saved(history), model_version='other')) == dict(
        unavailable='unsupported_model_version')


def _row(game, date, player, market, quote, result='loss', dominant='rate_residual', unavailable=None):
    row = dict(game_id=game, game_date_et=date, player_id=player, market=market, quote_id=quote, result=result,
               unavailable=unavailable, dominant_component=None if unavailable else dominant,
               minutes_deviation=None, rate_residual=None, rounding_residual=None)
    if not unavailable:
        row.update(minutes_deviation=1.0, rate_residual=-2.0, rounding_residual=0.0)
    return row


def _collected(rows):
    return dict(rows=rows, push=1, void=2, game_ids=['g1', 'g2'], dates=['2026-11-01', '2026-11-02'])


def test_aggregates_count_distinct_clusters_not_rows():
    rows = [
        _row('g1', '2026-11-01', '7', 'rebounds', 1),
        _row('g1', '2026-11-01', '7', 'points', 2, result='win', dominant='minutes_deviation'),
        _row('g1', '2026-11-01', '8', 'rebounds', 3, unavailable='actual_minutes_missing_or_nonpositive'),
        _row('g2', '2026-11-02', '7', 'rebounds', 4),
    ]
    report = audit.build_report(1.0, 'rebounds', 40, _collected(rows))
    assert report['counts'] == dict(decided=4, wins=1, losses=3, push=1, void=2, unavailable=1, decomposed=3)
    assert report['clusters'] == dict(games=2, dates=2, players=2, player_games=3)
    assert report['by_result']['loss']['dominant_component']['rate_residual'] == 2
    assert report['by_result']['loss']['unavailable'] == 1
    assert report['mean_absolute_terms']['minutes_deviation'] == 1.0


def test_report_is_deterministic_under_input_reordering():
    rows = [_row('g1', '2026-11-01', str(p), m, q, result='win' if q % 2 else 'loss')
            for q, (p, m) in enumerate([('7', 'rebounds'), ('7', 'points'), ('8', 'rebounds'), ('9', 'assists')])]
    shuffled = list(rows)
    random.Random(4).shuffle(shuffled)
    first = json.dumps(audit.build_report(1.0, 'rebounds', 40, _collected(rows)), sort_keys=True)
    second = json.dumps(audit.build_report(1.0, 'rebounds', 40, _collected(shuffled)), sort_keys=True)
    assert first == second


def test_decided_confidence_conditions_on_no_push_and_leaves_half_lines_unchanged():
    assert audit.decided_confidence(.6, 0.) == .6
    assert audit.decided_confidence(.6, None) == .6
    assert audit.decided_confidence(.45, .1) == pytest.approx(.5)
    assert audit.decided_confidence(.6, 1.) is None
    assert audit.decided_confidence(None, 0.) is None
    rows = [dict(_row('g1', '2026-11-01', '7', 'rebounds', 1, result='win'), saved_confidence=.45, decided_confidence=.5),
            dict(_row('g2', '2026-11-02', '8', 'rebounds', 2), saved_confidence=.7, decided_confidence=.7)]
    confidence = audit.build_report(1.0, 'rebounds', 40, _collected(rows))['confidence']
    assert confidence == dict(n=2, win_rate=.5, mean_saved_confidence=pytest.approx(.575),
                              mean_decided_confidence=pytest.approx(.6), brier_decided=pytest.approx((.25 + .49) / 2))
