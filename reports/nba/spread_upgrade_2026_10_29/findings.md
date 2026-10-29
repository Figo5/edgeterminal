# NBA spread component: data, experiment, and activation boundary

The new live component preserves a selection for every supported game. Its immediate numerical change is schedule-verified rest applied to fresh input snapshots using the existing fixed rest constants. Possessions and player availability are recorded as evidence/context; neither automatically adds an unvalidated point adjustment.

## Data acquired

- Sixty additional public ESPN team-season schedules: all 30 teams for season-ending years 2024 and 2025. All 60 requests succeeded; exact bodies, actual retrieval timestamps, URLs, and SHA-256 values are saved under `data/research/nba_spread/bootstrap-2026-10-29` (87,572,878 raw bytes).
- Thirty existing season-ending-2026 schedules were reused. The combined reconstruction has 7,386 owner-only team rows from 90 schedules.
- ESPN's `seasonType=2` tag includes one NBA Cup championship per year; the resulting 1,231-game year is therefore **ESPN regular-season-tagged scope**, not exactly the NBA's 1,230-game standings schedule. The Cup championship is tagged `competition.type.id=39` / `commissioners-cup`, with a neutral site. This source limitation is retained explicitly; no prior record is rewritten.
- Actual source receipts are October 2026. Historical reconstruction assumes final scores were available four hours after their recorded tipoff. It does not invent historical receipt timestamps or use the newly downloaded data to validate old live selections.

## Locked retrospective comparison

The [protocol](protocol.md) preceded candidate metric calculation. Fit cutoff was October 1, 2025; the expanding coefficient prefix, next 120 whole development games, and final 120 residual-calibration games all precede that cutoff. Development chose only among ridge penalties 1, 10, and 100. Shared feature transforms add recent/season net-score differences and opponent schedule strength to the original game margin. Optional possession features are absent from these score-only archives; player availability never receives a hand-assigned point value.

| Held 2025–26 period | Games | Baseline margin MAE | Candidate margin MAE |
| --- | ---: | ---: | ---: |
| Entire tagged season | 1,231 | 11.7323 | 11.4946 |
| October–December | 492 | 11.0111 | 10.9376 |
| January–February | 401 | 12.2761 | 12.1021 |
| March–April, previously examined | 338 | 12.1368 | 11.5844 |

Whole-year margin RMSE moved 14.9159 → 14.6686, and winner Brier moved 0.21247 → 0.20888. The whole-year MAE improvement is about 0.238 point (2.0%). These are preliminary score-forecast results; there are no historical sportsbook lines, so they cannot establish historical spread-selection accuracy or profitability. March–April was already inspected in earlier research. No October 2026 outcome entered any fit, calibration, or selection.

Detailed forecasts, sources, and the analysis artifact are in [evaluation.json](evaluation.json), reproduced by [evaluate.py](evaluate.py). Final code added metadata/dependency hashes, uncertainty metadata consistency, and a contradictory-input rejection after the numerical study; these do not alter valid-data point transforms. The experimental artifact's byte catalogue describes its study snapshot and is not registered or active.

## Automatic learning and activation

The game component has a separate immutable candidate/event/run registry. The five-minute worker checks bounded bootstrap progress; fitting is deferred around NBA or NFL kickoff. Waiting checks resume daily, successful/rejected proposals weekly, errors after five minutes. A damaged active bundle rolls back to the separately identified baseline with a recorded incident.

Possession evidence also accumulates automatically from final box scores the grader has already archived. Enrichment performs no HTTP requests, uses only sources received by the forecast/fitting as-of, verifies final identity/score/time and raw bytes, records the declared symmetric estimate and exact box-source ID, and caches parses by connection/artifact hash. Sparse box coverage stays explicit. Finishing the historical bootstrap immediately refreshes the waiting status rather than leaving an obsolete bootstrap message for a day.

New activation requires at least 60 verified **future-policy** game forecast/line/final pairs across seven dates, after the historical archive was available. Training-source receipts and labels must precede the earliest validation snapshot. The fitting/development/calibration cohorts are disjoint whole-game chronological groups. Validation compares the candidate with the exact saved live projection on the exact saved line; pushes remain excluded from binary cover scores. At least ten new games are required before another proposal can reuse a rolling validation window.

The candidate must improve margin MAE by at least the greater of 0.25 point or 2%, improve cover Brier by at least 0.002, show paired calendar-day 95% intervals below zero for both, preserve coverage, and avoid a lower spread win count. Database activation triggers also reject a candidate without a passing recorded evaluation. The prior tiny opponent-rating study cannot bypass these gates by changing its name.

**The new fitted candidate remains inactive.** Historical score metrics alone never activate it. One model stays live, with fresh append-only pretipoff revisions and the unchanged supported-game pick coverage.

## Verification and orchestration

OpenCode Go (`opencode-go/kimi-k3`) gave a read-only architecture review emphasizing isolated game versions, source receipts, actual-line validation, coverage preservation, and no guessed injury effects. The implementation uses compact project-native Python/SQLite modules rather than adding a parallel stack.

The 24 focused regressions cover baseline parity, future-label poisoning, source receipts versus feature cutoffs, explicit complete/unknown rest, availability/possession context without numerical bumps, contradictory input rejection, invalid artifacts, source identity/hash/owner checks, chronological and materiality gates, paired day intervals, no duplicate validation games, failed activation SQL refusal, source-priority deferral, rollback, calibration-label isolation, receipt-bounded read-only box enrichment, bootstrap completion status, and fitting-path rehash of a source whose transform was already cached. No pinned `nba_model.py`, `nba_prop_model.py`, or `pricing.py` source bytes changed.
