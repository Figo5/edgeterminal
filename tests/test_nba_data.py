"""NBA identity and ESPN parsers against trimmed real ESPN response shapes.

Fixtures are excerpts of real site.api.espn.com responses captured 2026-10-20
(scoreboard 2026-10-20/2026-10-10/2026-02-15, summary 401810831 OKC-MIN,
OKC roster/statistics, Julius Randle gamelog). Unused fields are dropped.
"""
import copy
import hashlib

import pytest

from edgeterminal import nba_data, nba_identity as ident


def event(eid, date, year, stype, state, home, away, hs='0', as_='0', comp='STD', name=None):
    names = {'pre': 'STATUS_SCHEDULED', 'in': 'STATUS_IN_PROGRESS', 'post': 'STATUS_FINAL'}
    return {'id': eid, 'date': date, 'season': {'year': year, 'type': stype},
            'status': {'type': {'name': name or names[state], 'state': state, 'completed': state == 'post'}},
            'competitions': [{'type': {'abbreviation': comp}, 'competitors': [
                {'homeAway': 'home', 'score': hs, 'team': {'id': home[1], 'abbreviation': home[0]}},
                {'homeAway': 'away', 'score': as_, 'team': {'id': away[1], 'abbreviation': away[0]}}]}]}


SCOREBOARD = {'leagues': [{'season': {'year': 2027, 'displayName': '2026-27'}}], 'events': [
    event('401909088', '2026-10-20T19:00Z', 2027, 2, 'pre', ('DET', '8'), ('BOS', '2')),
    event('401909090', '2026-10-21T01:30Z', 2027, 2, 'pre', ('SA', '24'), ('OKC', '25')),
    event('401902645', '2026-10-10T22:30Z', 2027, 1, 'post', ('TOR', '28'), ('LAC', '12'), '114', '106'),
    event('401838140', '2026-02-15T22:00Z', 2026, 2, 'post', ('STARS', '132374'), ('WORLD', '111386'), '40', '35', 'ALLSTAR'),
]}

KEYS = ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "freeThrowsMade-freeThrowsAttempted",
        "rebounds", "assists", "turnovers", "steals", "blocks", "offensiveRebounds",
        "defensiveRebounds", "fouls", "plusMinus"]
