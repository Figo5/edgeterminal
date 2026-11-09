"""A sealed source's full week, rather than locally present games, gates learning."""
import csv
from copy import deepcopy

import pytest

from edgeterminal import db, nfl_game_sources as sources
from edgeterminal import nfl_spread_data as data, nfl_spread_learning as learning
from edgeterminal import nfl_weekly_preparation as weekly
from test_nfl_weekly_preparation import fixture, schedule_archive, schedule_rows, NOW

FIRST = 'nfl-2026-reg-07-buf-min'
SECOND = 'nfl-2026-reg-07-nyj-ne'


def _event(ident, home, away, start):
    return dict(id=ident, sport='NFL', home_team=home, away_team=away, start_time=start,
                status='STATUS_FINAL', meta=dict(season=2026, season_type=2, week=7))


def _prepared_two_game_week(directory):
    rows = schedule_rows()
    rows.extend([
        dict(rows[0], game_id='2026_07_BUF_MIN', week='7', gameday='2026-10-25'),
        dict(rows[0], game_id='2026_07_NYJ_NE', week='7', gameday='2026-10-25',
             gametime='16:25', home_team='NE', away_team='NYJ', home_score='13', away_score='10'),
    ])
    db.upsert_event(_event(FIRST, 'Minnesota Vikings', 'Buffalo Bills', '2026-10-25T17:00:00Z'))
    schedule_archive(directory, rows)
    assert weekly.prepare_schedule(2026, 8, now=NOW, allow_network=False)['status'] == 'ready'


def test_real_prepared_week_requires_missing_supported_game_and_is_label_blind(fixture, monkeypatch):
    _prepared_two_game_week(fixture)
    reader = csv.DictReader
    class NoOutcomes(dict):
        def __getitem__(self, key):
            if key in ('home_score', 'away_score', 'completed', 'status'):
                pytest.fail('Schedule membership inspected an outcome')
            return super().__getitem__(key)
        def get(self, key, default=None):
            if key in ('home_score', 'away_score', 'completed', 'status'):
                pytest.fail('Schedule membership inspected an outcome')
            return super().get(key, default)
    class IdentityReader:
        def __init__(self, *args, **kwargs):
            self.reader = reader(*args, **kwargs)
            self.fieldnames = self.reader.fieldnames
        def __iter__(self):
            return (NoOutcomes(row) for row in self.reader)
    def forbidden(*args, **kwargs):
        pytest.fail('Identity preparation called a score/final or label reader')
    monkeypatch.setattr(csv, 'DictReader', IdentityReader)
    monkeypatch.setattr(weekly, '_parse_schedule', forbidden)
    monkeypatch.setattr(sources, '_known_finals', forbidden)
    monkeypatch.setattr(data, 'labels', forbidden)
    examples = [dict(game_id=ident, season=2026, week=7, as_of=NOW-10000,
                     snapshot_id=ident) for ident in (FIRST, SECOND)]
    with data.schedule_proofs():
        assert not data.complete_week(2026, 7, NOW)
        data.prepare_schedule_proofs([2026], now=NOW)
        assert not data.complete_week(2026, 7, NOW)
        assert learning._groups(deepcopy(examples), NOW) == {}
        db.upsert_event(_event(SECOND, 'New England Patriots', 'New York Jets', '2026-10-25T20:25:00Z'))
        assert data.complete_week(2026, 7, NOW)
        assert {e['id'] for e in data.week_universe(2026, 7)} == {FIRST, SECOND}
        assert set(learning._groups(deepcopy(examples), NOW)) == {(2026, 7)}
    assert not data.complete_week(2026, 7, NOW)


def test_real_prepared_week_rejects_a_modified_cached_body(fixture):
    _prepared_two_game_week(fixture)
    descriptor = weekly.read_schedule(2026, NOW)['descriptor']
    path = weekly._source_path(descriptor['raw_path'])
    path.write_bytes(path.read_bytes() + b'changed after source publication')
    with data.schedule_proofs(), pytest.raises(ValueError, match='hash|source|schedule'):
        data.prepare_schedule_proofs([2026], now=NOW)
