"""First-class web-research provider.

One abstraction for every kind of web evidence the app uses:

    OpenAI web search  ->  Tavily  ->  Brave  ->  DuckDuckGo HTML

Everything that comes back is normalised into the same record shape, classified
for reliability, given publication/retrieval timestamps where the backend offers
them, tagged with the players/teams it mentions, de-duplicated by URL, and cached
per topic. Callers never see a backend-specific payload.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, Iterable, List, Optional

from ..config import settings
from . import cache as rcache
from .sources import classify, freshness, relevance_label, topics

log = logging.getLogger("edgeterminal.research")

MAX_ITEMS = 8
SNIPPET_CHARS = 420

# Structured, reproducible inputs beat prose: research never replaces these.
STRUCTURED_FIRST = ("odds", "usage", "injuries", "scores", "schedule")

_TEAM_TOKENS = ("Chiefs", "Broncos", "Bills", "Ravens", "Bengals", "Browns", "Steelers",
                "Texans", "Colts", "Jaguars", "Titans", "Raiders", "Chargers", "Rams",
                "49ers", "Seahawks", "Cardinals", "Cowboys", "Giants", "Eagles",
                "Commanders", "Packers", "Lions", "Vikings", "Bears", "Falcons",
                "Panthers", "Saints", "Buccaneers", "Dolphins", "Patriots", "Jets")

_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]{2,})\b")


def _entities(text: str, player: Optional[str] = None,
              team: Optional[str] = None) -> Dict:
    teams = sorted({token for token in _TEAM_TOKENS if token in text})
    if team and team.split()[-1] not in teams:
        teams.append(team.split()[-1])
    names = {match for match in _NAME_RE.findall(text)}
    if player:
        names.add(player)
    return dict(players=sorted(names)[:12], teams=sorted(set(teams)))


def normalize_item(item: Dict, *, query: str = "", backend: str = "",
                   now: Optional[float] = None, player: Optional[str] = None,
                   team: Optional[str] = None) -> Dict:
    stamp = float(now if now is not None else time.time())
    url = item.get("url")
    title = (item.get("title") or "").strip()
    snippet = re.sub(r"\s+", " ", (item.get("snippet") or item.get("content") or "")).strip()
    verdict = classify(url, title)
    published = item.get("published_at") or item.get("published")
    topic = item.get("topic") or topics(url, title)[0]
    return dict(title=title[:220], url=url, snippet=snippet[:SNIPPET_CHARS],
                publisher=item.get("publisher") or verdict["domain"],
                domain=verdict["domain"], reliability=verdict["reliability"],
                reliability_reason=verdict["reason"], topic=topic,
                topics=topics(url, title),
                published_at=published, retrieved_at=float(item.get("retrieved_at") or stamp),
                freshness=round(freshness(published, stamp, topic=topic), 4),
                relevance=relevance_label(published, stamp, topic=topic),
                entities=_entities(f"{title} {snippet}", player, team),
                backend=backend, query=query, structured=False,
                source_type="WEB_RESEARCH")


class WebResearchProvider:
    """Central research entry point. Cache-first, gap-driven, provenance-keeping."""

    name = "web-research"

    def __init__(self, backends: Optional[Iterable[Callable]] = None,
                 cache: Optional[rcache.TTLCache] = None, now: Optional[Callable[[], float]] = None):
        self._now = now or time.time
        self.cache = cache or rcache.shared_cache()
        self._backends = list(backends) if backends is not None else None
        self.queries: List[Dict] = []

    # --------------------------------------------------------------- backends
    def backends(self) -> List[Callable]:
        if self._backends is not None:
            return self._backends
        chain: List[Callable] = []
        if settings.has_llm():
            chain.append(self._openai_backend)
        chain.append(self._search_provider_backend)
        return chain

    def _openai_backend(self, query: str, limit: int) -> List[Dict]:
        from ..agents import llm
        return llm.web_search(query, limit=limit)

    def _search_provider_backend(self, query: str, limit: int) -> List[Dict]:
        from ..providers import registry as R
        rows, source, _demo = R.web_search(query, limit)
        return [dict(row, backend=source) for row in rows]

    # ------------------------------------------------------------------ search
    def search(self, query: str, topic: str = "general", limit: int = 5,
               player: Optional[str] = None, team: Optional[str] = None,
               force: bool = False) -> Dict:
        """Run (or reuse) one research query. Returns items + provenance."""
        key = re.sub(r"\s+", " ", query.strip().lower())
        cache_key = f"{topic}:{key}:{limit}"
        if not force:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return dict(cached, cache="hit")
        items: List[Dict] = []
        used: List[str] = []
        errors: List[str] = []
        for backend in self.backends():
            try:
                raw = backend(query, limit) or []
            except Exception as e:  # noqa: BLE001 - research must never break a request
                errors.append(f"{getattr(backend, '__name__', 'backend')}: {type(e).__name__}")
                continue
            if not raw:
                continue
            label = raw[0].get("backend") or getattr(backend, "__name__", "backend")
            used.append(str(label))
            items = [normalize_item(row, query=query, backend=str(label), now=self._now(),
                                    player=player, team=team)
                     for row in raw if row.get("url")]
            if items:
                break
        deduped: List[Dict] = []
        seen = set()
        for item in items:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            deduped.append(item)
        deduped.sort(key=lambda i: (
            {"PRIMARY": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNVERIFIED": 4}[i["reliability"]],
            -i["freshness"]))
        payload = dict(query=query, topic=topic, items=deduped[:MAX_ITEMS],
                       backends_used=used, errors=errors, retrieved_at=self._now())
        self.cache.set(cache_key, payload, topic=topic)
        self.queries.append(dict(query=query, topic=topic, backend=used,
                                 results=len(payload["items"]), at=payload["retrieved_at"]))
        return dict(payload, cache="miss")

    def research_plan(self, plan: Iterable[Dict], *, budget: int = 4,
                      player: Optional[str] = None, team: Optional[str] = None) -> Dict:
        """Execute a planner output, respecting a hard query budget."""
        steps = list(plan)[:max(0, budget)]
        results, items = [], []
        for step in steps:
            found = self.search(step["query"], topic=step.get("topic", "general"),
                                player=step.get("researched_for") or player, team=team)
            results.append(dict(gap=step["gap"], priority=step.get("priority"),
                                why=step.get("why"), query=step["query"],
                                cache=found["cache"], backend=found["backends_used"],
                                items=found["items"], errors=found["errors"]))
            items.extend(found["items"])
        return dict(steps=results, items=items, budget=budget,
                    queries_used=len(steps), budget_exhausted=len(list(plan)) > budget)

    def trace(self, limit: int = 25) -> List[Dict]:
        return self.queries[-limit:]


_provider: Optional[WebResearchProvider] = None


def shared() -> WebResearchProvider:
    global _provider
    if _provider is None:
        _provider = WebResearchProvider()
    return _provider