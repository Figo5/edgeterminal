# Continuous learning integration review — 2026-10-28

The approved change adds controlled NBA/NFL learning to the existing built-in
refresh. Future versions may advance their training cutoff. Saved forecasts,
quotes, selections, grades and prior versions remain immutable.

## Orchestration and review

- Registry agent: immutable JSON/source/code archives; append-only decisions;
  independent sourced metric gates; verified predecessor rollback.
- NBA agent: season-aware production/minutes proposal, count uncertainty,
  exact capture-source replay and daily cohort diagnostics. Independently
  reviewed registry, controller and NFL serving/sealing.
- NFL agent: chronological whole-week Ridge/calibration proposal and saved
  selection diagnostics. Independently reviewed NBA source/serving and worker
  wiring.
- Parent: built-in scheduling, serving/API/UI integration, source-priority
  ordering, per-cycle integrity recovery and live verification.

Review fixed repeat-attempt version collisions, unverified excluded void
evidence, mixed-version capture risk, the game/prop evaluation-window mismatch,
and integrity recovery between nightly checks. Review did not change candidate
prediction parameters after inspecting the held-out results.

## Verification

- Full Python suite: 972 passed.
- JavaScript UI suite: 17 passed; JavaScript syntax and git whitespace checks
  passed.
- Actual worker run: success; nightly diagnostics recorded for both sports;
  NBA and NFL proposals correctly rejected and current models retained.
- Replay/provenance: NBA 197 actual lines /40 games; NFL 214 lines /43 games.
- Preservation snapshot: all 16,617 existing protected table rows remain
  present byte-for-byte; all nine protected original model/freeze files retain
  their hashes.
- Running application restarted; both status APIs expose enabled learning,
  current baseline versions, recorded decisions and future check timestamps.
  About & data rendering was checked in the local browser.
- NBA page checks report NBA source/learning health and do not inherit the
  unrelated NFL weekly-snapshot warning. Actual NBA learning failures remain
  visible. Live NBA check is healthy; NFL retains its separate freeze warning.

The first-cycle summary is in
`reports/model_learning/initial_cycle_2026_10_28.json`. Model-specific evaluations
are in `reports/nba/learning_initial_2026_10_28.json` and
`docs/research/nfl_weekly_learning_validation.json`.

An existing NFL weekly game-snapshot warning remains: the original freeze
guard refuses a new weekly snapshot while model code changes are uncommitted.
This review does not waive that guard or commit unrelated/pre-existing work.
The source/prop grading and new learning cycle completed successfully.

No new Codex automation, provider subscription, Odds API access, official pick,
external message, commit or push was introduced. PASS / RESEARCH_ONLY remains.

## Release follow-up

The user subsequently requested fixing the identified snapshot blocker. The
reviewed implementation was committed, the pre-kickoff Week 8 bundle was
created and deeply verified, and live status is now healthy with no warnings.
See `docs/research/release_fix_2026_10_28.md` for the 989-test release verification
and source/record preservation evidence. The earlier commit warning above
describes the initial learning review and is now resolved.