MIN_ROWS = [
    ('3064514', 'Julius Randle', True, False, "COACH'S DECISION", ['35', '32', '11-18', '3-5', '7-8', '7', '6', '1', '0', '1', '2', '5', '4', '-5']),
    ('4431671', 'Jaden McDaniels', True, False, "COACH'S DECISION", ['16', '2', '1-3', '0-2', '0-0', '1', '1', '2', '0', '0', '0', '1', '4', '-17']),
    ('3032976', 'Rudy Gobert', True, False, "COACH'S DECISION", ['29', '2', '1-4', '0-0', '0-0', '7', '2', '4', '1', '0', '1', '6', '1', '-23']),
    ('3934673', 'Donte DiVincenzo', True, False, "COACH'S DECISION", ['30', '16', '6-9', '4-7', '0-0', '9', '1', '1', '0', '0', '0', '9', '1', '-7']),
    ('4594268', 'Anthony Edwards', True, False, "COACH'S DECISION", ['37', '19', '6-17', '2-5', '5-10', '6', '5', '6', '1', '1', '1', '5', '5', '-7']),
    ('2968436', 'Joe Ingles', False, False, "COACH'S DECISION", ['2', '0', '0-0', '0-0', '0-0', '1', '0', '0', '0', '0', '0', '1', '0', '+3']),
    ('2993874', 'Kyle Anderson', False, False, "COACH'S DECISION", ['12', '0', '0-1', '0-0', '0-0', '5', '0', '2', '1', '0', '2', '3', '0', '-2']),
    ('4396971', 'Naz Reid', False, False, "COACH'S DECISION", ['28', '6', '1-5', '0-3', '4-4', '4', '0', '4', '2', '0', '1', '3', '3', '-4']),
    ('5157587', 'Rocco Zikarsky', False, False, "COACH'S DECISION", ['2', '2', '1-3', '0-0', '0-0', '1', '0', '0', '0', '1', '1', '0', '0', '+3']),
    ('4397002', 'Ayo Dosunmu', False, False, "COACH'S DECISION", ['33', '18', '7-9', '4-5', '0-0', '4', '0', '2', '0', '0', '0', '4', '1', '-3']),
    ('4432247', 'Jaylen Clark', False, False, "COACH'S DECISION", ['2', '0', '0-1', '0-1', '0-0', '1', '0', '0', '0', '0', '0', '1', '0', '+3']),
    ('4432847', 'Terrence Shannon Jr.', False, False, "COACH'S DECISION", ['2', '3', '1-2', '1-1', '0-0', '0', '1', '0', '0', '0', '0', '0', '0', '+3']),
    ('4592492', 'Bones Hyland', False, False, "COACH'S DECISION", ['13', '3', '1-5', '1-4', '0-0', '0', '2', '0', '0', '2', '0', '0', '0', '-9']),
    ('3195', 'Mike Conley', False, True, "COACH'S DECISION", []),
]
OKC_ROWS = [
    ('4222252', 'Isaiah Hartenstein', True, False, "COACH'S DECISION", ['21', '0', '0-3', '0-0', '0-0', '12', '3', '1', '0', '2', '4', '8', '3', '+13']),
    ('4433255', 'Chet Holmgren', True, False, "COACH'S DECISION", ['32', '21', '9-13', '1-4', '2-2', '9', '2', '0', '3', '1', '3', '6', '2', '+23']),
    ('4278073', 'Shai Gilgeous-Alexander', True, False, "COACH'S DECISION", ['33', '20', '7-22', '2-4', '4-5', '3', '10', '1', '2', '1', '1', '2', '3', '+3']),
    ('4397020', 'Luguentz Dort', True, False, "COACH'S DECISION", ['18', '3', '0-4', '0-2', '3-4', '0', '0', '0', '1', '0', '0', '0', '0', '-6']),
    ('4900671', 'Ajay Mitchell', True, False, "COACH'S DECISION", ['27', '11', '4-13', '1-4', '2-2', '5', '3', '2', '2', '0', '3', '2', '1', '+1']),
    ('4432823', 'Jaylin Williams', False, False, "COACH'S DECISION", ['19', '5', '2-7', '1-5', '0-0', '3', '2', '2', '1', '0', '1', '2', '2', '-2']),
    ('2991350', 'Alex Caruso', False, False, "COACH'S DECISION", ['22', '17', '6-10', '1-3', '4-4', '2', '1', '0', '3', '0', '1', '1', '0', '-3']),
    ('4395702', 'Isaiah Joe', False, False, "COACH'S DECISION", ['21', '20', '8-14', '4-8', '0-0', '4', '0', '1', '0', '0', '0', '4', '2', '+23']),
    ('4397183', 'Aaron Wiggins', False, False, "COACH'S DECISION", ['6', '0', '0-1', '0-1', '0-0', '0', '0', '0', '0', '0', '0', '0', '2', '-11']),
    ('4683692', 'Cason Wallace', False, False, "COACH'S DECISION", ['25', '4', '1-4', '0-0', '2-2', '6', '7', '0', '3', '2', '2', '4', '1', '+11']),
    ('4683778', 'Jared McCain', False, False, "COACH'S DECISION", ['17', '15', '5-10', '5-9', '0-0', '0', '0', '0', '1', '0', '0', '0', '0', '+13']),
    ('4697270', 'Payton Sandfort', False, True, "COACH'S DECISION", []),
    ('3133626', 'Kenrich Williams', False, True, "COACH'S DECISION", []),
]


