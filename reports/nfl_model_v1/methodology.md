# NFL model v1 retrospective diagnostic

Generated 2026-09-23 for the 2023, 2024, and 2025 regular seasons. This is a
retrospective research diagnostic, not a validated betting policy or a forward
record. The model was built after these seasons and its settings were changed
after an earlier 2025 replay. None of the three is an untouched final test.

## Inputs and cutoff

- Schedule, final scores, reference closing spread and total: cached nflverse
  `games.csv.gz`.
- Team play-by-play EPA and success rate: cached nflverse PBP for each evaluated
  season and the preceding season (2022 through 2025).
- For a game in Week W, current-season team scoring and PBP features use only
  games from weeks strictly before W. Week 1 uses the completed prior season.
- Neutral-site games receive no home-field adjustment. Ties are excluded from
  binary win-probability scoring and calibration.
- Replay applies the same scoring-rate shrinkage and quantitative score model
  as live NFL analysis. Live ESPN stat aggregation and sourced context feeds are
  not reproduced historically; the replay is therefore not a perfect
  production-path simulation.
- Closing market lines are evaluation comparators, never model features or
  calibration targets. Their availability as a pregame quote at a particular
  timestamp is not independently proven here.

## Fixed baselines

- Home/league: 1.7-point home advantage (zero at neutral sites), 44-point total.
- Elo: 1500 initial rating, 45 rating-point home adjustment, K=20, and 28
  rating points per projected margin point; completed earlier weeks only.
- Rolling form: last eight completed games per team, with four 22-point
  pseudo-games for scored and allowed; 1.7-point home advantage, zero at
  neutral sites.
- Market: nflverse reference closing spread and total, on matched games only.

The comparison-baseline formulas were specified for this diagnostic. The
candidate model's settings were **not** preregistered before the historical
seasons. No pick-gate optimization was performed in this report, but the past
seasons must not be treated as independent validation.

## Main findings

Each season has 272 games. In 2025, the model's margin MAE was 10.3771 and
total MAE was 10.8298. The market reference was better at 9.7224 and 10.3934.
The paired bootstrap 95% intervals for model-minus-market MAE were
+0.2092 to +1.0754 points for margin and +0.0758 to +0.7836 for total. The
model went 132-139 ATS with one push (48.71% of decisions) and won 47.43% of
graded total sides. Its 2025 home-win Brier score was 0.227605 on 271
non-ties; the closing-line probability baseline scored 0.212151. The model
also trailed the market on margin and total MAE in 2023 and 2024. The 2024
ATS hit rate of 52.61% is a retrospective, selected-model result and is not
evidence of a validated pick policy. Edge-bucket hit rates have wide Wilson
intervals and are not monotone.

At -110, the break-even win rate is 52.38%. Recorded closing prices imply a
2023 spread/total ROI of -10.30%/-8.01%, 2024 +0.98%/+0.76%, and 2025
-6.82%/-9.20% on one-unit stakes. The timing and availability of those prices
for a real pregame decision are unverified. These ROI values are diagnostics,
not attainable returns.

## Policy

All NFL market designations remain **PASS**. The quantitative projection and
observed quote can be inspected, but there is no evidence-backed OFFICIAL or
LEAN threshold. A future policy must be preregistered, evaluated on an
untouched chronological period, then tracked in forward shadow mode.

Full counts, Brier/log loss, calibration, Wilson intervals, paired MAE intervals,
price-based diagnostics, and baseline errors are in `backtest_results.json`.
`replay_manifest.json` records the code commit, exact commands, Python version,
bootstrap seed, and SHA-256 hashes for the replay source files and cached public
data assets used here. A future code or data change requires a new manifest and
report; it cannot retroactively make these seasons untouched.
