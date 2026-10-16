# Local research beta

Open http://127.0.0.1:8799/. Keep `HOSTED_MODE=false` and `ODDS_PROVIDER=espn`.
This stage has no subscription checkout, cloud deployment, paid upgrade, or outgoing alerts.

## Product

- The dark weekly board shows projected scores, winner, model spread/total and observed game prices.
- A matchup shows five distinct supported player/stat markets, the original line and price, model projection/probability, source timestamp and a short explanation. A later observed line is displayed separately; it cannot rewrite a locked selection.
- Save games from the board and selections from the matchup. **Saved & account** uses an automatic local profile. Saved selections show results, line-change updates and result corrections in a local inbox. The navigation shows unread updates.
- Season record separates selections locked before kickoff from archived rankings applied later to old pregame forecasts. Weekly winner/spread/total results preserve each saved forecast and its contemporaneous line. Missing historical forecasts stay **Not recorded**; partial Week 1 coverage is disclosed.
- Live probability checks show observed outcomes by confidence band and market. They describe this selection policy only; correlated props and small samples cannot demonstrate calibration or profitability.
- About & data keeps source and operational diagnostics available. The board reports actual worker timestamps, prop coverage and unresolved failures.

The existing fixed models, training cutoff, Week 3 candidate artifact, immutable freezes, PASS and RESEARCH_ONLY remain in force. There are no official picks or wagers. Extra explanation metadata is added only to newly generated prop forecasts; old forecasts are not rewritten.

## Updates

Opening or reloading the site displays the saved slate immediately, then checks for due updates through `POST /api/product/nfl/check`. The visible page checks again every minute. All visitors share a five-minute cooldown, and a recent scheduled worker check is reused. **Check updates** uses the same path. Provider cadence, quota limits and the worker lock still apply; a page visit cannot force a prop request.

The existing `com.edgeterminal.research-refresh` LaunchAgent also checks every five minutes while this Mac is awake and the user is logged in. It keeps capturing and grading when the page is closed. Without either a background worker or page visits before kickoff, a capture window can be missed and is never backfilled.

The scheduled command remains:

```sh
cd ~/edgeterminal
.venv/bin/python -m edgeterminal.research_refresh run
```

This refreshes the current schedule, checks due ESPN game quotes, captures supported props, locks selections before kickoff and grades due records. It never calls Odds API or backfills past games. Five props are shown only when five supported markets are available. Waiting for lines is a real coverage limit, not a reason to invent selections.

A sleeping or powered-off Mac cannot serve this localhost website or refresh its data. Refresh on page visit removes the manual update step; operation with the Mac off still requires hosting the backend and its persistent research records elsewhere. Local service health requires no provider request:

```sh
.venv/bin/python -m edgeterminal.service_ops status
```

Read-only endpoints include `/api/product/status`, `/api/product/methodology`, `/api/product/nfl`, and `/api/product/performance`.

## Backups

After a worker run, including an actual page-triggered run, the maintenance step creates at most one SQLite backup per day and retains three completed backups in `data/backups/`. Failed backups are reported in service health and retried no sooner than six hours later. Each backup uses SQLite's online backup API, includes committed WAL data, runs `quick_check`, and has a SHA-256 manifest of the database and registered freezes. Files are readable only by the owner.

Manual backup:

```sh
.venv/bin/python -m edgeterminal.service_ops backup
```

These backups protect against accidental database damage, but are on the same disk. They do not protect against loss of the Mac. Before migration or recovery, separately preserve `.env`, `data/research/`, `data/nflverse/`, `reports/`, model artifacts and the repository. Keep `.env` private.

Recovery: stop both LaunchAgents, preserve the damaged database and its WAL/SHM files, verify the chosen backup SHA against its manifest and run SQLite `quick_check`. Restore the backup to the configured database path without reusing the damaged WAL/SHM files. Restore matching source archives and freeze artifacts, then start the services and check integrity and the season record. Do not restore over a running database.

## Future invited beta foundation

Optional member sessions and server-side preview boundaries are implemented and tested, but are inactive locally. No accounts were invited and no messages were sent during setup. Future hosted mode requires `HOSTED_MODE=true`, an HTTPS `PUBLIC_URL`, ESPN configured, and a separate background worker. It allows the cadenced page-update check but denies legacy internal API endpoints and forced provider-refresh routes, uses hashed single-use 15-minute sign-in tokens and HttpOnly cookies, restricts requests by host/origin, and checks beta access on the server.

An owner can generate an invitation locally using `python -m edgeterminal.members EMAIL --days 30`. The link is a credential: share it only with its intended recipient. Optional SMTP settings enable a tester to request a replacement link; without SMTP, owner-generated invitations are required. The command itself does not send email or charge money. This path is tested in isolated databases, not deployed.

## When results justify a hosted version

Evaluate several full weeks of prospective selections: coverage, quote freshness, missing results, correction rate, observed probability bands and repeat usage. Collect user feedback before setting a price. A winning streak alone is insufficient.

Next launch steps are an always-on worker, an off-machine database backup, licensed data redistribution rights, eligible payment-provider approval, real subscription lifecycle handling and customer support. None are enabled now. The current Python/SQLite service is not a drop-in Cloudflare Workers application; choose an appropriate origin or migrate the runtime/storage deliberately when hosting is approved. Never expose the unrestricted local mode through a public tunnel.

## Coverage and source repairs (2026-10-06)

Every game now explains its missing prop coverage using the latest targeted capture: complete main-line market count, model support, last check, and next check under the existing cadence. Missing source configuration, failed captures, quota pauses and model-input gaps are distinct from a feed returning no lines. About & data lists the waiting games. No line-release date is inferred from an empty response.

Targeted requests use the feed's `receiving_receptions` stat ID and the five previously used books: DraftKings, FanDuel, Caesars, ESPN BET and Bovada. Only the five modeled main markets are requested. Existing quota and cadence guards remain unchanged. Stat identifiers are documented in [SportsGameOdds stats](https://sportsgameodds.com/docs/data-types/stats).

Postgame ingestion accepts signed yardage while receptions and rushing attempts remain nonnegative. The canonical team mapper handles nflverse's LA Rams alias. Replaying the existing Week 3 sources added 508 previously missing quote-side grade revisions with zero provider requests and no outcome corrections. All 57 archived ranked props now have results: 28 wins and 29 losses. The 25 live Week 5 props remain pending and separate. One player outside the archived top-five record still needs participation evidence; no zero or inactivity is inferred. Historical freezes and model parameters were not changed.
