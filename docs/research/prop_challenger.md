# Player-prop challenger contract

Goal: improve point forecasts and uncertainty without rewriting the original research record.
Action: save a separate prospective challenger for supported current pregame quotes.
Baseline: original Week 3 model families and residual sigmas.
Hypothesis: blending the fitted forecast with recent usage improves role-sensitive forecasts; empirical residuals capture skew and bias missed by a symmetric normal distribution.
Selection: choose blend weight from 0, 0.25, 0.5, 0.75, 1 using only 2024 mean absolute error. Fit development models on seasons before 2024. Evaluate once on 2025 using models fit before 2025; never choose parameters from 2025 or 2026 outcomes.
Training cutoff: final models use only rows before 2026 Week 3. Later weeks update pregame inputs only.
Probability check: Brier score at synthetic thresholds based on prior usage, not archived betting lines. This is a distribution diagnostic, not evidence of a betting edge or prop win percentage.
Gates: 2025 MAE no worse than baseline and synthetic-threshold Brier no worse than baseline. Failure keeps the challenger in shadow; success still requires prospective evidence before replacing the main model.
Availability: a quoted line does not prove playing time. Retain an explicit unresolved status unless supported by a timestamped availability source. Snap shares and routes are not present in this dataset and must not be fabricated.
Safety: PASS / RESEARCH_ONLY, no official picks, no new provider calls, no historical selections, no modification of immutable freezes or current selection policy.
Artifacts: versioned models, calibration residuals, source hashes, evaluation report and pregame shadow snapshots. Existing main model remains the fallback.

## Recording and availability

The worker appends a new forecast-with-lines snapshot when both fresh ESPN spread and total lines exist before kickoff. It retains the original projection and parent snapshot ID, saves the quote timestamps, and never repairs past games with later lines. Repeated checks leave a complete saved snapshot alone.

Timestamped injury observations are checked before new top-five and challenger rankings. Reported out/inactive and doubtful players are excluded from new selections. Unconfirmed availability remains unconfirmed; missing starter announcements do not prove a quarterback is starting. Existing locked selections remain immutable and display updated availability warnings separately.

The Season record page exposes challenger totals, paired forecast errors, and statistical Brier score. Upcoming challenger picks are not exposed through the public comparison summary. These are separate from the main model's record.
