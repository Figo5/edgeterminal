# NBA totals planning reviews — 2026-11-05

Phase 1/2 planning only, based on isolated checkout `0082fa8f672bed0fbb6da09ff39fedf49ded5a41`. No implementation, fitting, tests, provider sports-data requests, model activation or changes to the running app occurred. The inspected outcomes described in the audit are development evidence, not an untouched validation cohort.

## Review evidence

The source packet contains numbered excerpts from `nba_model.py`, `nba_spread_model.py`, `nba_game_forecasts.py`, `nba_spread_learning.py`, `nba_ops.py`, and `learning_worker.py`, with source SHA-256 values. No database, `.env`, secrets, or live operational data were supplied. Both reviews received the same architecture constraints with distinct modeling versus integration focus, with tools disabled. Their records were retained with the planning evidence.

## Integration observations

1. The current spread wrapper deliberately strips possessions and retains the arithmetic total. Its learner and `validation_snapshots()` evaluate spread-specific labels/lines. Totals therefore needs separate pure scoring and evaluation modules, a separate candidate registry/consumption family, and a totals-specific snapshot reader. Spread-only filters must not reduce totals validation coverage.
2. Integrate a totals result after existing margin inference. Preserve `home_margin`, winner/spread probabilities and all spread model fields exactly. Update total and the two projected scores by the same half-delta, with reconciliation tolerance and totals-specific uncertainty/probabilities. Retain explicit spread and totals component identities; a composite revision fingerprint must include both component artifacts and their exact code hashes. Do not relabel old forecasts or overwrite a component fingerprint with another model’s identity.
3. Capture each component’s exact selected row set as well as a canonical source-bound union for revision validation. A single undifferentiated union is insufficient to prove which rows produced each prediction. Do not let wider totals histories silently change spread replay windows. Freeze every row, source ID/hash/receipt, feature/quote cutoff, availability context, schedule, possession derivation and overtime-normalization method required to reproduce the result. A corrected/retrieved source cannot be backdated into past as-of features.
4. Keep baseline support as a hard invariant. Every game the incumbent can score must receive a finite totals forecast and pick when a saved line exists. The dedicated model must have validated season-aware scoring fallback for unavailable pace/OT/availability evidence and record its selected regime and reason. Missing possession rows are missing; no inferred numerical values, final DNP availability proxies, or opaque mean filling.
5. Queue a distinct `NBA_TOTALS` component through existing offline worker isolation. Serving/capture/grading may enqueue and report status; they must never fit, do expensive replay, fetch sports-provider evidence for fitting, or block grading. Failed or waiting totals jobs keep the incumbent and complete game coverage.
6. Pre-register the finite candidate family, chronological date boundaries, support rules and decision criteria using earlier data. Separate fit/development/calibration from later untouched future dates. Freeze candidate and incumbent predictions before validation labels become available; bind feature and label receipts to cohort boundaries. Seal game/date consumption atomically before inspecting results, including failed proposals. Retrying unchanged artifacts may resume publication but cannot spend an evaluated cohort on a tuned candidate. Evidence corrections invalidate publication proofs.
7. Meaningful verification before implementation includes exact margin/winner/prop/NFL invariance, complete supported-game totals/fallback coverage, source tampering and late receipts, OT eligibility, deterministic replay, no post-tipoff writes, no mutation of historical forecasts/grades/artifacts, whole-date disjointness, one-time holdout consumption, offline queue isolation and publication/resume failures. These are proposed checks; none have been run during planning.

## Review outcomes

Both reviews completed. Full review text and process records were retained with the planning evidence. These are planning reviews, not implementations or validation results.

### Statistical architecture feedback

One review recommends a finite ladder: frozen incumbent, season-aware scoring/shrinkage, pace × efficiency with evidence-bound partial pooling, then opponent adjustment only if earlier development supports it. Candidate hyperparameters (decay, prior-season discount, league shrinkage, possession coverage floor, rest treatment and residual family) belong solely to fit/development selection. Calibration must use a later distinct earlier-data cohort.

