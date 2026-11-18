# Snapshot and model release repair — October 28, 2026

The NFL Week 8 snapshot was blocked because `nfl_repro_bundle.code_commit()`
correctly refuses uncommitted package or dependency changes. The user requested
fixing this identified blocker and completing the site/model release.

The repair retains the strict guard and commits the reviewed implementation,
tests, required frozen model assets and supporting research documentation.
Five temporary-Git regression cases verify that uncommitted tracked, untracked,
staged and dependency changes are refused, while committing reviewed code
restores acceptance even with unrelated local research results still dirty.

## Additional review fixes

- NBA and NFL health views attribute source and learning failures to the
  affected sport. Shared worker/runtime failures remain visible to both.
  The shared throttle and complete audit history are preserved.
- New NBA learning artifacts also fingerprint imported uncertainty constants
  and the baseline manifest. A dependency change cannot silently retain an
  approved adaptive version's identity.
- The active NBA and NFL frozen model bytes remain unchanged. Existing
  forecasts, selected main lines, locks and grades are protected. Neither
  previously rejected candidate is promoted by this release.

Required release assets include NBA's parameter JSON and code manifest, and
NFL's fixed manifest, reviewed estimator and manifest checksum. Runtime
candidate/source archives, the SQLite database, credentials, logs and generated
capture ledgers remain local. No remote push or new automation is needed.

The operational verification is to generate Week 8's 14-game snapshot before
the first kickoff, deeply verify its reproducibility bundle, run the shared
worker and read back both sports' live status. The snapshot path must remain
exclusive, and the original Week 3 bytes must remain unchanged.

PASS / RESEARCH_ONLY, ESPN game quotes, SportsGameOdds quota guards and
five-minute final-result grading remain in force. This release improves
reliability and validation; it does not promise a higher future win rate.

## Verified release result

- Implementation commit: `6e3a71bf7dfd4ee39f3004cbdfb78e90974d437f`.
- Full verification: 989 Python tests and 17 JavaScript tests passed.
- Week 8 snapshot: 14 games, 1,978 prior training games and ESPN quotes for all
  14 games. All 123 bundled artifacts passed deep verification. The snapshot
  was created before kickoff and records the committed implementation.
- Normal built-in worker completed successfully. NFL and NBA check endpoints
  both return `ok: true`; live product status is `healthy` with no warnings.
  The LaunchAgent remains loaded at a 300-second interval.
- The NBA/NFL active versions remain `nba-research-2.0.1` and
  `nfl-opportunity-empirical-2.0.0`. Both have successful diagnostics; rejected
  experimental updates remain inactive.
- All 16,617 pre-existing protected rows and nine original model/freeze files
  passed the preservation check. The original Week 3 snapshot still matches.

Machine-readable evidence: `reports/model_learning/release_health_2026_10_28.json`.
The live NFL board was also checked in the browser: the snapshot warning has
cleared, current quotes are displayed and games awaiting five sourced props
show their guarded next check. Missing props are not backfilled.
