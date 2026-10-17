# EdgeTerminal

> **Status: personal research beta.** The touchdown model is explicitly experimental
> and has not been calibrated or shown to be profitable. Several recommendation paths
> are deliberately disabled (see *Release limits* below). Use it to browse verified
> prices and research a game, not as a source of betting advice.

EdgeTerminal is a local **NFL-only** betting research and analytics terminal. It compares stored prices with deterministic projections, exposes confidence and risk inputs, records odds history. It does **not** place bets.

The active product and UI are NFL-only. Reusable provider abstractions for other sports remain dormant for future expansion.

## Quick start

Requires Python 3.9+ (3.11 recommended).

```bash
git clone https://github.com/Figo5/edgeterminal.git
cd edgeterminal
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env if you have provider credentials.
PORT=8799 .venv/bin/python -m uvicorn edgeterminal.api:app --host 127.0.0.1 --port 8799
```

Open <http://127.0.0.1:8799>.

These commands use a macOS/Linux shell. On Windows PowerShell, create the
environment with `py -3.11 -m venv .venv`, install with
`.\.venv\Scripts\python.exe -m pip install -r requirements.txt`, and start with
`.\.venv\Scripts\python.exe -m uvicorn edgeterminal.api:app --host 127.0.0.1 --port 8799`.
Copy `.env.example` to `.env` with `Copy-Item .env.example .env` first.

## Weekly projections and top-five record

The home screen shows the current NFL week, projected scores, winners, spreads,
totals, and five player props per game. Once ESPN's default week is entirely final,
the app checks the next week's schedule automatically. The week selector keeps
previously stored weeks accessible.

The **Season record** shows weekly and cumulative winner, spread, total and prop results. It counts the five strongest modeled prop probabilities for
each game, with wins, losses, pushes, voids, and pending results. There are no stakes
or bankroll inputs in this flow. One side per player/stat market is selected from
observed main lines; five distinct markets are required. Selections lock before
kickoff and cannot be overwritten. Games without enough supported lines wait for
the provider; the selection ledger never backfills completed games.

Older prop forecasts appear as **Archived ranking**: a read-only top-five ranking
of the earliest saved pregame freeze, with fewer rows when fewer supported markets
were saved. This is retrospective selection of original predictions, not a claim
that the top-five policy existed then. Season totals include these labeled archive
rows. Game records use the latest saved pregame forecast and its own market lines,
with final scores from verified grade sources. Repeated forecasts count once.
Legacy Weeks 1–2 market predictions are matched to ESPN results by exact teams
and kickoff time, using the more probable side of each saved market forecast.
Hash-checked result artifacts in `data/research/season_results/` preserve source
evidence. No new prediction is generated. Week 1 has only one saved game; Week 2
has sixteen. Weeks without saved predictions show **Not recorded**, never
synthetic results.
Research chat has been removed from the interface.

The existing research LaunchAgent runs every five minutes while this Mac is awake.
Missing prop boards are fetched in batches every six hours early in the week, with
more frequent checks near kickoff, subject to the provider's quota guards. The UI
reads new stored data every minute. Postgame grading uses verified result sources
and appends corrections. Historical freezes remain intact; the original prop
model's training cutoff and parameters stay fixed, while recent completed games
update the next week's player features. ESPN supplies game odds; SportsGameOdds
supplies player lines. This flow never calls Odds API.

Port `8799` is EdgeTerminal's default; `8787` is reserved for CivicFolio.

## Running it as a service

`tools/edgeterminal.sh` wraps the same virtualenv and `.env` loading. Credentials are
never passed as arguments: the app reads `.env` itself. The script identifies its own
process by PID file and a verified command line, so it never runs a broad `pkill` that
could hit CivicFolio or another uvicorn.

```bash
./tools/edgeterminal.sh start      # background, logs to data/edgeterminal.log
./tools/edgeterminal.sh status     # pid, port and /api/health
./tools/edgeterminal.sh restart
./tools/edgeterminal.sh stop
```

Foreground (the exact command, useful when debugging startup):

```bash
PORT=8799 .venv/bin/python -m uvicorn edgeterminal.api:app --host 127.0.0.1 --port 8799
```

To survive terminal closure and restart at login, install the optional launchd agent.
This is opt-in and reversible; nothing is installed unless you run it:

```bash
./tools/edgeterminal.sh install-service     # writes ~/Library/LaunchAgents/com.edgeterminal.app.plist
./tools/edgeterminal.sh uninstall-service   # unloads and removes it
```

## Weekly NFL updates

The local research LaunchAgent checks ESPN's current NFL schedule automatically.
When ESPN advances to the next week, EdgeTerminal stores that slate and creates
missing pregame board projections. The open NFL board checks stored data every
15 minutes; reopening the page also loads the latest stored week. The app
service above keeps the web interface available after login.

