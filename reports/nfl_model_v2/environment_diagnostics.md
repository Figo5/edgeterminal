# Environment diagnostics, 2026-09-23

## The Odds API

`ODDS_API_KEY` is present in EdgeTerminal's `.env` and the running app says it
is configured. With this key, the free `/v4/sports` and
`/v4/sports/americanfootball_nfl/events` endpoints both returned HTTP 200;
the latter returned 32 events and cost zero credits. The response header
reported **one remaining usage credit**. EdgeTerminal requests `h2h`,
`spreads`, and `totals` in `ODDS_REGIONS=us,uk`: up to six credits per
successful odds call under the provider's published formula. The current key
is therefore valid but does not have enough remaining quota for that
configured request. No credential is missing now. The previously seen 401
could not be reproduced with the present key; without its original error
body, it cannot be attributed more precisely. A quota reset or additional
credits are needed for normal multi-market use. The final credit was not
spent on a likely unsuccessful odds request. ESPN/DraftKings fallback quotes
remain available for the research board.

## Review tooling

Review-tool credentials and routing were validated separately. No EdgeTerminal
code depends on them.

## Pi

`pi-twins` declared `typebox` as a peer dependency, but the Pi extension
package directory did not have it. Pinned `typebox@1.3.34` was installed in
the Pi extension package directory. Importing `typebox/schema` now resolves `Check`, and
`pi --help` exits 0 with empty stderr. No EdgeTerminal code depends on Pi.
