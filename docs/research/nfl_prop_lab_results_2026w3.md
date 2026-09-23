# NFL prop lab: 2026 Week 3 research snapshot

All projections below are research estimates. The product remains `RESEARCH_ONLY` / `PASS`; no prop picks or edge thresholds are defined.

## Forward record

- Immutable prop freeze SHA-256: `8234353c49cef8f337dac840aff8835c350453cf31aeef7955687e8ea6609304`
- Archived quote batch: `7bf93452-5e1b-409a-bb93-18007ff97abe`
- 16 canonical Week 3 games; 12 have paired quoted player markets; 97 projected players and 216 distinct player-market projections.
- 880 complete main-line groups (1760 quote sides), of which 1746 quote sides / 873 groups received projections. 56 groups are one-sided; 6781 alternate quotes remain observed without comparison. No invalid main price/line or duplicate main side was found.
- 312 unique archived player identities matched exactly by name and team across all collected markets; 16 did not match and none were ambiguous.

## Chronological validation

Rows from 2022–2023 establish history. Each 2024 and 2025 week is fitted from strictly earlier weeks. The 2024 development MAE selects one family per market. The 2025 holdout is reported after selection and does not change it. Bias is prediction minus actual. This evaluation conditions on an appearing player with prior opportunity; it does not test availability or a wagering strategy.

| Market | Family | 2024 N | 2024 MAE | 2024 RMSE | 2024 bias | 2025 N | 2025 MAE | 2025 RMSE | 2025 bias |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| passing_yards | rolling | 590 | 73.19 | 91.92 | +5.27 | 591 | 73.98 | 91.99 | +9.56 |
| passing_yards | ridge (selected) | 590 | 65.84 | 82.57 | +7.07 | 591 | 67.18 | 83.42 | +13.50 |
| passing_yards | boosted | 590 | 65.89 | 82.82 | +1.53 | 591 | 65.48 | 81.02 | +9.80 |
| rushing_yards | rolling | 2060 | 19.21 | 27.13 | +0.56 | 2084 | 18.96 | 28.08 | +1.22 |
| rushing_yards | ridge (selected) | 2060 | 18.34 | 25.80 | -0.48 | 2084 | 17.81 | 25.62 | +0.66 |
| rushing_yards | boosted | 2060 | 18.52 | 26.17 | -0.72 | 2084 | 17.83 | 25.74 | +0.38 |
| rushing_attempts | rolling | 2060 | 3.15 | 4.36 | +0.15 | 2084 | 3.07 | 4.33 | +0.22 |
| rushing_attempts | ridge | 2060 | 3.05 | 4.16 | +0.16 | 2084 | 2.94 | 4.03 | +0.16 |
| rushing_attempts | boosted (selected) | 2060 | 3.04 | 4.19 | +0.04 | 2084 | 2.92 | 4.04 | +0.09 |
| receptions | rolling | 4156 | 1.53 | 2.04 | +0.06 | 4190 | 1.47 | 1.96 | +0.13 |
| receptions | ridge | 4156 | 1.46 | 1.91 | +0.04 | 4190 | 1.39 | 1.81 | +0.12 |
| receptions | boosted (selected) | 4156 | 1.45 | 1.91 | +0.01 | 4190 | 1.39 | 1.81 | +0.10 |
| receiving_yards | rolling | 4156 | 20.42 | 28.39 | +0.85 | 4190 | 19.82 | 27.39 | +1.18 |
| receiving_yards | ridge (selected) | 4156 | 19.08 | 26.00 | +0.72 | 4190 | 18.53 | 25.15 | +1.36 |
| receiving_yards | boosted | 4156 | 19.19 | 26.19 | +0.40 | 4190 | 18.56 | 25.12 | +1.03 |

## Week 3 examples

One example per market, from the frozen artifact. These are not recommendations. Full projections and each sportsbook quote are in `data/research/nfl_prop_lab_2026_week3.json`; a deduplicated player-market list is in `data/research/nfl_prop_lab_week3_player_projections.csv`.

| Market | Player | Projection | Residual σ | Example observed book line |
|---|---|---:|---:|---|
| passing_yards | Jordan Love | 261.3 | 82.3 | fanduel 226.5 |
| rushing_yards | Bijan Robinson | 70.3 | 25.8 | fanduel 77.5 |
| rushing_attempts | Bijan Robinson | 15.9 | 4.2 | draftkings 18.5 |
| receptions | Christian Watson | 4.4 | 1.9 | fanduel 4.5 |
| receiving_yards | Bijan Robinson | 44.0 | 26.0 | fanduel 40.5 |

The initial uncertainty scale is one 2024 walk-forward residual standard deviation per market and selected family, applied through a Normal CDF. It is not player-specific or calibrated against sportsbook prices. This report makes no claim of betting edge.

## Input preservation

The exact player-stat CSVs and `games.csv.gz` schedule used for this run are stored in `data/research/nfl_prop_lab_inputs_2026w3.tar.gz`. The companion manifest lists SHA-256 for the archive and each extracted file and refers to the immutable prop freeze. Input stats were retrieved from the [official nflverse-data `stats_player` release](https://github.com/nflverse/nflverse-data/releases/tag/stats_player).
