"""Keep API startup and refresh tests away from saved research reports."""
from functools import wraps
from pathlib import Path

import pytest

from edgeterminal import nfl_forward_grading, nfl_prop_challenger
from edgeterminal.config import settings


@pytest.fixture(autouse=True)
def no_unmocked_game_source_process(monkeypatch):
    """Fixture refreshes may enqueue work but cannot launch a real network child."""
    from edgeterminal import nfl_game_sources
    dispatch = nfl_game_sources.dispatch_maintenance
    popen = nfl_game_sources.subprocess.Popen
    def guarded(*args, **kwargs):
        if nfl_game_sources.subprocess.Popen is popen:
            kwargs['launch'] = False
        return dispatch(*args, **kwargs)
    monkeypatch.setattr(nfl_game_sources, 'dispatch_maintenance', guarded)


@pytest.fixture(autouse=True)
def isolated_default_research_reports(tmp_path, monkeypatch):
    # NBA integration tests opt in explicitly. Legacy worker fixtures must not fetch live NBA feeds.
    monkeypatch.setattr(settings, 'nba_research_enabled', False)
    # Worker tests use temporary databases. Their empty grades must never
    # replace the real challenger's cached performance summary.
    monkeypatch.setattr(nfl_prop_challenger, 'DIRECTORY', tmp_path / 'prop_challenger')
    live_reports = Path(__file__).resolve().parents[1] / "reports/nfl_model_v2"
    original_sync_all = nfl_forward_grading.sync_all

    @wraps(original_sync_all)
    def sync_all(season=2026, freeze_dir="reports/nfl_model_v2", provider=None, now=None):
        # API startup uses an absolute project path even with a temporary database.
        # Preserve explicit fixture directories while isolating the live default.
        if Path(freeze_dir).resolve() == live_reports:
            freeze_dir = tmp_path / "nfl_model_v2"
        return original_sync_all(season, freeze_dir, provider=provider, now=now)

    monkeypatch.setattr(nfl_forward_grading, "sync_all", sync_all)
