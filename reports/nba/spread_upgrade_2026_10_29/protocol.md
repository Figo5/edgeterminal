# NBA game-component upgrade: locked retrospective experiment

Written before calculating candidate metrics. This does not permit activation.

- Archive all 30 ESPN team schedules for season-ending years 2024, 2025, and 2026. Verify exact response bytes, requested team/season, owner-only history, pair identity, and contradictory duplicates.
- Fit the production candidate method on score labels before October 1, 2025: expanding prefix for coefficients, next 120 whole games for choosing one of ridge penalties 1/10/100 on margin MAE, final 120 whole games for residual standard deviation only.
- Baseline is unchanged raw-score scoring with fixed existing home court and rest constants. Candidate learns a residual from the shared six-feature game transform: baseline margin, recent net-score difference, season-shrunk net-score difference, historical opponent-strength difference, optional possession net difference and coverage. Missing possession features stay missing/zero with their coverage flag. No injury point adjustment or October 2026 labels enter this experiment.
- Assess the complete 2025–26 regular season, including early-season transfer. Report margin MAE/RMSE, winner Brier, and early/late-season slices. The 2026 March–April labels were previously examined, so this is retrospective, not a fresh independent holdout.
- Historical response receipts are October 2026. Reconstructed historical features assume eligible final scores became available four hours after tipoff; receipts are never backdated or described as historically recorded.
- There are no historical sportsbook lines. No historical ATS/profit claim or activation can follow from this experiment. The live registry separately requires at least 60 actual new-policy saved forecast/line/final pairs across seven dates, chronological training receipts, material margin and cover-Brier improvement, paired day-block intervals, no ATS count regression, and complete supported-game coverage.
- No production DB mutation, old forecast revision, or registered artifact replacement occurs in the experiment.
