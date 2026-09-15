"""TTL cache for research, usage and prop-price lookups.

Breaking news must not be overcached; season stats can live for hours. The cache
also records hit/miss counters so the debug view can show whether research is
actually being reused.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Topic TTLs in seconds. Odds churn constantly; injuries can break minutes before
# kickoff, so they get a short window on purpose.
TTL_BY_TOPIC = {
    "odds": 180.0,
    "injury": 900.0,
    "role": 2400.0,
    "depth_chart": 2400.0,
    "weather": 2700.0,
    "news": 1800.0,
    "stats": 43200.0,
    "general": 1800.0,
}
DEFAULT_TTL = 1800.0


def ttl_for(topic: str) -> float:
    return TTL_BY_TOPIC.get(topic, DEFAULT_TTL)


class TTLCache:
    """In-memory TTL store with counters.

    `now` is injectable so tests (and the debug view) can advance the clock
    without sleeping.
    """

    def __init__(self, now: Optional[Callable[[], float]] = None,
                 persist_path: Optional[Path] = None):
        self._now = now or time.time
        self._items: Dict[str, Dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.persist_path = Path(persist_path) if persist_path else None
        if self.persist_path and self.persist_path.exists():
            try:
                payload = json.loads(self.persist_path.read_text())
            except Exception:  # noqa: BLE001 - a corrupt cache is not fatal
                payload = {}
            for key, entry in (payload.get("items") or {}).items():
                if float(entry.get("expires_at", 0)) > self._now():
                    self._items[key] = entry

    # ------------------------------------------------------------------ core
    def set(self, key: str, value: Any, ttl: Optional[float] = None,
            topic: Optional[str] = None) -> None:
        window = float(ttl if ttl is not None else ttl_for(topic or "general"))
        self._items[key] = dict(value=value, stored_at=self._now(),
                                expires_at=self._now() + window, topic=topic or "general")
        self.writes += 1
        self._flush()

    def get(self, key: str) -> Optional[Any]:
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        if float(entry["expires_at"]) <= self._now():
            self._items.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return entry["value"]

    def set_topic(self, topic: str, key: str, value: Any,
                  ttl: Optional[float] = None) -> None:
        self.set(f"{topic}:{key}", value, ttl=ttl, topic=topic)

    def get_topic(self, topic: str, key: str) -> Optional[Any]:
        return self.get(f"{topic}:{key}")

    def age(self, key: str) -> Optional[float]:
        entry = self._items.get(key)
        return None if entry is None else round(self._now() - float(entry["stored_at"]), 3)

    def purge(self) -> int:
        expired = [k for k, e in self._items.items() if float(e["expires_at"]) <= self._now()]
        for key in expired:
            self._items.pop(key, None)
        self._flush()
        return len(expired)

    # ------------------------------------------------------------------ info
    def stats(self) -> Dict:
        return dict(hits=self.hits, misses=self.misses, writes=self.writes,
                    entries=len(self._items),
                    topics=sorted({e.get("topic", "general") for e in self._items.values()}))

    def describe(self, limit: int = 50) -> List[Dict]:
        rows = []
        for key, entry in list(self._items.items())[:limit]:
            rows.append(dict(key=key, topic=entry.get("topic"),
                             age_s=round(self._now() - float(entry["stored_at"]), 1),
                             ttl_s=round(float(entry["expires_at"]) - float(entry["stored_at"]), 1)))
        return rows

    def _flush(self) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(json.dumps(dict(items=self._items)))
        except Exception:  # noqa: BLE001 - caching must never break a request
            pass


_cache: Optional[TTLCache] = None


def shared_cache() -> TTLCache:
    global _cache
    if _cache is None:
        _cache = TTLCache()
    return _cache