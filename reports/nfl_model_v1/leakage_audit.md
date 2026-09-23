# NFL v1 leakage audit

## Controls present

- `team_features` admits only regular-season games with `week < target_week`.
- `baselines_before` admits only the previous season and earlier weeks of the
  target season. Same-week games are excluded regardless of replay order.
- Replay rejects a same-season or future-season `prior_season` value.
- The CLI loads prior-season PBP when a prior season is requested.
- A provider game ID maps to one canonical internal game; ambiguous matches are
  refused. Historical predictions and their input snapshots are append-only.
- Numeric NFL injury, weather and rest adjustments are disabled pending a
  chronological feature evaluation.
- Neutral-site home-field adjustment is zero in replay and live projections
  when the schedule provider flags the game. Historical ties do not enter
  binary win-probability or calibration metrics.
- The 2023-2025 historical replays are retrospective development diagnostics.
  Model settings were selected after these seasons and after an earlier 2025
  replay. They cannot serve as an untouched final validation set.

## Remaining blockers

- The live model's ESPN team stats and historical nflverse replay feature path
  are similar but not identical. Publication timestamps for every live stat
  value need explicit capture before replay can claim full production parity.
- Historical injury and forecast feeds are not captured at pregame timestamps.
- The nflverse reference market lines are treated as closing comparators, but
  their point-in-time provenance has not been validated for CLV or achievable
  ROI. Recorded-price returns are diagnostic only.
- Model coefficients and score-distribution scales have not been selected on a
  clean training period with an untouched final evaluation season.
- No forward shadow predictions have been graded.
- Week labels, rather than each game's kickoff timestamp, enforce feature
  cutoffs. This was checked for the 2025 schedule, but a general reschedule
  guard remains to be implemented.

**Verdict:** research-only. Do not use these metrics to issue official picks.
