# NBA accuracy review — November 12, 2026

The retained prop model's 557 decided selections won 60.14%, versus a mean
saved probability of 74.02%. This descriptive gap motivates calibration work;
it does not authorize replacing the model with an unvalidated adjustment.

The existing selected-policy calibration protocol was replayed with verified
pregame inputs and later final boxes. Its outer cohort contained 233 decided
selections from 49 games on seven Eastern dates. Only points qualified on the
separate earlier market-choice cohort. On the outer cohort, aggregate Brier
score worsened from 0.238492 to 0.239687; points Brier worsened from 0.218961 to
0.225924. The candidate fails the unchanged promotion gate. No calibration
coefficients, selection rules or original predictions are changed by this
release. The examined outer games must be recorded as consumed by the registry.

## Evidence throughput

Policy replay previously searched every archived final response body separately
for every game. A measured lookup took 5.513 seconds, while the indexed archived
URL lookup returned the same artifact in 0.000306 seconds. The selected response
still passes the complete hash, receipt, event, team, tipoff and reconciled-box
checks. Future receipts remain excluded, and an invalid latest result fails
closed rather than falling back to a favorable older result.

A covering metadata index accelerates the prequote player-history catalog.
Replay and capture now share verified source decoding inside a bounded review
scope. Every reuse checks the applicable receipt cutoff. A subsequent review
verifies the archive bytes again; the cache is discarded on exit and exceptions.
These changes preserve the statistical method and allow the existing role,
opponent/rate and calibration proposals to reach their validation checks sooner.

Regression coverage includes future receipts, wrong final identities, corrupted
archives, repeated verification, earlier receipt limits, fixed top-five replay,
pregame clock crossings, transaction rollback and registry promotion protections.
