"""Keep API startup and refresh tests away from saved research reports."""
from functools import wraps
from pathlib import Path

import pytest

from edgeterminal import nfl_forward_grading


@pytest.fixture(autouse=True)
def isolated_default_research_reports(tmp_path, monkeypatch):
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
