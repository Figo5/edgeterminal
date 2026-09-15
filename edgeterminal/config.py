"""Central configuration. Everything comes from env vars / .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _b(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default


def _csv(key: str, default: str) -> List[str]:
    return [x.strip().lower() for x in (os.getenv(key) or default).split(",") if x.strip()]


class Settings:
    # LLM
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

    # storage
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/edgeterminal.db")

    # providers
    demo_mode = _b("DEMO_MODE", False)
    odds_provider = os.getenv("ODDS_PROVIDER", "auto").strip().lower()
    odds_api_key = os.getenv("ODDS_API_KEY", "").strip()
    odds_regions = os.getenv("ODDS_REGIONS", "us,uk").strip()
    preferred_books = _csv("PREFERRED_BOOKS", "fanduel,bet365")
    stats_provider = os.getenv("STATS_PROVIDER", "auto").strip().lower()
    nflverse_cache_dir = Path(os.getenv("NFLVERSE_CACHE_DIR", str(ROOT / "data/nflverse/pbp")))
    news_provider = os.getenv("NEWS_PROVIDER", "auto").strip().lower()
    injury_provider = os.getenv("INJURY_PROVIDER", "auto").strip().lower()
    weather_provider = os.getenv("WEATHER_PROVIDER", "auto").strip().lower()
    search_provider = os.getenv("SEARCH_PROVIDER", "duckduckgo").strip().lower()
    tavily_api_key = os.getenv("TAVILY_API_KEY", "").strip()
    brave_api_key = os.getenv("BRAVE_API_KEY", "").strip()

    # decision thresholds
    official_pick_confidence = _f("OFFICIAL_PICK_CONFIDENCE", 80)
    lean_confidence = _f("LEAN_CONFIDENCE", 65)
    min_edge_pct = _f("MIN_EDGE_PCT", 2.0)

    # Product scope. Dormant families keep their provider/model code so a sport
    # can be reactivated by adding it here, but only these are fetched,
    # analysed, and served.
    active_sports = ["NFL"]

    # ---------------------------------------------------------------- research
    # Web research is a first-class provider, but it is budgeted: a request may
    # spend a small, fixed number of queries and nothing more.
    research_enabled = _b("RESEARCH_ENABLED", True)
    research_query_budget = int(_f("RESEARCH_QUERY_BUDGET", 4))
    research_results_per_query = int(_f("RESEARCH_RESULTS_PER_QUERY", 5))
    openai_search_model = os.getenv("OPENAI_SEARCH_MODEL", "").strip()

    # Player props are higher variance than game markets: the confidence ceiling
    # is deliberately lower and the Official Pick bar is separate.
    props_enabled = _b("PROPS_ENABLED", True)
    prop_official_confidence = _f("PROP_OFFICIAL_CONFIDENCE", 72)
    prop_lean_confidence = _f("PROP_LEAN_CONFIDENCE", 60)
    prop_confidence_cap = _f("PROP_CONFIDENCE_CAP", 74)
    prop_max_units = {"player_anytime_td": 1.0, "player_1st_td": 0.5}
    prop_default_max_units = _f("PROP_DEFAULT_MAX_UNITS", 1.5)
    debug_endpoints = _b("DEBUG_ENDPOINTS", True)

    # freshness rules (seconds) -- see docs/Stale data protection
    stale_odds_s = _f("ODDS_STALE_SECONDS", 15 * 60)
    stale_news_s = _f("NEWS_STALE_SECONDS", 6 * 3600)
    stale_injury_s = _f("INJURY_STALE_SECONDS", 12 * 3600)
    stale_weather_s = _f("WEATHER_STALE_SECONDS", 3 * 3600)
    stale_season_stats_s = _f("STATS_STALE_SECONDS", 3 * 24 * 3600)

    # server
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8799"))

    # versioning -- stored on every prediction so old rows stay comparable
    model_version = "et-model-0.4.0"
    confidence_version = "et-conf-0.4.0"

    @property
    def db_path(self) -> Path:
        url = self.database_url
        raw = url.split("///", 1)[1] if "///" in url else url
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / raw
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def has_llm(self) -> bool:
        return bool(self.openai_api_key)


settings = Settings()

SPORTS = {
    "NFL": {"key": "americanfootball_nfl", "label": "NFL", "espn": "football/nfl"},
    "CFB": {"key": "americanfootball_ncaaf", "label": "NCAAF", "espn": "football/college-football"},
    "NBA": {"key": "basketball_nba", "label": "NBA", "espn": "basketball/nba"},
    "PGA": {"key": "golf_pga_championship_winner", "label": "PGA", "espn": "golf/pga"},
}
