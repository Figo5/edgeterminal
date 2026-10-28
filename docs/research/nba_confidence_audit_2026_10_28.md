# NBA confidence audit — 2026-10-28

Scope: read-only investigation of the 20 selections saved for October 27 by
`nba-research-2.0.1`. No serving code, parameter artifact, predictions, locks,
grades, model registry or provider settings were changed. No provider calls or
current-season parameter fitting were performed.

## What the saved probabilities claimed

The 20 saved probabilities average **78.038%**, summing to **15.6076 expected
wins**. Actual results were **9 wins / 11 losses (45%)**. Brier score is
**0.333468**, compared with 0.25 for an unconditional 50% forecast on this sample.
Log loss is **0.884672**. These are descriptive scores on four correlated games;
20 selections cannot establish a stable calibration curve or an independent
statistical significance claim.

| Selected market | Wins | Selections | Mean saved confidence |
|---|---:|---:|---:|
| Assists | 1 | 6 | 74.625% |
| Three-pointers made | 3 | 4 | 81.360% |
| Points | 4 | 6 | 78.707% |
| PRA | 1 | 3 | 79.223% |
| Rebounds | 0 | 1 | 77.660% |

The largest selected-market failure was assists. The **100%** Ausar Thompson
under-0.5-threes selection **won**; it exposes a probability-design problem but
was not the cause of an October 27 loss. Correlated player/game predictions must
not be treated as independent trials when explaining the slate result.

## Reproduced finite-history certainty problem

`nba_prop_model.parametric_probabilities` delegates count markets to
`pricing.prob_over_count`. The latter clamps the estimated mean to `1e-6`, then
matches a negative-binomial distribution to that mean and the sample standard
deviation or market floor. At mean 0 and the threes standard-deviation floor
1.3, the under-0.5 probability is **0.9999999999915147**, which is saved rounded to
**1.0**. A zero estimated rate from five to fifteen prior games is treated nearly
as if the true rate were known to be zero. Parameter-estimation uncertainty is
missing.

This is not a negative-binomial arithmetic error. A low mean combined with a
large variance legitimately produces many zeros and rare very large counts.
The modeling problem is imposing a variance floor on a very low estimated mean
without accounting for uncertainty in that estimated rate. For example,
mean 0.1 and SD 1.3 imply under-0.5 probability **98.238%**, while a Poisson law
with the same fixed mean gives **90.484%**. Neither comparison alone identifies
the correct model for NBA outcomes.

Frozen-source historical reconstruction confirms the uncertainty problem:
**533** March–April three-pointer examples had a forecast mean of zero, but
**7** next games produced at least one three-pointer. The zero-event rate was
**98.687%**, rather than certainty. For assists with forecast mean at most 0.25,
**80** cases had mean selected confidence **96.789%** but selected-side win rate
**68.75%**. These are diagnostic slices defined by model inputs, not cuts chosen
from the October 27 outcomes.

Relevant implementation: `nba_prop_model.py:61` (pooled rates and minutes),
`nba_prop_model.py:147` (count probabilities), `pricing.py:134` (moment-matched
count law), and `nba_model.py:651` (four-decimal saved confidence).

## Historical validation does not yet validate the actual selection policy

The fitter's probability score uses a synthetic half-integer threshold near the
player's older baseline (`nba_model_fit.py:108`, `:206`). Its CRPS integrates a
fixed market support. These are useful distribution diagnostics, but they do
not replay real sportsbook main lines or the actual five-per-game ranking.
Live selection ranks the strongest quoted side by model probability
(`nba_model.py:658`), so its high-confidence tail can have different errors.

On the March–April common synthetic thresholds, mean selected confidence was
about **55.3% for points**, **62.9% for assists**, and **68.1% for threes**. Those
numbers are materially different from October 27's mean **78.0%** across the
selected actual lines. The existing historical report cannot establish that a
78% selected-side forecast on those actual main lines is calibrated.