def box_team(abbr, tid, rows, total_points):
    return {'team': {'id': tid, 'abbreviation': abbr}, 'statistics': [{
        'keys': KEYS, 'totals': ['', total_points] + [''] * 12,
        'athletes': [{'athlete': {'id': i, 'displayName': n}, 'starter': s, 'didNotPlay': d,
                      'reason': r, 'stats': st} for i, n, s, d, r, st in rows]}]}


SUMMARY = {
    'header': {'id': '401810831', 'season': {'year': 2026, 'type': 2}, 'competitions': [
        {'date': '2026-03-15T17:00Z', 'type': {'abbreviation': 'STD'},
         'status': {'type': {'name': 'STATUS_FINAL', 'state': 'post', 'completed': True}},
         'competitors': [{'homeAway': 'home', 'score': '116', 'team': {'id': '25', 'abbreviation': 'OKC'}},
                         {'homeAway': 'away', 'score': '103', 'team': {'id': '16', 'abbreviation': 'MIN'}}]}]},
    'boxscore': {'players': [box_team('MIN', '16', MIN_ROWS, '103'), box_team('OKC', '25', OKC_ROWS, '116')]},
}

ROSTER = {'timestamp': '2026-10-20T12:58:40Z', 'status': 'success',
          'season': {'year': 2027, 'displayName': '2026-27', 'type': 2},
          'team': {'id': '25', 'abbreviation': 'OKC'},
          'athletes': [
              {'id': '2991350', 'displayName': 'Alex Caruso', 'jersey': '9', 'position': {'abbreviation': 'G'},
               'injuries': [], 'status': {'type': 'active', 'name': 'Active'}},
              {'id': '4684208', 'displayName': 'Brooks Barnhizer', 'jersey': '23', 'position': {'abbreviation': 'F'},
               'injuries': [{'status': 'Out', 'date': '2026-10-19T22:11Z'}],
               'status': {'type': 'active', 'name': 'Active'}}]}

TEAM_STATS = {'status': 'success', 'season': {'year': 2027, 'displayName': '2026-27'},
              'requestedSeason': {'year': 2026, 'displayName': '2025-26'},
              'team': {'id': '25', 'abbreviation': 'OKC'},
              'results': {'stats': {'categories': [
                  {'name': 'general', 'stats': [{'name': 'avgRebounds', 'value': 44.121952},
                                                {'name': 'gamesPlayed', 'value': 82.0}]},
                  {'name': 'offensive', 'stats': [{'name': 'avgPoints', 'value': 119.8}]}]}}}

GL_NAMES = ["minutes", "fieldGoalsMade-fieldGoalsAttempted", "fieldGoalPct",
            "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "threePointPct",
            "freeThrowsMade-freeThrowsAttempted", "freeThrowPct", "totalRebounds", "assists",
            "blocks", "steals", "fouls", "turnovers", "points"]
GAMELOG = {
    'names': GL_NAMES,
    'events': {
        '401871157': {'id': '401871157', 'gameDate': '2026-05-16T01:30:00.000+00:00', 'homeTeamId': '16',
                      'awayTeamId': '24', 'leagueAbbreviation': 'NBA',
                      'team': {'id': '16', 'abbreviation': 'MIN'}, 'opponent': {'id': '24', 'abbreviation': 'SA'}},
        '401811007': {'id': '401811007', 'gameDate': '2026-04-12T23:30:00.000+00:00', 'homeTeamId': '16',
                      'awayTeamId': '7', 'leagueAbbreviation': 'NBA',
                      'team': {'id': '16', 'abbreviation': 'MIN'}, 'opponent': {'id': '7', 'abbreviation': 'DEN'}},
        '401812729': {'id': '401812729', 'gameDate': '2025-10-17T00:00:00.000+00:00', 'homeTeamId': '4',
                      'awayTeamId': '16', 'leagueAbbreviation': 'NBA',
                      'team': {'id': '16', 'abbreviation': 'MIN'}, 'opponent': {'id': '4', 'abbreviation': 'CHI'}}},
    'seasonTypes': [
        {'displayName': '2025-26 Postseason', 'categories': [
            {'type': 'event', 'events': [{'eventId': '401871157', 'stats': ['24', '1-8', '12.5', '0-2', '0.0', '1-2', '50.0', '7', '1', '0', '0', '3', '0', '3']}]},
            {'type': 'total', 'stats': []}]},
        {'displayName': '2025-26 Regular Season', 'categories': [
            {'type': 'event', 'events': [{'eventId': '401811007', 'stats': ['28', '5-13', '38.5', '3-7', '42.9', '6-6', '100.0', '2', '4', '0', '0', '0', '1', '19']}]}]},
        {'displayName': '2025-26 Preseason', 'categories': [
            {'type': 'event', 'events': [{'eventId': '401812729', 'stats': ['27', '10-18', '55.6', '2-5', '40.0', '1-2', '50.0', '8', '4', '0', '2', '4', '3', '23']}]}]},
    ]}


