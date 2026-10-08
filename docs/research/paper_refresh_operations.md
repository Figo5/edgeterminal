# PAPER research refresh and mock portfolio

`PAPER / EXPERIMENTAL / NOT A REAL BETTING RECOMMENDATION` is a separate append-only ledger. The NFL product remains `RESEARCH_ONLY` and `PASS`; this system does not call the product decision or pick endpoints. The active, versioned policy is `EXPERIMENTAL_PAPER_V1`, documented in [experimental_paper_v1.md](experimental_paper_v1.md). No Week 3 game freeze, prop freeze, market quote, model, grade, or PASS designation is changed by activation.

## Commands and local scheduling

From `/Users/giofiore/edgeterminal`:

```sh
.venv/bin/python -m edgeterminal.research_refresh run
.venv/bin/python -m edgeterminal.research_refresh status
.venv/bin/python -m edgeterminal.research_scheduler install
.venv/bin/python -m edgeterminal.research_scheduler status
.venv/bin/python -m edgeterminal.research_scheduler logs --lines 80
.venv/bin/python -m edgeterminal.paper_operations
.venv/bin/python -m edgeterminal.research_scheduler uninstall
```

The installer writes `~/Library/LaunchAgents/com.edgeterminal.research-refresh.plist`, bootstraps `com.edgeterminal.research-refresh`, and sends stdout/stderr to `~/Library/Logs/EdgeTerminal/`. It runs the project virtualenv from the repository root and loads `.env` there. The LaunchAgent ticks every five minutes while the user is logged in. The worker uses a nonblocking file lock to prevent overlap. No open Terminal window or browser is required. A sleeping or offline Mac cannot capture a missed quote; the next wake retries only work that remains prospective. The worker never fabricates a market observation.

`paper_operations` is a read-only position audit. It verifies each entry's deterministic ID, registered V1 hash, frozen projection SHA, quote-pairing SHA, raw provider response hashes, sportsbook line and price, model probability, availability evidence hash, and the immutable-position database triggers. It identifies the original frozen quote ID that the sourced prop grader will use. The audit lists ESPN's expected final-game scoreboard source and nflverse's player-stat release; neither is fetched by the audit command. A position remains pending until those source bytes are archived, the game is final, the four-hour wait passes, and the required participation evidence is present. A missing stat row, inactive player, or unproven zero requires a reviewed NFL Game Book PDF and attestation. The subsequent PAPER settlement applies the official stat to the entry line and book without changing the position.

The worker refreshes ESPN schedule/status every six hours normally, every two hours within 36 hours of a kickoff, and every 30 minutes within two hours. It captures current-week ESPN game prices every 15 minutes during the day before kickoff and every five minutes in the final hour. The separate research capture also checks within 15 minutes of each mapped kickoff. These are append-only observations, never edits to a freeze. Game context (ESPN injuries and Open-Meteo game-time weather) runs at most every three hours for games within six hours. An announced starting QB is **not** inferred from injury listings; without timestamped starter evidence, game PAPER positions fail closed. Existing game freeze creation is attempted in its 36-hour window by the existing cutoff-safe weekly workflow. Game and prop grading use their existing postgame delay and append-only revisions. The prop results workflow still requires reviewed Game Book participation evidence for ambiguous zeros/inactivity.

SportsGameOdds capture is scheduled only for games with an existing, hash-verified frozen prop projection. Games more than 24 hours away are not polled. The minimum interval per game is six hours at 6–24 hours before kickoff, one hour at 1–6 hours, 15 minutes at 15–60 minutes, and five minutes in the final 15 minutes. It stops at kickoff. Due games in the same week share one targeted request window. The provider adapter requests main lines, may paginate to at most ten HTTP requests, and archives exact response bytes, response hashes, selected quota headers, provider notices, and request failures. A provider `lastUpdatedAt` is the quote-observation time used for V1's frozen 15-minute freshness rule; retrieval time and raw response provenance are retained separately. No snapshot or position is fabricated when a scheduled tick is missed while the Mac sleeps or is offline.

A failed capture backs off for at least 30 minutes; HTTP 401/403/429 and quota errors pause automated capture for 24 hours. Local guards stop at 180 recorded requests or 1,200 returned/targeted event objects in a rolling 30-day window. These are conservative local limits, not a claim of remaining provider allowance. The provider may omit bookmaker odds on the current plan; notices and omission counts are visible in Data health. Numeric account quota is shown only when the provider exposes it. A successful response with no valid quote still creates no PAPER position. The worker never calls Odds API. The status API reads stored state and never ingests data. The old once-per-week capture path was removed so it cannot spend a second request without a frozen projection.

## Missed-window monitoring

Every worker invocation now records a minimal immutable tick, including ticks with no due action. `research_refresh status` and Data health show whether the LaunchAgent is loaded, the last tick, last successful refresh, next scheduled refresh, today's recorded SportsGameOdds request count in `America/New_York`, provider cooldown, and active capture windows. Requests made before the request ledger existed are outside this count. A tick gap longer than 12 minutes during an active prop window raises a **possible** sleep/offline/stopped-worker warning; it cannot prove which happened. Before the first tick recorded by this monitor, status says tick history is unavailable. A capture becomes overdue after its next due time plus 10 minutes; an old last capture receives a separate warning. After kickoff, a game without a successful final-15-minute observation is marked as a missed final pregame window. These warnings are computed from immutable timestamps and do not create replacement observations. If the Mac wakes before kickoff, the ordinary cadence may take a new contemporaneous quote. If it wakes after kickoff, the missing pregame observation remains missing. A provider failure or quota cooldown remains visible and does not trigger rapid retries.

