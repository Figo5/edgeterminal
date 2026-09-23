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
provider, source endpoint, and provider quote update time when present. Prices and lines are
recorded **per sportsbook**; the provider's consensus is not substituted for a
book quote. Yes/no anytime touchdown prices have a null line, because the
provider's documented market has no numeric O/U line. Empty, unavailable,
invalid, unpriced, unlined, and ambiguous player offers are omitted.

The eight canonical markets are `passing_yards`, `passing_touchdowns`,
`interceptions`, `rushing_yards`, `rushing_attempts`, `receptions`,
`receiving_yards`, and `anytime_touchdowns`. The adapter accepts full-game
over/under markets for the first seven and full-game touchdown yes/no for the
last. It deliberately ignores alternate periods and alternate lines for now.

SportsGameOdds player IDs are linked to stable local IDs such as
`sgo:JOSH_ALLEN_1_NFL`. These IDs do not depend on name spelling. Player name
and a team ID matching one of the event's teams are required for a quote. A
provider ID that later names a different player is a hard error. Cross-provider
identity is **not inferred** from names; a future provider can attach a
verified alias to the local ID. Team transfers do not change player ID.

`nfl_prop_capture_batches` preserves the complete fetched JSON structure, its
SHA-256, retrieval time, and counts. JSON is canonicalized before hashing; it
is not claimed to be byte-identical to the HTTP response body. Every capture
appends a new batch and new quote rows. SQLite triggers prohibit changes or
deletion of batches and quote rows. `nfl_prop_player_aliases` is also append
only. Game lines remain in `odds_snapshots` and never receive these props.

## Read-only board

`GET /api/research/nfl/prop-board/{canonical_game_id}` returns the latest
stored quote for each player, market, side, and sportsbook. Optional `market`
filters to one canonical market. The response is always `RESEARCH_ONLY` and
`PASS`, with no recommendation or predicted probability. This route performs
no provider call. Older observations remain queryable in SQLite by batch or
game ID even when the board shows a newer quote.

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
