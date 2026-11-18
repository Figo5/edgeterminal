# NBA totals historical corrections: safety plan

Planning review at base `0a23eef`, November 6, 2026. The plan for this correction
is not yet confirmed. This document recommends a narrow offline dataset
reconstruction change; it does not authorize implementation, fitting, activation,
or release.

## Evidence and finding

Reviewed `nba_totals_data.source_history()`, `box_features()`, `training_examples()`, the totals model's reconstruction and phase chronology, learner protocol, candidate/source seals, the approved November 5 plan, and the saved read-only `TOTALS-LEARNER-CHECK.json`. The saved audit reports ESPN event `401909840`, HOU–DAL: source `2964` received at `1792670677.887271`, then source `7946` at `1793038313.965354`; estimated raw possessions changed from `100.72` to `101.22`. This review did not independently read those raw live-database bodies. The audit is development evidence, not a fit, validation result, or proof of improved predictions.

At this base, `training_examples()` scans successful history and final-box receipts through its cutoff. Its scoring signature conflict at lines 624–626 and possession/duration semantic conflict at lines 649–650 turn an otherwise valid provider correction into a whole-job integrity error. The remedy should resolve independently verified historical versions by actual receipt time. It must not replace the verifier with a permissive last-row overwrite.

## Recommended resolution contract

1. **Scope resolution to reconstruction.** Change the offline reconstruction path and narrowly required chronology metadata/guards. Preserve selected-source serving replay in `prepare()`, saved input-map verification, forecast selection, final/grade readers, prospective freezes, publication rules, pinned prop/spread/model catalogs, and historical rows. A source later than an old forecast's as-of cannot change its replay.
2. **Verify before resolving.** Every considered receipt for an in-scope owner/game must pass hash/body size, HTTP status, finite actual receipt, strict HTTPS host/path/query, source kind, JSON ambiguity, season, canonical numeric event/team identity, orientation, played time, final status, and score checks. Verify superseded versions too; an intact newer source cannot hide tampered older evidence. Check actual source rows rather than trusting parse caches or metadata alone.
3. **Use actual receipt order.** Eligibility is `received_at <= reconstruction_cutoff`, inclusive. Resolve identity `(owner team, event)` for scoring and event for two-sided box evidence. IDs provide deterministic iteration only. A larger ID with an older receipt cannot win. A correction just after the cutoff has no effect on the earlier reconstruction.
4. **Treat equal-time conflict as ambiguity.** Semantically conflicting eligible versions at the same receipt time fail closed, irrespective of IDs, insertion order, or a later non-tied version. Exact semantic duplicates may coalesce deterministically, with their immutable source identities retained for audit. Semantic equality must cover every used field and verified identity, not just raw possessions. Reversed insertion order must give the same output or the same rejection.
5. **Resolve whole coherent versions.** A winning box supplies the pair of teams' raw possessions, duration evidence, pace and efficiency inputs together. Never take possessions from one revision and duration or scores from another. Reconcile that complete version against the selected scoring rows. Mirrored owner schedules must still agree on teams, orientation, time and reciprocal scores; asynchronous conflicting schedule corrections remain an integrity error until coherent evidence exists. A newer valid score revision cannot be enriched with an older box carrying different scores.
6. **Keep validation strict.** A legitimate later possession correction can supersede earlier valid possessions. A changed duration is eligible only when the new final-period/line-score evidence independently proves it and every score still reconciles. Contradictory period counts, one-sided line scores, invalid attempts/counts, duplicate statistics, wrong route/event/team/score/time or non-finite numbers fail; receipt order does not cure them.
7. **Do not silently reuse stale advanced evidence.** First validate a relevant newest source's envelope, event and available structure before classifying it as optional missingness. If it is a regression from previously complete evidence to an incomplete final box or unproved duration, recommend a visible, specific reconstruction error rather than silently retaining the older complete values. If no eligible complete advanced version ever existed, keep the established score-only missing-evidence behavior. A contradictory source is not missing optional data. This strict policy is simple and reviewable; a future explicit stale-evidence policy would be a separate contract.
8. **Preserve provenance and determinism.** Each winning value must retain the supplying source ID, hash and actual receipt. The returned source envelope must cover selected score, label, possession and duration dependencies, plus considered superseded evidence whose integrity influenced acceptance. Do not alter archived bodies, source timestamps or IDs. Keep a bounded diagnostic showing the old/new source IDs and changed fields; do not write diagnostics to historical forecasts or source rows.

Repeated unchanged schedule downloads need an explicit provenance rule: an earlier identical verified source can still prove that same row was available earlier. If it is used, name that actual source and receipt. Never attach its early receipt to a changed later row or to later possession/duration values. The implementation plan must freeze this rule before tests are written, since choosing every latest refresh indiscriminately can unnecessarily move label receipts forward.

## Chronology must include feature corrections