`GET /api/research/refresh-status` and `GET /api/research/paper` are read-only. The current product shows saved rows first, then opening or reloading its page calls `POST /api/product/nfl/check` to run due work. Visible pages check every minute; visitors share a five-minute cooldown and reuse recent worker ticks. Provider cadence and quota guards apply. See [local beta operations](../local_beta_operations.md). **Refresh odds** checks the current ESPN schedule, captures free pregame game prices, and runs the quota-guarded research worker. A SportsGameOdds prop request occurs only when the worker's existing cadence and limits allow it. The button never invokes Odds API. Page checks may initiate a provider request only when the ordinary worker determines it is due. The following PAPER tab description concerns the legacy audit interface. The PAPER tab shows the mock bankroll, available bankroll after reserved stakes, positions, conditional settlements, W-L-P/V, units, P/L, ROI, and results by market/model/policy. It lists fresh research opportunities separately from frozen PAPER positions. The archived model-line distribution includes stale quote sides and is labeled accordingly.

After kickoff, unresolved positions appear under **Awaiting settlement** instead
of **Open positions**. A sourced player stat can show a verified statistical
win or loss while the sportsbook interpretation remains pending. Stakes stay
reserved and PAPER P/L remains unchanged until a conditional settlement is
supported by the versioned house-rule evidence.

## Policy and ledger

`data/research/paper_policy_example.json` remains a disabled example. `data/research/experimental_paper_v1.json` is the enabled mock policy, with a $1,000 PAPER bankroll and frozen selection and exposure rules. Its probability cutoff is arbitrary and selected only to control the number of mock positions, not a validated betting threshold. Reusing a registered version with changed content fails. The account's starting bankroll is fixed by its first registration; changing policy later does not reset the account or rewrite positions.

`paper_policies`, `paper_accounts`, `paper_positions`, `paper_opportunity_skips`, `paper_settlement_revisions`, `research_refresh_runs`, and `research_refresh_steps` are append-only SQLite tables. API request attempts/results, capture attempts, raw responses, normalized snapshots, and quote-to-freeze pairings are also immutable. A new main-line quote is paired only when the frozen player/game/market identity, kickoff, point projection, uncertainty, and model version agree; the original freeze is never amended. Every new position freezes the source research freeze SHA, source quote ID, player/game, market, side, line, price, book, projection, probability, implied probability, quote-observation and creation times, time to kickoff, quote and availability ages, availability evidence SHA/source, model version/hash or freeze-contained research signature, policy version/hash, and stake. A quote ID can create only one PAPER position across policy versions; prop player/market/game is unique. Prop positions require a positive timestamped ESPN injury-status observation for that exact player no more than V1's six hours old. Questionable, doubtful, ruled out, missing, and stale statuses fail closed. Absence from the ESPN injury feed is not proof of availability. Missing context, stale price, invalid quote, missing projection, or bankroll limits record a skip reason. Later same-book/main-line observations remain separate from the original position for future price-movement research; no CLV label is assigned without a qualifying pre-kickoff observation.

V1 allows only the five continuous/count NFL prop markets. Game spread, total, and moneyline are excluded. The generalized prop projection path now accepts a target season/week and separately archived current player history. Original training inputs, the training cutoff before 2026 Week 3, model selection, and sigma stay fixed. Recent completed weeks update features only. A new freeze records source quotes and both input hash sets before kickoff. Historical freezes are not amended.

## Top-five product record

The primary product now uses `top-five-confidence-v1`, independent of the legacy
mock money ledger. For each upcoming game it locks five distinct player/stat
markets, ordered by strongest modeled over/under probability, from verified main
line pairs. The selection table rejects updates and deletes. Every selected side
counts once, without a probability threshold, bankroll, stake, or sportsbook payout
requirement. Statistical outcomes use the latest verified immutable grade revision.
Wins and losses determine win rate; pending, pushes, and voids are shown separately.
No prior games are selected retrospectively. Fewer than five available markets
leaves a game waiting for the next bounded capture. Early-week requests are batched
every six hours, accelerating near kickoff under the existing provider quota guards.
The background worker grades all due prop freezes, including older weeks after
the schedule rolls forward. The main navigation is Projections, Research record,
and About & data; the legacy paper ledger and APIs remain available for audit.

Prop PAPER settlements read the existing latest sourced prop grade revisions, retaining sportsbook-specific conditional interpretation; ambiguous or corrected settlement stays pending. A position entered from a later quote uses its immutable pairing to find the original frozen projection's official-stat grade, then applies that actual stat to the position's own line, side, and book. Its PAPER settlement revision stores the source grade and evidence hashes plus the settlement-rule version and book-rule snapshot. It never substitutes the original freeze's different line or sportsbook settlement. Game moneyline settlements read existing game grade revisions; a tie stays pending because book rules may differ. Every correction adds a PAPER settlement revision, and reports use only the latest revision. Paper P/L is a mock accounting calculation from recorded American odds. It does not assert that a price was attainable or that a sportsbook paid a ticket. Historical simulations, if developed later, must use another namespace and cannot enter this ledger. The UI reports quote-side skip counts from the last 24 hours; these can include multiple books or repeated evaluations and are not distinct missed positions.

The current descriptive distribution should be inspected with:

```sh
.venv/bin/python -m edgeterminal.paper_research distribution
```

Do not tune V1 from Week 3 outcomes. Its fixed threshold was chosen from the archived pregame opportunity count only. The disabled example remains available for dry runs; the worker loads the versioned experimental policy.
