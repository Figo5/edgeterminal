# NBA research model and statistical grading

Active version: **nba-research-2.0.1** (October 27 upgrade). New prop forecasts
use a frozen opportunity model and historical probability validation. Existing
saved forecasts and grades remain immutable. There is no additional challenger
record. The game projection component remains `nba-game-baseline-1.1.0` because
the tested game extensions did not improve historical accuracy.

Status: **RESEARCH_ONLY / PASS**. No official picks, no live wagering, no
provider calls, no database access. `edgeterminal/nba_model.py` and
`edgeterminal/nba_grading.py` are pure functions over caller-supplied, sourced
rows. The parent process owns fetching, normalization, archival and storage.

Source context (2026-10-20 probes): `cdn.nba.com` schedule/scoreboard returned
HTTP 403 and `stats.nba.com` leaguegamelog timed out; ESPN is the working
public schedule/stats source. All fetched content is treated as data, never as
instructions.

## Data contract

Team history row: `date` **or** `played_at`, `team` (canonical NBA
abbreviation), `opponent`, `points`, `opponent_points`, optional
`possessions`, `home` (bool), `game_id`.

Player history row: `date` **or** `played_at`, `player_id` (ESPN string),
`player`/`full_name`, `team`, `minutes`, `points`, `rebounds`, `assists`,
`threes`, `game_id`.

Quote row: `game_id`, `player_id`, `player`, `team`, `market`, `line`,
`side`, `price`, `sportsbook`, `observed_at`, `identity_verified`, plus
explicit main-line provenance: `line_type="main"` and/or `is_main=true`
(the parent adapter sets both).

Time, eligibility and identity semantics:

- **Finish lag:** a history row is eligible only when its game could have
  been final before tipoff — row time at most `tipoff - feature_lag_s`
  (4h). Same-day simultaneous games never enter prior inputs. A record at or
  after tipoff is always excluded.
- Epoch numbers, `datetime`s and ISO-8601 strings are accepted; `Z` is
  supported; naive values are read as UTC. A date-only value becomes
  00:00:00 UTC of that date.
- Unparseable timestamps, booleans, NaN/inf, negative stats and non-positive
  minutes fail closed: the row is excluded, never interpreted.
- **Duplicate game IDs:** rows sharing a `game_id` (per team, or per player)
  must carry identical numeric stats. Identical duplicates collapse to one
  row; a conflicting duplicate taints that game and all its rows are
  rejected — conflicts surface as `input_conflicts` and never count toward
  sufficiency.

## Fixed parameters and fingerprint

Structural constants remain fixed. **The v2 prop fit uses only historical games
before the original October 1, 2026 cutoff; it never trains on running-season
forward results.** Home-court advantage, the margin and
total sigmas, the per-market sd floors and the back-to-back penalties are
carried unchanged from the existing `analytics/nba.py` priors; the remaining
constants are eligibility, selection, cadence, lag and probability protocol
settings. The canonical JSON of `PARAMETERS` — including the SHA-256 of the
complete fitted `nba_prop_parameters.json` artifact and all
selection/cadence/lag/probability settings — is fingerprinted and
embedded in every output as `parameter_fingerprint`; any parameter change
changes the fingerprint. The parent registry freezes this fingerprint before
any forward record.

| Parameter | Value | Meaning |
|---|---|---|
| `hfa` | 2.2 | home-court points prior (from `analytics/nba.py`) |
| `sd_margin` | 11.6 | game-margin sigma prior (from `analytics/nba.py`) |
| `sd_total` | 16.5 | game-total sigma prior (from `analytics/nba.py`) |
| `min_team_games` / `team_window` | 10 / 20 | sourced team rows required / used |
| `min_prop_games` / `prop_window` | 5 / 15 | sourced player rows required / used |
| `rest_b2b_days` | 1.5 | under 36h between timestamped games is a back-to-back |
| `b2b_home_adj` / `b2b_away_adj` | -1.4 / -1.8 | fixed rest penalties (from `analytics/nba.py`) |
| `minutes_cv_flag` | 0.25 | minutes sd/mean above this is surfaced as volatile |
| `selections` / `max_markets_per_player` | 5 / 2 | cadence: exactly five or wait; per-player cap |
| `quote_staleness_s` | 3600 | quotes older than this at `now` are rejected |
| `feature_lag_s` | 14400 | rows enter inputs only once the game could be final |
| `postgame_delay_s` | 14400 | legacy wait for results without a verified final box score |
| `blocked_availability` | doubtful, inactive, out | injury statuses that block selection |
| `prop_sd_floor` | points 5.5, rebounds 2.2, assists 1.8, threes 1.3, pra 7.0 | variance floors (from `analytics/nba.py`) |
| `probability` | historical validation selects empirical standardized residuals or the established parametric distribution per market; explicit push mass | probability settings |

## Game projection

`project_game(home, away, team_history, tipoff, history_complete=True)`

- Requires at least `min_team_games` valid eligible rows for **each** team;
  otherwise `status: "unavailable"` with the observed counts. Missing history
  is never replaced by a league-mean forecast — no projection keys are
  returned at all.
