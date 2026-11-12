# October 28 NBA spread audit: inputs and serving

The saved inputs reproduce all twelve game forecasts exactly. The forecasts were frozen 45.54–47.81 hours before tipoff and did not incorporate subsequent completed games. That is a demonstrated serving weakness, including four wrong rest classifications. It does **not** explain away the 3–9 spread result: a replay using fresher archived pretipoff inputs changes one side and remains 4–8; margin error gets slightly worse.

This audit opened `data/edgeterminal.db` with SQLite `mode=ro` and `query_only=ON`. It called only pure parsing and projection functions, made no provider requests, fitted no parameters, and changed no production sources, database records, or pinned model files. Outputs are this report, `inputs_audit.py`, and `inputs_replay.json` in this directory. Target outcomes are used only for retrospective error measurement.

## Exact reconstruction and integrity

For each saved `kind=game` forecast, the replay starts from **its own** `source_artifact_ids`, checks the archived bodies, and parses the team-history sources. It retains only the schedule owner's team, matching `load_team_history()`; blindly retaining both sides from a team schedule would manufacture a different historical input. It applies the same strict duplicate comparison, four-hour feature lag, and twenty-row window.

- 12/12 serialized forecast payload SHA-256 values match.
- Every referenced source is HTTP 200, has matching body SHA-256, and was received no later than its forecast.
- 12/12 numerical projections match: scores, margin, total, home-win probability, pace state, observations, rest days, rest flags, conflicts, and uncertainty constants. All input-conflict counts are zero.
- Every team has twenty eligible rows. “Twenty observations” is true, but it does not imply twenty current-season games or complete recent schedule coverage.
- Full reconstructed rows, exact selected windows, source receipts, source IDs, per-team scoring means, and later pretipoff windows are in `inputs_replay.json`.

All saved game forecasts are `nba-research-1.1.0`, fingerprint `79026f8a1c09b06e247bd5e2c0335d86f8930c92c416679299dadfc479cf4da4`. The current package is `nba-research-2.0.1`, with explicit `nba-game-baseline-1.1.0`. Git commit `6e3a71b` changes the prop model and adds game-component metadata; its only `project_game()` change is adding that metadata to the base response. The numeric team-game algorithm and constants are unchanged. The replay intentionally compares numerical/structural fields rather than pretending current version metadata matches the historical forecast.

## Demonstrated weaknesses, ordered by remediation priority

1. **The first forecast is permanent even when its inputs become incomplete.** `nba_ops.forecasts()` selects games within two days, but on finding an existing game forecast it appends missing spread/total lines and then continues without rerunning `project_game()` (`nba_ops.py:410–436`). The October 28 forecasts were saved October 26, 7:11–11:33 p.m. Eastern. Later archived schedules received before target tipoff add eighteen team-game observations across eighteen of twenty-four teams; every matchup is affected. Current-season source receipts were 0.0006–29.31 hours old at forecast creation and 45.55–76.26 hours old by tipoff. There is no forecast-level source-age/completeness gate in `_teams_history()` or `forecasts()`.

2. **Rest is asserted complete when the cached history is incomplete.** `project_game()` defaults `history_complete=True`, and `forecasts()` supplies no completeness evidence. `_rest()` uses the last scored row, ignoring scheduled intervening games. All saved flags say normal. Pretipoff archived schedules establish DET home (0.96 days), NYK away (1.00), SAS away (1.04), and LAL away (0.98) were back-to-back. The saved gaps were 2.98, 3.04, 4.04, and 3.23 days respectively. The same fixed baseline would apply −1.4 to DET and −1.8 to the three away teams, before allowing for their newly added scoring rows. Those four rest errors alone change no spread side on the original saved lines.

3. **The effective sample is 85% previous-season games, with no decay or roster continuity adjustment.** Across the 480 selected team rows, 408 are 2025–26 and 72 are 2026–27. Each team contributes only two to four current-season games; sixteen to eighteen older games receive equal per-row weight. This is an intentional baseline design, not evidence that the parser mislabeled a season. It makes offseason personnel changes and current form slow to enter the estimate. The exact two already-old missing rows are TOR's October 25 game (`401909864`) and LAC's October 25 game (`401909865`), whose tipoffs preceded forecast creation by 26.79 and 28.55 hours. Their first archived final owner-schedule receipts in this database occur after the respective freeze, so this audit does not claim that a final owner-source receipt was already available at that instant.

4. **The actual inputs cannot activate the possession branch, and opponent strength is unused.** `nba_sources.team_history()` produces points and points allowed, but no possessions. None of the 480 selected rows has possessions, and all twelve forecasts have `pace_adjusted=False`. On these inputs, the margin is `0.5 × (home mean raw net points − away mean raw net points) + 2.2 + rest adjustments`. Opponent IDs and historical home/away flags are parsed but discarded by `_team_rows()`; no schedule-strength, opponent-adjusted efficiency, team lineup, availability, travel, or matchup feature reaches the game calculation. These are demonstrated missing features. Whether any particular missing feature caused a particular October 28 loss is not established by this audit.

