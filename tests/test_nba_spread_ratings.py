"""Pure opponent-adjusted ratings: schedule strength, chronology and fail-closed inputs."""
from datetime import datetime, timezone

import pytest

from edgeterminal import nba_spread_ratings as ratings

TEAMS = ['ATL', 'BOS', 'BKN', 'CHA', 'CHI', 'CLE', 'DAL', 'DEN']
STRENGTH = {t: 7. - 2 * i for i, t in enumerate(TEAMS)}  # ATL best, DEN worst
START = datetime(2026, 10, 20, 23, tzinfo=timezone.utc).timestamp()
DAY = 86400


def rows(home, away, home_points, away_points, ts, gid):
    stamp = datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    return [dict(team=home, opponent=away, points=home_points, opponent_points=away_points, home=True, played_at=stamp, game_id=gid),
            dict(team=away, opponent=home, points=away_points, opponent_points=home_points, home=False, played_at=stamp, game_id=gid)]


def league(days=60, start=START):
    """Round robin where margin is exactly strength difference plus three home points."""
    history, gid = [], 0
    for day in range(days):
        for k in range(4):
            home, away = TEAMS[(day + k) % 8], TEAMS[(day + k + 1 + day % 3 * 2) % 8]
            if home == away:
                continue
            margin = STRENGTH[home] - STRENGTH[away] + 3.
            history += rows(home, away, 110. + margin / 2, 110. - margin / 2, start + day * DAY, f'g{start}-{gid}')
            gid += 1
    return history


TIP = START + 61 * DAY


def test_ratings_recover_strength_order_home_edge_and_margin():
    fitted = ratings.fit(league(), TIP)
    assert fitted['status'] == 'ok' and fitted['games'] >= ratings.MIN_LEAGUE_GAMES
    net = {t: fitted['offense'][t] + fitted['defense'][t] for t in TEAMS}
    assert sorted(TEAMS, key=net.get, reverse=True) == TEAMS
    assert fitted['home_edge'] == pytest.approx(3., abs=.2) and fitted['mean'] == pytest.approx(110., abs=.2)
    out = ratings.project(fitted, 'ATL', 'DEN')
    assert out['margin'] == pytest.approx(STRENGTH['ATL'] - STRENGTH['DEN'] + 3., abs=1.) and out['total'] == pytest.approx(220., abs=1.)
    assert ratings.project(fitted, 'DEN', 'ATL')['margin'] < 0


def test_a_win_over_a_strong_team_counts_for_more_than_the_same_score_over_a_weak_one():
    base = league()
    extra = lambda other: [r for d in range(5) for r in rows('CHI', other, 120., 100., START + (60 + d / 10) * DAY - 5 * 3600, f'x{d}')]
    net = lambda history: (lambda f: f['offense']['CHI'] + f['defense']['CHI'])(ratings.fit(history, TIP))
    assert net(base + extra('ATL')) > net(base + extra('DEN')) + .5


def test_only_games_finished_before_tipoff_and_as_of_can_enter():
    history = league()
    fitted = ratings.fit(history, TIP)
    late = rows('ATL', 'DEN', 60., 160., TIP - 3600, 'late') + rows('ATL', 'DEN', 60., 160., TIP + DAY, 'future')
    assert ratings.fit(history + late, TIP) == fitted
    earlier = ratings.fit(history, TIP, as_of=START + 50 * DAY)
    assert earlier['games'] < fitted['games'] and earlier['cutoff'] == START + 50 * DAY
    for bad in (TIP, float('nan'), True):
        with pytest.raises(ValueError):
            ratings.fit(history, TIP, as_of=bad)


def test_contradictory_rows_fail_closed_and_thin_inputs_are_unavailable():
    history = league()
    with pytest.raises(ValueError, match='contradictory'):
        ratings.fit(history + [dict(history[0], points=history[0]['points'] + 1)], TIP)
    assert ratings.fit(history + [dict(history[0])], TIP) == ratings.fit(history, TIP)  # identical duplicate collapses
    thin = ratings.fit(league(days=20), START + 21 * DAY)
    assert thin == dict(status='unavailable', reason='insufficient_league_games', games=thin['games'])
    assert ratings.project(thin, 'ATL', 'DEN') is None
    fitted = ratings.fit(history, TIP)
    assert ratings.project(dict(fitted, team_games=dict(fitted['team_games'], DEN=3)), 'ATL', 'DEN') is None
    junk = [dict(team='ATL', opponent='ATL', points=1, opponent_points=1, home=True, played_at='2026-10-21T00:00Z', game_id='j'),
            dict(team='ATL', opponent='XXX', points=1, opponent_points=1, home=True, played_at='2026-10-21T00:00Z', game_id='k'),
            dict(team='ATL', opponent='BOS', points=float('nan'), opponent_points=1, home=True, played_at='2026-10-21T00:00Z', game_id='l')]
    assert ratings.fit(history + junk, TIP) == fitted


def test_an_earlier_season_is_discounted_against_the_current_one():
    previous = league(start=START - 300 * DAY)  # same teams, reversed strengths last season
    flipped = [dict(r, points=r['opponent_points'], opponent_points=r['points']) for r in previous]
    fitted = ratings.fit(flipped + league(), TIP)
    assert fitted['offense']['ATL'] + fitted['defense']['ATL'] > fitted['offense']['DEN'] + fitted['defense']['DEN']
