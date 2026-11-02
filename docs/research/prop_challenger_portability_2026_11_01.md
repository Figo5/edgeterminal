# Challenger artifact portability — pre-commit review

This fixes the two challenger readers that used the absolute Mac path saved in
`active.json`. The destination now reads the same sealed artifact under its own
research directory. The stored pointer and every model, manifest, snapshot and
seal remain byte-for-byte unchanged.

Base release: `8d1cc4d245acf1f1e35c595d158050152658a254`.
Isolated branch: `fix/challenger-artifact-portability`.
Worktree: `/Users/giofiore/edgeterminal-portability-fix`.
Proposed commit: `fix(nfl): verify challenger artifacts after migration`.
Nothing has been staged, committed, installed or cut over.

## Exact scope

1. `edgeterminal/nfl_prop_challenger.py`: shared verified artifact access for
   `shadow()` and `selected_rows()`; anchored file access for their evidence.
2. `tests/test_nfl_challenger_portability.py`: relocation, identity, tampering,
   containment, race, nonregular-file, descriptor-cleanup and ordering cases.
3. `tests/test_nfl_prop_challenger.py`: replace an unsealed dummy manifest with
   real fixture bytes and their digest.
4. `tests/test_nfl_prop_result_ingest.py`: the same fixture-only sealing update
   for existing automatic-grading tests. No expected grading result was changed.
5. This review document.

No dependency, deployment setting, quota, grading rule, cutoff, parameter,
selection policy or model-training function changes. AST comparisons confirm
the existing model/ranking functions and `VERSION`, `WEIGHTS`, `CUTOFF` are
unchanged. No training was run.

## Resolution and containment contract

- Native pointers must identify one direct child of the configured trusted
  challenger directory. The production directory is derived from repository ROOT.
- The only relocated legacy form is the full exact prefix
  `/Users/giofiore/edgeterminal/data/research/prop_challenger/` followed by the
  19 ASCII-digit timestamp identity emitted by the existing trainer. That full
  identity maps to the same direct child at the destination. The old source
  location is never accessed as a fallback.
- No basename/hash search, alternate root, nested suffix, relative path,
  traversal, duplicate pointer key or malformed manifest digest is accepted.
  The exact destination manifest must match the stored pointer's SHA-256 and
  the existing fixed training contract before either reader uses it.
- Directory handles are opened from `/` through every destination ancestor and
  artifact using `O_DIRECTORY | O_NOFOLLOW`, then held for the operation. Evidence
  reads, snapshot enumeration, and fresh snapshot/seal creation are relative to
  these handles. Substituting a path cannot redirect them to an outward symlink.
- Regular evidence files use no-follow, nonblocking opens. The existing model
  digest is checked before deserializing those exact bytes from memory. Snapshot
  seals and manifest identities are verified, including the existing-snapshot
  return path. Creation is exclusive and never overwrites existing evidence.
- Existing non-directory bases fail closed. Ordinary missing bases/pointers
  retain the existing untrained behavior. Directory handles close on success
  and errors. Original first-selection ordering is preserved, including ties.

## Implementation and independent review

Codex coordinated the scoped plan, implementation and independent security/code
review. Claude implemented tests and code with file tools only; test execution
was controlled separately with networking disabled and temporary DB/cache paths.
The user's no-commit instruction overrides the TDD skill's checkpoint commits.

Independent review reproduced two medium issues in the first implementation:
a regular-file base silently appeared untrained, and a directory substitution
could bypass final-component no-follow protection. Both were fixed with new
regressions and descriptor anchoring, including fresh-output writes. A separate
regression prevented an unrelated filename tie-break from changing selection
ordering. Native re-review finds no remaining blocker in this scoped change.

OpenCode Go final-review status: **unavailable, not approved**. The exact-hash
Kimi review timed out after 240 seconds with only a `step_start` event and no
review text or tool events. Local catalog/integration checks did not identify a
specific provider error. The native independent review and its 167 offline
tests passed. No output from incomplete external attempts is counted as review
approval; this limitation is included in the commit-confirmation packet.

Reviewed production-file SHA-256:
`e6633f82b09226f3aff92e98999ba889fb484047e126e7ea1dffdf4f14fe8d1e`.

## Meaningful verification

- Initial valid RED: 24 failed, 51 passed, reproducing the relocation and initial
  path/hash-verification defects before the first production edit.
