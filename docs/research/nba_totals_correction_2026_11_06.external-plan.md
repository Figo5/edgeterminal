# NBA totals correction: automated plan reviews

Two automated planning reviews completed on November 6, 2026 and both returned
**PLAN: CONDITIONAL**. These are planning reviews, with implementation confirmation
still pending incorporation into the consolidated plan. Neither response is
implementation approval, a test result, fitting readiness, activation evidence,
or release permission.

The frozen packet was 97,749 bytes, SHA256
`c0447d114b177c12446857f97267745948b2e3749f30b6813a1d2176434763cc`. It supplied
the complete `nba_totals_data.py` and expressly identified learner/protocol,
model chronology and relevant test excerpts, with full-file hashes for all six
source files. Before/after source hashes match. Base commit:
`0a23eefba02a8fe4829120c6f2227afda457bba2`. The HOU–DAL 401909840 incident facts,
source IDs 2964/7946, possessions 100.72→101.22 and actual receipts
1792670677.887/1793038313.965 were supplied by the task owner; the reviews did
not fetch source bodies or query a live database.

The reviews agree on the central fix: resolve eligible possession-stat
corrections by actual receipt through the training cutoff, while retaining all
source evidence and rejecting identity, score, duration, corruption and
ambiguous equal-clock conflicts. They require the plan to pin these details:

- Validate candidate bytes, route, final identity, teams, scores, played-at time and duration before classifying optional advanced evidence. The current completeness prefilter can otherwise hide an invalid latest artifact and retain older enrichment.
- Only possession-stat changes and their derived pace/efficiency may differ. Changed scores or duration still fail. Pin duration presence transitions explicitly; both recommend treating missing↔present duration as a conflicting duration transition among otherwise feature-complete artifacts. Keep the existing 5→4-period test unchanged.
- Resolve an entire correction chain deterministically; an invalid or ambiguous earlier member must not disappear merely because a later version looks valid. Equal-clock differing semantics fail before any artifact-ID tie-break. Semantically equivalent refetches may collapse deterministically with their evidence retained.
- Seal considered source IDs, hashes, actual receipts and the resolution choice. Apply the same selected event evidence to both team rows. Preserve selected possession/duration receipt clocks separately from schedule label receipt.
- Keep serving on saved source IDs, original archives/maps/forecasts/freezes/grades/proofs immutable, source and code binding intact, deadlines propagated, and all existing outer-validation gates unchanged.

The reviews differ on complete→incomplete advanced-evidence disappearance. One
suggests score-only with an explicit withdrawn descriptor as one possible policy;
the other recommends a visible failure. Consolidation selects **visible fail-closed
after previously complete evidence**, while no valid advanced evidence ever
continues to preserve score-only coverage. This is a conservative resolution of
the reviewed condition, not an externally issued unconditional pass.

Both reviews identify a feature-receipt chronology gap: existing phase checks use
schedule-based `label_received_at`, while later box corrections can change
earlier-phase features. The packet explicitly asked for this risk assessment.
Consolidation will carry selected possession/duration receipts and per-example
feature availability separately from `label_received_at`, and strengthen the
totals phase guard so all earlier-phase feature and label availability precedes
later-phase `as_of`. Existing floors, outer gates and waiting behavior remain; no
broader phase redesign or old-artifact resealing is proposed. This post-review
consolidation addresses the shared risk; its concrete implementation has not been
externally reviewed. Receipt clocks must remain actual, and chronology may still
require waiting after the incident is fixed.

Minimum meaningful tests from the combined reviews: the real possession-correction
pattern selects the later source while preserving original bytes; unchanged 5→4
duration rejection; complete/incomplete and duration-presence transitions;
latest-invalid and invalid-middle chains; equal-clock conflict/equivalence;
cutoff before/at/after receipt; selected feature receipts and phase chronology;
repeatable source resolution and seal sensitivity; source-byte corruption after
cache warmup; older saved-ID serving replay; chronology waiting with no candidate
registration; unchanged 60-pair/seven-date gates.

Two raw suggestions need qualification. A proposed failure on artifact-ID/receipt-order
inversion adds a policy beyond actual-receipt ordering; the packet provides no
evidence that IDs are receipt clocks, so that suggestion is not automatically
adopted. The other reviewer's statement that current training silently resolves
differing equal-clock semantics by rowid is inaccurate: current
`training_examples` raises on any semantic enrichment difference. Equal-clock
conflict coverage remains necessary for the new resolver. Neither qualification
weakens the fail-closed plan.

No product/test files changed, no tests or real fits ran, and no provider-data
requests, DB writes, commits/staging, deployment or service operations occurred in
this review task.
