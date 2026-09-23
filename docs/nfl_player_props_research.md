# NFL player-prop observations (research only)

This feed is separate from game lines, the frozen NFL game model, and every pick
path. It stores observed prices; it does not project player outcomes, calculate
edges, or recommend bets. Live product behavior remains `RESEARCH_ONLY` / `PASS`.

## Provider and capture

SportsGameOdds v2 `GET /events` is the first adapter. Set
`SPORTSGAMEODDS_API_KEY` in `.env`, then run an explicit pre-week capture:

```sh
.venv/bin/python -m edgeterminal.nfl_props --season 2026 --week 4
```

This requires a canonical NFL schedule in the local database. It requests only
NFL events in that week's kickoff range, paginates up to ten pages, and refuses
to run after the first local kickoff. No app startup, page load, or normal test
uses this API. The key is sent in an HTTP header and is not placed in the URL.
There is no automatic polling or guarantee of capturing a closing quote.

The command returns event mapping and quote counts, plus markets and bookmakers
**actually observed in that retrieval**. `events_mapped / events_seen` is the
mapping rate. A provider event is linked only when its home and away teams match
a single canonical schedule game with a nearby kickoff. A previously linked
provider ID can survive a reschedule if the teams still match. Unmatched events
are counted in the immutable batch but contribute no quotes. A contradictory
event mapping aborts the whole capture.

## Normalized records

`nfl_prop_snapshots` contains canonical game ID, SportsGameOdds event ID,
canonical player ID, provider player ID, player name, team, opponent, market,
line, side, American price, normalized sportsbook, retrieval time, kickoff time,
provider, source endpoint, line type, alternate offer index, and provider quote
update time when present. Prices and lines are recorded **per sportsbook**;
the provider's consensus is not substituted for a
book quote. Yes/no anytime touchdown prices have a null line, because the
provider's documented market has no numeric O/U line. Empty, unavailable,
invalid, unpriced, unlined, and ambiguous player offers are omitted with
reason counts in the immutable batch audit.

The eight canonical markets are `passing_yards`, `passing_touchdowns`,
`interceptions`, `rushing_yards`, `rushing_attempts`, `receptions`,
`receiving_yards`, and `anytime_touchdowns`. The adapter accepts full-game
over/under markets for the first seven and full-game touchdown yes/no for the
last. It requests `includeAltLines=true` and stores each available alternate
book quote separately. It ignores other periods and bet types.

SportsGameOdds player IDs are linked to stable local IDs such as
`sgo:JOSH_ALLEN_1_NFL`. These IDs do not depend on name spelling. Player name
and a team ID matching one of the event's teams are required for a quote. A
provider ID that later names a different player is a hard error. Cross-provider
identity is **not inferred** from names; a future provider can attach a
verified alias to the local ID. Team transfers do not change player ID.

`nfl_prop_raw_responses` archives each HTTP response body as bytes, with its
SHA-256, safe quota headers, request filters, and page number **before** JSON
parsing or quote normalization. This survives a normalization failure and can
be replayed locally with `ArchivedSportsGameOddsProps` without another API
call. `nfl_prop_capture_batches` stores the combined canonical JSON, audit
counts, and event-level coverage; its hash applies to that canonical JSON.
Every successful capture appends a new batch and quote rows. SQLite triggers
prohibit changing or deleting raw pages, batches, and quotes. Player aliases
are also append only. Game lines remain in `odds_snapshots`.

## Read-only board

`GET /api/research/nfl/prop-board/{canonical_game_id}` returns **all** main and
alternate quotes from the most recent stored batch for that game. It does not
carry forward an older line when a newer batch lacks it. Optional `market`
filters to one canonical market. The response is always `RESEARCH_ONLY` and
`PASS`, with no recommendation or predicted probability. This route performs
no provider call. Older observations remain queryable in SQLite by batch or
game ID even when the board shows a newer quote.

Create a write-once coverage report without an API request:

```sh
.venv/bin/python -m edgeterminal.nfl_prop_coverage \
  --batch-id BATCH_ID --output reports/nfl_props/coverage.json
```

The report shows every provider event and every locally scheduled game,
mapping status, player and quote counts, books, markets, freshness, warnings,
raw-response hashes, quota notices, and one checked quote sample per market.

## Operational limits

Without `SPORTSGAMEODDS_API_KEY`, live mapping success, book coverage, and
quote counts are unknown. Fixture results prove parsing and persistence only;
they are not evidence that those props or books are currently available in a
subscription. A live capture spends SportsGameOdds request/object allowance;
its published limits vary by plan and may differ for existing subscribers.
Availability can vary by jurisdiction, book, player, market, and time. A local
schedule error or missing player/team ID causes conservative omission. Stored
quotes prove what the feed returned when retrieved, not that a wager was
available to this user at that price.

Fixture tests use `tests/fixtures/sgo_nfl_props.json`; they never call the API.