- Verified-byte/nonblocking IO RED: three intended failures, then fixed.
- Directory-anchoring RED: 10 genuine failures, eight passes. The fresh-output
  fixture's no-swap control succeeds; the failures reached actual escaped reads
  and writes. A saved pre-anchor module and receipt preserve the evidence.
- Ordering RED: one intended failure, then original ordering restored.
- Final offline GREEN: **231 passed** — 167 portability/security cases and
  64 existing challenger, automatic-grading and result-ingestion tests.
- Standard-library tracing: **51/53 executable lines, 96.23%**, scoped to the
  new IO/resolution helpers and nested opener. This is not branch or repository
  coverage; child-process FIFO traces are not merged.
- The existing local Linux image ran the final source at actual `/app`, with
  the old Mac root absent, source/artifact mounts read-only, and networking off.
  It returned all **146** original sealed selections with canonical digest
  `26b0562f829a169b6a60ce318ed45e9ed29ae81cd310a6aae171da80aee837dd`,
  read an existing snapshot through `shadow()`, and successfully deserialized
  the original hash-verified joblib bytes from memory. All **145 protected
  artifact files** retained their original hashes.
- The container was only a disposable offline test process. No web/worker was
  started, no dependencies installed, and no live DB or credentials mounted.
  This is not a new Windows native image build or production deployment.

Evidence: `output/portability-2026-11-01/` contains RED/GREEN logs, scoped tracing,
the container receipt, immutable-byte comparisons, and independent review.
No repository-wide test-suite pass is claimed.

## Genuine worker results, inspected separately

The running Mac remains at the approved base release. This audit only queried
its existing results; it did not invoke refresh, grading, or sports providers.

The scheduled worker recovered naturally. The completed run
`6cd14cd6d4fb434bb673551b3eec33b4` (November 1, 21:17:05–21:18:31 EST, 86.46 seconds)
has `success=1`, including top-five grading, challenger grading and NBA research
grading. Earlier failed runs remain in immutable history.

MIN–DET's new 70-row player-stat artifact supports both teams (35 each). Its five
challenger props were automatically graded: Justin Jefferson receiving yards
33 and receptions five, Amon-Ra St. Brown receptions seven, and Sam LaPorta
receptions three won their saved under selections; Jahmyr Gibbs's 23 carries
lost the saved under 22.5. The challenger reuses the verified main result
evidence; its stored evidence hash was independently checked. No dedicated
challenger result file is needed for already-covered player/market pairs. No
selection or line was changed. This resolved the earlier whole-game coverage warning.

The previous NBA error was an ESPN final-summary fetch for
`nba-2026-401909285` (LAL at GSW, October 30), endpoint
`https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401909285`.
The stored diagnostic retains only `ProviderError`; a precise connection/timeout
subtype cannot be recovered from it. The latest successful scheduled NBA grading
check reports **zero source errors**. Four completed-game props across three games
still have `missing_player_stat` evidence and remain pending. The historical
error key was neither removed nor treated as a fresh failure after recovery.

## Remaining migration gates

- Obtain the required explicit commit confirmation. Deploying this source and
  rebuilding/verifying the Windows package/image are separate later steps.
- Windows is still verified at the base release and does not yet contain this
  uncommitted fix. No existing bundle, manifest or ZIP was replaced here.
- The 14 documented live Mac symlinks remain untouched. Their guarded conversion,
  final state archive, private credential transfer and single-writer cutover
  remain separately deferred by the user.
- A backlog-processing Mac run exceeded the PC controller's 900-second limit;
  the later completed grading cycle took 86.46 seconds. Verify a genuine
  real-state cycle on the destination during the later authorized cutover.
  Offline image preflights alone do not establish runtime grading health.

Mac services stay running and authoritative throughout this preparation.

## November 2 morning status recheck

At 07:59:47 EST, the latest completed genuine worker tick was the November 1
21:28:49–21:29:12 run `1a228819c562477fa069a673f3162c77`, successful including all
three grading checks above. There was then an overnight tick gap, and a new
worker process was running at inspection. The last successful grading proves
the MIN–DET/NBA-source recovery; it does not establish uninterrupted overnight
refresh or a fresh completed tick after the gap. No manual refresh or restart
was used to hide that gap. Use the final read-only receipt for any later status.
