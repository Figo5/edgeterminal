"""nflverse play-by-play ingestion and deterministic team aggregation.

Data license: CC-BY-4.0. Source assets are cached locally and never committed.
Historical seasons should be frozen by hash; the current season is mutable as
NFL stat corrections are published.
"""
from __future__ import annotations

import csv
import gzip
import hashlib

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

import httpx

from .normalize import normalize_team

PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"


def _number(row: Mapping, key: str):
    try:
        value = row.get(key)
        return float(value) if value not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


def aggregate_team_features(plays: Iterable[Mapping], season: int,
                            retrieved_at: Optional[float] = None) -> Dict[str, Dict]:
    groups = defaultdict(lambda: defaultdict(list))
    games = defaultdict(set)
    for row in plays:
        team = normalize_team(None, row.get("posteam"))
        if not team:
            continue
        epa = _number(row, "epa")
        success = _number(row, "success")
        dropback = _number(row, "qb_dropback") == 1
        rush = _number(row, "rush_attempt") == 1
        if epa is not None:
            groups[team]["epa_play"].append(epa)
            if dropback:
                groups[team]["epa_dropback"].append(epa)
            if rush:
                groups[team]["epa_rush"].append(epa)
        if success is not None:
            groups[team]["success_rate"].append(success)
            if dropback:
                groups[team]["dropback_success"].append(success)
            if rush:
                groups[team]["rush_success"].append(success)
        cpoe = _number(row, "cpoe")
        if cpoe is not None:
            groups[team]["cpoe"].append(cpoe)
        proe = _number(row, "pass_oe")
        if proe is not None:
            groups[team]["proe"].append(proe)
        yards = _number(row, "yards_gained")
        if yards is not None and (dropback or rush):
            groups[team]["explosive_play_rate"].append(1.0 if yards >= 20 else 0.0)
        no_huddle = _number(row, "no_huddle")
        if no_huddle is not None:
            groups[team]["no_huddle_rate"].append(no_huddle)
        if row.get("game_id"):
            games[team].add(str(row["game_id"]))
    stamp = float(retrieved_at if retrieved_at is not None else time.time())
    out = {}
    for team, metrics in groups.items():
        values = {key: round(sum(xs) / len(xs), 6) for key, xs in metrics.items() if xs}
        values.update(games=len(games[team]), plays=len(metrics.get("epa_play", [])),
                      season=int(season), source="nflverse-pbp", retrieved_at=stamp)
        out[team] = values
    return out


def load_team_features(season: int, cache_dir: Path, refresh: bool = False,
                       timeout: float = 90.0) -> Dict[str, Dict]:
    """Fetch a yearly CC-BY nflverse CSV.GZ once and aggregate it locally."""
    archive, retrieved = _ensure_archive(season, cache_dir, refresh, timeout)
    with gzip.open(archive, "rt", encoding="utf-8", newline="") as handle:
        return aggregate_team_features(csv.DictReader(handle), season, retrieved)


def _ensure_archive(season: int, cache_dir: Path, refresh: bool = False,
                    timeout: float = 90.0):
    """Download-once, hash-stamp, and return (archive path, retrieved_at)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / f"play_by_play_{season}.csv.gz"
    metadata = cache_dir / f"play_by_play_{season}.metadata.json"
    if refresh or not archive.exists():
        url = PBP_URL.format(season=int(season))
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        archive.write_bytes(response.content)
        metadata.write_text(json.dumps({"source_url": url, "retrieved_at": time.time(),
                                        "sha256": hashlib.sha256(response.content).hexdigest(),
                                        "license": "CC-BY-4.0"}, indent=2))
    retrieved = json.loads(metadata.read_text()).get("retrieved_at", archive.stat().st_mtime) \
        if metadata.exists() else archive.stat().st_mtime
    return archive, retrieved


def pbp_rows(season: int, cache_dir: Path, refresh: bool = False, timeout: float = 90.0,
             columns: Optional[Iterable[str]] = None) -> List[Dict]:
    """Stream the cached play-by-play file into plain rows (optionally narrowed).

    Used by the historical replay; the running application only needs the
    aggregated team features.
    """
    archive, _ = _ensure_archive(season, cache_dir, refresh, timeout)
    keep = tuple(columns) if columns else None
    rows: List[Dict] = []
    with gzip.open(archive, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append({k: row.get(k) for k in keep} if keep else dict(row))
    return rows


def load_cached_team_features(season: int, cache_dir: Path) -> Dict[str, Dict]:
    """Read only an existing cache; normal application refresh never downloads PBP."""
    archive = cache_dir / f"play_by_play_{season}.csv.gz"
    if not archive.exists():
        return {}
    return load_team_features(season, cache_dir, refresh=False)


def merge_feature_seasons(current: Dict[str, Dict], prior: Dict[str, Dict],
                          season: int) -> Dict[str, Dict]:
    """Per-team features, current season first, prior season for teams without it.

    Early in a season most teams have no plays yet. Falling back per team (rather
    than only when the whole file is missing) keeps the efficiency inputs
    populated while labelling exactly which season each row came from.

    Play-by-play bookkeeping (`games`, `plays`, `season`, `source`,
    `retrieved_at`) is namespaced as `feature_*`: ESPN owns the game counts that
    drive shrinkage, so a prior-season PBP row must never inflate the apparent
    current-season sample.
    """
    bookkeeping = ("games", "plays", "season", "source", "retrieved_at")

    def _features(row: Dict, prior_season: bool) -> Dict:
        metrics = {k: v for k, v in row.items() if k not in bookkeeping}
        metrics.update(feature_games=row.get("games"), feature_plays=row.get("plays"),
                       feature_season=row.get("season"),
                       prior_season_features=bool(prior_season))
        return metrics

    merged: Dict[str, Dict] = {}
    for team, row in (current or {}).items():
        merged[team] = _features(row, prior_season=False)
    for team, row in (prior or {}).items():
        if team in merged:
            continue
        merged[team] = _features(row, prior_season=True)
    return merged
