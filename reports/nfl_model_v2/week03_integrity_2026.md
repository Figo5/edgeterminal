# Week 3 pre-kickoff integrity — 23 September 2026

**Status: intact with one reproducibility warning.** The frozen candidate is
still `RESEARCH_ONLY` / PASS. No Week 3 game has been graded and no official
pick has been enabled. The first kickoff is 24 September, 20:15 EDT.

| Check | Result |
| --- | --- |
| Freeze file against committed Git artifact | Exact byte match; SHA-256 `399566baa6ae059d8fac55050ab07201796837a91541000e482dcfa5b1b776d1` |
| ESPN game identities and kickoff | 16/16 mapped to one matching ESPN event; teams and UTC kickoffs match the frozen record exactly. All frozen ET timestamps carry `-04:00`, valid for these September dates. |
| Freeze timing | Created 23 September at 11:35:05 EDT, before all 16 kickoffs. |
| Model code/version | `linear` research version linked to freeze SHA; code commit `3cf4d84` and current `nfl_research.py` both match the frozen code SHA `f6b70c2445af82930ebe3e7a2b4c65a31d6fe4d8f62d600833933b0c760cbb0b`. |
| Feature/model hash | Registered feature hash `61d709598f0d377be940d8824f83ab59a117c81b0fa025e382623956f08c452c`; model hash `f6b70c2445af82930ebe3e7a2b4c65a31d6fe4d8f62d600833933b0c760cbb0b`, matching the freeze's code-derived fallback. |
| Training cutoff | Registered 1,903 prior games. The 2026 play-by-play cache has zero Week 3 plays; all eight play-by-play source hashes still match their frozen values. Current schedule has 16 Week 3 games and no Week 3 scores. The freeze implementation selects training rows with `(season, week) < (2026, 3)`. |
| Frozen market provenance | 64 immutable sidecar quotes: 16 home spreads, 16 totals, 32 moneylines. Each quote ID, book, market, side, line, price, and capture time matches its original immutable source snapshot; 16 snapshot SHA attestations match. The corrected spread sidecar is the negative of the erroneous spread field in the original file for each game. |
| Immutability | SQLite update/delete triggers exist on freezes, market sidecars, and attestations. Market and attestation insertions close atomically with initial registration; later insertions are rejected. The original source snapshots and shadow entries also have update/delete guards. |
| Grading delay | Four hours after the actual ESPN kickoff; moved games and premature finals fail closed in tests. The registered freeze has zero grade rows. |
| Product write paths | Inspected `api.py` routes and `web/app.js`: no exposed route or frontend action writes a research freeze file or changes frozen projections/prices. API refresh may run the append-only grader. |

**Reproducibility warning:** the locally refreshed `games.csv.gz` now hashes
`f32f9fad68dd5802bfa43c03e012b21c2632486fb5e03586ce4c9823e6b6e8a7`;
the freeze records
`a06349d3c8e909847211600e31c86c090d33888fd29104733ea0b365326c64ba`.
The original schedule bytes are no longer in this cache, so an exact refit
from current cached files cannot be asserted. This does not change the
committed freeze or its predictions. The model hash in this original freeze is
a code/config proxy, not a saved fitted-coefficient artifact hash.

The machine and Codex scheduler must stay awake and online for near-kickoff
captures. A missed capture cannot be recreated afterward. A kickoff change
after the daily schedule refresh could make the local capture window stale;
the health report flags known changes. ESPN identity or final-score corrections
still require audit review. See [machine-readable checks](week03_integrity_2026.json),
[capture health](capture_health_2026_week03.json), and
[operations](../../docs/nfl_forward_operations.md).
