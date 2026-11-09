# Gate 1 planning review and reconciliation

The governing task list is [game_quality_2026_11_09.plan.md](game_quality_2026_11_09.plan.md), at base `c7511fe`. This review is planning evidence, not an implementation/test/numerical readiness claim.

The delegated NBA/NFL planners confirmed a real totals chronology gap, stale production NFL advanced features and missing scheduled NFL pregame revisions. Independent native safety review required durable incomplete stages and a bounded source-maintenance process outside capture/grading locks. The governing plan includes these requirements; the live checkout has no implementation edits from this task.

Genuine external reviews used a frozen 171,460-byte packet, SHA-256 `c16f400f290251d4040e734bd80046ce7ec92df2c956725a6a0b6cc04ce13ace`, with tools disabled. Claude `claude-opus-5-5` completed in 146.27 seconds; OpenCode Go `opencode-go/kimi-k3` default completed in 77 seconds. Canonical identities and absence of tool activity were verified. [Full external report](../../output/game-quality-plan/external-review/REPORT.md) and [receipts](../../output/game-quality-plan/external-review/review-receipt.json) preserve exact findings and document snapshot drift.

Go gave a snapshot PLAN PASS. Claude gave a conditional pass requiring C1–C3; these were incorporated afterward:

1. Exclude entire NFL Weeks 8 and 9, not only audited calendar-date games. Keep regular-season cohorts explicit, require six outer weeks within one season, and report insufficient season remaining. No playoff or silent cross-season outer pooling.
2. Match NFL inner/outer incumbent input-policy identities. No invented old PBP receipts or retroactive use of a newer source. Explicitly report incompatible coverage; pair candidate and counted incumbent with the same saved pregame revision and quote.
3. New NBA inner development/calibration rows require own-as-of feature availability and the originally saved incumbent baseline. Cutoff-reconstructed evidence is fit-only under the new staged contract. Claude described this as existing v1 semantics, but the current fitter does not enforce that restriction. The governing plan openly introduces it as a stronger new rule and preserves genuine v1 bytes/readers.

Further explicit reminders now cover January season identity, genuine but incomplete source publication, no automatic deletion of referenced immutable evidence, legacy analysis input refresh versus learned spread isolation, and a terminal retained decision without outer consumption when the incumbent wins development selection.

The final governing bytes include amendments that external reviewers did not see. Their PASS is not claimed for those later bytes. The [native final review](game_quality_2026_11_09.safety-final.md) is responsible for checking the amendments and records the final governing hash.

No training, coefficient selection, outer evaluation, candidate registration, provider request, service restart, commit, deployment or Windows cutover was performed. No runtime test results are claimed before implementation. The plan remains at Gate 1; isolated TDD begins only after approval. Commit/release requires separate reviewed Gate 2 confirmation.
