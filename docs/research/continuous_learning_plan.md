# Controlled continuous learning

Approved scope: daily input adaptation, nightly error diagnostics, periodic
retuning, and automatic activation only after validation. The user approved
moving future model versions beyond the original fixed training cutoffs.
Original models, forecasts, main-line observations, five-prop locks and grades
remain immutable. One active model serves each sport; this adds no separate
user-facing challenger or Codex automation.

## Implementation slices

1. Add an append-only model registry with verified immutable JSON artifacts,
   recorded source/code hashes, training cutoffs, evaluations and activation or
   rollback events.
2. Add NBA and NFL learning adapters without editing the existing pinned model
   implementation or parameter files. Before any accepted update they serve
   the existing baseline. Training remains outside quote capture and HTTP
   request handling.
3. Prepare chronological fit, calibration and held-out whole-game cohorts from
   archived sources. Test predictions at actual saved main lines. Training and
   calibration games cannot overlap validation games.
4. Require at least 30 held-out games, 100 observations, and 20 observations for
   every supported market. Independently recompute probability/point metrics
   from sourced results. Require no market-level MAE, Brier or log-loss
   regression and an aggregate Brier improvement. Otherwise retain the active
   model and record the reason.
5. Run nightly diagnostics and weekly retuning through the existing built-in
   refresh worker. Insufficient data is a waiting state, not evidence of an
   improvement. Record failures and retry without provider quota bypasses.
6. Expose current learning state, last evaluation and retained/activated model
   version in About & data. Verify source timing, artifact integrity, training
   and serving consistency, automatic grading and saved-record preservation.

## Statistical and operational limits

An automated learning process cannot guarantee a monotonically improving
future win rate. Validation supports an activation decision; only later saved
predictions provide a new forward assessment. A game used for fitting is not
an independent test of that fitted version. Original October 27 outcomes are
not rewritten or counted as new picks.

Recent inputs may adapt during inference using a version's fixed formulas.
Retuning creates a different version with its own cutoff and evidence. Source
failures, damaged candidate artifacts, invalid schemas or inadequate validation
keep the current model in place. A damaged active artifact restores a verified
prior version or the original baseline. Rollbacks retain all earlier forecasts
and grades.

No provider subscription, Odds API call, official pick, bankroll/stake feature,
email, external notification or recurring Codex automation is introduced.
