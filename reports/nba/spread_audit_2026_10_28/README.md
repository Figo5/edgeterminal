# NBA spread review: October 28, 2026

The saved **3–9 against-the-spread record is correct**. The night showed poor margin forecasts and several selections on small model/market differences. The audit also found a freshness limitation, whose contribution to these losses is not established. There was no grading sign error, and the prop upgrade did not change the game algorithm. No production code, model parameters, forecasts, locks, or grades were changed by this investigation.

## Verified record and forecast errors

The source audit independently matched all 12 picks to their original saved spread lines and verified archived final scores, identities, source hashes, and grade revision hashes. There were zero audit failures. See the [per-game grading ledger](grading_audit.md) and [machine-readable evidence](grading_audit.json).

| Measure | October 28 | October 20–27 | Through October 28 |
| --- | ---: | ---: | ---: |
| Games with saved spread comparisons | 12 | 56 | 68 |
| Spread record | 3–9 | 30–26 | 33–35 |
| Spread accuracy | 25.0% | 53.6% | 48.5% |
| Mean absolute margin error, model | 12.64 points | 11.75 points | 11.90 points |
| Mean absolute margin error, original market line | 11.17 points | 11.43 points | 11.38 points |
| Outright winner accuracy | 8/12 | 35/56 | 43/68 |

Market comparisons use the exact original saved spread, not a later or closing line. Full metrics and all 12 margin comparisons are in [market_diagnostics.json](market_diagnostics.json).

Five losses occurred even though the model identified the eventual outright winner: CLE, DET, PHI, HOU, and DEN. Correctly picking the winner does not determine whether a selected team covers. Cleveland won by 10 against its saved −10.5 line; Charlotte lost by 8 against +7.5. Both were half-point losses. Larger misses included forecasting Atlanta to win by 9.6 when it lost by 12, and Oklahoma City to win by 7.43 when it lost by 24.

All five selections with less than two points of model/market disagreement lost on October 28. Four were under one point, including Cleveland at just 0.10 point. This is a reason to validate an abstention rule, **not evidence that a two-point filter is established**: before this night, the same under-two-point group was 10–8. The current implementation chooses a side for every available spread, regardless of uncertainty or size of disagreement. Ten of October 28's picks were home teams; only 26 of the preceding 56 were home picks, so this night alone does not establish a persistent home-selection bias.

The 68-game spread record does not establish a useful advantage. A descriptive 95% Wilson interval for 33/68 is roughly 37.1%–60.2%; it also assumes independent trials and does not account for shared team or daily effects. These figures are a small research sample, not a profit calculation.

## Freshness and input limitations

All 12 game projections were saved **45.54–47.81 hours before tipoff**. The worker continues checking every five minutes, but `nba_ops.forecasts()` skips an existing game projection and can only append missing market lines. Thus a healthy worker does not mean that an already saved game projection has been recomputed. Original comparison lines were also saved well before tipoff; the report does not relabel later lines as original picks.

Replaying the exact archived inputs reproduces all 12 numeric projections. Twenty games per team were used, with only 2–4 from the current season: 80–90% of each team's input was from the previous season. All 480 selected team rows lacked possessions, so the live fallback uses raw points scored/allowed rather than possession-normalized efficiency. There is no roster, lineup, injury-impact, or opponent-strength feature in this game baseline. These are demonstrated input omissions; this audit cannot quantify how much each caused a particular result. The NBA distinguishes pace from scoring efficiency through possessions and per-100-possession ratings in its [official statistical explanation](https://jr.nba.com/basictraditional-stats-vs-advanced-stats/).

Later archived pretipoff schedules added 18 team-game observations across 18 of the 24 teams, affecting every matchup. The early freezes also missed four back-to-back flags: Detroit, New York, San Antonio, and the Lakers. Rest currently derives from the last scored history row rather than an independently checked schedule.

**Freshness alone would not have rescued the night.** Replaying the same fixed baseline with later available pretipoff team histories, while keeping original spread lines, flips only Cleveland to Washington: a diagnostic 4–8. Mean absolute margin error actually worsens from 12.64 to 13.00 points. These hypothetical results are separate from the real 3–9 record and were not stored as grades. See [the frozen-input replay](inputs_findings.md).

The saved package label is `nba-research-1.1.0`. The later `2.0.1` upgrade changed the prop model; the game algorithm is numerically unchanged (`nba-game-baseline-1.1.0`). Relabeling or loading the later package would not fix these margin errors. Prop adaptive learning currently does not train the spread/game component.

## Tested model alternatives

We locked a small offline protocol before computing candidate metrics: opponent-adjusted team-strength ratings with three penalties and three recency settings, plus two shrinkage settings and the unchanged baseline. January 2026 selected settings; February fit a separate winner-probability calibration. October outcomes entered none of these fits or selections. Chronological ordering follows the [time-series evaluation principle](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split) that training must precede evaluation.

On 338 March–April games, the selected opponent-strength model moved margin MAE from **12.1368 to 12.1287 points**, only **0.0081 point**. The 95% paired day-bootstrap interval spans both improvement and deterioration. Its winner-probability score improved slightly, also with an interval crossing zero; February calibration made that score worse for both models. Development data selected no recency decay and no shrinkage over the baseline. See [the experiment and limitations](experiment_findings.md).

This is insufficient evidence to activate a replacement spread model. The historical archive has one season, no historical sportsbook lines, no historical lineup/possession features, and was retrieved after the historical games. March–April was also examined in the previous October 27 study, so it is a retrospective assessment rather than a fresh holdout. Better winner calibration is not proof of improved spread accuracy.

## Improvements to prioritize

1. **Correct future forecast freshness.** Publish game-day projections and refresh them on a defined pretipoff schedule using append-only revisions with explicit input/source receipts. Preserve the first published forecast and every historical grade. Predetermine which revision and matching market observation will become the scored record for future games; never select the best revision after the result. Check schedule-based rest and completed-game coverage independently. A five-minute worker tick should expose projection age and missing input coverage, not imply fresh predictions automatically.
2. **Build the data needed for a stronger margin model.** Collect multiple historical seasons with point-in-time market lines, possession statistics, roster changes, and verified player availability. Evaluate season-aware priors, opponent strength, and player-impact features on chronological periods that have not already been examined. More weight on a few new-season games is a hypothesis to test, not a change justified by these 12 outcomes.
3. **Validate when to abstain.** Keep showing the game projection, but only label a spread selection when a predeclared, separately tested uncertainty/advantage rule passes. Do not choose a two-point threshold because all five such picks lost yesterday. Evaluate accuracy, coverage, calibration, and performance versus the original available market across multiple dates before enabling it.

A minor metadata issue also surfaced: ten archived sportsbook labels say `Unknown`, although their raw offers identify DraftKings. Original lines and prices still match exactly; this is not the reason for 3–9.

## Reproduction and scope

Three independent audit tracks covered grading, frozen inputs, and model evaluation. Parent reconciliation checked the cross-report counts and produced the saved-market diagnostic. All scripts open SQLite read-only and write only report artifacts in this directory. No provider requests or Odds API calls were made; PASS / RESEARCH_ONLY and existing pinned model artifacts remain intact.

```sh
.venv/bin/python reports/nba/spread_audit_2026_10_28/grading_audit.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python reports/nba/spread_audit_2026_10_28/inputs_audit.py
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python reports/nba/spread_audit_2026_10_28/experiment_strength.py
.venv/bin/python reports/nba/spread_audit_2026_10_28/market_diagnostics.py
```