There are three distinct clocks: when the game was played, when the selected final label was received, and when each selected feature version was received. `training_examples()` already marks its historic outputs `reconstructed=True` and carries the reconstruction cutoff. That retrospective assumption must remain explicit: reconstruction is not a saved pregame forecast and cannot become prospective validation.

Keep every selected source receipt at or before the reconstruction/training cutoff. Historical feature games must precede the target by the existing four-hour lag, with target exclusion and opponent-window chronology unchanged. A late download is not evidence of earlier availability.

One existing boundary needs explicit treatment: `model.features(..., reconstruction_cutoff=...)` permits reconstructed features through the overall cutoff, but `fit_candidate()` checks cross-phase availability using only `label_received_at`. A fit feature corrected after the first development as-of must not influence that earlier development comparison merely because its target's label was received earlier. Carry the actual selected possession/duration receipt and a per-example evidence maximum separately from the actual label receipt. A narrow totals-only guard must enforce that all earlier-phase feature and label evidence predates the next phase's earliest as-of; per-row development/calibration features must also meet their applicable forecast availability contract. If that cannot be established, defer/reject the affected dataset rather than backdate it or weaken the model guard. Do not rename an evidence maximum `label_received_at`.

Keep whole Eastern dates disjoint across fit/development/calibration and outer validation. Preserve the 660/120/120 floors, multiple-season requirement, fixed finite candidate family, source/code/protocol seals, weekly proposal cadence, and the predeclared 60-game/seven-date prospective gate and all metric thresholds. The correction can change the reconstructed development data; it cannot manufacture chronology or readiness.

Saved forecasts, selections, input maps, grades, original sources, model artifacts, reservations and evaluations are immutable. Corrections affecting a sealed candidate require the existing reject/wait lifecycle; never refit it under the same version or refresh its original source envelope. Already inspected November 4 and earlier outcomes, this November 6 audit and any other inspected/consumed games/dates remain development evidence. A name change, error, rejected candidate or reconstructed label cannot return a spent date to fresh validation. No validation-label access before the existing durable reservation/seal guard.

## Concrete acceptance tests after plan confirmation

| Case | Required result |
| --- | --- |
| Valid HOU–DAL-shaped possession revision, earlier cutoff | Original raw possessions and original source identity/receipt; no later source leakage. |
| Same fixture, cutoff exactly at the later receipt | Corrected possessions, pace and efficiency, all bound to the later box source; scores unchanged. |
| Same fixture, cutoff after the later receipt | Same deterministic corrected result; repeated reconstruction is identical. |
| Later database ID but older receipt | Receipt time wins; IDs cannot advance availability. |
| Identical repeated evidence | Deterministic deduplication and honest source/receipt provenance; no correction error. |
| Equal receipt with conflicting scores, possessions or duration | Integrity error, including reversed insertion order and a still-later valid version. |
| Corrupted superseded body or newest body | Integrity error even if another source otherwise supplies valid values. |
| Wrong host/route/kind/query/event/team/orientation/season/time/score | Integrity error before optional-missingness fallback or revision selection. |
| Valid newer duration change versus contradictory period/line-score evidence | The coherent new duration can win; internally contradictory evidence fails. |
| Newest incomplete advanced evidence after a valid complete version | Visible fail-closed result; no silent old-value fallback or mixed-version row. |
| No advanced evidence ever available | Existing score-only coverage retained, with deterministic missingness. |
| Owner schedules corrected at different times and still disagree | Integrity error; no arbitrary home/away version choice. |
| Coherent scoring correction | Reciprocal latest scores and honest later label receipts; no reuse of an older incompatible box. |
| Late feature correction crossing a fit/development or development/calibration boundary | Deferral/rejection under real evidence chronology, even when label receipts alone would pass. |
| Source or label just beyond cutoff; target/future game; violated lag/opponent window | Existing exclusion/rejection preserved. |
| Old saved forecast/input map and prospective reservation | Identical replay, hashes, forecast/grade/selection bytes and spent game/date membership. |
| Protected prop/spread/model files and protocol | Byte-identical files; unchanged pinned verification and numerical gate contract. |

Use synthetic isolated SQLite fixtures first. After confirmation, a bounded owned archive subset may reproduce the reported old/new evidence and demonstrate zero source/provider writes. Include test receipts and protected hashes in review. Do not fit a real model or interpret successful dataset replay as activation evidence.

## Operational constraints

Keep reconstruction in the existing offline learner, outside capture/grading, request/status reads, publication locks and SQLite writer transactions. Retain network denial, bounded source/body reads, deadline checks, existing failure visibility and incumbent fallback. Avoid new dependencies, new providers, requests for demonstration, quota overrides, schema-wide migrations or broad shared-parser rewrites. Any later confirmed release needs the established source/code seal checks and separate commit/release confirmation; current Mac services remain running throughout this planning stage.

For this review only the assigned safety-plan file and its planning receipt are written. No application/test changes, tests, fitting, database scans/writes, provider/network requests, staging, commits or service actions were performed.
