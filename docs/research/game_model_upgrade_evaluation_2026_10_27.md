# NBA game model upgrade assessment — 2026-10-27

The existing NBA game score model is retained. The available historical data
does not support promoting the tested alternative. The active player-prop
upgrades are separate from this assessment; no improvement in game score
accuracy or game win probabilities is claimed here.

## Data and chronology

The local sourced team-history cache contains 2,462 team rows from the
2025–26 regular season before the fixed October 1, 2026 cutoff. All 30 teams
are represented. The comparison used only games with at least ten earlier
eligible games for both teams, and preserved the existing four-hour feature
lag. No October 2026 forward outcomes were used.

| Period | Eligible game forecasts | Use |
| --- | ---: | --- |
| October–December 2025 | 337 | Fit initial regression |
| January 2026 | 233 | Select regularization |
| February 2026 | 168 | Calibrate win probabilities |
| March–April 2026 | 338 | Historical acceptance assessment |

The alternative was ridge regression over each team's prior five- and
twenty-game scoring and points allowed, capped sourced rest days, and the
difference between recent and longer-term scoring margins. Separate models
predicted game margin and total. Ridge penalties of 10, 100 and 1,000 were
compared on January MAE. The selected models were refit on data before
February 1. A one-feature logistic link from the predicted margin to the
observed home winner was fit on February games only. The baseline was the
existing `nba_model.project_game` arithmetic on exactly the same games.

## Results

Lower is better for every metric below.

| Metric | Existing model | Alternative |
| --- | ---: | ---: |
| January margin MAE | 11.8311 | 11.8454 |
| January total MAE | 15.0643 | 15.4769 |
| March–April margin MAE | 12.1368 | 12.0836 |
| March–April total MAE | 15.0195 | 15.1059 |
| March–April home-winner Brier score | 0.17912 | 0.19436 |

The alternative failed development-period point-error comparisons. Its
small holdout margin change does not offset the worse totals and win
probabilities. The improved February in-sample Brier score (0.20923 versus
0.22345) did not generalize to March–April and is not evidence for deployment.

## Limits and decision

The cache contains a single historical NBA season and no possession counts.
It therefore cannot establish a learned pace adjustment, lineup effect or
injury point adjustment. Current roster/injury snapshots cannot be treated
as historical pregame inputs. More seasons and sourced point-in-time
possession and lineup information would permit a better-supported future
study. Historical betting lines were not used; these results do not measure
market selection win rate.

No game projection formula, saved forecast, selection or grade was changed
by this assessment. Regularized score fitting and probability calibration
were evaluated and rejected for the current game component.
