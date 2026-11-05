# Planner review: NBA totals Gate 1

Planning only, in `/Users/giofiore/edgeterminal-nba-totals` at base `0082fa8f672bed0fbb6da09ff39fedf49ded5a41`. Read the ECC planner instructions, orch-pipeline, audit, relevant source and existing tests. No implementation, test execution, fitting, provider requests, DB initialization, service changes or commits performed.

I recommend the consolidated `nba_totals_2026_11_05.plan.md` for Gate 1 approval. Its six tasks are the implementation task_list. The following source-specific details refine that plan without weakening its gates.

## Concrete architecture

- Add `edgeterminal/nba_totals_model.py` for pure season/pace features, artifact validation, total distributions and composition; `nba_totals_data.py` for exact evidence/universe/replay; `nba_totals_store.py` for append-only protocol/candidate/event/run/holdout/proof records; `nba_totals_learning.py` for offline chronology/calibration/evaluation/publication. Do not put totals into the prop-specific `model_learning_store.py`.
- Compose in `nba_ops.py::_refresh_game_projection` after `nba_spread_model.project_game`. Preserve raw/home margin, winner/ATS probabilities and anchor fields; recompute both score aliases from total plus unchanged margin within current tolerance. Take one totals artifact snapshot per tick. No eligible approved artifact means incumbent numerical output.
- Reuse unchanged `nba_game_forecasts.save_revision/selected` and grading. Extra totals fields are already snapshot-hashed, but need mandatory semantic validation before saving and independent replay. Add totals version/fingerprint/cutoff/branch/probability method and source-bound submanifest; union/deduplicate used rows and source IDs into existing manifests. Preserve spread component identity and add composite metadata.
- `nba_game_forecasts.validation_snapshots` is spread-gated: its line/result checks exclude total-only games. A separate totals verifier must enumerate complete-date incumbent support, original total lines, exact selected forecast hashes, grade chains and verified final identities without using that filter.
- Extend `learning_worker.COMPONENTS/_execute/checkpoint_candidate` with `NBA_TOTAL`; reuse offline runtime, OS lock, resource bounds and checkpoint recovery. Reuse `learning_publication.publish_lock/same/file_seals`; totals needs its own active/comparator/evidence state because `spread_state` binds a spread event. No new service/scheduler is necessary.

## Model/data refinements

Retain ten-row support and four-hour completion lag. Separate season/current/recent scoring rates with restrained shrinkage; predeclare a small development-only grid such as shrinkage `{5,10,20}` and recent weights `{0,.25,.5}`. Compare unchanged baseline and earlier-data intercept-only correction. Do not subtract the audited +5.74 and describe it as independently validated.

Pace uses the existing symmetric-box estimate as raw source evidence, independently verified duration/OT normalization, and opponent-adjusted points per possession. Unknown duration stays missing. A proposed fixed support rule is five verified pace/duration rows per team and at least half of each selected window, plus supported priors. Below support, retain the approved score-only branch or incumbent. Avoid double-counting rest adjustments.

Historical schedules provide scores, not necessarily possession/duration/lineup training coverage. Implement the pace branch with explicit waiting/shadow status if earlier evidence is inadequate; normal final grading accumulates evidence automatically. Season-only promotion can proceed independently, while enabling pace requires a new frozen candidate and fresh dates in the same family. Availability remains context, with no final-DNP leakage or arbitrary injury deduction. Preserve all supported games through fallback.

Calibrate total residuals separately from fit/development; displayed mean and probability mean agree. Preserve integer push mass and conditional nonpush probabilities. Missing total line prevents its line-based decision/grade, never a supported score forecast.

## Frozen validation checklist

Follow the consolidated plan's chronological whole-ET-date fit/development/calibration/outer-holdout separation, minimum 60 unused prospective pairs across seven unused full dates, material MAE/Brier improvements, paired-date 95% support, no RMSE/absolute-bias/win/coverage regression, and saved-line MAE gate. Suggested earlier feasibility floors are 900 multi-season score examples and separate 120-game development/calibration prefixes; freeze actual minimums before evaluation. These score counts do not establish pace evidence sufficiency.