**Refresh odds** in the header checks ESPN's current week and captures fresh
pregame game prices for its unstarted games, then reloads the board. The local
research worker also captures ESPN game prices every 15 minutes during the
24 hours before kickoff and every five minutes in the final hour. These checks
never invoke the Odds API. The button also runs the quota-guarded research
worker. A manual prop check can run after a 30-minute minimum interval, while
the provider quota and failure cooldowns remain enforced.
The board reports when ESPN has no current quote and keeps each observed price
labeled with its own timestamp.

## Release limits

This beta deliberately refuses to produce some numbers rather than guess them.

- **Anytime-TD outcomes are not mutually exclusive.** Player probabilities are never
  normalised against each other and do not sum to 1.

Two *independent* conditions each block a recommendation. They are different problems
with different fixes, and they are reported separately rather than merged into one
"insufficient data" message:

1. **No same-player No quote → no two-sided market de-vig.** A fair (no-vig) probability
   is estimated only from the *same player's* Yes and No at the *same book*, which is a
   genuinely complementary pair. No book currently returns a No price for anytime TD, so
   `market_prob` is `null`. This is a *market coverage* limitation. An opposing price is
   never fabricated, and Yes quotes from other books do not substitute for it.
2. **Insufficient or unvalidated modelling → no defensible model-based recommendation.**
   The touchdown model is an uncalibrated rate projection, and a player with no usable
   usage yields no model at all (`model_prob: null`) rather than a floor value. This is a
   *modelling* limitation and would remain even if every book posted a No price.

Either condition alone leaves the row `NOT RECOMMENDABLE`. Verified sportsbook prices
stay visible in both cases, with the book, the quote, and that quote's own implied
probability. Thresholds were not lowered to compensate for either.
- **First-TD and last-TD are disabled.** They settle differently and the race model
  behind them is unvalidated. Anytime probabilities are never reused for them.
- **Edge, EV and fair price appear only** where a model *and* a fair market
  probability both exist. Elsewhere they are `null`, not estimated.
- **Confidence is a 0-100 data-quality score, not a win probability.**
- The touchdown model is an uncalibrated Poisson rate projection. No profitability or
  calibration claim is made.
- Parlays remain PASS unless supported; prices from different books are never combined
  into a quoted ticket.

## Demo mode

`DEMO_MODE=true` forces deterministic fixture providers. The header displays **DEMO DATA** and every fixture-derived recommendation carries `is_demo=true`. Fixture odds are never presented as live/current odds.

Demo mode supports the complete workflow without credentials:

1. Open the NFL dashboard.
2. Press **Refresh Odds** to normalize fixture prices and append a SQLite odds snapshot.
3. Inspect confidence-80+ official picks in **Best Bets**.
4. Open a matchup for projections, market prices, team comparison, injuries, news, weather, disagreement, risks, and sources.
5. Ask the analyst for the best bet in the selected game or the best parlay.

In live mode, the background research worker captures ESPN game prices before
kickoff. The current product shows saved data immediately, then opening or reloading the
page checks for due updates. **Check updates** uses the same shared five-minute
cooldown and provider cadence/quota guards. Localhost still needs the Mac backend
running. The legacy demo interface keeps its explicit fixture-price refresh.

## Live integrations

Copy `.env.example` to `.env`. All credentials remain server-side.

- No-key mode uses ESPN's public NFL scoreboard for the real schedule, results, and
  available consensus moneyline/spread/total prices. ESPN does not expose FanDuel
  or bet365 prices in this feed; the UI names the actual provider and never
  relabels consensus prices as a sportsbook.
- `ODDS_API_KEY`: optional The Odds API free-tier key. When configured it supplies
  named-book comparisons; FanDuel and bet365 are preferred but the best numerical
  price always wins. Coverage and quotas are controlled by that provider.
- `OPENAI_API_KEY`: optional. Enables the official OpenAI SDK tool-calling loop.
- `OPENAI_MODEL`: centralized model selection; defaults to `gpt-4o-mini`.
- ESPN public endpoints provide schedule/stats/news/injuries where supported.
- Open-Meteo provides geocoding and kickoff weather for outdoor venues, without a key.
- nflverse play-by-play adds locally cached EPA/play, EPA/dropback, EPA/rush,
  success rates, CPOE, PROE, explosive-play rate, and no-huddle tendency.
- `SEARCH_PROVIDER` selects optional web research.

Without `OPENAI_API_KEY`, chat uses a deterministic router over the same backend analysis tools. Dashboard calculations never depend on OpenAI.

## Configuration

