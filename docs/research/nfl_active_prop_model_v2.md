# Active NFL opportunity model v2

The active prop path uses `nfl-opportunity-empirical-2.0.0` for newly saved
pregame forecasts. This is a direct model upgrade, not a parallel challenger.
Existing selections, quotes, forecasts, model labels and grades remain intact.
The original `nfl_prop_lab.project_week3` remains available for reproducing its
historical output. The worker's existing exactly-five-or-wait policy,
availability checks, source identity controls and five-minute grading continue.

The training cutoff remains **before 2026 Week 3**. No outcomes from the
prospective research record are used to fit, calibrate or select this upgrade.
`PASS` and `RESEARCH_ONLY` remain in every new output.

## What changed

The model augments the existing prior-three/prior-five player, team, opponent,
rest and position features with opportunity changes, recent efficiency and a
volume-times-efficiency projection. All derive from existing sourced prior
statistics; routes, snaps, injuries and quarterback adjustments are not invented.

Four predetermined point models are compared: the existing selected baseline,
ridge with the added features, squared-error gradient boosting, and median-loss
gradient boosting. Early-2024 development selects squared-error boosting for
passing yards and median-loss boosting for the other four markets. Median loss
targets typical absolute error, so lower MAE can coexist with higher RMSE on
unusually large outcomes.

An empirical residual distribution replaces one symmetric market-wide normal
curve. It preserves asymmetric errors and, where calibration supports it,
conditions on predicted volume with shrinkage toward the market distribution.
Residuals come from a separate later historical period. Discrete simulated
outcomes include explicit push probability; receptions and carries cannot
be negative. Half an observation on each tail prevents certainty.

Top-five selection still ranks supported model probabilities. The price edge
remains descriptive. There is no new minimum-edge filter or retrospective change
to which historical props count.

## Chronological evaluation

1. Fit candidate models on 2022–2023; choose a family using 2024 Weeks 1–9 MAE.
2. Refit that family using only data before 2024 Week 10. Collect out-of-sample
   calibration residuals in 2024 Weeks 10–18. Compare global versus conditioned
   residuals using Weeks 10–14 to estimate and Weeks 15–18 to assess them.
3. Fit point models before 2025 and assess predictions throughout 2025, with
   each observation's features restricted to earlier weeks. Reject a point
   change if 2025 MAE regresses and reject empirical probabilities if the
   historical synthetic-threshold Brier score regresses.
4. Fit the accepted point configuration using all training rows strictly before
   the unchanged 2026 Week 3 cutoff. Freeze it before serving new predictions.

The per-market acceptance gates use 2025, so **2025 is acceptance validation**,
not an untouched final test of the deployed combination. The model family and
calibration-method candidates are fixed before that check. Settings are not
tuned against 2026 forward results.

| Market | 2025 rows | Baseline MAE | Active MAE | MAE reduction | Baseline synthetic Brier | Active synthetic Brier |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Passing yards | 591 | 67.4780 | 66.6611 | 1.21% | 0.18397 | 0.18012 |
| Rushing yards | 2,084 | 17.8959 | 16.9573 | 5.24% | 0.18777 | 0.18408 |
| Carries | 2,084 | 2.9396 | 2.8488 | 3.09% | 0.18110 | 0.17363 |
| Receptions | 4,190 | 1.4076 | 1.3632 | 3.16% | 0.19280 | 0.19132 |
| Receiving yards | 4,190 | 18.6962 | 17.7977 | 4.81% | 0.19277 | 0.18750 |

RMSE improves for passing yards and increases slightly for the four median-loss
markets. The complete report records MAE, RMSE, bias, calibration row counts,
candidate scores and acceptance decisions. Baselines in this table use a fixed
fit before 2025; they should not be mixed with the older lab report's weekly
refit metrics.

Synthetic half-integer thresholds are built around the player's previous-three
average with fixed residual-scale offsets. They are **not historical sportsbook
lines**, do not establish top-five hit rate at actual odds, and do not prove
future accuracy or profitability. Actual newly saved forecasts provide the
future assessment.

The final point estimators are refitted on all rows before the fixed cutoff,
while calibration residuals remain frozen from late 2024. Their transfer to a
larger final fit and the 2026 population is an assumption, not independently
verified calibration of that final fitted object. The saved residuals do not
automatically adapt to changing roles, league conditions or forward errors.

## Artifacts and operation

`data/research/nfl_prop_model/v2/manifest.json` stores all configuration,
calibration residuals, training-input hashes, the old validation-input hash,
parameters and evaluation. `models.joblib` contains fitted estimators. Both are
read-only. `edgeterminal/nfl_prop_model_artifact.py` pins the exact manifest
SHA-256 in code; the manifest pins model bytes and source hashes for the actual
feature-construction and inference/probability functions and their constants.
The manifest also records Python, NumPy, scikit-learn and joblib versions for
serialization and numerical provenance. Serving checks implementation hashes
and the version, parameter fingerprint and cutoff **before deserializing**.
The training schedule's byte hash is recorded alongside player-stat input
hashes. It is provenance for fitting, not a requirement that the current live
schedule file never change.

The preactivation `provenance_review.json` records a metadata-only review that
added the feature-code hashes and cached training-schedule hash. The cached
schedule's September 14 modification time predates the October 27 fit. The
fitted model bytes, configuration, evaluation, parameter fingerprint and cutoff
were verified unchanged; no fit or calibration was rerun for these additions.
Its inference-provenance extension records the final metadata checksum after
the runtime probability/point-function pins and dependency versions were added.

`load_artifact()` should be called before provider capture, so a missing or
changed model cannot spend capture quota. `project_current(...)` only loads
the fixed estimators and computes current prior-only features; it never fits
or calls a provider. Quotes older than one hour, future observations or
provider timestamps, past kickoffs and ambiguous player identities are rejected.
Every new forecast and freeze carries model identity, parameter fingerprint,
manifest hash, feature contract and the fixed cutoff.

To verify the installed artifact without network requests:

```sh
.venv/bin/python -m edgeterminal.nfl_prop_model verify
```

The offline `train` action refuses to overwrite an existing frozen version.
Any future fit requires an explicitly reviewed new model version and artifact
directory. It is not scheduled or run by refresh.