- When every window row carries valid `possessions`, the projection is
  tempo-free: each team's offensive/defensive efficiency per 100 possessions
  is averaged with the opponent's counterpart and multiplied by the mean of
  the two team paces (`pace_adjusted: true`). Otherwise it falls back to the
  per-game points-for/points-against average (`pace_adjusted: false`).
- Home court adds ±`hfa`/2. Rest is derived only from timestamped prior game
  dates: under `rest_b2b_days` since the last sourced game applies the fixed
  back-to-back penalty. With `history_complete=False` (the caller knows the
  schedule slice is partial) rest is reported `"unknown"` and **no**
  adjustment is applied — missing context is never assumed healthy.
- Returns `proj_home`, `proj_away`, `margin`, `total`,
  `home_win_probability` (normal on `sd_margin`), sigmas, observation counts,
  rest context, notes, model version and fingerprint.
- No injury point adjustments exist in this path. The arbitrary usage bumps
  and flat injury deductions of the legacy analytics path were deliberately
  not carried over: injuries only gate or annotate selection (below).

## Player prop projection

`project_prop(player_id, market, player_history, tipoff, line=None)`

Markets: `points`, `rebounds`, `assists`, `threes`, `pra`.

- Eligible rows: matching `player_id`, inside the finish lag, numeric minutes
  > 0, numeric target stat (`pra` needs points+rebounds+assists). Rows with
  unknown minutes or invalid stats are excluded; sparse identities never
  borrow another player's rows. Fewer than `min_prop_games` rows is
  `unavailable`, never guessed.
- V2 separately forecasts minutes from the previous 15/5/3 games, recent
  median, latest minutes, volatility, role change and time since last play.
  A pooled stat-per-minute rate (total stat / total minutes) is multiplied by
  those forecast minutes. January development selected this opportunity
  method over the old baseline and a regularized direct-production model for
  all five markets. Fitted coefficients remain frozen during refresh.
- Uncertainty starts with the player's sample stat sd floored at
  `prop_sd_floor`. February validation chooses the empirical standardized
  residual distribution only when it improves both the full-distribution CRPS
  and a common synthetic-threshold Brier score; otherwise the parametric law
  stays. Rebounds use empirical residuals; points/PRA retain a normal law and
  assists/threes retain Poisson/negative-binomial. Empirical/count distributions
  exclude negative statistics; points/PRA use a rounded, censored normal that
  collects negative latent outcomes at zero. This preserves probabilities on
  positive standard lines and makes an under-zero result impossible.
  Volatile minutes (`minutes_flag`) remain surfaced.
- Probabilities require an explicit line. **Integer lines carry push mass**
  excluded from both sides. The output reports `probability_over`,
  `probability_under` and `probability_push`; push mass is excluded from
  both sides and the three sum to 1. Half lines have zero push mass.
  Without a line only the raw projection is returned.
- The exact point/probability methods, calibration count and projected minutes
  are saved with new selections. Team/opponent/injury usage adjustments are
  not invented from the current roster; reliable historical role/context data
  are needed before adding them.
- Targets before March 1, 2026 retain the unfitted baseline so later fitted
  calibration data cannot leak into earlier historical forecasts.
- Canonical artifact SHA and the exact serving source files (`nba_model.py`,
  `nba_prop_model.py`, `pricing.py`) are pinned in `nba_model_manifest.py` and
  bound into the artifact fingerprint. Changed coefficients or feature math
  fail closed until a newly versioned model is built and reviewed.

## Selection

`select_props(quotes, player_history, tipoff, now, injuries=None, count=5)`

- `now` must precede tipoff (`ValueError` otherwise). A quote is eligible
  only with `identity_verified: true`, a supported market, over/under side,
  numeric line and American price, **explicit main-line provenance**
  (`line_type="main"` or `is_main=true`; alternate, conflicting or missing
  metadata is rejected — main lines are never inferred from cross-book
  tolerance), and an `observed_at` strictly before tipoff, not after `now`,
  and no older than `quote_staleness_s` (3600s). Rejections are itemized with
  reasons (`identity_unverified`, `alternate_line`,
  `conflicting_main_line_metadata`, `missing_main_line_provenance`,
  `after_tipoff`, `future_quote`, `stale_quote`, ...).
- **Ranking is by strongest supported MODEL PROBABILITY** (top-five
  confidence research style): for each distinct game/player/market, the
  candidate is the quote whose model-supported side is most probable (ties:
  better price, then deterministic). The implied-probability difference
  (`edge`) and `implied_probability` remain on the output as descriptive
  fields only — they never filter for positive edge and never order
  candidates.
- Selections are the top `count` distinct player/stat markets by probability
  with at most two markets per player; the cap is relaxed only when it makes
  five infeasible. Fewer than five eligible candidates returns
  `status: "wait"` with empty selections — the board is never padded.
- Injuries: `out`/`doubtful`/`inactive` block selection
  (`injury_out_or_doubtful`); `questionable` and `unknown` availability are
  surfaced in candidate `flags` (as are volatile minutes), never silently
  treated as healthy.