# ---------------------------------------------------------------- identity
def test_team_identity_requires_consistent_espn_id_and_abbreviation():
    assert ident.team_code({'id': '9', 'abbreviation': 'GS'}) == 'GSW'
    assert ident.team_code({'id': '26', 'abbreviation': 'UTAH'}) == 'UTA'
    assert len(ident.TEAMS) == 30
    for bad in ({'id': '9', 'abbreviation': 'LAL'}, {'id': '132374', 'abbreviation': 'STARS'},
                {'abbreviation': 'GS'}, {'id': '9'}):
        with pytest.raises(ValueError):
            ident.team_code(bad)


def test_season_label_game_id_and_eastern_date():
    assert ident.season_label(2027) == '2026-27'
    assert ident.game_id(2027, '401909090') == 'nba-2026-401909090'
    tip = ident.parse_tipoff('2026-10-21T01:30Z')
    assert tip.isoformat() == '2026-10-21T01:30:00+00:00'
    assert ident.eastern_date(tip) == '2026-10-20'
    for bad in ('2026-10-21T01:30', 'tomorrow', None):
        with pytest.raises(ValueError):
            ident.parse_tipoff(bad)
    with pytest.raises(ValueError):
        ident.game_id(2027, '40190A')


# ---------------------------------------------------------------- scoreboard
def test_scoreboard_keeps_only_nba_games_on_requested_eastern_date():
    out = nba_data.parse_scoreboard(SCOREBOARD, '2026-10-20')
    ids = [g['game_id'] for g in out['games']]
    assert ids == ['nba-2026-401909088', 'nba-2026-401909090']
    late = out['games'][1]
    assert late == {
        'game_id': 'nba-2026-401909090', 'espn_event_id': '401909090', 'season_start_year': 2026,
        'season_label': '2026-27', 'season_type': 'regular', 'competition': 'STD',
        'home_team': 'SAS', 'away_team': 'OKC', 'tipoff_utc': '2026-10-21T01:30:00Z',
        'game_date_et': '2026-10-20', 'status': 'scheduled', 'home_score': None, 'away_score': None}
    reasons = {r['espn_event_id']: r['reason'] for r in out['rejected']}
    assert reasons['401902645'].startswith('season type')
    assert '401838140' in reasons


def test_scoreboard_rejects_invalid_final_scores_and_date_mismatch():
    bad = copy.deepcopy(SCOREBOARD)
    bad['events'] = [event('401810001', '2026-03-15T17:00Z', 2026, 2, 'post', ('OKC', '25'), ('MIN', '16'), '116', '116'),
                     event('401810002', '2026-03-15T17:00Z', 2026, 2, 'post', ('OKC', '25'), ('MIN', '16'), '1x6', '103'),
                     event('401810003', '2026-03-15T17:00Z', 2026, 2, 'post', ('OKC', '25'), ('MIN', '16'), '-1', '103'),
                     event('401810004', '2026-03-15T17:00Z', 2026, 2, 'post', ('OKC', '25'), ('OKC', '25'), '110', '103'),
                     event('401810005', '2026-03-16T17:00Z', 2026, 2, 'pre', ('OKC', '25'), ('MIN', '16'))]
    out = nba_data.parse_scoreboard(bad, '2026-03-15')
    assert out['games'] == []
    assert [r['espn_event_id'] for r in out['rejected']] == ['401810001', '401810002', '401810003', '401810004', '401810005']


