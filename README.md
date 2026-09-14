# EdgeTerminal

EdgeTerminal is a local sports-market research and analytics terminal. It compares stored prices with deterministic projections, exposes confidence and risk inputs, records odds history, and provides a tool-grounded chat analyst. It does **not** place bets.

The current complete vertical slice is NFL-first. CFB, NBA, and PGA provider/analytics abstractions are present but are secondary.

## Quick start

Requires Python 3.9+ (3.11 recommended).

```bash
cd ~/edgeterminal
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env if you have provider credentials.
PORT=8799 .venv/bin/python -m uvicorn edgeterminal.api:app --host 127.0.0.1 --port 8799
```

Open <http://127.0.0.1:8799>.

Port `8799` is EdgeTerminal's default; `8787` is reserved for CivicFolio.

## Demo mode

`DEMO_MODE=true` forces deterministic fixture providers. The header displays **DEMO DATA** and every fixture-derived recommendation carries `is_demo=true`. Fixture odds are never presented as live/current odds.

Demo mode supports the complete workflow without credentials:

1. Open the NFL dashboard.
2. Press **Refresh Odds** to normalize fixture prices and append a SQLite odds snapshot.
3. Inspect confidence-80+ official picks in **Best Bets**.
4. Open a matchup for projections, market prices, team comparison, injuries, news, weather, disagreement, risks, and sources.
5. Ask the analyst for the best bet in the selected game or the best parlay.

The application does not poll odds. Refreshes occur only on startup for an empty database, on the button/API action, or when a chat analysis detects stale prices.

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
public endpoints are unofficial and can change or omit fields. Advanced EPA/PROE,
complete role/snap data, and comprehensive player props are not claimed when the
upstream feed does not provide them.

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
- parlay probability with explicit rule-based correlation adjustments

Official picks require confidence `>= 80` by default. Confidence is trust in the estimate; edge is price value. Either can be high while the other is low.

## Tests

Tests isolate SQLite in temporary files and do not call paid providers or OpenAI.

```bash
.venv/bin/python -m pytest -q
```

The suite covers pricing conversions, edge, unit sizing, confidence bounds, provider normalization, odds snapshot persistence/history, line movement, parlay correlation, deterministic chat routing, and the API/static-UI vertical slice.

## Local storage

SQLite defaults to `data/edgeterminal.db`. The schema stores events, append-only odds snapshots, analyses, predictions, closing lines, results, calibration rows, and timestamped research sources. Local database files are intentionally not committed.

## Responsible use

Outputs are probabilistic research estimates. A `PASS`, `WATCHLIST`, or `WAIT FOR PRICE` result is expected when evidence or price does not support a play. EdgeTerminal does not use guaranteed-win language and does not place wagers.
