# NBA totals implementation — November 5, 2026

The three approved improvements are implemented in the isolated `feat/nba-totals-learning` worktree, based on `0082fa8f672bed0fbb6da09ff39fedf49ded5a41`. Gate 1 was approved. Gate 2 commit/release approval is outstanding. The live Mac checkout and services remain unchanged.

Proposed commit: `feat(nba): add season-aware totals and automatic validation`.

## Resulting behavior

1. Season-aware scoring and recent-form weights belong to a separately versioned totals component. Its finite candidate family and chronological fit/development/calibration rules are sealed before future validation.
2. Verified pace × scoring efficiency and opponent context use receipt-bound ESPN evidence, reliable duration/overtime normalization and explicit missingness. Missing advanced evidence preserves scoring support. Totals compose after the existing spread projection, preserving margin, winner/spread probabilities, decisions and identities. Score aliases reconcile; raw model base and already-served comparator remain separate.
3. Automatic totals learning runs in the existing separate offline service. The source worker queues work and freezes pure pretip inference. Every supported game keeps its forecast and, when a sourced line exists, a totals pick. Existing five-minute capture and final-only grading continue. About & data exposes check times, waiting/error/coverage and runner health; the season record attributes each saved total once across versions.

## Readiness and numerical limits

Code release does not establish a stronger numerical model or bypass validation. The incumbent remains live while evidence collects. Fitting requires at least 660 fit, 120 development and 120 calibration targets spanning multiple seasons, with disjoint whole Eastern dates and actual receipt chronology.

The read-only archive inventory found enough structural history but **insufficient chronological development/calibration support today**: earliest actual historical receipt October 20, with only 121 subsequent games versus 240 needed for those two phases. This bounded inventory is a necessary-condition check, not a real fit or all-version training replay. The learner returns a visible waiting state and checks again automatically; it cannot backdate downloads. [Readiness inventory](../../output/nba-totals-implementation/real-replay/TRAINING-READINESS.md).

After candidate sealing, validation starts on the next complete Eastern date and needs at least 60 unused prospective forecast/line/final pairs across seven unused complete dates. All inspected outcomes through November 4 are excluded. Games/dates are consumed before labels and stay consumed after rejection/error. Candidate inference is independently reproduced before metrics and publication.

Activation requires material MAE and conditional-over Brier improvements, negative upper paired-date 95% bounds, no RMSE/bias/decided-win/coverage regression, and the saved-line point-error benchmark. Missing finals or a verified same-game score correction awaiting its matching grade retain the same pending reservation. Contradictory identity, incomplete latest source coverage, corruption or changes invalidating an immutable evaluation fail closed. No grades are forced and no numerical superiority is claimed.

## Scope and preservation

The scoped release contains seven application source files, ten test files and five planning/review documents: **22 files**. Four source modules and seven tests are new. Existing application edits are limited to `learning_worker.py`, `nba_ops.py` and `web/nba_ui.js`.

The existing tests add the totals-version contract, freeze the physical clock for pretip fixtures and isolate tool-transport unit tests from today's external NFL schedule. Separate context/source integration tests remain. No application transport or readiness threshold changed.

[Exact diff](../../output/nba-totals-implementation/release/SCOPED-DIFF.patch) · [File manifest](../../output/nba-totals-implementation/release/SCOPED-MANIFEST.json). Generated public archive subsets, databases, traces and model CLI transcripts are evidence only and excluded from the commit. No blanket staging is authorized.

Independent comparison found all 141 protected application/deployment/model-artifact files match the base. Pinned props, parameters, cutoffs, pricing, spread logic, forecast/grade policy, original source parser and old seal catalogs stay byte-identical. The live checkout still has only its three pre-existing generated NFL report changes.

## Verification

- Final full offline Python suite: **1,700 passed in 48.82 seconds**. External sockets/DNS denied; five attempted external resolutions were blocked. [Python receipt](../../output/nba-totals-implementation/FULL-PYTHON-FINAL.txt), [network guard](../../output/nba-totals-implementation/NETWORK-GUARD.json).
- Full JavaScript suite: **28 passed**, zero failures. [Receipt](../../output/nba-totals-implementation/FULL-JAVASCRIPT-FINAL.txt).
- **14/14 actual November 4 pretip snapshots replayed and relocated**, preserving all incumbent fields, manifests and source hashes. The bounded subset used 195 public artifacts, 560 scoring rows and ten verified overtime rows. Every game retained the exact incumbent without a validated artifact. The new totals parser's ESPN `displayValue` correction has real failing-before/passing-after tests; the original parser is untouched. Final frozen source reverified all 14 maps at both owned subset locations, with zero writes, in 5.89 seconds. [Replay report](../../output/nba-totals-implementation/real-replay/REPORT.md), [final frozen replay](../../output/nba-totals-implementation/real-replay/FINAL-FROZEN-REPLAY.json).
- Final scoped executable-line coverage: model **92.80%**, data **92.59%**, store **89.52%**, learner **94.75%**. These are module line figures, not repository/branch coverage. No dependency installed.
- Independent native code/security review: **PASS**. Final correction-wait/identity delta verified with 76 focused tests; earlier full frozen review independently ran 109 Python and three JavaScript tests. Real deadlines, raw-base parity, forged-freeze rejection, date/source guards, checkpoint recovery and preserved bytes are covered. [Review](../../output/nba-totals-implementation/independent-review/REVIEW.md).
- Genuine external review: final **OpenCode Go CODE/SECURITY PASS** with all reviewed hashes stable. Claude's full review identified the final correction-lifecycle issue; its fix passed native review and the final Go recheck. Claude's last delta request hit its session usage limit, so no final Claude acknowledgement is claimed. [Review chain, model identities, source binding and limitation](../../output/nba-totals-implementation/final-external-review/REPORT.md). Earlier moving-code approvals are not substituted for final hash verification.

At the final read-only operational check, the site was healthy, the genuine scheduled worker tick succeeded, current warnings/errors were empty, both scheduled services retained 300-second intervals and exit code zero, and the app remained running. [Receipt](../../output/nba-totals-implementation/LIVE-PRESERVATION-FINAL.json).

No real fit/activation, demonstration provider request, quota change, credential action, Codex automation, Windows cutover, symlink or final archive was performed. This Mac must still be awake and online.

## Release after Gate 2

Recheck hashes/base/live state, commit only the 22 manifest-listed files, and use the established guarded local code release procedure. Preserve generated reports and all historical evidence. Check health, genuine scheduled cycles, supported forecasts, grade/status freshness and season reconciliation. Let historical chronology and future validation wait automatically. Windows cutover remains separate.

The requested [ecc:orch-pipeline Gate 2](</Users/giofiore/.codex/plugins/cache/ecc/ecc/2.2.3/skills/orch-pipeline/SKILL.md>) requires: “do not commit until the user confirms.” Approval authorizes the scoped commit/local code release; numerical activation must still pass its own gates.
