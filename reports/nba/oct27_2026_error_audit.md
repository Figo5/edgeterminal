# NBA October 27, 2026 — frozen-input error audit

The active prop model `nba-research-2.0.1` saved 20 pre-tipoff selections across four games: **9 wins, 11 losses**. All original projections, selections and grades are unchanged. This is a descriptive audit of the newly observed forward night, not model fitting or a retrospective replacement of its picks.

## Evidence and integrity

For each saved prop forecast, this audit read only the `nba_source_artifacts` identified by its immutable `source_artifact_ids`. Each source body matched its archived SHA-256 and successful HTTP status. All referenced pregame sources arrived before the saved forecast, which preceded tipoff. Gamelogs were parsed with `nba_sources.player_history`, combined with the same exact-history deduplication, filtered by the four-hour finished-game lag and selected into the same fifteen-game window.

All 20 saved point forecasts, minutes forecasts and probabilities reproduced with the pinned active model. For each grade, the referenced archived ESPN final summary had a complete final box score; the saved game ID, player ID, player name, team and actual statistic matched. No source, identity or grading discrepancy was found. These are ESPN final-boxscore checks; no independent official NBA gamebook verification is claimed.

## Findings

1. **Production rates are slow to adapt across seasons.** Every selected player's fifteen-game input window contains only two or three games from the new season, with twelve or thirteen from the old season. The active opportunity model forecasts minutes separately but uses the pooled stat-per-minute rate across all fifteen games. A current change in assists or scoring opportunity can therefore be heavily diluted. Prior current-season averages already differed materially from several selections; these were knowable pregame inputs, not hindsight statistics.
2. **Minutes explain four of the eleven loss errors; production rate explains seven.** Across 17 distinct players, minutes MAE is 4.359 minutes and actual-minus-expected mean bias is +0.465. The four minutes-dominated losses are Tatum assists (42 actual vs 33.3 forecast minutes), Reed rebounds (26 vs 20.3), Achiuwa points (14 vs 31.2) and Sexton points (14 vs 24.3). This decomposition is descriptive and does not prove a different minutes model would have predicted the changes.
3. **The first game has shared overtime exposure.** Archived ESPN describes BKN at BOS as `Final/OT`. All five selected sides were unders, and four lost. Both Derrick White unders lost. This is correlated game/player exposure, not twenty independent tests of confidence.
4. **Some losses are ordinary large production surprises.** Champagnie played 25 minutes versus 26.8 forecast but made eight threes versus a 2.0 projection; his minutes were not the cause. Duncan Robinson played more minutes than forecast, but his production fell enough to lose his PRA over.
5. **Confidence remains an important diagnostic.** Mean saved confidence was 78.038%, compared with the night's 45% realized decision rate; the Brier score was 0.333468. This single correlated 20-pick night does not by itself estimate true calibration. Ausar Thompson's 100% under probability followed fifteen zero-three games and a zero point estimate, not certainty about his future behavior.

## Examples of known pregame role differences

| Selection | Model projection | Prior new-season average | Actual | Outcome |
|---|---:|---:|---:|---|
| White assists under 5.5 | 3.69 | 6.00 (3 games) | 7 | Loss |
| Ellis assists under 2.5 | 1.75 | 2.67 (3 games) | 4 | Loss |
| Tatum assists under 7.5 | 5.91 | 8.00 (3 games) | 8 | Loss |
| Sabonis assists under 6.5 | 5.19 | 7.00 (3 games) | 8 | Loss |
| Castle assists over 6.5 | 8.08 | 4.67 (3 games) | 5 | Loss |
| Sexton points over 10.5 | 16.38 | 9.00 (2 games) | 9 | Loss |

A simple switch to recent averages is not demonstrated to help: several winning Mamukelashvili and Holiday overs also opposed their tiny new-season samples. Any recency or season weighting must be selected and evaluated on earlier historical periods, with the October 27 outcome kept out of fitting and method selection.

## All saved selections and error decomposition

The components below use the exact pooled prior rate: `actual minus forecast = prior rate × (actual minutes − predicted minutes) + (actual production − prior rate × actual minutes)`. Displayed forecast minutes are rounded to 0.1; tiny residual differences from projection rounding remain in JSON. The larger absolute component labels the dominant forecast miss, not a proven causal explanation.