Seal outer games and full dates atomically **before reading labels or computing metrics**. Rejections/errors remain consumed, including across family-name changes. Existing spread code evaluates before registration; do not copy that ordering. Inspected audit dates stay development-only. Missing finals delay their full date, missing pace falls back, integrity conflict blocks an attempt, and no loss-driven subset or spread-line requirement is permitted. Historical reconstruction must not backdate receipts. Later completed games can be future prediction inputs without parameter refits.

## Exact seal and portability surfaces

- `nba_model_manifest.py` pins `nba_model.py`, `nba_prop_model.py`, `pricing.py` and prop artifact. Existing adaptive props also seal `nba_learning.py`, `nba_learning_data.py`, `nba_sources.py`, `nba_data.py`, `nba_quotes.py`, `nba_identity.py`, `nba_model_fit.py`, `analytics/nba.py` and manifest. Keep bytes unchanged.
- `nba_spread_learning.code_catalog` seals `nba_spread_model.py`, `nba_spread_learning.py`, `nba_spread_data.py`, `nba_spread_market.py`, `nba_model.py`, `nba_sources.py`, `nba_identity.py`, `nba_game_context.py`, `nba_game_forecasts.py`, `pricing.py`, `analytics/nba.py`, manifest. In-place shared OT/snapshot parsing changes would invalidate accepted spread artifacts despite unchanged equations; prefer additive parsing/verifiers.
- Seal actual totals train/serve/feature/evaluation dependencies, including composition in `nba_ops.py`; archive immutable source under `data/research/nba_totals/code/<sha>.py`. Test code changes between fit/registration/publication. Never reseal old artifacts or relax checks.
- Store new ledgers in the existing main DB; optional immutable JSON/code/report files under `data/research/nba_totals` and `reports/nba_totals`. Preserve raw sources/receipts/digests and relative portable paths; reject traversal/symlink escapes and test `/app` relocation.
- `deploy/state_backup.ARCHIVES` already includes research/nflverse/reports; online backup retains all tables/triggers/committed WAL. No format change expected. Synthetic restore must retain protocols, holdout ownership, sources/code, active events, original receipt times and old grade chains. Existing runtime revision checks and worker/learner roles suffice. No live archive or writer stop belongs to this planning task.

## Meaningful tests and principal risks

Add `test_nba_totals_data/model/store/learning/integration/learning_isolation.py`. Cover season rollover/few current games; pace vs efficiency synthetic examples; OT/missing duration; duplicate/numeric-ID conflicts; tampered cached source bytes; future receipts; total-without-spread; supported score-without-line; integer pushes/ties; raw anchored-margin reconciliation; full-date late labels; seal-before-label access; consumed failed holdouts; stale active/code/result proof; near-tipoff capture priority; interruption/resume without refit; and synthetic restore preserving old NFL/props.

After approval, use temporary DB/research/report roots and denied providers/network. Run relevant existing game-forecast/live integration, spread/anchor/isolation, worker/publication, grading/ops, model-version/pinned-NBA/common-learning, NFL five-minute, archive/runtime and NBA JavaScript regressions. Verify protected bytes independently of mocks. No live data symlinks or credentials reads.

Principal risks are inadequate historical pace evidence, correlated nightly errors, old outcomes leaking into fresh validation, source-seal invalidation and accidental capture-lock contention. The consolidated plan addresses them with baseline fallback, whole-date sealed holdouts, additive modules and the existing independent learner/publication architecture. Numerical activation may correctly remain waiting after code delivery.

Gate 1 still requires user approval before implementation/tests; Gate 2 requires separate commit confirmation. The site, five-minute cadence, existing saved state and live numerical models remain unchanged during planning.
