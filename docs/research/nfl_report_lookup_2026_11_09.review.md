# NFL report lookup repair — November 9, 2026

Proposed commit: `fix(nfl): avoid quadratic research report scans`. Base `3a02eb3e269935fb29eae96bcf41d95f64c4343f`, isolated worktree `/Users/giofiore/edgeterminal-nfl-report-lookup`, branch `fix/nfl-report-lookup`. No live code edits, staging, commit or installation have occurred.

## Observed problem

The November 9 automatic catch-up cycle took 1,605.996 seconds while retaining the shared source-worker lock. It completed successfully after the Mac woke; earlier ESPN HTTP 403 responses and a verified 3:38–7:56 AM Eastern low-power sleep had left results stale. Most observed CPU time was spent before the NFL grading step returned, delaying the later NBA update step. Independent source inspection found a concrete quadratic lookup in the cumulative NFL diagnostic report: each non-tied over-side grade scanned the full grade list for its first matching companion. The source worker calls this report after new grade revisions.

This repair changes that lookup only. A six-field index records the **first** grade for each `(game, player, market, sportsbook, line, side)` key. The original over-row traversal, direction choice and output ordering remain intact. Duplicate keys still select the first matching grade, rather than the last. Empty diagnostic sets avoid building the index. Valid persisted fields are finite scalar values already constrained by unchanged observation sealing and grading writers.

## Scope and unchanged behavior

Exactly three proposed files: `edgeterminal/nfl_prop_grading.py`, new `tests/test_nfl_prop_report_lookup.py`, and this review. The production diff adds eight lines and replaces one scan inside `forward_report`. It introduces no dependency or public API change. Database queries, provenance/hash checks, source handling, grading, calibration, model parameters, training cutoffs, quotas, scheduling, selection/freeze bytes and activation rules are untouched. Current source/learning services keep running. This is a faster report calculation, not a numerical model change or a reinterpretation of past results.

This is a **small** repair: one internal function, obvious confirmed root cause,
no new contract or data source. It started with a failing regression; no formal
plan phase was produced. Code/security review ran after implementation. The
pre-commit gate remains required; TDD checkpoint commits were withheld to honor
it.

## Executed verification

| Check | Observed result |
| --- | --- |
| Regression before source edits | Valid RED: 64 pairs required 6,496 candidate-key reads, exceeding the 1,536-read work budget; five semantic cases passed |
| Focused implementation tests | 26 passed offline; complete captured cumulative/single report outputs equal |
| Function executable-line coverage | `forward_report`: 120/120 lines, 100%; not a whole-module coverage claim |
| Full offline Python suite | **1,786 passed in 51.84 seconds**, no external request permitted |
| Separate semantic/error probes | 52 cases passed, including 40 randomized valid reports; identical SQL read sequence and stored fixture bytes |
| Separate focused suite | 27 passed, including the existing week-3 rehearsal |
| Separate complexity check | 200/400/800 grade inputs: 700/1,400/2,800 candidate-key reads; baseline 10,500/41,000/162,000; matching full report hashes |
| Actual stored report parity | **24,734 grades and 25,638 frozen quote sides**, complete JSON and serialized report bytes identical to the report emitted by the unchanged live code; indexed calculation **1.160 seconds** |

The actual replay used only owned read-only copies of the 32 public research freezes, their grade revisions and the existing public calibration report. It copied no account, credential, raw provider-feed or other application state. It is not a migration/state archive. Socket access and default/live DB opening were denied; SQLite changes were zero and the owned database SHA-256 stayed unchanged. Baseline and indexed serialized report SHA-256 both equal `bb40d73a7677558c6f5f751647ec683e04ff9e9b926aa820f6c7984564a14fe4`.

The regression suite covers duplicate first-match behavior, original diagnostic order, over/under directions, missing companions, tied projection/line, empty candidates, positive/negative/zero and numeric-equivalent lines, retained integrity failures, cumulative and single-freeze reports. All original report calculations remain in place. No real fitting, activation, force grade or retrospective selection occurred.

## Review

Native review reported **CODE PASS / SECURITY PASS**, bound to the final source
hash `bc8d2720da352e54c0e515e4f75558208056e662c7e9a3050f9850a6d28ac247` and test
hash `53b0e5f8b58129b1d535eb0b272671e764c02544225a5394b54496f50955f1a6`.

Two automated reviews each reported **CODE PASS**. Neither found a confirmed
defect; optional test comments did not require a source change. Review records
confirmed completion; no reviewer tool use occurred and source hashes stayed
stable.

## Operational limits and next action

The Mac's existing worker has already caught up the available game/result data without this uninstalled patch. Its long cycle and the old source failures remain in historical records; no warning was suppressed. Two Ladd McConkey NFL selections correctly remain pending: the verified player-stat feed has no player row and no verified participation manifest is available. Tonight's five NFL selections are legitimately pending before kickoff. Subsequent normal source retries remain responsible for new evidence.

The indexed full report's 1.160-second replay establishes output parity and removes the measured quadratic work. A full source-worker cycle on the repaired code has **not** run, so no new end-to-end refresh duration is claimed. The patch does not solve Mac sleep or an upstream provider denying requests. Future code release must preserve the live services, reports, symlinks and historical ledgers and be checked through the existing guarded worker.

Confirmation is needed before committing and releasing these exact three files. No Windows cutover or final archive is included.
