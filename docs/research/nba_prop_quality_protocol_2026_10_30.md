# NBA prop quality protocol — 2026-10-30

Protocol: `NBA propsquality2.0`. This implementation prepares proposals; it does
not activate a model or claim an improvement from fitting alone. The original
NBA point model, prop model, pricing implementation, saved forecasts, and grades
remain unchanged.

## Two explicitly different proposal paths

`nba-selected-policy-calibration-v2` preserves the incumbent's exact raw top-five
membership, order, side, and point forecast. Earlier actual main lines fit a
regularized probability map; separate earlier whole dates choose which markets
may update. Unchanged markets dispatch their frozen parameters exactly.
Calibration occurs after selection, including when corrected confidence drops
below 50%. The calibrator is bound to the retained selector's hash. Sparse
markets use an identity map. Original residual/development catalogs and prior
calibration provenance remain attached to subsequent proposals.

`nba-role-policy-v2` uses observed minute variability, recent workload changes,
and current-season support as role proxies. It makes no assumed lineup or
injury adjustment. Coefficients fit on an earlier historical block; a later
block estimates minute residual uncertainty. Count probabilities integrate a
fixed five-point predictive minute mixture and rate uncertainty. Empirical
points/PRA residuals already include workload variation and are not widened a
second time. Separate whole games choose role markets and calibrate the new
selected policy before an untouched outer cohort is scored.

## Evidence required for a ranking change

Future provider captures save every eligible fresh available main offer, both
current roster sources, available player-history sources, source receipts and
hashes, and the frozen incumbent selector and original five IDs. Selected quotes
reuse the contemporaneously saved observations. No extra provider request is
made. Captures and quote rows are append-only and must be physically written
before tipoff in a single transaction. Inputs are reconstructed from the named
source bodies, rather than trusted from mutable cache values. Captures lacking five eligible incumbent selections remain
archived but are explicitly excluded using their pretip eligibility.

Old selected-five records can support fixed-membership calibration. They cannot
prove a new ranking policy. The role path waits for at least 100 full captures
from the same selector, with sufficient dates for separate earlier 30-game
market-choice and 30-game calibration blocks and at least 40 later games on
seven Eastern dates. It replays both complete five-selection policies against
the same full offer universe. An unknown selected participation/stat blocks the
whole proposal; it is never dropped to improve a score or treated as zero.

Archived bodies, roster/quote identity, freshness, frozen selector replay,
candidate membership and fields, and verified final-box labels are checked again
by the registry. Delayed official actual start times do not reinterpret the
schedule or feature cutoff that an original capture used.

Registry verification may reuse a context/final proof inside one
`validation_scope()` call. It rechecks receipt limits on each use and discards
the cache when the call finishes. There is no trusted cache across runs.

The existing forecast/selection-lock store APIs retain their supplied-clock
contract. This refresh resamples immediately before each call and the full
context transaction checks the physical clock under its write lock. A small
remaining interval between the legacy lock API's validation and final insert is
not eliminated here; changing the shared store's clock contract is deferred.
No past game is enrolled or backfilled by this feature.

## Evaluation and promotion

Decided-outcome Brier and log-loss use
`conditional_side_given_no_push`: side probability divided by one minus push
probability. Live displayed probabilities retain their actual unconditional
side and integer push mass.

Market choice uses a disjoint inner cohort. Whole-game/date holdouts are checked
for availability before outer forecasts are computed. Registry promotion still
requires its coverage, per-market nonregression, material probability gain,
game/date uncertainty bounds, and full-policy protections. Rejected outer
cohorts remain consumed. Serving never fits, uses results to select a revision,
reduces the supported five-prop coverage, or changes `PASS` / `RESEARCH_ONLY`.

## Verification

Focused regressions cover source and clock failures, complete-offer replay,
retained numerical equality, fixed membership below 50% confidence, conditional
push handling, whole-date partitions, unknown participation, consumed holdouts,
reachable role proposals, and invariance of fitting/market choices when only
outer labels are changed. All tests use isolated fixtures; no provider requests,
live database mutations, service changes, or activation are part of this lane.
