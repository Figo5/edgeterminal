# EdgeTerminal NFL current-system audit

Audited 2026-09-23 from `~/edgeterminal`, `main` at `e97bfd6`.
This is an audit of the working copy and the running app, not an endorsement of its
predictions. The pre-existing `.gitignore` edit was left untouched.

## Evidence gathered

- Read `README.md`, configuration, API, database,
  provider, model, backtest, chat, and browser code. The handoff is historical;
  its passing-test claim does not describe this checkout on today's date.
- Baseline: `235 passed, 11 failed` from `.venv/bin/python -m pytest -q`.
  Ten failures cover multi-turn conversation identity/market retention; one
  cache-freshness test now fails because its fixed 2026-09-18 fixture has kicked
  off by the current date. The latter requires a test clock, not weaker live
  validation.
- Existing service was running on localhost port 8799. Opened the current NFL
  dashboard and Atlanta at Green Bay game detail in a browser. The dashboard
  showed `ODDS STALE`, but displayed quotes from old games as if comparable
  slate prices. It displayed Denver at Kansas City twice for Sep 14 under
  different provider IDs, and past games marked `scheduled`. The first 20 rows
  included old games, so it did not represent the current weekly board.
- The game detail projected Atlanta 19.1, Green Bay 21.78 and total 40.9,
  warned that odds were about 10,019 minutes old, reported six priced markets,
  then rendered an empty Market Board. It had an unstructured long injury list
  and unavailable weather due to missing venue coordinates/city.
- Existing cached 2025 backtest replayed 272 regular-season games: margin MAE
  10.5082, total MAE 11.2727, Brier 0.224967, ATS 49.45% on 271 decisions,
  totals 47.43%. These are diagnostics only: no market baseline comparison,
  historical price timing proof, or independent leakage review exists yet.
- Local database has 71 NFL events, 469 prediction rows, and **zero results**.
  Six `demo-nfl-*` fixtures dated Sep 23 remain in the database. One current
  provider status showed The Odds API empty, ESPN odds fallback active, ESPN
  injuries/news/stats active, and weather unavailable for the inspected game.

## Component decisions

| Component | Classification | Current implementation and required action |
| --- | --- | --- |
| Schedule source | REPAIR | ESPN scoreboard and The Odds API event list both write events. A new response upserts but never reconciles old scheduled rows. Build a canonical, week-scoped schedule and preserve past results separately. |
| Game/event identity | REPLACE | Provider IDs (`espn-*`, Odds API hex IDs, `demo-*`) are stored directly as primary keys. Same matchup can appear twice. Add one internal identity with provider-ID mappings, season, week, type, kickoff, home/away and reschedule handling. |
| Team aliases | KEEP / REPAIR | `providers/normalize.py` already centralizes names and JAX/JAC, WSH/WAS, LA/LAR. Tighten unknown aliases: returning an unrecognized raw string is unsafe for event matching. |
| Odds source | REPAIR | The Odds API (keyed) and ESPN consensus are structured; demo fallback exists. Keep provider adapters, but separate provider event IDs from internal IDs and never present stale quotes as current. |
| Market normalization | REPAIR | Rows include book, market, selection, line, price and captured time. Spread orientation is implicit and the slate selects the lowest implied probability independently of point, producing absurd apparent “best” spreads/totals. Add one home-spread convention and compare like lines. |
| Line history | REPAIR | Append-only `odds_snapshots` exists. `FIRST OBSERVED` is not a sportsbook opener. Quotes from an old kickoff remain selectable; opening and closing definitions need source/timing checks. |
| Stats source | REPAIR | ESPN standings plus cached nflverse PBP. Current team stats are shrunk to a prior. The live projection's feature inputs and cutoffs need explicit snapshot timestamps. |
| Injuries | REPAIR | ESPN structured feed exists; the page dumps many rows. `football.injury_adjustment` applies unvalidated fixed point deductions (QB 4.5, etc.). Remove quantitative injury changes until independently validated; use availability to gate a designation. |
| Weather | REPAIR | Open-Meteo exists, but Odds API event records lack location in the inspected game. The model applies fixed weather point changes without historical validation. Keep sourced context; quantify only after backtesting. |
| News | KEEP / REPAIR | ESPN news has links and retrieval times. Keep sourced qualitative context but filter relevance and age. |
| Model | REPLACE | `analytics/football.py` has a deterministic points formula, fixed home field and residual SD, heuristic EPA/success adjustment, plus injury/weather adjustments. Score, margin and total reconcile. Coefficients and uncertainty are not trained or calibrated; baseline comparison is missing. |
| Model/market boundary | REPLACE | `evaluate_market` blends raw model probability with market consensus for NFL (`select_best_model_quote`) and calls the result `model_prob`. Keep raw model, calibrated probability and market price as separate named outputs. |
| Confidence | REPLACE | The 0–100 score mixes data quality, agent agreement, edge and heuristic penalties. It is not an empirical prediction interval or calibrated probability. |
| Pick policy | REPLACE | Agent strategist uses configured 80/65 confidence thresholds and edge percentages without chronological policy validation. Keep PASS as default until a pre-registered policy is evaluated. |
| Props | EXPERIMENTAL / DEFER | Large TD/yardage pipeline and on-demand Odds API calls. No current validated player-usage model. Hide from primary NFL game-market workflow; preserve code for a later pass. |
| Parlays | REMOVE from visible product | Endpoint and dashboard concepts still exist, despite the NFL-game-market priority. Defer all parlay recommendations. |
| Chat | REPAIR | Explicit conversation context exists and frontend echoes it by tab, but ten HTTP tests fail on the current date/data. Game/market precedence requires renewed end-to-end verification. |
| Frontend | REPLACE | NFL-only navigation is present, but home is Best Bets, not a weekly board; current slate is a dense table and shows stale quotes. Matchup depth exists but mixes context, props and numeric recommendations. |
| Persistence | REPAIR | SQLite stores events, odds, predictions, analyses, results, sources and quarantine. `save_prediction` uses `INSERT OR REPLACE` with a deterministic ID, so reruns can overwrite issued history. `data_snapshot` is partial metadata, not frozen model inputs. |
| Backtesting | REPAIR | `backtest.replay` uses prior weeks for current-season feature aggregation and labels prior-season fallback. It does not reproduce the full live feature path (injuries, weather, ESPN stats and market blend), does not enforce all input publication times, and has no independent baseline or final holdout gate. |
| Historical results | REPLACE | Local results table is empty; grading and CLV are manual endpoints. No automatic shadow grading or usable performance page in the visible UI. |

## Priority blockers before any official designation

1. Canonicalize games and show only the scheduled current week once each.
2. Stop treating old or mismatched quotes as current and normalize spread/total
   orientation and line-shopping rules.
3. Freeze all inputs for each prediction and preserve immutable prediction rows.
4. Establish chronological baselines, leakage checks, calibration, and a held-out
   pick-policy evaluation. The current 2025 ATS and total rates do not support
   an `OFFICIAL` designation.
5. Repair multi-turn chat tests and current browser behavior.

Until those are complete, the correct readiness class is **RESEARCH_ONLY**.
