"""Real official PDF regressions and conservative source/identity boundaries."""
import json
import hashlib
from datetime import date
from pathlib import Path

import httpx
import pytest

from edgeterminal import db, nfl_official_participation as official
from edgeterminal.config import settings, ROOT

BOOKS = ROOT / 'data/research/participation_review'
GAME = 'nfl-2026-reg-06-no-nyg'
KICKOFF = 1792342800.
SCORE = {'home': 27, 'away': 34}
PERSON = {'skattebo': {'short': 'C.Skattebo', 'full': 'Cam Skattebo', 'team': 'nyg', 'jersey': 44}}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None: db._conn.close(); db._conn = None
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{tmp_path / "official.db"}')
    db.init()
    yield
    db._conn.close(); db._conn = None


def test_real_skattebo_gamebook_proves_participation_and_zero_stats():
    result = official.parse_book((BOOKS / 'no-nyg-2026-week06.pdf').read_bytes(), GAME, KICKOFF, SCORE, PERSON)
    p = result['skattebo']
    assert p['active_roster'] and p['snap_played']
    assert p['official_stats']['receptions'] == 0


@pytest.mark.parametrize('change', ['wrong_score', 'wrong_date', 'wrong_game', 'wrong_jersey'])
def test_gamebook_identity_conflicts_never_resolve(change):
    raw = (BOOKS / 'no-nyg-2026-week06.pdf').read_bytes()
    if change == 'wrong_jersey':
        with pytest.raises(ValueError):
            official.parse_book(raw, GAME, KICKOFF, SCORE, {'skattebo': {**PERSON['skattebo'], 'jersey': 12}})
    else:
        with pytest.raises(ValueError):
            official.parse_book(raw, GAME if change != 'wrong_game' else 'nfl-2026-reg-06-nyg-no',
                KICKOFF if change != 'wrong_date' else KICKOFF - 86400,
                SCORE if change != 'wrong_score' else {'home': 28, 'away': 34}, PERSON)


def test_real_hurts_absence_is_not_itself_inactivity():
    raw = (BOOKS / 'car-phi-2026-week06.pdf').read_bytes()
    people = {'hurts': {'short': 'J.Hurts', 'full': 'Jalen Hurts', 'team': 'phi', 'jersey': 1}}
    assert official.parse_book(raw, 'nfl-2026-reg-06-car-phi', KICKOFF, {'home': 14, 'away': 25}, people) == {}


def transaction(rows, year=2026):
    return (f'<option value="/team/transactions/{year}" selected>{year}</option><table>' +
            ''.join(f'<tr><td>{day}</td><td>{text}</td></tr>' for day, text in rows) + '</table>').encode()


@pytest.mark.parametrize('extra,expected', [
    ([], True),
    ([('10/18', 'Activated QB Jalen Hurts.')], False),
    ([('10/17', 'Activated QB Jalen Hurts from Injured Reserve.')], False),
    ([('10/19', 'Activated QB Jalen Hurts from Injured Reserve.')], True),
])
def test_ir_uses_latest_game_date_transaction_without_future_leakage(extra, expected):
    raw = transaction([('10/17', 'Placed QB Jalen Hurts on Injured Reserve.')] + extra)
    assert official.reserved(raw, 2026, date(2026, 10, 18), 'Jalen Hurts') == expected


def test_transaction_for_another_player_or_wrong_season_cannot_void():
    raw = transaction([('10/17', 'Placed QB Other Player on Injured Reserve. Signed QB Jalen Hurts.')])
    assert not official.reserved(raw, 2026, date(2026, 10, 18), 'Jalen Hurts')
    with pytest.raises(ValueError, match='season'):
        official.reserved(transaction([], 2025), 2026, date(2026, 10, 18), 'Jalen Hurts')


@pytest.mark.parametrize('conflict', ['', 'week', 'name', 'jersey', 'abbreviation'])
def test_identity_requires_week_team_full_name_gsis_and_unique_jersey(monkeypatch, conflict):
    monkeypatch.setattr(db, 'q1', lambda *args: {'team': 'New York Giants'})
    stats_raw = b'season,season_type,week,player_id,team,player_name,player_display_name\n2026,REG,5,id,NYG,C.Skattebo,Cam Skattebo\n'
    roster_raw = b'season,week,game_type,gsis_id,team,full_name,jersey_number\n2026,6,REG,id,NYG,Cam Skattebo,44\n'
    if conflict == 'week': roster_raw = roster_raw.replace(b'2026,6', b'2026,5')
    if conflict == 'name': roster_raw = roster_raw.replace(b'Cam Skattebo', b'Other Skattebo')
    if conflict == 'jersey': roster_raw += b'2026,6,REG,other,NYG,Other Player,44\n'
    if conflict == 'abbreviation': stats_raw += b'2026,REG,5,other,NYG,C.Skattebo,Other Skattebo\n'
    people = official.identities(stats_raw, roster_raw, [{'quote_id': 1, 'gsis_player_id': 'id', 'player_name': 'Cam Skattebo'}], GAME)
    assert bool(people) is (not conflict)


@pytest.mark.parametrize('text', ['RUSHING ATT YDS AVG LG TD\nA.Player 1 5 5.0 5 0',
    'RUSHING ATT YDS AVG LG TD\nA.Player 1 5 5.0 5 0\nTotal 2 5 5.0 5 0',
    'RUSHING ATT YDS AVG LG TD\nA.Player 1 5 5.0 5 0\nA.Player 1 5 5.0 5 0\nTotal 2 10 5.0 5 0'])
def test_incomplete_conflicting_or_duplicate_stat_tables_cannot_supply_zero(text):
    with pytest.raises(ValueError): official._table(text, 'RUSHING', 6, (0, 1, 4))


def test_cache_shares_successes_and_failures_and_checks_hash(isolated, monkeypatch):
    calls = []
    class Source:
        def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(200, content=b'source', request=httpx.Request('GET', url))
    monkeypatch.setattr(official, 'client', lambda: Source())
    url = 'https://www.nfl.com/games/fixture'
    raw, ref = official.fetch(url, 100.)
    assert official.fetch(url, 101.)[0] == raw and len(calls) == 1
    key = 'official_participation:' + hashlib.sha256(url.encode()).hexdigest()
    db.kv_set(key, {**db.kv_get(key), 'sha256': '0' * 64})
    with pytest.raises(ValueError, match='hash'):
        official.fetch(url, 102.)
    class Missing:
        def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(404, request=httpx.Request('GET', url))
    monkeypatch.setattr(official, 'client', lambda: Missing())
    with pytest.raises(httpx.HTTPStatusError): official.fetch(url, 30000.)
    with pytest.raises(ValueError, match='scheduled retry'): official.fetch(url, 30001.)
    assert len(calls) == 2


def test_untrusted_redirect_never_fetches_destination(isolated, monkeypatch):
    calls = []
    class Source:
        def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(302, headers={'location': 'https://evil.test/book.pdf'}, request=httpx.Request('GET', url))
    monkeypatch.setattr(official, 'client', lambda: Source())
    with pytest.raises(ValueError, match='untrusted'):
        official.fetch('https://www.nfl.com/games/fixture', 100.)
    assert len(calls) == 1