## Freshness-only diagnostic

The “refreshed” column uses the latest archived successful **same-URL** team schedules received before each target tipoff, retains the same twenty-game window and four-hour lag, and changes no model parameter. This is a retrospective diagnostic, **not** a saved forecast, replacement grade, or claimed forward record. It excludes the October 28 target games from inputs.

Current-season counts and raw-net means are shown **home/away**. Every exact window has twenty rows. Positive margins favor the home team.

| Game | Current rows H/A | Mean raw net H/A | Saved margin | Refreshed margin | Actual margin | Absolute error saved → refreshed |
|---|---:|---:|---:|---:|---:|---:|
| MIA @ ATL | 3/3 | 10.25/−4.55 | +9.60 | +9.70 | −12 | 21.60 → 21.70 |
| WAS @ CLE | 2/3 | 3.85/−12.95 | +10.60 | +10.20 | +10 | 0.60 → 0.20 |
| CHA @ DET | 3/3 | 10.60/7.05 | +3.97 | +1.70 | +8 | 4.03 → 6.30 |
| PHI @ IND | 3/4 | −4.20/2.00 | −0.90 | −0.73 | −5 | 4.10 → 4.27 |
| ORL @ TOR | 2/3 | 8.75/−3.55 | +8.35 | +8.05 | +30 | 21.65 → 21.95 |
| NYK @ CHI | 4/3 | −10.70/4.50 | −5.40 | −4.15 | −6 | 0.60 → 1.85 |
| HOU @ MIL | 3/3 | −11.05/9.00 | −7.83 | −7.25 | −32 | 24.17 → 24.75 |
| GSW @ MIN | 4/3 | 5.80/−7.15 | +8.67 | +7.92 | −5 | 13.67 → 12.92 |
| DEN @ NOP | 3/2 | −2.55/8.90 | −3.53 | −3.10 | −13 | 9.47 → 9.90 |
| SAS @ UTA | 3/3 | −11.60/10.85 | −9.03 | −7.73 | −6 | 3.03 → 1.73 |
| OKC @ DAL | 4/3 | −7.90/11.35 | −7.43 | −7.62 | +24 | 31.43 → 31.62 |
| LAL @ LAC | 2/3 | 4.00/5.75 | +1.32 | +2.80 | −16 | 17.32 → 18.80 |

Only CLE/WAS flips: the saved forecast chooses CLE −10.5 by 0.10 points; the refreshed fixed baseline chooses WAS +10.5 by 0.30 points. CLE won by ten, so that diagnostic side wins. The other eleven sides stay unchanged. The actual saved record remains 3–9. The diagnostic is 4–8, while mean absolute margin error worsens from 12.639 to 12.999 points: three forecasts become closer and nine farther. Maximum margin change is 2.27 points. This is evidence against treating freshness alone as a complete performance fix, or selecting model coefficients from these twelve outcomes.

## Future-only fix design

Preserve every original forecast and grade. Add append-only forecast revisions with unique IDs and explicit `as_of`, component version/fingerprint, eligible feature cutoff, exact source IDs, and selected row IDs. Declare the day-of-game publication/finalization schedule before evaluating new outcomes; only future games should use this selection policy. Bind the chosen forecast revision to a separately timestamped spread quote and preserve that pair for grading.

Before creating a revision, verify each team's current-season schedule receipt is within a declared freshness limit and reconcile all known prior games against completed rows. Use the full schedule to establish rest rather than inferring it from the last available scored row. Mark incomplete rest unknown and withhold a finalized projection when required context is missing. Refresh after intervening finals and near the predetermined pretipoff finalization time, while enforcing source receipt ≤ as-of and a declared completion/feature-lag rule. These are data/serving safeguards and do not require fitting against October 28.

Then evaluate model changes separately on chronological historical holdouts and new forward games: season-aware weighting, opponent-adjusted efficiencies and possessions, roster/availability features, and calibrated spread uncertainty. The present game function accepts no availability inputs; refreshing it cannot by itself make it respond to lineup news. Do not attach arbitrary injury point values or choose new weights from this twelve-game slate.

## Code evidence

- `edgeterminal/nba_ops.py:159–179`: strict duplicate comparison and owner-only schedule history.
- `edgeterminal/nba_ops.py:257–258`: cached histories are pooled without freshness checks.
- `edgeterminal/nba_ops.py:410–436`: first forecast within two days; existing forecasts only get missing market lines.
- `edgeterminal/nba_sources.py:105–134`: completed regular-season score rows, with no possessions or team availability.
- `edgeterminal/nba_model.py:179–222`: eligible rows, conflict rejection, fixed twenty-row window; opponent/home-away identifiers discarded.
- `edgeterminal/nba_model.py:225–307`: rest default, raw scoring fallback, constants, and explicitly unadjusted lineup/matchup uncertainty.

