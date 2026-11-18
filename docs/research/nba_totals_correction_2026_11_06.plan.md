# NBA totals historical correction repair — implementation plan

Scoped defect repair at base `0a23eefba02a8fe4829120c6f2227afda457bba2`. Work only in `/Users/giofiore/edgeterminal-nba-totals-corrections`, branch `fix/nba-totals-corrections`. No implementation or regression-test changes have started. The live app and both automatic workers remain running.

## Confirmed failure

The historical fitting adapter rejects any difference between archived final-box possession features for the same event. For ESPN HOU–DAL event `401909840`, source `2964` reports Dallas shooting `41-79`; later source `7946` reports `41-80`, with unchanged scores and reliable duration. The derived full-game possession estimate changes from `100.72` to `101.22`. Both original bodies and receipts verify. The existing learner repeatedly stops with `Totals historical box correction conflict`.

Actual source/stat evidence and operational evidence were recorded at planning
time. The latest genuine source/grading tick succeeded with no source warnings;
this failure is in the separate offline totals learner, not grading or live
capture.

## Ordered implementation tasks after confirmation

1. **Reproduce, then repair the offline evidence boundary.** Write failing cutoff/correction and availability regressions first. Resolve one coherent verified possession-stat version per canonical event, selected by actual receipt at or before the fixed reconstruction cutoff. Preserve selected, superseded and duplicate source identities in the sealed fitting evidence. Carry actual possession/duration receipts and a separate per-example feature-evidence maximum, retaining the true label receipt. Strengthen the totals-only chronological phase check to cover selected feature evidence as well as labels; return the existing truthful history-waiting state if separation cannot be established. Do not redesign phase partitioning, change sample floors, numerical parameters or activation gates.
2. **Verify the real failure and preservation, then review.** Cover the integrity matrix below and run the actual reconstruction/conversion adapters against owned copies of the relevant public archive, including the actual HOU–DAL versions and before/at/after-cutoff cases. Use final frozen source, deny network and writes, and repeat after relocation. No real model fitting or activation. Run appropriate totals/worker/publication/forecast/grade/pinned-model/UI regressions, obtain code/security review, and present the exact reviewed diff for separate commit/release confirmation.

## Frozen evidence-resolution rules

- Fitting reconstruction only. Saved serving maps continue to name and replay their original source IDs. No stored source, receipt, forecast, selection, freeze, grade, model, reservation or evaluation bytes are rewritten or resealed.
- Discover only eligible receipts actually received at or before the cutoff. Future evidence cannot affect the result, its provenance or an error at an earlier cutoff. Select by receipt time; immutable IDs identify evidence and only break ties between semantically identical receipts. Receipt/ID inversion alone is not a new rejection rule.
- Verify each relevant candidate's raw hash, size, duplicate-free JSON, exact HTTPS route/query, kind/status, canonical event/season/teams/orientation/time/final scores, and available duration structure **before** classifying advanced stats as missing. Invalid or contradictory evidence cannot be hidden by a completeness prefilter or a newer receipt.
- Only same-event statistical corrections with unchanged score and unchanged proven duration/method are eligible. Keep both teams' possessions, duration and efficiency coherent from the same selected box. Do not accept score, identity, regulation/OT, known/unknown duration or method changes in this repair. The existing five-period-to-four-period rejection remains unchanged.
- Reject conflicting semantics at any equal receipt timestamp, regardless of insertion order or a later otherwise valid correction. Equal semantics at the winning timestamp select the smallest immutable ID. Preserve all examined eligible descriptors, original hashes/receipts, and an explicit selected/superseded/duplicate resolution policy in the fitting source seal.
- If no complete advanced evidence ever existed, preserve score-only coverage. A newest incomplete receipt after complete evidence fails visibly; it cannot silently resurrect older advanced inputs. This conservative choice resolves the reviews' optional-missingness question.
- Never attach an original score receipt to a changed later feature value. Keep selected component receipts distinct from label receipts, verify them against the reconstruction cutoff and applicable phase boundaries, and seal their availability evidence. Insufficient chronology waits; no backdating or weakened gate is allowed. Existing historical reconstruction assumptions remain explicit and cannot become prospective pregame evidence.
- Keep archive work in the offline learner, outside capture/grading and writer/publication locks. Reuse existing source verifiers and deadline handling, precompute event metadata where useful, and measure read/memory costs. No arbitrary latest-N truncation, skipped corruption, new feed, request demonstration, dependency, quota override or persistent cache is authorized.

## Required verification cases

Before/at/after correction cutoff; three-version chains and reordered insertion; equal-clock conflicts/equivalents; corruption of old or latest bodies; wrong route/event/team/score/time; changed or missing duration; newest incomplete regression and never-complete fallback; original saved map replay; honest component/label receipts; late feature evidence crossing either chronological phase boundary; no target/future leakage; source-seal sensitivity; deterministic owned-archive relocation; actual final adapter completion without this conflict; unchanged numerical gates, protected files and historical ledgers.

Successful reconstruction does not establish model readiness or better accuracy. The current numerical model remains live; later candidates still require all chronological training conditions and the sealed unused prospective 60-game/seven-date gate. All already inspected outcomes, including November 5, remain development evidence. No forced fit, grade or activation clears a status warning.

## Planning review and qualifications

[Delegated planner](nba_totals_correction_2026_11_06.planner.md) · [Safety review](nba_totals_correction_2026_11_06.safety-plan.md) · [Automated planning reviews](nba_totals_correction_2026_11_06.external-plan.md).

Both reviews gave conditional plans. This consolidated plan resolves their eligibility, missingness, duration, correction-chain and provenance questions explicitly. The safety review and both automated reviews identified the additional feature-receipt chronology gap; the necessary totals-only guard is included openly. The broader safety suggestion to accept changed scores/durations is declined to keep this repair narrow. One review's suggested rejection based solely on receipt/ID ordering is not adopted; actual receipt cutoff, verified content and deterministic provenance remain authoritative. The other review's description of current conflicting equal-clock handling is not used as evidence; the current implementation already rejects changed semantics.

## Review and operational limits

Plan confirmation is required before code is written. Confirmation starts
isolated TDD and review. Commit/release separately requires confirmation before
the exact reviewed repair is released.

Keep the Mac services and their automatic cadence running. No live checkout edits during development, manual provider demonstrations, changed quotas, forced grades, model tuning against inspected results, official picks, additional automation, Windows cutover, credential transfer, symlink conversion or final state archive. This Mac must still be awake and online.