It distinguishes full-game including-OT labels from regulation-normalized pace. Verified possession rows can inform efficiency; pace per 48 requires verified minutes/period counts. Unknown minutes cannot silently mean 48. Any full-game overtime contribution must be estimated on earlier data and validated, not assigned from target-game eventual OT.

Its strongest operational finding is seal-before-evaluate: a totals implementation must not copy the current spread ordering where metrics are computed before `_register_prepared` inserts consumption. Failure/error paths must not make evaluated outcomes reusable. A source correction changes the proposal evidence identity and invalidates publication while the used holdout remains spent.

Its suggested sample threshold (120 games/14 dates), bootstrap/bias/interval gates and proposal cap are recommendations to decide and freeze before the future cohort, not approved numeric requirements. The review explicitly recognizes the current component's six completed dates and missing surrounding modules as limitations.

### Integration feedback

The other review agrees on an additive totals component, totals-specific reader without spread requirements, queue-only hooks into a separate `NBA_TOTALS` offline worker action, composite component provenance, append-only model/validation/publication state and explicit low-possession scoring fallback. It highlights the placement of composition before `_refresh_game_projection` removes selected inputs, so the saved manifest includes every totals source binding.

It notices that the pinned baseline's duplicate signature contains possessions: absent versus later enriched possession data must be resolved in the new totals assembler before entering that scorer, or an enrichment can taint a game and reduce support. This does not authorize ignoring conflicting verified possession estimates; exact source receipts and a deterministic correction/conflict policy remain mandatory. The pinned prop/margin scorer is not edited.

It proposes totals-only gates on paired mean error/MAE, actual saved-line comparisons, complete support and calibrated uncertainty, but uses the spread minimums (60/7) as a proposed starting point. It recommends direct tests for total-without-spread reader support, late receipts, source mutation, complete fallback coverage, output invariance, independent consumption and evidence correction.

### Reconciled recommendations and rejected assumptions

- **Keep:** separate totals model/data/learner and registry; evidence-bound pace and season treatment; deterministic all-supported-game fallback; independent uncertainty; component-level frozen inputs; source receipts; seal-before-evaluate; proposal/correction integrity; exact output invariance outside totals.
- **Do not adopt a same-line convex market anchor as a way to improve pick direction.** It preserves the over/under side against that line. It may be an explicitly labeled numeric benchmark, not proof of better pick decisions or a substitute for a dedicated pace model.
- **Do not silently weaken task acceptance criteria to report-only pick and market recommendations.** The main plan must explicitly predeclare candidate/incumbent/saved-line comparisons and actual-line over/under decision criteria. Any threshold or statistical power tradeoff requires a clear plan decision before validation labels are inspected.
- **Do not copy a spread row-count threshold as a statistical justification.** Neither 60/7 nor 120/14 was independently powered here. Plan sample size and guardrails before the future holdout; do not claim significance from this review.
- **Do not accept the claim that grade payloads lack a totals result.** The supplied excerpts do not establish that claim. Preserve the grade schema; the totals reader must verify/recompute the total outcome from the saved line and verified final regardless of any convenience field.
- **Do not treat a possession-enrichment difference as permission to ignore contradictions.** Absent-versus-verified enrichment can be resolved from source evidence; conflicting verified numeric values need an explicit receipt-safe policy.
- **Keep the incumbent fallback and old records honest.** The suggestion that fixed 16.5 must never appear is too broad: when no totals artifact is accepted, the unchanged incumbent may retain its stored sigma. An accepted totals artifact must identify its calibrated or validated fallback uncertainty rather than mislabel the incumbent constant as newly learned.
- **The audit already defines error as forecast minus final.** A request to establish the sign is satisfied by the audit, and the suggestion that old-season weighting is the likely causal source remains a hypothesis for earlier-data testing.

### Recommendation

Proceed with the additive implementation plan only after the main plan fixes the finite model ladder, exact snapshot and source contracts, fallback support, candidate freeze time, chronological date boundaries, one-time consumed cohort semantics, correction policy and decision thresholds. Review completion does not authorize fitting, automatic activation or deployment. No implementation work was performed here.
