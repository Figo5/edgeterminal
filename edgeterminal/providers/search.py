"""Web research abstraction. DuckDuckGo HTML by default (no key), Tavily or
Brave when a key is present. Results are treated as leads, never as facts."""
from __future__ import annotations

import html
import re
import time
from typing import Dict, List, Optional
from urllib.parse import unquote

from ..config import settings
from .base import ProviderError, SearchProvider, get_json, get_text

TRUSTED = ("espn.com", "nfl.com", "nba.com", "pgatour.com", "ncaa.com", "theathletic.com",
           "apnews.com", "reuters.com", "cbssports.com", "nbcsports.com", "si.com",
           "weather.gov", "rotowire.com", "fantasypros.com", "pff.com", "sportsline.com")
COMMUNITY = ("reddit.com", "x.com", "twitter.com", "forum", "blogspot")


def classify(url: Optional[str]) -> str:
    u = (url or "").lower()
    if any(t in u for t in TRUSTED):
        return "reported"
    if any(c in u for c in COMMUNITY):
        return "community"
    return "unverified"


class NoSearch(SearchProvider):
    name = "no-search"

    def search(self, query: str, limit: int = 5) -> List[Dict]:
        return []


class DuckDuckGo(SearchProvider):
    """Parses the no-JS HTML endpoint. Best-effort by design."""
    name = "duckduckgo"
    RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    SNIP = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)

    def search(self, query: str, limit: int = 5) -> List[Dict]:
        body = get_text("https://html.duckduckgo.com/html/", {"q": query}, provider=self.name)
        hits = self.RE.findall(body)[:limit]
        snips = [re.sub(r"<[^>]+>", "", s).strip() for s in self.SNIP.findall(body)[:limit]]
        out = []
        for i, (href, title) in enumerate(hits):
            url = href
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                url = unquote(m.group(1))
            out.append(dict(title=html.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
                            url=url, snippet=html.unescape(snips[i]) if i < len(snips) else "",
                            publisher=re.sub(r"^www\.", "", (url.split("/")[2] if "//" in url else "")),
                            trust=classify(url), retrieved_at=time.time()))
        return out


class Tavily(SearchProvider):
    name = "tavily"

    def search(self, query: str, limit: int = 5) -> List[Dict]:
        if not settings.tavily_api_key:
            raise ProviderError(self.name, "TAVILY_API_KEY not set")
        from .base import client
        r = client().post("https://api.tavily.com/search", json={
            "api_key": settings.tavily_api_key, "query": query,
            "max_results": limit, "search_depth": "basic"}, timeout=15)
        r.raise_for_status()
        return [dict(title=x.get("title"), url=x.get("url"), snippet=x.get("content", "")[:400],
                     publisher=(x.get("url", "").split("/")[2] if "//" in x.get("url", "") else ""),
                     trust=classify(x.get("url")), retrieved_at=time.time())
                for x in r.json().get("results", [])]


class Brave(SearchProvider):
    name = "brave"

    def search(self, query: str, limit: int = 5) -> List[Dict]:
        if not settings.brave_api_key:
            raise ProviderError(self.name, "BRAVE_API_KEY not set")
        from .base import client
        r = client().get("https://api.search.brave.com/res/v1/web/search",
                         params={"q": query, "count": limit},
                         headers={"X-Subscription-Token": settings.brave_api_key,
                                  "Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        return [dict(title=x.get("title"), url=x.get("url"),
                     snippet=re.sub(r"<[^>]+>", "", x.get("description", ""))[:400],
                     publisher=(x.get("profile") or {}).get("name"),
                     trust=classify(x.get("url")), retrieved_at=time.time())
                for x in (r.json().get("web", {}).get("results") or [])]
