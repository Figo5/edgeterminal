# Game forecast quality — release review

Status: code and security reviews reported no blocking findings for the final
frozen bytes. All required regression checks pass. No commit or deployment. Base `c7511fe490586bee8e1c76885fb0fa28aedec4b5`. The user approved the governing [implementation plan](game_quality_2026_11_09.plan.md), SHA-256 `9752126259e59631809848c79468330780df76b85a1ac7b5d19fde4b7d2efcf9`. Work is isolated in `/Users/giofiore/edgeterminal-game-model-quality`.

## Resulting behavior

NFL game forecasts gain automatically prepared current play-by-play inputs and pregame revisions on the five-minute worker. Annual decompression and weekly research fitting leave the capture/grading critical section. A bounded source child verifies and publishes immutable source artifacts; the existing offline learner prepares unchanged legacy weekly bundles. Passive board/detail reads use the same sealed pregame revision. Saved probabilities and season results retain the exact line observed by that forecast; newer prices remain separately visible until a matching revision is saved.

NBA totals learning retains a sealed fit bank while collecting valid later model-selection and calibration games. The existing finite numerical family is preserved. The new staged protocol explicitly requires own-as-of features and the originally saved incumbent for inner comparisons; reconstructed historical examples qualify only for fitting. Separate append-only ledgers preserve every v1 contract and source byte.

NFL spread learning is a separate component, with fixed incumbent/affine/market-residual families and strict chronological and whole-week validation. It cannot modify projected winner, total or player-prop game scripts. Corrected source/results trigger fixed-artifact verification; temporarily withdrawn finals remain pending, and integrity failure restores only a separately reverified prior accepted component or the scoring baseline. A losing night alone cannot cause rollback. Complete-week membership must match an authenticated cached schedule, using a bounded identity-only reader that never consults score fields before outer reservation. Missing schedule proof waits; a damaged separate component keeps the verified baseline forecast available while the worker explicitly reports an integrity error. Corrupted baseline inputs still refuse a new forecast.

## Meaningful verification

The final offline regression run passes **2,056 Python tests** in 77.17 seconds; all **30 JavaScript tests** pass. External sockets are denied (loopback allowed for fixture servers), credentials are stripped, dotenv is disabled, and the default test database is owned by the worktree. No real provider or fitted-production-model request is needed for verification. Earlier intermediate full runs remain preserved separately.

Executed-line coverage uses stdlib tracing/disassembly, with no new coverage dependency: NBA five-module core 90.27%; NFL four-module core 92.02%; NFL source 88.21%; weekly preparation 83.17%; automatic forecast module 90.75%. Final receipts and source hashes were recorded
with the release evidence.

The exact release diff's SHA-256, explicit path list and all file hashes are
recorded in the adjacent release manifest. Nothing is staged.

- Source fixtures cover bounded streaming, invalid identities/receipts/hashes, symlink and relocation checks, incomplete coverage, source-priority publication, crash/PID reuse recovery and read-only status. Weekly fixtures use the real unchanged BundleStage with fake estimators, verify the final path after temporary cleanup, enforce an atomic queued target and refuse a publication crossing kickoff.
- Actual owned NFL archive replay verifies 89 prior games against official finals and relocated replay. Original source hashes remain unchanged; live SQLite reports zero changes; network is denied.
- Actual owned NBA archive replay verifies 3,656 examples, canonical legacy adapter parity and a deterministic 1,077-game fit prefix spanning two seasons. Its digest is `600ae83a1f40a962a7d313c8bc4c20d6b6eb6e05bf8d60e9a17fa335cb12f622`. Source-only archive rows yield zero eligible own-as-of saved-baseline inner examples. No real fitting, registration, evaluation or activation was performed.
- Executed review regressions also close four automated external findings, two native NBA findings, three valid review findings, whole-week schedule coverage and the component-only fallback failure. The external rollback concern was independently disproved using two genuinely accepted synthetic candidates: a corrupt newer proof restores only a still-verified prior proof, and corrupting both falls back to baseline while all 120 consumed games remain consumed.
- Contract regressions cover distinct legacy/staged activation triggers, raw candidate basis versus served comparison basis, durable progress through missing phases, source/result corrections, reserved holdout persistence, exact snapshot/line pairs, passive selector ties and storage tampering.
- Scoped automated reviews are preserved in the external evidence. The first broad review attempt timed out; a completed narrow review covered bank/stage control only, and another covered the four NFL spread modules. Neither is presented as a whole-feature review result. Code/security reviews cover the complete integration and final remediation bytes.

## Final review decisions

- NBA code and security review: no unresolved high or medium findings; 122 separate focused tests pass. Final author hashes and the six archive-replay dependencies match.
- NFL code and security review: no unresolved high or medium findings; 208 separate focused tests pass with no drift in the 30-file reviewed manifest. This includes source/weekly offload, shared integration, the label-blind full-week guard and component-only fallback. Initial request-changes reports remain preserved.

## Evidence limits and release guard

This is code readiness, not demonstrated numerical improvement. Every supported game retains its incumbent pick. NBA replacement models still require 660 fit, 120 development, 120 calibration, then 60 genuinely future validation games across seven dates. NFL replacements require 40/20/20 whole-week inner games, followed by 60 unseen games across six completed regular-season weeks in one season. The inspected 2026 Weeks 8 and 9 are excluded whole. An NFL learned replacement may not activate this season. All cutoffs, frozen histories, quota guards, PASS and RESEARCH_ONLY remain protected.

Existing v1 totals source files remain byte-identical. The changed shared ops/worker files affect the v1 code catalog, so release must recheck that there is no old v1 active or pending candidate; a newly appeared candidate blocks an automatic release until reviewed compatibility is established. A read-only live check currently finds zero candidates. The unchanged live app remains listening; its latest three genuine refresh cycles succeeded. These operational observations will be rechecked before release confirmation and after release.

No model was trained against last week, no unsupported result was force-graded, no Odds API/provider demonstration was called, and no credential transfer, Windows cutover, symlink materialization or final state archive was performed. The current host still needs to be awake and online.

## Required confirmation

Proposed scoped commit: `feat(models): refresh game inputs and preserve staged learning`. Exact paths, hashes, diff and the final review decisions accompany this review.
Release must recheck live HEAD, absence of old v1 totals candidates and worker
activity before installing; do not interrupt a genuine learner. Commit/release
does not override numerical activation gates or include Windows migration.
Commit and release remain pending confirmation.
