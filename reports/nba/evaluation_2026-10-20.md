# NBA baseline evaluation — 2026-10-20

PASS / RESEARCH_ONLY. Historical evaluation is separate from the prospective NBA record.

Chronological 2025–26 holdout: 295 game forecasts; 8215 player/stat forecasts across 105 currently rostered players.

Fixed descriptive baseline. Current-roster historical sample has survivorship bias. No historical betting lines were used; no retrospective selection win rate or profitability is claimed. Each holdout forecast uses only earlier games.

Model: nba-research-1.1.0. Fixed cutoff: 2026-10-01T00:00:00Z.
Parameter fingerprint: `79026f8a1c09b06e247bd5e2c0335d86f8930c92c416679299dadfc479cf4da4`.

Game margin MAE: 12.525; total MAE: 14.882; home-win Brier: 0.1779.

| Market | Holdout forecasts | MAE |
|---|---:|---:|
| points | 1643 | 4.961 |
| rebounds | 1643 | 2.014 |
| assists | 1643 | 1.47 |
| threes | 1643 | 0.948 |
| pra | 1643 | 6.449 |

The JSON report includes the chronological per-game outcomes and source catalog. Exact public response bytes remain in NBA SQLite artifacts. NBA CDN requests returned HTTP 403 and NBA Stats timed out; ESPN is the identified primary broadcaster source. No historical market lines or retrospective selection win rate are claimed.
