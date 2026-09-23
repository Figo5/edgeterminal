"""Provider interfaces + a hardened HTTP helper.

Every provider is swappable. A provider failure is a structured, logged
`ProviderError` -- never an exception that reaches the UI, and never fabricated
data.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..logs import RedactSecrets

log = logging.getLogger("edgeterminal.providers")

DEFAULT_TIMEOUT = 12.0
UA = "EdgeTerminal/0.4 (+https://localhost; python-httpx)"


class ProviderError(Exception):
    def __init__(self, provider: str, detail: str):
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail


_client: Optional[httpx.Client] = None


def _safe_error_detail(error: Exception) -> str:
    """Provider errors can reach logs and API status without logger filters.

    HTTP exceptions include the full request URL, including query credentials.
    Describe the endpoint without its query before constructing ProviderError.
    """
    if isinstance(error, (httpx.HTTPStatusError, httpx.RequestError)):
        request = error.request
        endpoint = f"{request.url.host}{request.url.path}" if request else "HTTP endpoint"
        if isinstance(error, httpx.HTTPStatusError):
            return f"HTTP {error.response.status_code} from {endpoint}"
        return f"{type(error).__name__} from {endpoint}"
    return f"{type(error).__name__}: {RedactSecrets().scrub(str(error))}"


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": UA, "Accept": "application/json,text/html;q=0.9"},
            follow_redirects=True,
        )
    return _client


def get_json(url: str, params: Optional[Dict] = None, provider: str = "http",
             retries: int = 2, timeout: float = DEFAULT_TIMEOUT,
             headers: Optional[Dict] = None) -> Any:
    last = ""
    for attempt in range(retries + 1):
        try:
            r = client().get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 429:
                last = "rate limited (429)"
                time.sleep(0.6 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - providers must never crash callers
            last = _safe_error_detail(e)
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    raise ProviderError(provider, last or "unknown failure")


def get_text(url: str, params: Optional[Dict] = None, provider: str = "http",
             timeout: float = DEFAULT_TIMEOUT) -> str:
    try:
        r = client().get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:  # noqa: BLE001
        raise ProviderError(provider, _safe_error_detail(e))


def get_json_meta(url: str, params: Optional[Dict] = None, provider: str = "http",
                  timeout: float = DEFAULT_TIMEOUT,
                  headers: Optional[Dict] = None) -> Tuple[Any, Dict[str, str]]:
    """Same hardening as `get_json`, but also returns response headers.

    Quota-bearing APIs report remaining requests in headers; the caller needs
    them without a second request.
    """
    try:
        r = client().get(url, params=params, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.json(), {k.lower(): v for k, v in r.headers.items()}
    except Exception as e:  # noqa: BLE001
        raise ProviderError(provider, _safe_error_detail(e))


# ------------------------------------------------------------------ interfaces
class OddsProvider:
    name = "odds"
    is_demo = False

    def list_events(self, sport: str) -> List[Dict]:
        raise NotImplementedError

    def fetch_odds(self, sport: str) -> List[Dict]:
        """Returns flat rows: event_id, sport, book, market, selection, line, price."""
        raise NotImplementedError


class StatsProvider:
    name = "stats"
    is_demo = False

    def team_stats(self, sport: str) -> Dict[str, Dict]:
        raise NotImplementedError

    def player_stats(self, sport: str, team: str) -> List[Dict]:
        return []


class InjuryProvider:
    name = "injuries"
    is_demo = False

    def injuries(self, sport: str, team: Optional[str] = None) -> List[Dict]:
        raise NotImplementedError


class NewsProvider:
    name = "news"
    is_demo = False

    def news(self, sport: str, query: Optional[str] = None) -> List[Dict]:
        raise NotImplementedError


class WeatherProvider:
    name = "weather"
    is_demo = False

    def forecast(self, lat: float, lon: float, when_iso: Optional[str] = None) -> Dict:
        raise NotImplementedError


class GolfProvider:
    name = "golf"
    is_demo = False

    def tournament(self) -> Optional[Dict]:
        raise NotImplementedError

    def field_stats(self) -> Dict[str, Dict]:
        raise NotImplementedError


class SearchProvider:
    name = "search"
    is_demo = False

    def search(self, query: str, limit: int = 5) -> List[Dict]:
        raise NotImplementedError
