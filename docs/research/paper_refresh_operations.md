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
.venv/bin/python -m edgeterminal.research_scheduler uninstall
```

The installer writes `~/Library/LaunchAgents/com.edgeterminal.research-refresh.plist`, bootstraps `com.edgeterminal.research-refresh`, and sends stdout/stderr to `~/Library/Logs/EdgeTerminal/`. It runs the project virtualenv from the repository root and loads `.env` there. The LaunchAgent ticks every five minutes while the user is logged in. The worker uses a nonblocking file lock to prevent overlap. No open Terminal window or browser is required. A sleeping or offline Mac cannot capture a missed quote; the next wake retries only work that remains prospective. The worker never fabricates a market observation.

The worker refreshes ESPN schedule/status every six hours normally, every two hours within 36 hours of a kickoff, and every 30 minutes within two hours. It takes an ESPN game-market observation at most every six hours within the 36-hour freeze window, then checks within 15 minutes of each mapped kickoff; the five minute ticks can capture multiple observed prices in that final window. These are append-only observations, never edits to a freeze. Game context (ESPN injuries and Open-Meteo game-time weather) runs at most every three hours for games within six hours. An announced starting QB is **not** inferred from injury listings; without timestamped starter evidence, game PAPER positions fail closed. Existing game freeze creation is attempted in its 36-hour window by the existing cutoff-safe weekly workflow. Game and prop grading use their existing postgame delay and append-only revisions. The prop results workflow still requires reviewed Game Book participation evidence for ambiguous zeros/inactivity.

SportsGameOdds prop capture is bounded to **one attempt per week**, in the 24 hours before the first kickoff, only if the API key is configured and no prop snapshots exist for that week. The provider adapter may paginate that attempt into up to ten HTTP requests and archives each response and rate header. A failed attempt is recorded and is not automatically retried that week. This is the only paid/quota-sensitive provider in the new worker; the worker never calls Odds API. A deliberate manual retry, if justified, must be made through the explicit prop capture command after examining the provider failure and quota. Provider errors and missing quotes appear in `research_refresh_steps` and the Data health UI. The status API reads stored state and never ingests data.

`GET /api/research/refresh-status` and `GET /api/research/paper` are read-only. Opening or reloading the UI fetches stored rows and does not run the worker. The existing explicit **Refresh data** control remains a user-triggered NFL product refresh; ordinary page refreshes never initiate it. The PAPER tab shows the mock bankroll, available bankroll after open stakes, positions, conditional settlements, W-L-P/V, units, P/L, ROI, and results by market/model/policy. It lists fresh research opportunities separately from frozen PAPER positions. The archived model-line distribution includes stale quote sides and is labeled accordingly.

## Policy and ledger

`data/research/paper_policy_example.json` remains a disabled example. `data/research/experimental_paper_v1.json` is the enabled mock policy, with a $1,000 PAPER bankroll and frozen selection and exposure rules. Its probability cutoff is arbitrary and selected only to control the number of mock positions, not a validated betting threshold. Reusing a registered version with changed content fails. The account's starting bankroll is fixed by its first registration; changing policy later does not reset the account or rewrite positions.

`paper_policies`, `paper_accounts`, `paper_positions`, `paper_opportunity_skips`, `paper_settlement_revisions`, `research_refresh_runs`, and `research_refresh_steps` are append-only SQLite tables. Every new position freezes the source research freeze SHA, source quote ID, player/game, market, side, line, price, book, projection, probability, implied probability, observed and creation times, kickoff, model version/hash or freeze-contained research signature, policy version/hash, and stake. A quote ID can create only one PAPER position across policy versions; prop player/market/game is unique. Existing source freezes are never rewritten. Prop positions require a timestamped available/active/healthy/cleared observation for that exact player. Missing context, stale price, invalid quote, missing projection, or bankroll limits record a skip reason.

V1 allows only the five continuous/count NFL prop markets. Game spread, total, and moneyline are excluded. The prop projection lab still has a Week 3-specific forward projection path; unattended generation of **future-week prop freezes** needs a separately reviewed generalization of that path and a model reproducibility bundle. The refresh worker captures future-week props but will not invent a projection or PAPER position where none is frozen. This prevents historical replay under today's policy from contaminating the prospective ledger.

Prop PAPER settlements read the existing latest sourced prop grade revisions, retaining sportsbook-specific conditional interpretation; ambiguous or corrected settlement stays pending. Game moneyline settlements read existing game grade revisions; a tie stays pending because book rules may differ. Every correction adds a PAPER settlement revision, and reports use only the latest revision. Paper P/L is a mock accounting calculation from recorded American odds. It does not assert that a price was attainable or that a sportsbook paid a ticket. Historical simulations, if developed later, must use another namespace and cannot enter this ledger.

The current descriptive distribution should be inspected with:

```sh
.venv/bin/python -m edgeterminal.paper_research distribution
```

Do not tune V1 from Week 3 outcomes. Its fixed threshold was chosen from the archived pregame opportunity count only. The disabled example remains available for dry runs; the worker loads the versioned experimental policy.
