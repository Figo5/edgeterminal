# Four model-quality improvements — release review

Date: 2026-10-30. Base: `35f307fe1f6cca72fff37f4d449b4aaa04b8567b`.
Implementation is isolated in `/Users/giofiore/edgeterminal-quality-upgrades`.
Nothing in this release has been committed, installed into the live checkout,
or activated. The Mac's live services and original saved records remain intact.

## Approved scope and result

1. **Player roles and opportunity.** NBA proposals use observed minutes,
   workload changes, volatility and predictive minute uncertainty. NFL proposals
   add prior-week targets/carries, team shares and air-yard signals from archived
   statistics. Missing lineup, snap or route information is never invented.
2. **Actual-line calibration.** Probability maps fit on earlier saved main
   lines; separate inner games choose eligible markets. Fixed-selector NBA
   calibration preserves the exact five, order and sides, including confidence
   below 50%. The calibrator is bound to its frozen selector. Decided-result
   evaluation conditions both models on no push; displayed probability mass
   stays unconditional and preserves the integer push mass.
3. **NBA spread validation.** The richer scorer and exact ESPN-line anchor have
   independent 40/20/20 fit/development/calibration prefixes and an untouched
   outer cohort of at least 60 games on seven Eastern dates. Feature-only,
   incumbent, same-line market reference and blend are compared together. A
   positive convex blend preserves the feature scorer's side against that exact
   line; this alone is not an improvement in the spread win rate.
4. **Selective automatic learning.** Each market must qualify before changing;
   retained markets use their original frozen scorer/configuration exactly.
   The built-in worker attempts the new protocol once, then respects its
   recorded waiting/retry/weekly cadence. Reads never train. No new Codex
   automation or public challenger was added.

## Evidence and operational protection

Full NBA source universes, source-derived roster/history, frozen selector and
original five are now captured together before tipoff, using the existing
guarded provider response. Quote observations and context commit atomically;
failed captures roll back together. The live five reuse those same quote IDs.
The multi-book current-line lookup now checks the selected sportsbook rather
than accidentally requiring a later-inserted different book.

Ranking changes require full common-universe replay of both five-selection
policies. Quotes, final results, retained outputs and both scorer policies are
independently rechecked at activation and nightly verification. Incomplete
pregame contexts remain archived and are excluded by their pretip eligibility.
Unknown selected participation cannot be silently dropped or graded as zero.

All promotion gates remain in place: independent game/date coverage, per-market
nonregression, material Brier improvement and paired uncertainty bounds. New
prop protocols add a seven-date cluster check. Evaluated outer games cannot be
reused, including games in older rejected proposals predating the ledger or
proposals with a renamed family. The spread ledger also consumes evaluated
outer games and dates after accepted or rejected attempts.

Historical NBA replay now recovers the archived scheduled tip rather than
substituting a later actual start. This fixes the opening-night source replay
exception without changing original quotes, forecasts, final data or grades.
Verification caches exist only within one check and never trust data across
runs. Multi-game captures finish other valid targets before surfacing a
per-game integrity error.

The ordinary capture path resamples time immediately before forecast/lock
writes. The broader legacy store API's small validation-to-write interval was
not redesigned in this release; the new full-context transaction has physical
clock checks through its final insert.

## Current evidence does not justify a new active model

An independent SQLite backup and copied source files were used for a bounded
offline replay. Network connections were disabled. No candidate was registered
or activated, no provider request was made, and no live data was written.

| Track | Current outcome |
| --- | --- |
| NBA calibration | Waiting for separate unused calibration, selection and outer groups; 18 available outer games in the snapshot. |
| NBA role scorer | Waiting for 100 future full contexts from the same frozen selector; none existed before this feature. |
| NFL role/calibration | Waiting for a wholly fresh outer window; earlier rejected evaluation games are already spent. Missing earlier actual-line cohorts also produce explicit coverage waits before fitting. |
| NBA spread | 3 qualifying future finals; at least 60 across seven dates are required, plus earlier independent anchor cohorts. |

No accuracy gain is claimed. Once released, the existing worker collects the
necessary evidence and evaluates updates automatically while retaining the
incumbent unless a complete proposal passes.

## Verification and orchestration

Codex coordinated separate NBA props, NFL props and NBA spread agents. Claude
and OpenCode Go reviewed the architecture and actual implementation. Cross-track
read-only review checked NFL and NBA policy replay. Concrete findings were
resolved: source-derived atomic contexts, incomplete-context eligibility,
delayed-tip replay, conditional probabilities, market/selector schema checks,
training-code hashes, global holdout reuse, retained-policy correspondence,
baseline identity, development receipts and nonfinite diagnostic handling.

An OpenCode run that returned orchestration output instead of a code review was
stopped and discarded. The successful replacement used a standalone session
and attached source files. Claude's conditional concern about pre-archive active
models does not apply: the saved registry contains only two rejected legacy
proposals and no activated candidates. Its suggested automatic ranking-change
inference was not applied to calibration-only updates; exact fixed membership
and side preservation are explicitly enforced instead.

Red/green regressions cover the new behaviors. The complete Python suite passed
**1,190 tests** in 37.77 seconds; all **20 JavaScript tests** passed. Compilation
and whitespace checks passed. Final test logs and file hashes
are recorded in `output/model-quality-2026-10-30/`. That directory also contains
the offline replay, protected-byte comparison and a synthetic power study.
The power study is a sensitivity check under stated assumptions, not an estimate
of a model's future performance. A test that assumed the checkout basename was
`edgeterminal` now checks the configured root, allowing isolated worktrees.

Nine protected model/source artifacts compare byte-for-byte with the live
checkout. Model pins, fixed historical cutoffs, existing freeze bytes, PASS and
RESEARCH_ONLY remain intact. No Odds API calls, paid services, new automations,
Windows cutover or state archive are included. Generated/copied runtime data,
reports, databases and review output are excluded from the source commit.

## Pre-commit gate

Proposed commit:
`feat(models): add role forecasts and guarded automatic learning`

The approved four-track plan satisfied the plan gate. The invoked
[`ecc:orch-pipeline`](../../../.codex/plugins/cache/ecc/ecc/2.2.3/skills/orch-pipeline/SKILL.md)
requires: “Present the diff summary and proposed messages; do not commit until
the user confirms.” Commit/release confirmation remains pending. Approval
would release the tested code; it would not bypass any data or model-promotion
gate or rewrite a saved result.
