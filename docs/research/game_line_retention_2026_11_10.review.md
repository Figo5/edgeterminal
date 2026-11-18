# NBA/NFL forecast line-retention release review

Status: implementation frozen, tested and reviewed in isolation; awaiting commit/release confirmation. No commit or deployment has been performed.

Base: `4c691b64d882565c044b91d3858705cd518f7ce5` on live `main`. Proposed commit: `fix(markets): retain verified NBA and NFL forecast lines`.

## Resulting behavior

A later empty or partially populated market response no longer discards a previously verified forecast comparison. Each complete spread, total or moneyline group retains its own original line, sides, book, source identity/hash and source time. A newer complete verified group replaces only that market. The producer resolves the comparison contract before projecting and sealing the next pregame revision. Fresh numerical/execution inputs remain subject to the existing freshness rules; copying an old line never renews its timestamp or makes it a current executable quote.

The board, grading, season record and learning readers resolve the same selected saved forecast and market-specific provenance. The UI labels retained comparisons and shows the original source time. Exact game/team/tipoff/source identity, offer coherence, immutable quote membership and hashes are checked. Wrong or ambiguous identities, source suspension, tampering, future/posttipoff offers and contradictory equal-time prices remain visible failures. Rescheduling permits verified new-tipoff prices without carrying old-tipoff lines. Legacy NBA enrolled forecasts and both genuine NFL legacy hash formats have explicit proof-checked compatibility adapters; unsupported old priced evidence cannot silently become a retained origin.

Existing snapshots, selected forecasts, grades, freezes, model parameters, cutoffs, activation gates and quotas remain unchanged. No Odds API calls, provider demonstrations or model fitting occurred during development. Automatic capture/grading retain their existing schedule; the separate learner remains responsible for fitting.

## Historical effects — separate research only

Outcome-blind selection takes the latest existing sealed priced pregame pair for each approved market, retaining that pair's original projection and model. The normal worker audits only the two approved games, with append-only bindings/results and exact final-source verification. These results are excluded from original live/headline W–L and from prospective training/validation/activation evidence. Unsupported evidence remains unavailable. Read-time audit failure shows a separate needs-review status; worker failures are still reported.

| Game | Existing saved comparison | Recovered research result |
| --- | --- | --- |
| Golden State–New Orleans | New Orleans +9.5 | Loss |
| Golden State–New Orleans | Over 231.5 | Loss |
| Buffalo–Minnesota | Minnesota +3.5 | Loss |
| Buffalo–Minnesota | Over 45.5 | Loss |

Socket-denied replay of the actual saved artifacts produced exactly four bindings and four results, was idempotent, and preserved hashes of the original ledger rows. It created no new past-game forecasts. The bounded inventory examined 25 NBA games, 40 NFL games and 107 forecast rows; only the two known gaps were candidates. This is a bounded inventory, not a claim about every historical game.

## Sealed learning compatibility

An operator-only registration verifies the exact approved target Git commit, its immediate base parent, full binary patch SHA and old/new Git blob catalogs. It archives old source bytes by content hash and appends an immutable authorization; no authorization has been registered in production. NBA totals and NFL spread readers can then verify their original stage chain, bank, code/source evidence and consumed reservations before appending one terminal context-change event. They do not reseal old evidence, reuse burned evaluation cohorts, fit inside refresh or force activation. Unknown code/protocol/source changes still fail.

The transition seals a retuning-run ID boundary atomically with its stage event, preserving normal retry cadence and equal-clock behavior without overwriting later genuine runs. NFL terminal calls continue checking the predecessor chain. NBA spread's proposal catalog includes the new line verifier; its numerical model is unchanged. The proposed exact catalogs cover NBA totals 27→29 files and NFL spread 19→21 files.

## Verification and review

Source/test freeze: 32 files, SHA-256 `c0fb05cf7c32f26e5c9f72e15daedbb86c43dada2abb6d7832d6b73c88b26b88`.