def test_live_scores_are_not_reported_as_results():
    sb = {'events': [event('401810999', '2026-03-15T17:00Z', 2026, 2, 'in', ('OKC', '25'), ('MIN', '16'), '50', '44')]}
    g = nba_data.parse_scoreboard(sb, '2026-03-15')['games'][0]
    assert g['status'] == 'in_progress' and g['home_score'] is None


# ---------------------------------------------------------------- summary
def test_summary_normalizes_played_dnp_and_checks_box_totals():
    out = nba_data.parse_summary(SUMMARY)
    assert out['game']['game_id'] == 'nba-2025-401810831'
    assert out['game']['status'] == 'final' and out['game']['home_score'] == 116
    assert out['box_complete'] is True
    randle = nba_data.player_line(out, '3064514')
    assert randle == {'espn_player_id': '3064514', 'name': 'Julius Randle', 'team': 'MIN',
                      'starter': True, 'status': 'played', 'reason': None,
                      'stats': {'minutes': 35, 'points': 32, 'rebounds': 7, 'assists': 6,
                                'threes': 3, 'pra': 45}}
    conley = nba_data.player_line(out, '3195')
    assert conley['status'] == 'dnp' and conley['stats'] is None
    assert conley['reason'] == "COACH'S DECISION"


def test_absent_player_is_not_listed_never_zero_or_inactive():
    out = nba_data.parse_summary(SUMMARY)
    line = nba_data.player_line(out, '9999999')
    assert line == {'espn_player_id': '9999999', 'status': 'not_listed', 'stats': None}


def test_injury_reason_dnp_is_inactive_and_missing_minutes_is_unverified():
    s = copy.deepcopy(SUMMARY)
    rows = s['boxscore']['players'][0]['statistics'][0]['athletes']
    rows[-1]['reason'] = 'LEFT ACHILLES TENDON'
    rows[5]['stats'] = ['--'] + rows[5]['stats'][1:]   # real ESPN shape seen for Mo Bamba
    out = nba_data.parse_summary(s)
    assert nba_data.player_line(out, '3195')['status'] == 'inactive'
    ingles = nba_data.player_line(out, '2968436')
    assert ingles['status'] == 'unverified' and ingles['stats'] is None


@pytest.mark.parametrize('reason', [None, '', '  '])
def test_dnp_without_reason_is_never_inferred_inactive(reason):
    s = copy.deepcopy(SUMMARY)
    s['boxscore']['players'][0]['statistics'][0]['athletes'][-1]['reason'] = reason
    conley = nba_data.player_line(nba_data.parse_summary(s), '3195')
    assert conley['status'] == 'dnp' and conley['reason'] is None and conley['stats'] is None


def test_box_totals_disagreeing_with_score_or_players_mark_incomplete():
    s = copy.deepcopy(SUMMARY)
    s['boxscore']['players'][0]['statistics'][0]['athletes'].pop(0)   # drop Randle's 32 points
    assert nba_data.parse_summary(s)['box_complete'] is False
    s = copy.deepcopy(SUMMARY)
    s['boxscore']['players'][1]['statistics'][0]['totals'][1] = '117'
    assert nba_data.parse_summary(s)['box_complete'] is False


