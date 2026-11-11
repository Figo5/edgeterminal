"""Opponent-allowed production factor: pure, as-of, and identity when unknown."""
from datetime import datetime, timezone

import pytest

from edgeterminal import nba_prop_opponent as opponent

DAY = 86400.


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _game(index, home, away, home_rebounds, away_rebounds, minutes=240.):
    ts = 1_760_000_000. + index * DAY
    played = _iso(ts)
    row = lambda team, rebounds: dict(player_id=team + '-p', team=team, game_id=f'g{index}', played_at=played,
        minutes=minutes, points=100., rebounds=float(rebounds), assists=20., threes=10.)
    return [row(home, home_rebounds), row(away, away_rebounds)], ts


def _league(games=8):
    """AAA allows 60 rebounds a game to BBB; CCC and DDD allow 40 to each other."""
    history, last = [], 0.
    for i in range(games):
        for rows, ts in (_game(2 * i, 'AAA', 'BBB', 40, 60), _game(2 * i + 1, 'CCC', 'DDD', 40, 40)):
            history += rows; last = ts
    return history, last + DAY


def test_allowed_rate_comes_from_the_other_teams_players():
    history, tip = _league()
    table = opponent.build(history)
    assert opponent.allowed(table, 'AAA', 'rebounds', tip) == pytest.approx(60 / 240)
    assert opponent.allowed(table, 'BBB', 'rebounds', tip) == pytest.approx(40 / 240)
    # League mean of 60, 40, 40, 40 is 45 per 240 minutes.
    assert opponent.ratio(table, 'AAA', 'rebounds', tip) == pytest.approx(60 / 45)
    assert opponent.ratio(table, 'CCC', 'rebounds', tip) == pytest.approx(40 / 45)


def test_pra_sums_points_rebounds_and_assists():
    history, tip = _league()
    assert opponent.allowed(opponent.build(history), 'CCC', 'pra', tip) == pytest.approx(160 / 240)


def test_only_games_finished_before_the_feature_lag_count():
    history, tip = _league()
    before = opponent.ratio(opponent.build(history), 'AAA', 'rebounds', tip)
    late, _ = _game(100, 'AAA', 'BBB', 40, 500)
    for row in late:
        row['played_at'] = _iso(tip - 3600)
    assert opponent.ratio(opponent.build(history + late), 'AAA', 'rebounds', tip) == before


def test_too_few_opponent_games_or_unknown_team_is_unavailable():
    history, tip = _league(games=opponent.MIN_GAMES - 1)
    table = opponent.build(history)
    assert opponent.ratio(table, 'AAA', 'rebounds', tip) is None
    assert opponent.ratio(opponent.build(_league()[0]), 'ZZZ', 'rebounds', tip) is None


def test_duplicate_rows_and_one_sided_games_do_not_count():
    history, tip = _league()
    clean = opponent.ratio(opponent.build(history), 'AAA', 'rebounds', tip)
    lonely, _ = _game(50, 'AAA', 'BBB', 40, 999)
    assert opponent.ratio(opponent.build(history + history + lonely[:1]), 'AAA', 'rebounds', tip) == clean


def test_opposing_team_is_found_from_the_shared_game():
    history, _ = _league()
    table = opponent.build(history)
    assert opponent.opposing(table, 'g0', 'AAA') == 'BBB'
    assert opponent.opposing(table, 'g0', 'CCC') is None


@pytest.mark.parametrize('ratio,strength,expected', [
    (1.2, 0., 10.), (None, 1., 10.), (1.2, 1., 12.), (1.2, .5, 11.), (.8, .5, 9.)])
def test_adjust_scales_toward_the_opponent_and_is_identity_when_unknown(ratio, strength, expected):
    assert opponent.adjust(10., ratio, strength) == pytest.approx(expected)


def test_adjust_rejects_strengths_outside_the_declared_grid():
    with pytest.raises(ValueError):
        opponent.adjust(10., 1.2, .3)