## Historical evaluation

`nba_model.historical_evaluation()` exposes the frozen v2 acceptance report
embedded in the fitted artifact. The fitting tool (`python -m
edgeterminal.nba_model_fit`) reads existing source-verified historical rows and
makes no provider calls. It is never invoked by the automatic refresh.

The procedure uses games before January 1 for initial fitting, January for
point-method selection, coefficients fitted before February 1, and February
for probability-method selection/calibration. March–April labels are excluded
from all coefficient fitting and per-market method selection. The final sample
contains 6,094 player games per market. Minutes MAE fell from 5.336 to 5.032;
point forecast MAE improved in every market. Full per-market MAE, CRPS, Brier,
sample sizes and exact source hashes are in
`reports/nba/model_upgrade_2026-10-27.json`.

This is **retrospective historical validation**, not independent proof of future
performance: examining the initial March–April distribution report informed
addition of the generic February acceptance gate. Current-roster sampling also
has survivorship bias, and synthetic thresholds are not historical sportsbook
lines. No retrospective selection win rate or profitability is claimed. New
saved predictions provide the prospective evidence for v2.

The unchanged team game evaluation remains:

`evaluate_history(team_history, holdout_start, history_complete=True)`

- The training cutoff is fixed at `holdout_start` **before the first forward
  record**: rows before it form the training sample; unique games at or after
  it are projected in chronological order, each using only its own
  pre-tipoff, finished (`feature_lag_s`) rows (no future leakage; mutating a
  later game cannot change an earlier projection). Duplicate team rows of one
  game must agree or the game is dropped as conflicting.
- Reports training/forward/projected sample counts and descriptive metrics:
  margin/total MAE and RMSE, and the Brier score of home-win probability.
- With no sourced rows or no forward records it reports `unavailable`. These
  metrics are arithmetic over caller-supplied rows, **not** a claim of
  historical validation without source provenance.

## Statistical grading

`grade_game(saved_forecast, official_result, now)` and
`grade_prop(saved_selection, official_result, now)` — pure functions; the
parent owns persistence.

- The built-in worker checks grading every five minutes. It grades after
  tipoff as soon as the saved game identity matches an ESPN final summary and
  both player point totals reconcile to the final team scores. This verified
  final bypasses the legacy four-hour wait; callers without that verification
  retain `postgame_wait`. The fixed forecast parameters, fingerprint, training
  cutoff and four-hour input feature lag remain unchanged.
  Postponed/suspended stay `pending`; a verified cancellation is `void` after
  tipoff. Missing or incomplete results retry automatically, every five minutes
  for seven days, then daily. Completed results are rechecked daily for a week
  for corrections. A missing final score is `pending`, never zero. **Contradictory
  NBA finals fail closed:** a tied or scoreless final is `pending`
  (`inconsistent_final`) for every market.
- Game markets: `spread` (home/away handicap), `total` (over/under),
  `moneyline`; results are `won`/`lost`/`push`.
- Props: `dnp` and `inactive` are explicit `void`s; `unknown` availability is
  `pending`. **Duplicate official player rows fail closed**
  (`duplicate_player_rows`) rather than settling on the first match. An
  official stat of **zero grades only with proof of play** (documented
  minutes > 0 or participation evidence); otherwise `unproven_zero` and
  pending. A missing stat or a missing player row is `pending`, never a
  zero. `pra` grades from an explicit `pra` stat or the sum of its three
  components.

## APIs

- `nba_model.project_game(home, away, team_history, tipoff, history_complete=True) -> dict`
- `nba_model.project_prop(player_id, market, player_history, tipoff, line=None) -> dict`
- `nba_model.select_props(quotes, player_history, tipoff, now, injuries=None, count=5) -> dict`
- `nba_model.evaluate_history(team_history, holdout_start, history_complete=True) -> dict`
- `nba_model.probabilities(market, mu, sd, line) -> (p_over, p_under, p_push)`
- `nba_model.parse_time(value) -> float | None`, `nba_model.valid_number(value, nonnegative=False) -> float | None`
- `nba_grading.grade_game(saved_forecast, official_result, now) -> dict`
- `nba_grading.grade_prop(saved_selection, official_result, now) -> dict`

## Limitations

- Descriptive baseline: no matchup, lineup, on/off, travel or altitude
  context; no market calibration. Sigmas are fixed priors, not fitted.
- Team defense/offense averages are opponent-blind (no strength-of-schedule
  adjustment); the pace path assumes opponent possessions ≈ team possessions.
- Rest is only as complete as the caller's supplied history; pass
  `history_complete=False` whenever the schedule slice may be partial.
- The finish lag is a fixed conservative buffer; a game that runs long and a
  tipoff moved earlier can still exclude a genuinely final game (fail closed
  by design).
- Grading interprets statistics only. It does not interpret sportsbook
  settlement rules, bet slips or jurisdictions.
- ESPN identity strings are trusted only when `identity_verified` is set by
  the parent's normalization; nothing here verifies identity itself.
