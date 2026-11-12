# Offline NBA margin experiment — October 28 audit

No production change was made. Settings were selected on January only; February fit the fixed winner-probability link. October 2026 outcomes were excluded from every fit and selection.

Verified 2,462 sourced team rows / 1,231 complete games. Eligible forecasts: {'initial_2025': 337, 'january': 233, 'february': 168, 'march_april': 338}. Four-hour feature lag and full-game grouping were preserved.

The fixed grid is in [experiment_protocol.md](experiment_protocol.md); exact source hashes, all January grid results, fitted probability links, per-game assessment forecasts, and day-block uncertainty intervals are in [experiment_results.json](experiment_results.json).

January selected **strength_alpha20_halfnone** overall. Family selections: strength `strength_alpha20_halfnone`; shrinkage `baseline_shrink1`. This selects the identity baseline over both proposed shrinkages, and selects no decay over faster recency weighting.

| Model | January margin MAE | Assessment margin MAE | Assessment margin RMSE | Winner Brier, original normal link | Winner Brier, February logistic link |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 11.8311 | 12.1368 | 15.4303 | 0.17912 | 0.18231 |
| strength | 11.5248 | 12.1287 | 15.3306 | 0.17336 | 0.17779 |
| shrinkage | 11.8311 | 12.1368 | 15.4303 | 0.17912 | 0.18231 |

Both probability columns use the same point forecasts. The original column uses SD=11.6 (production rounded baseline probabilities); the logistic column fits each model on February only. February logistic scores are in-sample. No January score from the later-fitted link is published. Only the assessment-period probability comparison is evaluated.

Negative differences below favor the candidate. Intervals use 2,000 paired calendar-day bootstrap resamples with a fixed seed; they describe sampling variability within this single season.

| Candidate | Margin MAE difference, 95% interval | RMSE difference, 95% interval | Raw-normal Brier difference, 95% interval |
| --- | ---: | ---: | ---: |
| strength | [-0.2663, +0.2579] | [-0.3777, +0.1780] | [-0.0144, +0.0033] |
| shrinkage | [+0.0000, +0.0000] | [+0.0000, +0.0000] | [+0.0000, +0.0000] |

The January-selected strength model improved assessment MAE by only 0.0081 point; its day-block interval includes deterioration. This does not establish useful spread-projection improvement. Raw-normal winner Brier improved by 0.00576, but its interval crosses zero; February logistic calibration worsened assessment Brier for both baseline and strength.

Keep the candidate experimental. Obtain additional historical seasons and point-in-time sportsbook lines; then assess offseason transfer and genuine spread forecasts prospectively.

This evaluates opponent-adjusted net-strength estimation rather than the recent-score ridge rejected October 27. It tests a limited available-data hypothesis; it does not estimate individual player availability or resolve stale forecast timing. A smaller winner Brier is not evidence for better spread selection without actual historical market lines.

Limitations:

- March-April labels were already examined in the October 27 alternative study; this is retrospective, not an independent fresh holdout.
- Rolling strength estimates incorporate earlier eligible assessment-game results for later targets, just as baseline histories do; hyperparameters and probability links stay locked.
- Historical source snapshots were received in October 2026; the reconstruction assumes final scores were available four hours after their recorded tipoff, not verified historical publication timestamps.
- One historical regular season; no preceding season available to validate offseason transfer, roster changes, or early-current-season weighting.
- No historical sportsbook lines: no ATS win-rate or profit claim. No October 2026 labels used in any fit, calibration, or selection.
- No possession, injury, lineup, or roster point-in-time inputs. Team-effect ratings cannot correct those omissions.
- No production code, database rows, saved forecasts, grades, or pinned model artifacts changed.

Reproduce with:

```sh
PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python reports/nba/spread_audit_2026_10_28/experiment_strength.py
```