def test_summary_identity_conflicts_raise():
    s = copy.deepcopy(SUMMARY)
    s['boxscore']['players'][0]['team'] = {'id': '7', 'abbreviation': 'DEN'}
    with pytest.raises(ValueError):
        nba_data.parse_summary(s)
    s = copy.deepcopy(SUMMARY)
    s['boxscore']['players'][1]['statistics'][0]['athletes'].append(
        s['boxscore']['players'][0]['statistics'][0]['athletes'][0])
    with pytest.raises(ValueError):
        nba_data.parse_summary(s)
    s = copy.deepcopy(SUMMARY)
    s['boxscore']['players'][0]['statistics'][0]['athletes'][0]['stats'][1] = '3.5'
    with pytest.raises(ValueError):
        nba_data.parse_summary(s)


# ---------------------------------------------------------------- roster / stats / gamelog
def test_roster_keeps_espn_ids_and_reported_injuries():
    out = nba_data.parse_roster(ROSTER)
    assert out['team'] == 'OKC' and out['season_label'] == '2026-27'
    assert out['players'][1] == {'espn_player_id': '4684208', 'name': 'Brooks Barnhizer', 'jersey': '23',
                                 'position': 'F', 'roster_status': 'active',
                                 'injuries': [{'status': 'Out', 'date': '2026-10-19T22:11Z'}]}


def test_team_stats_reports_data_season_not_current_label():
    out = nba_data.parse_team_stats(TEAM_STATS)
    assert out['team'] == 'OKC'
    assert out['data_season_label'] == '2025-26' and out['current_season_label'] == '2026-27'
    assert out['games_played'] == 82
    assert out['stats']['offensive.avgPoints'] == 119.8
    bad = copy.deepcopy(TEAM_STATS)
    bad['results']['stats']['categories'][0]['stats'][0]['value'] = 'NaN'
    with pytest.raises(ValueError):
        nba_data.parse_team_stats(bad)


def test_gamelog_excludes_preseason_and_normalizes_props():
    out = nba_data.parse_gamelog(GAMELOG, '3064514')
    assert [g['espn_event_id'] for g in out['games']] == ['401871157', '401811007']
    playoff = out['games'][0]
    assert playoff == {'espn_player_id': '3064514', 'espn_event_id': '401871157',
                       'game_id': 'nba-2025-401871157', 'season_label': '2025-26',
                       'season_type': 'postseason', 'tipoff_utc': '2026-05-16T01:30:00Z',
                       'team': 'MIN', 'opponent': 'SAS', 'home': True,
                       'stats': {'minutes': 24, 'points': 3, 'rebounds': 7, 'assists': 1,
                                 'threes': 0, 'pra': 11}}
    assert out['rejected'] == [{'espn_event_id': '401812729', 'reason': 'season type Preseason'}]


# ---------------------------------------------------------------- fetch / archive
def test_fetch_archives_exact_body_url_receipt_time_and_hash():
    body = b'{"events": []}'
    seen = []

    def getter(url):
        seen.append(url)
        return 200, body

    art = nba_data.fetch(nba_data.scoreboard_url('2026-10-20'), 'scoreboard', getter=getter, now=1792500000.0)
    assert seen == ['https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261020&limit=200']
    assert art == {'kind': 'scoreboard', 'url': seen[0], 'received_at': 1792500000.0, 'http_status': 200,
                   'sha256': hashlib.sha256(body).hexdigest(), 'body': body}
    assert nba_data.payload(art) == {'events': []}
    with pytest.raises(ValueError):
        nba_data.payload(dict(art, http_status=403))
    with pytest.raises(ValueError):
        nba_data.payload(dict(art, body=b'{"tampered": 1}'))


def test_urls_are_espn_public_endpoints_only():
    assert nba_data.summary_url('401810831').endswith('/basketball/nba/summary?event=401810831')
    assert nba_data.roster_url('OKC').endswith('/basketball/nba/teams/25/roster')
    assert nba_data.team_stats_url('OKC', 2026).endswith('/teams/25/statistics?season=2026')
    assert nba_data.gamelog_url('3064514').startswith('https://site.web.api.espn.com/apis/common/v3/')
    with pytest.raises(ValueError):
        nba_data.scoreboard_url('2026-13-01')
    with pytest.raises(ValueError):
        nba_data.gamelog_url('abc')
