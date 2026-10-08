# 2026 player prop evidence

Combined record: **55–49 (52.9%)**, with 28 pending (3 historical, 25 locked before kickoff).

This season total combines historical reconstructions with genuinely locked selections. Reconstructions were created after the games and are a partial retrospective sample.

| Week | Wins | Losses | Pending | Win rate | Coverage |
|---|---:|---:|---:|---:|---|
| 1 | 4 | 1 | 0 | 80.0% | 1 games |
| 2 | 13 | 10 | 2 | 56.5% | 5 games |
| 3 | 38 | 38 | 1 | 50.0% | 16 games |
| 4 | 0 | 0 | 0 | — | 0 games |
| 5 | 0 | 0 | 25 | — | 5 games |

## Method

Latest complete archived pregame main-line pairs; fixed 2024 family and sigma; training and features stop before the target week (maximum original Week 3 cutoff). The top five are chosen by confidence before reading outcomes. These forecasts were reconstructed after the games. Historical availability/injury inputs are not reconstructed; missing lines and unmatched players are excluded. This is a partial retrospective sample, not the prospective live record.

Week 1 contains DEN–KC only; Week 2 contains five games. Week 3 combines 57 original archived forecasts and 20 new replayed selections. Week 4 has no archived supported prop lines. No sportsbook lines were guessed.

Prior-week model inputs and original Week 3 parameters are hash-checked. Actual results are read after selecting the five strongest player/stat markets. Confidence percentages are uncalibrated model estimates; this sample does not establish profitability.

Three historical selections remain pending because official participation/stat rows are missing: Nico Collins receptions and receiving yards (Week 2 CIN–HOU), and Jerry Jeudy receptions (Week 3 CAR–CLE). Missing rows are not graded as zero.

Result source: https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv
Result source SHA-256: `74dce52a31ffad854e516886bb16b39f88e02f77f292bca53e134ba5ec041cb4`

No API requests, new live locks, bankroll changes, or training against forward results were required. Week 3 candidate bytes and Week 3/4 game freezes were verified unchanged.
