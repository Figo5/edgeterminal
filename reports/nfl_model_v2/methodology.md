# NFL model research v2: protocol and limitations

Status: **research only**. No model in this report changes the live projection,
the existing frozen Week 3 predictions, or the PASS designation.

## Audit and hypotheses

The v1 score model averages each team's points scored with the opponent's
points allowed, then adds a capped difference in team EPA/play and success
rate. Its v1 replay trails the closing market on margin, total and win
probability in every evaluated season (2023–25). Raw team offense is not
separated from defense faced, game score is noisy, the EPA adjustment is
hand weighted, and the replay lacks validated QB, opponent, and schedule
context. These are hypotheses about the gap; ablation measures whether the
added groups help, and cannot prove a causal explanation.

## Data and information cutoff

The lab reads cached nflverse regular-season schedules and play by play for
2019–25: 256 completed games in each of 2019 and 2020, 272 in 2021, 271 in
2022, and 272 per season in 2023–25. It derives one offense and defense row
per team/game from pass and run plays, excluding kneels and spikes. EPA/play,
success, early-down EPA, explosive rate (20+ pass or 10+ run yards), sack and
QB-hit rates per dropback, and interception/lost-fumble rate are measured.
Opponent adjustment subtracts the aggregate EPA strength of defenses or
offenses faced, calculated only from games already completed at the target
week. A QB's historical strength uses the most recent observed primary
passer, weighted by his prior dropbacks; a recent QB change is an instability
flag. Neither identifies the target game's actual starter. Scheduled rest,
home/neutral status and a coarse away-team time-zone shift complete the
context. Neutral games receive no inferred home-stadium travel shift.

All games in season S, week W share a feature snapshot built from seasons
before S and weeks strictly below W in S. Outcomes from week W are appended
only after all its predictions. For 2023–25, each family is refitted before
each week on every earlier completed game. There is no random split, market
line feature, target-game QB field, postgame weather field, or same-week
result in the training input. Ties remain in margin/total fitting but are
excluded from classifier fitting and binary scoring.

The linear family uses standardized Ridge regression (alpha 100) for margin
and total and logistic regression (C 0.1) for win probability. The boosted
family uses histogram gradient boosting with 60 iterations, seven leaves,
35 minimum examples per leaf, learning rate 0.05, L2 20 and seed 7. The
richer Elo family regresses ratings 30% toward 1500 each season, uses capped
margin-sensitive K updates, and pairs Elo margin with rolling total. A fixed
50/50 linear/boosted ensemble is scored; it is retained only if the
development-period comparison supports it. Hyperparameters are research
choices, not values tuned on a clean external test.

Comparators on matched games are a 1.7-point home/44-point league baseline,
the existing fixed Elo baseline, the existing rolling-scoring baseline, the
v1 live-model retrospective replay, and nflverse reference closing lines.
Closing win probabilities use normalized home/away moneylines when both are
valid; otherwise a fixed-normal spread conversion. Reference closes are
never training features. Their exact pregame publication and achievable
prices have not been independently verified.

## Evaluation discipline

2023–24 are development comparisons; 2025 is a chronological diagnostic
only. The v1 model was already inspected and changed after seeing these
seasons, so none is an untouched final holdout for the project. The first
legitimate out-of-sample test begins with 2026 Week 3, with projections
frozen before kickoff. Completed 2026 Weeks 1–2 enter that week's training
set but were never candidate evaluations. Candidate selection must happen before viewing its
forward outcomes. We report per-season and pooled margin/total MAE, win
Brier, ATS and totals side hit rates and counts. Betting-rate intervals use
2,000 deterministic game-level bootstrap resamples (seed 20260923); paired
market-error intervals resample matched game differences. These intervals
do not account for all within-week dependence or retrospective model search.
The edge-bin and game-condition analysis is exploratory and cannot set a
betting threshold without independent validation and multiplicity control.

Structured injury reports exist for some seasons, but the 2025 nflverse
injury CSV lacks `date_modified` and historical reports cannot be proven to
match pregame availability for every game. Historical schedule temperature
and wind describe game conditions, not archived forecasts. Both are excluded.
The time-zone feature is only a coarse travel proxy. QB starter changes
announced immediately before kickoff are likewise not captured. These
omissions and retrospective closing-line provenance limit any real-money
interpretation.
