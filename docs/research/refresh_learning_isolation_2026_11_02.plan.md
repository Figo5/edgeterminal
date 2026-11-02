# Refresh isolation and NBA participation — proposed plan

Prepared November 2, 2026. Base: `d6392d701c83d48f32e7a2b8224cfc26af649a66`.
Worktree: `/Users/giofiore/edgeterminal-refresh-isolation`.
The running checkout and its services remain unchanged during preparation.

## Findings

The ordinary refresh holds its shared research lock while calling both prop
learning and NBA spread learning. Diagnostics, fitting and verification can
therefore delay the next source/grading check. Model activation and predecessor
verification also perform expensive work inside a SQLite write transaction.

The previously reported two-hour learning duration needs correction: learning
rows share the outer refresh's start timestamp. The approximately 1h57m interval
is whole-cycle wall time, not measured fitting/CPU time. The sequential NBA
retuning window was approximately 7m33s; actual job/phase timings are missing.

All four pending NBA props have now been traced to correctly matching archived
rosters and quote events. Three official NBA gamebooks explicitly list the
three players inactive and match the saved game dates, teams and final scores:

| Date | Game | Selected player | Pending props | Existing-rule outcome |
|---|---|---|---:|---|
| October 29 | MEM at POR, 124–103 | Robert Williams III | 1 | Void with verified inactivity evidence |
| October 30 | NYK at PHI, 93–98 | Jaylen Brown | 2 | Void with verified inactivity evidence |
| October 30 | LAL at GSW, 95–112 | Ziaire Williams | 1 | Void with verified inactivity evidence |

The existing validator accepts the Brown and Ziaire proof in a read-only
preview. Robert's proof exposes a parser defect: both the score and inactive
section parser shorten Portland's nickname to `Blazers`, while the gamebook
prints `Trail Blazers`. All three final pages have been visually reviewed.
No sources have yet been inserted into the live evidence ledger or grades.

Official sources:

- https://statsdmz.nba.com/pdfs/20261029/20261029_MEMPOR.pdf
- https://statsdmz.nba.com/pdfs/20261030/20261030_NYKPHI_book.pdf
- https://statsdmz.nba.com/pdfs/20261030/20261030_LALGSW.pdf

Private PDF bytes, rendered first pages, exact source receipts and read-only
validator evidence are under `output/pending-nba-2026-11-02/`.

## Proposed implementation

1. Add one dedicated automatic learning process with its own OS lock, durable
   job state, interruption recovery and measured per-job/phase timing. Duplicate
   processes must never fit concurrently because of a stale heartbeat alone.
2. Keep the five-minute worker responsible for source capture, grading, bounded
   historical collection and cheap active-model checks. Heavy diagnostics and
   prop/spread fitting run outside its lock, with limited numerical threads and
   background priority, without provider requests or credentials.
3. Move expensive source/evaluation verification outside SQLite write
   transactions. Publish under a short research lock and transaction only after
   checking the same relevant result revisions, game/forecast identities,
   baseline event, candidate/code hashes and holdout ownership. Reject stale
   evaluated labels, including labels already superseded before verification.
   Preserve consumed holdouts, idempotency, rollback safety and all model gates.
4. Integrate a separate Mac LaunchAgent and prepared always-on runtime/Compose
   learning role. Keep queued/running/waiting/failed/interrupted status visible;
   a healthy source tick must not conceal an unresolved learning failure. Do not
   let the ordinary runtime's 900-second timeout terminate the learner.
5. Correct canonical multiword NBA nickname parsing. Add a bounded automatic
   official gamebook fallback for selected players absent from reconciled final
   boxes: exact trusted URL variants, game/date/score and frozen-roster checks,
   explicit inactive proof, limited size/time/retries, and append-only evidence.
   Identify machine-verified evidence honestly; do not label it a human review.
   Unsupported or contradictory cases stay pending and visible.
6. After reviewed release, archive the three already verified gamebooks using
   their actual receipt metadata and let the normal grader append four voids
   under the existing inactive rule. Keep original selections and prior grades.
   Verify the future automatic fallback through offline fixtures, not extra
   SportsGameOdds calls. If scoped, correct the `Day-To-Day` display label
   without changing selection eligibility.
7. Run meaningful offline regressions and independent code/security review,
   present the exact diff and obtain commit/release confirmation before changing
   the live checkout. No model parameters, training cutoffs, selection membership,
   quotas, grading rules, official-pick settings or Windows cutover are changed.

## Required verification

- A blocked fit leaves the research lock and SQLite writer available for a
  separate refresh/grading process; source cadence continues.
- Duplicate learners, interruption, retry and timeouts recover without duplicate
  fitting, activation or holdout reuse.
- Corrections, identity/baseline/code changes and publication lock contention
  prevent stale activation, with visible recovery status.
- No long-lived read transaction pins the WAL during fitting.
- Learning errors and stale/interrupted jobs remain distinguishable from healthy
  source ticks. Accurate wall/monotonic/CPU timings replace shared-start claims.
- The learner cannot fetch providers; ordinary quota/cadence controls remain.
- Portland gamebook parsing succeeds only for exact matching final proof.
- Missing, malformed, ambiguous, wrong-date/team/score, tampered, future-receipt
  and contradictory PDFs never create a grade. Repeated valid proof is idempotent.
- The four actual frozen selections remain unchanged and preview as four voids
  after evidence validation; their wins/losses do not change.
- Local service and prepared always-on lifecycle tests cover the separate
  learner. Future migration instructions must stop this additional writer before
  a final state archive; the archive tool remains unchanged.

## Approval gates

This document is a proposal, not completed implementation. `ecc:orch-fix-defect`
delegates to `ecc:orch-pipeline`: Gate 1 requires plan approval before production
implementation, and Gate 2 requires confirmation before committing. Live
services and their five-minute schedule continue while the work is prepared.
