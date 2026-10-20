"""NBA team and game identity from ESPN, independent of the NFL modules.

Game ID is `nba-{season_start_year}-{espn_event_id}`. ESPN labels a season by
its ending year (season.year=2027 is 2026-27), so the start year is year-1.
Teams resolve only when ESPN's numeric team ID and abbreviation agree; there
is no fuzzy matching, and All-Star / exhibition opponents are refusals.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')

# canonical code -> (ESPN team id, ESPN abbreviation, display name)
TEAMS: Dict[str, tuple] = {
    'ATL': ('1', 'ATL', 'Atlanta Hawks'), 'BOS': ('2', 'BOS', 'Boston Celtics'),
    'BKN': ('17', 'BKN', 'Brooklyn Nets'), 'CHA': ('30', 'CHA', 'Charlotte Hornets'),
    'CHI': ('4', 'CHI', 'Chicago Bulls'), 'CLE': ('5', 'CLE', 'Cleveland Cavaliers'),
    'DAL': ('6', 'DAL', 'Dallas Mavericks'), 'DEN': ('7', 'DEN', 'Denver Nuggets'),
    'DET': ('8', 'DET', 'Detroit Pistons'), 'GSW': ('9', 'GS', 'Golden State Warriors'),
    'HOU': ('10', 'HOU', 'Houston Rockets'), 'IND': ('11', 'IND', 'Indiana Pacers'),
    'LAC': ('12', 'LAC', 'LA Clippers'), 'LAL': ('13', 'LAL', 'Los Angeles Lakers'),
    'MEM': ('29', 'MEM', 'Memphis Grizzlies'), 'MIA': ('14', 'MIA', 'Miami Heat'),
    'MIL': ('15', 'MIL', 'Milwaukee Bucks'), 'MIN': ('16', 'MIN', 'Minnesota Timberwolves'),
    'NOP': ('3', 'NO', 'New Orleans Pelicans'), 'NYK': ('18', 'NY', 'New York Knicks'),
    'OKC': ('25', 'OKC', 'Oklahoma City Thunder'), 'ORL': ('19', 'ORL', 'Orlando Magic'),
    'PHI': ('20', 'PHI', 'Philadelphia 76ers'), 'PHX': ('21', 'PHX', 'Phoenix Suns'),
    'POR': ('22', 'POR', 'Portland Trail Blazers'), 'SAC': ('23', 'SAC', 'Sacramento Kings'),
    'SAS': ('24', 'SA', 'San Antonio Spurs'), 'TOR': ('28', 'TOR', 'Toronto Raptors'),
    'UTA': ('26', 'UTAH', 'Utah Jazz'), 'WAS': ('27', 'WSH', 'Washington Wizards'),
}
_BY_ESPN_ID = {espn_id: (code, abbr) for code, (espn_id, abbr, _) in TEAMS.items()}

# ESPN season.type -> our label. 1 preseason and 4 off-season are refused.
SEASON_TYPES = {2: 'regular', 3: 'postseason', 5: 'play-in'}


def team_code(team: Dict) -> str:
    espn_id, abbr = str(team.get('id') or ''), str(team.get('abbreviation') or '')
    hit = _BY_ESPN_ID.get(espn_id)
    if not hit or hit[1] != abbr:
        raise ValueError(f'not an NBA team or ESPN id/abbreviation conflict: {espn_id}/{abbr}')
    return hit[0]


def espn_team_id(code: str) -> str:
    if code not in TEAMS:
        raise ValueError(f'unknown NBA team code: {code}')
    return TEAMS[code][0]


def season_label(espn_year: int) -> str:
    year = int(espn_year)
    if not 2000 < year < 2100:
        raise ValueError(f'implausible ESPN season year: {espn_year}')
    return f'{year - 1}-{year % 100:02d}'


def game_id(espn_year: int, espn_event_id: str) -> str:
    season_label(espn_year)
    if not re.fullmatch(r'\d{6,12}', str(espn_event_id)):
        raise ValueError(f'invalid ESPN event id: {espn_event_id!r}')
    return f'nba-{int(espn_year) - 1}-{espn_event_id}'


def parse_tipoff(value: Optional[str]) -> datetime:
    """ESPN timestamps: '2026-10-21T01:30Z' or '2026-05-16T01:30:00.000+00:00'. Naive is refused."""
    if not isinstance(value, str):
        raise ValueError('missing tipoff time')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f'invalid tipoff time: {value!r}')
    if parsed.tzinfo is None:
        raise ValueError(f'tipoff time has no timezone: {value!r}')
    return parsed.astimezone(timezone.utc)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def eastern_date(moment: datetime) -> str:
    return moment.astimezone(EASTERN).date().isoformat()