- Final full offline Python run: **2,261 passed in 79.72 seconds**; credential environment stripped, dotenv disabled, owned test database and sockets denied. Prior full run before the final presentation-only fix: **2,259 passed in 89.74 seconds**.
- JavaScript regression: **34 passed**, zero failed.
- Actual archived-data replay: no failures; exactly four historical results, idempotent, original rows unchanged.
- New executable-line coverage: NBA resolver **90.72%**, NFL resolver **89.34%**, learning context helper **89.53%**, historical audit **89.46%**. These are traced line coverage, not branch-coverage claims; no dependency installed.
- Native code/security review: **no blocking findings**, **260 Python and 4 JavaScript tests passed**, with zero network attempts and all 32 hashes stable, including the final F3 follow-up.
- Two automated external review passes were attempted. One completed the initial full implementation review with findings, which were reproduced, repaired and rechecked; its final corrective supplement **timed out at the 300-second cap**, so no final pass is claimed. The other's initial full review timed out; its later bounded corrective code/security review completed with scoped findings, including the conditional path preflight and explicit nonblocking availability observations. The final one-line public-view exception isolation follows that review's F3 recommendation and is verified by code review plus two reproduced failing-then-passing regressions, not by another review pass. Neither reviewer examined the final audit file after that one-line change.

Review findings fixed include the actual legacy NFL publication failure, mixed-provider input eligibility, source proof passivity, exact historical final-source verification, fresh-market omission/override, rescheduled kickoff handling, isolated audit endpoint failures, atomic first-install audit DDL, terminal stage ancestry checks and retry-clock preservation. Original numerical/runtime modules were independently checked unchanged. Tests exercise rejected unsafe evidence as well as successful retention; fixture fixes supply real seals and complete offers rather than bypass validation.

### Review dispositions and operating limits

Review finding F1 is a mandatory release preflight: actual archive-root ancestors must be real directories. Read-only Mac inspection confirmed the configured database is `/Users/giofiore/edgeterminal/data/edgeterminal.db` with no symlink archive ancestors. The 14 documented migration symlinks remain unchanged and are not archive ancestors. Repeat this check at deployment; do not relax no-follow/containment checks.

Official NFL quotes with an unexpected phase deliberately fail visibly; the real writer emits current pregame rows. Foreign/demo quotes are excluded from production comparison admission. A legacy priced market needed for retention but lacking complete verified official proof still blocks publication visibly rather than being silently erased. The original demo-mode execution path is outside this production-evidence policy. Missing audit append-only triggers on an otherwise complete ledger are recreated idempotently without rewriting rows; partial missing tables are rejected. Duplicate bounded source checks remain content verified; no unsafe size/mtime cache was introduced.

## Exact scope

32 application/test files plus the reviewed plan and this release review: 34 proposed files. The exact full patch including new files, final scoped hashes and counts were recorded with the release evidence. Generated research reports, credentials, databases, source artifacts, model artifacts and other output are outside the commit scope. A temporary Git index builds the review patch without staging the real worktree index.

## Release conditions

Commit and release are awaiting confirmation.

After confirmation, recheck exact base/deployed source and acquire genuine research/learner idle boundaries plus independent source-child leases. Recheck all three learners for active or pending learned candidates; if any appeared, stop and reassess compatibility rather than overriding guards. Confirm archive-root ancestors are nonsymlink directories and all component catalog names satisfy the verifier. Preserve immutable ledger/file prefixes and rollback information. Commit only the reviewed scope, install only the exact reviewed commit without resetting unrelated changes, register the exact reviewed learning-context authorization before workers resume, and verify a genuine automatic cycle, source status and reconciled records. No Windows cutover, migration symlink materialization, service disablement or final state archive is included.

Live read-only check: health OK at base 4c691b6, app and research refresh running, scheduled learner loaded with last exit 0. Live tracked changes remain the three pre-existing generated research reports. No live source edit or service stop occurred during implementation.
