# Automatic NBA and NFL learning

EdgeTerminal uses its existing five-minute built-in research worker and a
separate offline learning process. It does
not require an external heartbeat, an open browser, or a user to request grading.
The Mac must be awake and online for source refresh and final grading. Fitting
itself reads archived local data and makes no provider requests.

## Cadence and decisions

- Completed games update the existing prior-game player inputs on normal
  source refreshes. Forecasts are saved before kickoff and never replaced.
- After 4 AM Eastern, the first available worker tick queues daily diagnostics.
  They describe saved probability accuracy, errors, market/version coverage
  and NBA role/season-transition cohorts. They do not tune parameters.
- A first retuning attempt runs immediately; a completed activation or rejection
  is followed by another scheduled attempt seven days later. Insufficient data
  waits 24 hours, or at least one hour plus new grade evidence. Failures retry
  after five minutes. An upcoming game within an hour postpones fitting so
  capture and grading take priority.
- Each candidate has disjoint fit/development, calibration and whole-game
  validation sets. Validation uses sourced actual locked main lines and final
  results. At least 30 games, 100 non-push rows, and 20 rows across ten games in
  every supported market are required.
- Promotion requires no market-level MAE, Brier or log-loss regression, at least
  0.002 aggregate Brier improvement, and a negative upper 95% bound for the
  paired per-game Brier difference. The gate rechecks quote/result identities,
  archived bytes, receipt times and the active comparison version.
- A rejected candidate is retained as evidence, while the current model keeps
  serving. No user-facing challenger is added. Future versions may have newer
  cutoffs; each old version retains its original cutoff and parameters.

## Integrity and recovery

Candidate parameters and provenance are plain immutable JSON. The registry
archives source and implementation bytes and appends activation/rejection and
rollback events. It does not load newly trained pickle/joblib objects.

Every worker learning tick checks the active artifact and implementation
hashes. Nightly checks also verify archived sources and the latest result
revisions. A failed active integrity check restores the nearest verified
approved predecessor, or the original baseline, with an audit event. A losing
night alone cannot trigger a rollback. Damaged original baseline artifacts
still fail closed through their existing integrity guards.

About & data displays the current version, last learning decision, diagnostics,
queued/running/failed jobs and next scheduled check. Reads never train or
activate a model. Heavy diagnostics/fitting use a separate process and OS lock;
only brief verified publication uses the source worker's research lock. No
expensive proof replay holds the SQLite write transaction. Current relevant
result/source/active-version fingerprints are checked before a decision.

Job heartbeats never authorize duplicate fitting while the OS lock is held.
Interrupted jobs recover automatically; durable registered proposals resume
publication without consuming their held-out cohort again. A queued retry does
not clear a failed check. Actual per-job wall/CPU timings replace the misleading
shared outer-worker start timestamp for new automatic jobs.

The independent learner has three-hour wall and one-hour CPU resource ceilings,
one numeric-library thread, no provider credentials and blocked networking.
These ceilings do not change the normal worker's 900-second hosted timeout,
model parameters, cutoffs, source quotas or validation rules. Stop both source
and learning writers for any future final state archive or host cutover.

Scheduled and hosted learning starts with `python -m edgeterminal.learning_cli run`.
The small launcher strips credentials before importing configuration, then imports
the worker under its canonical package name. Starting the worker module directly
as `__main__` creates a separate job-owner context from the module that model
stores import to save checkpoints. That loses registration checkpoints after a
busy publication or interruption. The launcher keeps one owner context, so
registered candidates resume without refitting or reusing their outer holdout.
The frozen worker implementation and model-code seals remain unchanged.

## First evaluation, October 28, 2026

The NBA proposal was checked on 197 actual selected lines across 40 completed
games. Aggregate MAE improved from about 4.200 to 4.166, but Brier increased
from 0.3011 to 0.3026 and some markets regressed. It did not earn activation.

The NFL proposal was checked on 214 actual selected lines across 43 games in
Weeks 5–7. Brier increased from 0.2642 to 0.2845, with market-level regressions.
It did not earn activation. The existing NBA `nba-research-2.0.1` and NFL
`nfl-opportunity-empirical-2.0.0` therefore remain the active models.

These are acceptance diagnostics, not new forward picks or a guarantee of
future gains. A scheduled learning process can adapt and test improvements;
it cannot guarantee a rising win rate. Everything remains PASS / RESEARCH_ONLY.
