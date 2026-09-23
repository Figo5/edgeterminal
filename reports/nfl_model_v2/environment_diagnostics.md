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

## Hermes / OpenCode Go / DeepSeek

`OPENCODE_GO_API_KEY` is present and `hermes auth status opencode-go` reports
logged in. The old `~/.hermes/config.yaml` selected `nous` with a Nous model
ID. A direct Go chat request without `x-opencode-session` returned HTTP 400
`MissingSessionID`; the same DeepSeek request with the session header
returned HTTP 200. Hermes' built-in Go route adds that header. The default
config was set to:

```yaml
model:
  default: deepseek-v4-flash
  provider: opencode-go
  base_url: https://opencode.ai/zen/go/v1
```

`hermes chat -Q -q 'Reply with OK only.' --max-turns 1 --run-budget 45`
returned `OK` and exit code 0. An explicit route is:
`hermes chat --provider opencode-go --model deepseek-v4-flash -q 'Your prompt' --oneshot`.
This uses OpenCode Go, not Nous. The standalone `opencode` CLI is not
installed and is not required for this Hermes route.

## Pi

`pi-twins` declared `typebox` as a peer dependency, but the Pi extension
package directory did not have it. Pinned `typebox@1.3.34` was installed in
`~/.pi/agent/npm`. Importing `typebox/schema` now resolves `Check`, and
`pi --help` exits 0 with empty stderr. No EdgeTerminal code depends on Pi.
