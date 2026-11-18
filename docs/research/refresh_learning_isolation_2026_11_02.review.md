# Refresh isolation and verified NBA participation — release review

Prepared November 2, 2026, from approved base
`d6392d701c83d48f32e7a2b8224cfc26af649a66` in the isolated
`fix/refresh-learning-isolation` worktree. The user approved the implementation
plan. Commit and local release still require separate confirmation.

## Result

The five-minute research worker now schedules heavy learning instead of running
it inside the source/grading lock. A separate automatic offline learner handles
diagnostics, fitting and expensive verification. Brief model publication checks
the exact relevant evidence again before activating. Ordinary source capture
and grading keep their existing cadence and quota guards.

Four completed-game NBA props have explicit official inactivity evidence.
The Portland parser now recognizes `Trail Blazers`, and the ordinary grader has
a bounded official-gamebook fallback for selected players missing from a
reconciled final box score. Unsupported or contradictory evidence stays pending
and produces a visible source failure; absence never becomes a zero.

No model formula, parameter, training cutoff, forecast, selection, existing grade,
quota or validation threshold was changed. PASS and RESEARCH_ONLY remain. No
Odds API request, real training or demonstration provider request was made.

## Timing finding

The approximately 1h57m historical interval was the entire refresh cycle, not
measured learning CPU time. Learning rows inherited the outer cycle's start.
NBA source receipts were around 08:03 EST, diagnostics finished around 08:04,
and sequential retuning finished around 08:12 (about 7m33s between those latter
observations). New independent jobs record actual start/end, elapsed monotonic
time and CPU time. The old whole-cycle timing does not prove every delay was
caused by fitting; isolation removes the demonstrated shared-lock bottleneck.

## Learning and publication

- Durable queued/running/completed/failed/interrupted/deferred jobs and
  append-only events use one separate OS flock. Stale heartbeats never permit
  a duplicate fitter. Deadlines and stopped processes recover on the next run.
- Registered proposals have atomic checkpoints. A busy research lock or
  near-kickoff publication defers that same proposal instead of refitting its
  consumed holdout. A successful retry cannot resurrect an older checkpoint.
- Hashing and full evaluation replay occur outside the research lock and SQLite
  writer transaction. Short publication checks compare relevant current result
  revisions, game/quote/source identities, baseline event, code/artifact hashes,
  source file seals and holdout ownership. Already superseded labels fail closed.
  Legacy NBA evaluation rows also require the exact official final-source kind,
  route and current content hash; a later same-byte polling receipt remains valid.
- The spread path also checks priority at publication time, covering fits that
  start early and finish in the pregame capture window. Corrections or dependency
  changes prevent stale activation; prior holdouts stay spent. Rollback predecessor
  verification likewise runs before the short transaction.
- The learner strips provider credentials before configuration import, skips
  dotenv reloading, blocks networking, lowers priority and caps numerical
  threads. The Mac LaunchAgent carries only required nonsecret state settings.
  Prepared Compose adds a keyless, networkless, single-CPU learner role.
- The existing hosted source controller retains its 900-second ceiling. The
  learner has separate three-hour wall and one-hour CPU ceilings. These are
  maximum resource budgets, not claimed expected durations.
- About & data shows separate job health and timing. A healthy source tick or
  queued retry cannot hide an unresolved learner failure. Integrity checks and
  scheduling are no longer presented as a completed fit.

## The four pending props

Archived source rosters and exact captured quote events agree with all three
saved player identities and teams. Each official gamebook matches the frozen
game date, teams and final score and explicitly names the selected player
inactive. The relevant final pages were inspected directly.

| Saved game | Player | Pending selections | Expected existing-rule result |
|---|---|---:|---|
| Oct 29, MEM 124 at POR 103 | Robert Williams III | 1 | Void |
| Oct 30, NYK 93 at PHI 98 | Jaylen Brown | 2 | Void |
| Oct 30, LAL 95 at GSW 112 | Ziaire Williams | 1 | Void |

Official receipts:

