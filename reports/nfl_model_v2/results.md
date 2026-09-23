# NFL model research v2 findings

All records below are retrospective diagnostics. The models were selected
with knowledge of prior project replays. Week 3 onward in 2026 is reserved
for forward evaluation; Weeks 1–2 were used only as completed training inputs.
**PASS remains the only designation.**

## Strongest family and market comparison

The regularized linear model is the forward research leader because it had
the lowest 2023–24 development margin and total MAE among the learned
families (10.1434 and 10.2766 on 544 games). The fixed ensemble had the
best learned-family win Brier (0.223595 versus linear 0.226103) but worse
score errors. The closing market beat both on all three development metrics:
9.7555 margin MAE, 9.9844 total MAE, 0.209436 Brier. The market also beat
the linear candidate on each of these metrics in 2025.

The following table uses the same 272 completed 2025 regular-season games;
one tie is excluded from binary win scoring. ATS and totals cells show
wins/graded decisions, hit rate, and the **95% game bootstrap interval**.
Pushes and model/line equalities are excluded from decision denominators.
The closing market is a forecast comparator, so it has no independent
against-itself betting side.

| Forecast | Margin MAE | Total MAE | Win Brier | ATS wins/n, hit rate [95% CI] | Totals wins/n, hit rate [95% CI] |
| --- | ---: | ---: | ---: | --- | --- |
| League/home | 11.0629 | 11.0184 | 0.248776 | 136/271, 50.18% [44.28%, 56.09%] | 137/272, 50.37% [44.49%, 56.25%] |
| Fixed Elo | 10.4086 | 11.0184 | 0.228545 | 141/271, 52.03% [46.13%, 57.93%] | 137/272, 50.37% [44.49%, 56.25%] |
| Rolling form baseline | 10.3749 | 10.6988 | 0.226585 | 141/271, 52.03% [46.13%, 57.93%] | 127/269, 47.21% [41.26%, 53.16%] |
| Current v1 score model | 10.3771 | 10.8298 | 0.227605 | 132/271, 48.71% [42.80%, 54.98%] | 129/272, 47.43% [41.18%, 53.68%] |
| Richer Elo | 10.4451 | 10.6988 | 0.228968 | 134/271, 49.45% [43.17%, 55.35%] | 127/269, 47.21% [41.26%, 53.16%] |
| Boosted trees | 10.3724 | 10.7905 | 0.229023 | 127/271, 46.86% [40.96%, 52.77%] | 138/272, 50.74% [44.85%, 56.99%] |
| **Regularized linear** | **10.3179** | **10.6725** | 0.229599 | 130/271, 47.97% [42.07%, 53.87%] | 144/272, 52.94% [47.06%, 58.82%] |
| Linear/boosted ensemble | 10.3033 | 10.7177 | 0.227731 | 127/271, 46.86% [41.33%, 52.77%] | 142/272, 52.21% [46.32%, 58.46%] |
| Closing market reference | **9.7224** | **10.3934** | **0.212139** | — | — |

In 2025, the linear model's paired error minus market error was +0.596
margin points (272 games; 95% bootstrap interval +0.206 to +0.982),
+0.279 total points (272; -0.119 to +0.692), and +0.01746 Brier
(271; +0.0061 to +0.0286). Positive means the market was better. The
2023–25 pooled linear results were 10.2016 margin MAE, 10.4086 total MAE,
and 0.227266 Brier on 816 games (815 non-ties); the market was 9.7445,
10.1207, and 0.210334, respectively. The model improved on league/home,
fixed Elo and simple rolling scoring for selected errors, but not on the
closing market.

## Features and ablation

The largest standardized linear margin coefficients fitted through 2024
were opponent-adjusted offensive EPA gap (+1.787), rolling margin (+1.735),
offensive success gap (+1.426), Elo margin (+1.245), raw defensive EPA gap
(+1.013), and opponent-adjusted defensive EPA gap (-0.889). Correlated
coefficients are not causal feature effects. In the total model, rolling
total (+2.128) and combined offensive EPA (+0.972) were largest.

Each row removes one full feature group, refits before every 2023–24 week,
and shows the change in error versus the full linear model on the same 544
games. Positive deltas mean the group helped the full model.

| Removed group | Margin MAE change | Total MAE change | Brier change |
| --- | ---: | ---: | ---: |
| Score, Elo, rest, home/travel context | -0.0060 | +0.1629 | -0.001495 |
| EPA, success, opponent adjustment | +0.0553 | +0.0303 | +0.002061 |
| Early downs and explosives | +0.0259 | -0.0233 | +0.000115 |
| Pressure, sacks, turnovers | -0.0096 | -0.0046 | -0.000526 |
| Historical QB strength/change | +0.0133 | -0.0250 | -0.002142 |

Only the EPA/success/opponent-adjustment group helped all three development
scores. The pressure/turnover group slightly hurt all three; the other
groups have mixed effects. These ablations are diagnostic, with small
differences vulnerable to sampling noise. No feature group was dropped
based solely on these results.

## Exploratory betting subsets

The best-looking predeclared edge bin for the linear candidate was totals
with a 2–4 point model/line difference: **133/240, 55.42%**, 95% bootstrap
interval **49.17%–61.67%**. The corresponding ATS bin was **137/258,
53.10%**, interval **47.29%–59.30%**. Both intervals cross the 52.38%
break-even rate at -110, even before adjusting for multiple inspected bins
and before verifying historical price availability. No subset shows a
statistically credible betting edge. The full exploratory bins and their
counts/intervals are in `research_results.json`; no threshold is adopted.

## Forward record and policy

The existing live v1 board has **16 immutable 2026 Week 3 PASS freezes and
0 graded games**. A separate linear research candidate froze all 16 Week 3
games at **2026-09-23 11:35:05 ET**, more than 32 hours before the first
kickoff; all 16 have an existing frozen pregame market quote. Its file
includes 1,903 prior completed training games, numeric features, input
hashes, model outputs, and an exclusive-create guard. It has **0 graded
games**. The candidate cannot issue or upgrade picks.

Real-money use remains blocked by the absence of an untouched graded
forward sample, market superiority in historical scoring, no validated
positive-EV subset, incomplete pregame QB/injury/forecast snapshots,
unverified reference closing-price provenance, and the current Odds API
credit shortage. Keep PASS until a prespecified policy clears these gaps.