See [`.env.example`](.env.example). Core variables:

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
DATABASE_URL=sqlite:///./data/edgeterminal.db
DEMO_MODE=false
ODDS_PROVIDER=auto
ODDS_API_KEY=
PREFERRED_BOOKS=fanduel,bet365
HOST=127.0.0.1
PORT=8799
```

No secret belongs in client-side JavaScript. `.env` and local database files are ignored.

## API

Primary routes use the existing `/api` namespace:

- `GET /api/health`
- `GET /api/config`
- `GET /api/dashboard?sport=NFL`
- `GET /api/slate?sport=NFL`
- `POST /api/refresh`
- `GET /api/event/{event_id}`
- `GET /api/best-bets`
- `GET /api/watchlist`
- `GET /api/line-moves`
- `GET /api/line-history`
- `GET /api/parlay`
- `POST /api/chat`
- `GET /api/performance`

Interactive API documentation is available at `/docs`.

Example manual NFL refresh:

```bash
curl -sS -X POST http://127.0.0.1:8799/api/refresh \
  -H 'content-type: application/json' \
  -d '{"sports":["NFL"]}'
```

Provider failures are returned as structured per-sport errors. Missing data lowers confidence or excludes a market; it is never fabricated.

### Provider order, freshness, and limitations

With `DEMO_MODE=false`, NFL uses: The Odds API (when keyed) → ESPN consensus
odds → labeled demo fallback. Schedule/results, standings-derived team summaries,
injuries, and news come from ESPN; outdoor weather comes from Open-Meteo. These
public endpoints are unofficial and can change or omit fields. Locally cached
nflverse PBP augments ESPN team summaries where teams have current-season plays.
Complete role/snap data and comprehensive player props are not claimed when the
upstream feed does not provide them.

### nflverse advanced NFL data

EdgeTerminal uses the nflverse PBP release under CC-BY-4.0. The source is
`https://github.com/nflverse/nflverse-data/releases/tag/pbp`; yearly CSV.GZ
assets are aggregated locally and are never committed. Current-season data is
mutable because NFL stat corrections are published after games. The cache stores
the source URL, retrieval time, license, and SHA-256 of the exact archive.

Acquire or refresh a season explicitly (normal dashboard refresh never downloads it):

```bash
.venv/bin/python -c "from edgeterminal.config import settings; from edgeterminal.providers.nflverse import load_team_features; print(len(load_team_features(2026, settings.nflverse_cache_dir, refresh=True)))"
```

For reproducible historical backtests, retain the generated metadata hash and
freeze completed-season archives. nflverse documents its update schedule at
`https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html`.

Odds expire after `ODDS_STALE_SECONDS` (default 900 seconds). Schedule/team data
cache for 15 minutes, injuries/news for 30 minutes, and weather for 15 minutes.
**Refresh Odds** bypasses the odds path and appends a new snapshot. Locally first
seen lines are labeled **FIRST OBSERVED**, never sportsbook openers. If every real
odds provider fails, the header explicitly switches to **DEMO DATA** rather than
silently mixing fixture prices into a live slate.

## Deterministic analytics

Python code—not the LLM—calculates:

- American odds and implied probability
- fair odds, edge, and expected value
- fixed-ladder unit sizing (0.25u through 5u)
- bounded 0–100 confidence with data-quality caps
- projection distributions and market probabilities
- line movement and CLV
- calibration from graded local history
- Brier score, log loss, ROI, CLV, and confidence-bucket backtest summaries
- parlay probability with explicit rule-based correlation adjustments

Official picks require confidence `>= 80` by default. Confidence is trust in the estimate; edge is price value. Either can be high while the other is low.

## Tests

Tests isolate SQLite in temporary files and do not call paid providers or OpenAI.

```bash
.venv/bin/python -m pytest -q
```

The suite covers pricing conversions, edge, unit sizing, confidence bounds, provider normalization, odds snapshot persistence/history, line movement, parlay correlation, deterministic chat routing, and the API/static-UI vertical slice.

## Historical backtest (NFL)

`edgeterminal.backtest` replays the *production* projection model over completed
nflverse seasons with strictly point-in-time features (week W sees only weeks
`< W`; week 1 falls back to the previous season and is labelled
`basis="prior_season"`). It never tunes thresholds — it measures the model.

```bash
.venv/bin/python -m edgeterminal.backtest --seasons 2025 --prior-season 2024 --json
```

It reports margin/total MAE, margin bias, Brier score, home-win accuracy, and
ATS / over-under hit rates against the closing `spread_line` and `total_line`.
Downloaded schedules and play-by-play stay in `data/nflverse/` and are never
committed. ATS grading follows the nflverse convention that `spread_line` is the
home team's handicap and a **positive** value means the home team was favoured.

## Local storage

SQLite defaults to `data/edgeterminal.db`. The schema stores events, append-only odds snapshots, analyses, predictions, closing lines, results, calibration rows, and timestamped research sources. Local database files are intentionally not committed.

## Responsible use

Outputs are probabilistic research estimates. A `PASS`, `WATCHLIST`, or `WAIT FOR PRICE` result is expected when evidence or price does not support a play. EdgeTerminal does not use guaranteed-win language and does not place wagers.

## Local beta product

The dark NFL board now includes saved games and props, local result/line updates, separate live and archived records, probability checks and daily database backups. Hosting and checkout are deferred. See [local beta operations](docs/local_beta_operations.md) for refresh limits, backup recovery and the inactive invited-member foundation.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Gio Fiore.
