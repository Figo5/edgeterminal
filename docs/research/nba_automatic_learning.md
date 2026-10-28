# NBA automatic learning

`nba_learning.py` serves one registry-approved artifact, or the unchanged
`nba-research-2.0.1` baseline when no adaptive artifact has passed validation.
It never fits during an HTTP request or a prop capture. `snapshot()` keeps a
capture's selection, point projections and saved headers on the same version.

The built-in worker can call `nightly_diagnostics(now)` and
`build_candidate(now)` separately. Neither function fetches provider data,
activates a model, changes an existing prediction, or writes grades. The common
learning controller records attempts and the append-only registry owns promotion
and rollback. Scheduled attempts remain subject to the normal refresh lock.

## Predeclared candidate method

Candidate minutes are a ridge fit using the existing nine minute features.
Production combines the 15-game per-minute rate with the most recent five
current-season games. The recent contribution shrinks by `n / (n + 5)` when
few current-season games exist. A per-market weight is chosen from the fixed
grid `0, .25, .5, .75` on development-game MAE; no weight is chosen from the
actual-line validation outcomes.

Rebounds, assists and threes integrate finite rate uncertainty through a
Gamma–Poisson predictive distribution with shape `weighted_events + .5`,
observed minute exposure, and fitted future minutes. Its mean is also the
reported point prediction, so a probability improvement cannot conceal a
different point forecast. Points and PRA use standardized residual calibration
with nonnegative support. Integer lines retain explicit push mass. This is a
candidate model assumption; it is used live only after the promotion gate passes.

## Data and chronological separation

- The expanding coefficient-fit prefix is followed by 120 whole development
  games and 120 whole residual-calibration games. Development counts as
  parameter fitting in the training catalog.
- The outer validation uses the latest 40 fully graded locked games, with the
  same real quoted side, book, line and frozen pregame player inputs for both
  models. All five grades must be terminal before a game enters.
- Training snapshots must already have been received before the earliest
  validation quote observation. A four-hour prior-game lag excludes unfinished
  and future labels. Conflicting player/game rows are discarded.
- Training, calibration and outer validation game IDs are disjoint. Prediction
  inputs for later validation games may include earlier completed games,
  matching the daily serving rule, without refitting any coefficients.
- Source bodies, source hosts, receipt times, forecast payloads and selection
  lock hashes are checked. Each held-out quote is reconstructed from its exact
  source payload and capture roster; actuals must match the final ESPN box.
- Voids and pushes never become decision outcomes. Excluded void evidence,
  including official participation PDFs, is checked and retained in the source
  catalog. Missing or conflicting evidence fails the attempt.

The JSON artifact records the fitted coefficients, method, development scores,
calibration residuals, game catalogs, label limits, source digests and serving /
training source-code hashes. A version identifies the complete artifact,
evaluation, source receipts and comparator. Identical retries are idempotent;
an official correction changes proposal identity. An adaptive active model must
have a cutoff preceding the proposed outer holdout, and another attempt requires
at least ten new held-out games.

The registry independently checks sourced quote/grade identities and recomputes
paired MAE, Brier score and log loss. Promotion needs sufficient whole-game and
per-market coverage, no per-market regression, at least `.002` overall Brier
improvement and a negative upper 95% bound on the paired game-level Brier
difference. These controls do not guarantee future improvement.

## First real attempt — October 28, 2026

The first candidate was evaluated on **197 decision props from 40 games** and
104 players. Three void outcomes were excluded. There were at least 20
decision rows in every market. Validation used October 23–27 Eastern games;
the latest kickoff was October 28 at 03:00 UTC.

| Market | Rows | Baseline MAE | Candidate MAE | Baseline Brier | Candidate Brier |
|---|---:|---:|---:|---:|---:|
| Points | 53 | 5.8918 | 5.8087 | .301172 | .311067 |
| Rebounds | 26 | 2.2230 | 2.2231 | .283414 | .300226 |
| Assists | 37 | 2.1326 | 2.0198 | .312858 | .302255 |
| Threes | 50 | 1.1152 | 1.1116 | .268397 | .269107 |
| PRA | 31 | 10.4081 | 10.4755 | .354288 | .344328 |

Aggregate Brier worsened from **.301063 to .302565**. Several market metrics
regressed, and the upper game-level Brier-difference bound was **+.008757**.
The candidate therefore **failed promotion**. The live baseline remains
`nba-research-2.0.1`; no adaptive projection was enabled and no parameters were
tuned in response to this holdout result.

This initial check is retrospective, including games examined during the
earlier confidence audit. It does not establish independent forward success.
Later scheduled builds follow the same declared method and shifted windows.
The full diagnostics and gate are retained in
[`learning_initial_2026_10_28.json`](../../reports/nba/learning_initial_2026_10_28.json).
All 233 saved forecasts, 56 selection locks and 289 grade revisions were
unchanged by the read-only fitting/evaluation attempt.

The targeted tests cover baseline serving parity, finite-zero uncertainty and
integer pushes, season-transition input adaptation, all real quote sides/books,
five-or-wait behavior, source integrity, chronological fitting separation,
calibration poisoning, past-game protection, capture-version snapshots and
correction-safe proposal identities. The combined adaptive and baseline run
passed 48 tests.