| Game | Player / market | Side and line | Forecast | Actual | Minutes forecast→actual | Minutes component | Production component | Result |
|---|---|---|---:|---:|---:|---:|---:|---|
| BKN at BOS | Derrick White / pra | under 24.5 | 17.07 | 28 | 28.8→33 | +2.49 | +8.43 | loss |
| BKN at BOS | Derrick White / assists | under 5.5 | 3.69 | 7 | 28.8→33 | +0.54 | +2.77 | loss |
| BKN at BOS | Keon Ellis / assists | under 2.5 | 1.75 | 4 | 24.9→25 | +0.01 | +2.25 | loss |
| BKN at BOS | Keon Ellis / threes | under 1.5 | 1.04 | 0 | 24.9→25 | +0.00 | -1.04 | win |
| BKN at BOS | Jayson Tatum / assists | under 7.5 | 5.91 | 8 | 33.3→42 | +1.55 | +0.54 | loss |
| DET at NYK | Ausar Thompson / threes | under 0.5 | 0.00 | 0 | 24.8→25 | +0.00 | +0.00 | win |
| DET at NYK | John Collins / assists | under 1.5 | 0.93 | 1 | 27.3→27 | -0.01 | +0.09 | win |
| DET at NYK | Paul Reed / rebounds | under 8.5 | 6.42 | 9 | 20.3→26 | +1.81 | +0.76 | loss |
| DET at NYK | Cade Cunningham / points | under 23.5 | 18.51 | 19 | 30.4→36 | +3.41 | -2.92 | win |
| DET at NYK | Duncan Robinson / pra | over 16.5 | 20.37 | 14 | 28.0→33 | +3.64 | -10.00 | loss |
| SAC at SAS | Precious Achiuwa / points | over 10.5 | 14.92 | 4 | 31.2→14 | -8.24 | -2.71 | loss |
| SAC at SAS | Keldon Johnson / points | over 10.5 | 14.33 | 14 | 23.3→22 | -0.80 | +0.49 | win |
| SAC at SAS | Domantas Sabonis / assists | under 6.5 | 5.19 | 8 | 31.3→37 | +0.95 | +1.86 | loss |
| SAC at SAS | Julian Champagnie / threes | under 2.5 | 2.00 | 8 | 26.8→25 | -0.13 | +6.13 | loss |
| SAC at SAS | Stephon Castle / assists | over 6.5 | 8.08 | 5 | 29.2→27 | -0.61 | -2.48 | loss |
| POR at LAL | Sandro Mamukelashvili / threes | over 0.5 | 1.85 | 3 | 22.4→24 | +0.13 | +1.02 | win |
| POR at LAL | Ziaire Williams / points | over 8.5 | 13.82 | 11 | 25.9→29 | +1.65 | -4.46 | win |
| POR at LAL | Collin Sexton / points | over 10.5 | 16.38 | 9 | 24.3→14 | -6.94 | -0.43 | loss |
| POR at LAL | Sandro Mamukelashvili / points | over 7.5 | 12.49 | 17 | 22.4→24 | +0.89 | +3.62 | win |
| POR at LAL | Jrue Holiday / pra | over 16.5 | 24.42 | 29 | 28.9→30 | +0.93 | +3.63 | win |

## Improvement candidates, not claims of proven gains

- Evaluate historically trained shrinkage of recent/season-specific production rates toward longer-term rates, with explicit gaps and changed role/team features. Avoid making a two-game average the sole forecast.
- Include uncertainty in playing time and rate estimation in predictive distributions; a sampled zero mean should not become an assertion of a certain future zero.
- Evaluate correlated-player and all-one-side concentration controls against a predeclared historical selection policy; do not rewrite last night's five picks or drop losing markets based on this night.
- Preserve source timing, fixed fit cutoffs and saved forecast/grade evidence. Match any proposed probabilities to their actual line/selection cohort before claiming calibration.

Detailed source IDs, exact fifteen-game windows, source hashes, per-minute rates, current/previous-season counts and grade provenance are retained in `oct27_2026_error_audit.json`. No provider call, target test, model change, retraining, database mutation or commit was performed by this audit.


## Capture timing and price benchmark

All four games' props were saved in one sourced capture. Their observations
preceded tipoff by approximately 7.98, 8.48, 8.48 and 11.48 hours. The current
policy opens relative to the first game of the slate. This is a potential
availability/rotation freshness limit, not demonstrated as the cause of this
night's losses: the latest game, POR–LAL, actually finished 4–1.

Using the exact opposite-side offer at the same bookmaker and line from that
same raw capture, proportional removal of the bookmaker margin gives a mean
selected-side probability of 55.21%, compared with the model's 78.04%. The
bookmaker benchmark's same-night Brier score is 0.23840 versus the model's
0.33347 (lower is better). This is a descriptive comparison on the same twenty
saved picks; no parameters were fitted and no alternative selections were
created. It does not independently establish calibration of either source.

## Recommended improvement order

1. Validate count probabilities that include uncertainty in the estimated
   production rate. Finite zero histories must not imply certainty. The
   companion confidence audit evaluates a fixed Gamma–Poisson candidate on
   older data and records both its improvements and point-error tradeoffs.
2. Validate recent/season-specific production-rate shrinkage alongside the
   existing minutes model, using earlier periods and the original cutoff.
   Two or three current-season games are insufficient to replace a longer
   history wholesale. Additional prior-season transition data would strengthen
   evaluation of opening-week behavior.
3. Evaluate five-selection concentration controls and capture timing as
   separate predeclared policy changes. Keep five supported markets or wait,
   respect the free-plan request budget, and preserve all earlier locks.
4. Evaluate probability quality on actual available main lines and selection
   cohorts, not just point-forecast errors and synthetic historical thresholds.

The live model remains `nba-research-2.0.1`. This request produced an audit and
historical experiments, with no production parameter or selection-policy
changes, retrospective replacement of picks, or new challenger record.