The raw Brier score combines calibration and discrimination, so a lower value
alone is not proof of improved reliability. Separate reliability curves and
market/role/season-start cohorts are needed. See the
[official scikit-learn calibration documentation](https://scikit-learn.org/stable/modules/calibration.html).

## A concrete, historical-only candidate

I tested one fixed exposure-aware Gamma–Poisson predictive law in scratch
scripts. Let `S` be total prior events, `E` total prior minutes and `m` the frozen
forecast minutes. A Jeffreys rate prior gives posterior shape `S + 0.5` and rate
`E`; the future count is negative binomial with shape `S + 0.5` and success
probability `E / (E + m)`. Zero prior events retain nonzero future-event
probability. For equal minutes and 15 zero-event games, under-0.5 is
`sqrt(15/16) ≈ 96.825%`, rather than 100%.

This derivation integrates uncertainty in the event rate. The Gamma–Poisson
posterior-predictive relationship and the distinction between observation and
parameter uncertainty are described in the
[Stan User's Guide](https://mc-stan.org/docs/stan-users-guide/posterior-prediction.html).
The Jeffreys `gamma(0.5, 0)` rate-prior form and exposure update are given in the
[European Commission JRC reliability-data handbook](https://publications.jrc.ec.europa.eu/repository/bitstream/JRC51773/jrc51773.pdf).
Applying that law to player minutes assumes a stable per-minute Poisson process;
this assumption is a hypothesis, not established basketball truth.

All calculations used the active artifact's **417 pinned source artifacts**;
each source body hash was checked, histories were parsed from their exact ESPN
bytes, and `nba_model_fit.examples` enforced the existing cutoff and prior-game
lag. No mutable current-season history or October 27 label was included.

February 15–28 validation uses point/minutes coefficients fitted before February
1. The comparator below is the existing **parametric** law for assists/threes;
no all-February empirical residuals were leaked into this February comparison.
There are **1,319 cases per market**.

| Market | Parametric Brier | Candidate Brier | Parametric CRPS | Candidate CRPS | Parametric log loss | Candidate log loss |
|---|---:|---:|---:|---:|---:|---:|
| assists | 0.250789 | 0.238495 | 0.997094 | 0.981496 | 0.705703 | 0.670165 |
| threes | 0.224049 | 0.219186 | 0.639478 | 0.633150 | 0.689090 | 0.621676 |

March–April retrospective checks compare with the actual deployed per-market
law, including empirical rebounds. There are **6,094 cases per market**.

| Market | Active Brier | Candidate Brier | Active CRPS | Candidate CRPS | Active log loss | Candidate log loss |
|---|---:|---:|---:|---:|---:|---:|
| assists | 0.256206 | 0.240913 | 1.013168 | 0.994504 | 0.721272 | 0.675019 |
| threes | 0.221011 | 0.214480 | 0.635906 | 0.628457 | 0.655557 | 0.608871 |
| rebounds | 0.244794 | 0.243400 | 1.367210 | 1.362376 | 0.682844 | 0.680335 |

There is a tradeoff. The posterior predictive **mean** is higher than the active
point forecast by `0.5 * m / E`; its February point MAE increases slightly:
assists **1.429451 → 1.434192**, threes **0.955683 → 0.965023**. The zero-mean
March–April threes slice also has slightly worse Brier under this candidate
(**0.013133 → 0.013743**). Its probability scores improve overall, but it is not
uniformly better and should not be presented as a validated win-rate fix.

It adds rate-estimation uncertainty but does not fully model changing rotations,
team context or extra game-to-game variance. A role-conditioned or hierarchical
count model may ultimately be better. Replacing every market with the current
empirical CDF is not a good shortcut: it worsens March–April Brier for points,
PRA and threes in this reconstruction.

This is retrospective diagnostic work motivated by the live result. February
and March–April were not newly untouched datasets. No candidate was tuned on
October 27, but future selected actual-line performance remains unproven.

## Prioritized next work

1. Replace finite-history near-certainty with an explicitly versioned predictive
   uncertainty model. The Gamma–Poisson candidate is concrete and promising on
   probability scores, but its point-MAE tradeoff and extra-dispersion limits
   need an explicit acceptance decision. A display-only 99% cap would conceal
   the issue rather than fix predictive uncertainty.
2. Validate probabilities on timestamped actual main lines and the selected
   top-five cohort, stratifying count rate, minutes volatility and start-of-season
   role changes. Keep all October 27 rows untouched and begin a new prospective
   assessment only after the rule/version is frozen.
3. Improve production-rate/context forecasts. Six selected assists resulted in
   one win; shrinking confidence alone cannot infer unexpected player usage.
   Coordinate this with the separate saved-input/minutes audit rather than
   cutting assists based on one losing slate.

Before activation, add regression tests for zero-event finite histories,
nonnegative support, exact integer push mass, monotone half/quarter lines,
probability sums, fitting/serving formula equality, and pre-cutoff/source-hash
invariants. Preserve every original record. A new version and shared-refresh
lock are required for any later live change because serving source is pinned.

## Reproduction artifacts

`reports/nba/confidence_audit_2026_10_28/` contains the read-only scripts and
machine-readable metrics. From the repository root:

```sh
PYTHONPATH=. .venv/bin/python reports/nba/confidence_audit_2026_10_28/audit_brier.py
PYTHONPATH=. .venv/bin/python reports/nba/confidence_audit_2026_10_28/audit_count_crps.py
PYTHONPATH=. .venv/bin/python reports/nba/confidence_audit_2026_10_28/audit_february.py
```

The scripts read source evidence and print/write temporary diagnostic metrics;
they do not call providers, fit coefficients, register models or save forecasts.
The reproduction rechecks exact source hashes. The source artifact's existing
survivorship and historical coverage limits remain applicable.