- [MEM–POR gamebook](https://statsdmz.nba.com/pdfs/20261029/20261029_MEMPOR.pdf)
- [NYK–PHI gamebook](https://statsdmz.nba.com/pdfs/20261030/20261030_NYKPHI_book.pdf)
- [LAL–GSW gamebook](https://statsdmz.nba.com/pdfs/20261030/20261030_LALGSW.pdf)

Exact receipt times, URLs, byte counts, SHA-256 values, PDFs, rendered pages and
identity previews remain in private review evidence.
The copied-data release rehearsal yielded exactly four new void revisions,
zero unresolved target props, and zero duplicate revisions on a second grading
pass. All fifteen original selections, their forecasts/locks and earlier grades
were unchanged. Wins and losses do not change when these four voids are added.

The future automatic fallback only checks absent sealed players after a verified
reconciled final. It allows exact trusted HTTPS routes without redirects,
limits downloads to 10 MB and bounded streaming time, tries at most two URL
variants per game and four requests per grading cycle, and caches unsuccessful
attempts for fifteen minutes. It requires frozen identity/hash, game/date/score
and explicit inactive proof. Machine verification is labeled separately from
directly inspected proof. Day-To-Day display is corrected after the existing
inactive eligibility guard, preserving who can be selected.

## Validation

All checks ran against isolated temporary state with networking disabled:

- **389 Python tests passed** across refresh/manual/visit scheduling, five-minute
  NFL grading, worker recovery, publication/store guards, spread isolation,
  deployment lifecycle, NBA sources/participation/grading, health and records.
- **97 additional Python tests passed** across NBA/NFL learning adapters and
  roles, active-model integration, policy evidence, public APIs/product views
  and unchanged state-archive behavior. Total: **486 Python tests**.
- **25 JavaScript tests passed**, including separate job status, safe text,
  queued-retry failure visibility and existing season records.
- A real separate refresh/grading process and SQLite write completed while a
  controlled fit was blocked. Child-process tests cover duplicate-run exclusion,
  SIGTERM recovery and independent wall/CPU deadline handling. No real fit ran.
- RED-to-GREEN evidence covers the original inline dispatch, publication races,
  source parser/fallback, retry recovery, lock safety, stale health, source
  request budgets and a failed/deferred checkpoint followed by successful retry.
- New queue/offline/dispatch helper executable-line coverage was 163/176
  (**92.61%**); participation validator **90.6%**, collector **96.7%**. These are
  scoped line measurements, not whole-repository or branch coverage. CLI and
  scheduler child/thread execution have separate behavioral tests.
- Publication helper coverage was 135/153 (**88.24%**), registry helpers 107/114
  (**93.86%**) and spread-isolation helpers 139/157 (**88.54%**) on the exact final
  source hashes, with the same scoped line-measurement limitations.
- An actual browser fixture preview at 390 px had no horizontal document
  overflow or undefined/NaN/invalid-date output; queued and failed job states
  remained visible. Preview uses production CSS and rendering functions.
- `git diff --check` passed. Code/security review includes the
  offline runner, publication guards, official-source boundary and guarded
  evidence-only release script. Its final receipt is stored with the exact diff.

Review resolved two publication/recovery findings before confirmation:
old checkpoints surviving a later successful retry, and a legacy evaluation
using a grade whose official final source had already been corrected. Both have
regressions. The shared source fixture was made realistic; production did not
receive a test exception for unknown source kinds or routes.

## Live status and release boundary

The live Mac checkout remains at `d6392d7`. At the latest read-only check,
`/api/health` reported healthy (demo disabled), the latest three completed source
ticks succeeded, and these four props were still pending. The snapshot used
SQLite query-only mode and made zero changes. No source, grade, model or service
was changed in the live checkout for this task.

The live spread-learning event table is empty and the prop-learning registry
contains only prior rejected decisions, with no learned activations. The
Day-To-Day display file participates in spread code seals; there is therefore
no currently active learned artifact to invalidate in this release. Existing
seal checks remain strict for any future artifacts.

After commit/release confirmation, the intended local release is to apply only the
reviewed code, load the separate learning LaunchAgent, retain the existing web
and five-minute source services, register the three reviewed receipts using the
guarded evidence-only tool, and verify the ordinary grader's four void revisions
plus source and learner job health. The release tool requires the exact approved
committed HEAD, clean production sources and immutable original evidence hashes;
it never writes grades itself. Model changes remain subject to existing gates.

The Mac still must be awake for automatic work. Windows deployment and cutover
are not performed here. The new learner is a third writer: all three services
and both locks must be quiescent before any future final state archive. The
archive tool remains unchanged; no symlinks were materialized, credentials sent
or final archive created.

## Release review

Proposed commit: `fix(refresh): isolate learning and verify NBA participation`.
The scoped manifest and exact diff were recorded as review artifacts; only
enumerated production, tests and research documentation are proposed for commit.
No blanket staging.

Commit requires confirmation after presenting the diff summary and proposed
messages. Commit and local release are therefore awaiting confirmation.
