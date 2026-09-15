# EdgeTerminal — NFL props v1 release handoff

**Status: personal research beta.** Wrong-event data and the invalid TD probability
normalisation are fixed. Unsupported recommendation paths are disabled, not hidden.

## Branch
- Baseline `50dd64c` → DeepSeek checkpoint `14b0d7e` (event/player integrity) → this
  release on **`nfl-props-v1`**. Nothing was reset, cleaned or force-pushed.
- The branch is `nfl-props-v1`; there is no `release/` prefix.

## What this release fixed

### 1. Anytime-TD probability maths (release blocker)
`no_vig_td_market` divided every player's Yes implied probability by the **sum across
all players**, forcing the field to total 1. Anytime-TD outcomes are not mutually
exclusive, so this was invalid, and it made each player's number depend on which other
players happened to be priced.

Measured on the live Lions @ Bills board (32 players, 6 books): raw implied Yes
probabilities summed to **6.55**, which the old code rescaled to 1.00. Jahmyr Gibbs at
-500 (**77.7%** implied) came out as a **11.8%** "fair" probability, fabricating a large
positive edge on every TD favourite.

Now: a fair probability is estimated only from the **same player's Yes and No at the
same book** (a genuinely complementary two-outcome market), median across the books
that quote both sides. No No price is ever fabricated; Yes quotes from other books do
not supply a missing No. Where no No exists, `market_prob` is `null` and the raw
vig-inclusive implied price is shown instead.

**Live finding:** none of the six books returns a No price for anytime TD, so today no
anytime-TD row is recommendable. That is the honest state, not a failure.

### 2. Other integrity defects found and fixed
- **Yes/No side was discarded** in the prop parser (`theoddsapi.py`). For TD markets the
  player was read as `description or name`, so a Yes and a No quote for one player
  collapsed into a single price series and were medianed together; a payload without
  `description` would have invented a scorer named "Yes".
- **First-TD contaminated the anytime board.** `props.py` pooled `player_1st_td` prices
  into the anytime pool and emitted every row labelled `player_anytime_td`. The board is
  now anytime-only and first/last-TD recommendations are disabled.
- **Every yardage prop got `market_prob = 1.0`.** Quotes were grouped by a key that
  *includes* the side, so each group held only Over or Under; de-vigging one side against
  itself returns 1.0. Grouping is now by (player, market, line) and a single-sided market
  is shown as a price, never priced.
- **The TD model saturated.** `opportunity_share` was a per-touch ratio
  (`(rz*1.4 + gl*2.0)/touches`), not a share of team touchdowns, so depth players hit the
  0.75 ceiling — the model claimed 75% for players the market priced near 18%. It is now
  the player's realised `td_per_game` relative to a league baseline, scaled by the game's
  projected environment. A player with no touchdowns yields no model, not a floor.
- **Research queries used the wrong team.** The planner received `team=home_team` for
  every player, producing "Jahmyr Gibbs expected role workload Buffalo Bills" for a
  Detroit player. Player-level gaps now use the team that player's usage resolved to, and
  fall back to the full fixture name rather than guessing a side.
- **`registry.event_props` crashed** with `NameError: db` whenever the caller omitted the
  event (the branch was never exercised).
- **Unscoped chat questions answered about a finished game.** `_resolve_event` ordered all
  stored events by kickoff and took the first, which was the oldest *final*. It now skips
  kicked-off games and excludes seeded `demo-` fixtures when not in demo mode.
- **The chat router dropped the selected game** for prop tools, so "this game" silently
  described a different one.

### 3. Browsing separated from recommendations
Three distinct states, in the API, the UI and chat:
- **A. Verified prices** — `prices[]`, state `NOT RECOMMENDABLE`, always shown, with the
  book, the vig-inclusive implied probability and the reason it is not recommendable.
- **B. Experimental analysis** — a model probability where usage supports one, labelled
  experimental. Edge/EV/fair price stay `null` here.
- **C. Official Picks** — unchanged production gates, and now additionally require a real
  fair market probability. No threshold was lowered.

"Best TD scorer" is answered as four separate things: most likely by the model, market
favourite, best estimated value, and whether any Official Pick exists.

### 4. Chat grounding and a stale-process trap
A chat answer quoted "overround 5.18% across 9 books", values absent from the current
payload. This was **not** a hallucination: port 8799 was still held by a uvicorn started
in a previous tool session on 14 Sep, serving the old proportional-normalisation board
(`method: proportional, overround: 5.18047, books: 9`). The model reported faithfully
what a stale process gave it.

That is exactly the failure mode of depending on a coding-agent-owned process.
`tools/edgeterminal.sh start` now detects an EdgeTerminal already holding the port that
it did not start, and adopts it with a warning instead of launching a duplicate that
loses the bind and leaves the health check passing against the old process. `restart`
replaces such a holder. Verify with `status` that the pid is the one you expect.

Independently of that, tool results for rendered tools now carry the application's own
deterministic rendering as `_authoritative_rendering`, so every quotable number is
present verbatim, and the system prompt states that confidence is a data-quality score,
that anytime probabilities never sum to 1, and that a null `market_prob` forbids any
edge claim.

### 5. Closeout: price/probability pairing
The release report read "Jahmyr Gibbs -500 (77.7% implied)". -500 is 83.3%, so the pair
was inconsistent. `american_to_prob` was verified correct (-500 -> 5/6, -350 -> 7/9,
+200 -> 1/3), so this was neither a report typo nor a conversion bug. It was two real
defects, both now fixed:

- **Worst price selected as best.** The anytime-TD loop compared raw American numbers,
  so `-500 < -350` and the least valuable quote won. Best now means lowest implied
  probability, the same test `best_prices()` already used. On a reproduction of the live
  book spread the selection moves from -500 (83.3%) to -330 (76.7%).
- **Implied probability did not belong to the displayed quote.** The board paired
  `best_price` with `raw_implied`, the *median across books* (0.776529 - exactly the
  77.7% that was reported). `implied_prob` is now computed from the displayed quote, and
  the cross-book median is retained separately as `market_median_implied`.

Tests cover the exact conversions, American/decimal/probability round trips (the +100 and
-100 evens boundary is asserted by value, since `prob_to_american` canonicalises to -100),
and that every board row's implied probability matches its own displayed price.

### 6. Why a recommendation is blocked: two separate conditions
These are distinct and are reported separately, never merged:
1. **No same-player No quote** -> no two-sided de-vig is possible, `market_prob` is null.
   A market-coverage limitation. No opposing price is fabricated.
2. **Insufficient or unvalidated modelling** -> no defensible model-based recommendation;
   missing usage yields `model_prob: null`, not a floor. A modelling limitation that
   would persist even with full No coverage.

Either one alone leaves the row NOT RECOMMENDABLE with its verified price still visible.
No threshold was lowered and no new model was introduced during closeout.

## Chat/research reliability pass

**Root causes of the wrong-game answer** (all four had to be fixed; any one alone
reproduced it):
1. No date, weekday or timezone handling existed anywhere in the application, and no
   timezone was configured. "Thursday night" had no resolution path at all.
2. The chat note told the model to "resolve ambiguous references to this event" with no
   precedence rule, so an explicit day lost to whatever the UI had selected.
3. `normalize_tool_args` validated only `find_best_bets`; a model-supplied `event_id`
   reached the provider unchecked.
4. There was no schedule tool, so the model could not ask which game fell on a day.

Kickoffs are stored in UTC and a Thursday-night game carries **Friday's** UTC date
(`2026-09-18T00:15Z` = Thu 17 Sep 20:15 EDT), so any UTC-date comparison is wrong.

**Central change.** `edgeterminal/agents/context.py` builds one validated context per
turn (task, market, teams, day constraint, local now, selected event, conversation
event) and resolves the event deterministically in `settings.timezone`
(`TIMEZONE`, default America/New_York). Authority order: explicit day/team, then the
selected UI event, then conversation context. An event is never chosen for being first
in the database, first with props, already played, or a demo fixture; duplicate
feed rows for one fixture are collapsed. Ambiguous slots ask one question; empty slots
say so and list the next games. `enforce_event_scope` pins every event-scoped tool to
the resolved id and logs any id the model proposed instead. `get_schedule` was added so
day questions are answered from the schedule rather than inferred.

`validate_answer` checks the drafted answer against the resolved event before display,
makes at most one bounded corrective attempt, then returns a flagged partial answer
rather than a confidently wrong one. The matchup/date/time/timezone header is rendered
from validated data, not narration.

**Research honesty.** Search backends return no publication date, so every result
previously scored the neutral weight and was labelled "recent" -- letting a Week 1
article read as current workload confirmation. Undated sources are now their own class
(`undated`): usable as background, never as current-role evidence.

**Two further defects found while auditing the above.** Both were in the *degraded*
paths, which is why the mocked happy-path tests did not catch them, and both restored
the original wrong-game behaviour:

5. `run_chat` dropped the validated context on its two degraded exits — the provider
   raising, and the model replying with no grounding tool call. Both called
   `fallback_answer(...)` without `ctx`, so the deterministic router fell back to the
   old "event id" note regex. An OpenAI outage on a "Thursday night" question therefore
   reproduced the exact defect this pass exists to fix. Both now pass `ctx`.
6. In `fallback_answer` the refactor computed the note-derived event id but then applied
   it only through `normalize_tool_args(..., ctx)`, which is a no-op when `ctx` is
   absent. So whenever context building failed, the selected game was silently dropped
   and `_resolve_event(None)` picked the next kickoff instead — a wrong game, chosen for
   being next on the schedule. The id is now pinned from the resolved value itself,
   independently of `ctx`.

Fixing (5) exposed a missing invariant: the unresolved-event early return produced a
displayed answer with an empty `tool_trace`, i.e. an answer attributable to nothing. The
event resolution is now recorded as a `resolve_event` trace entry, so every displayed
answer traces to a tool result or a named deterministic step.

**Verification.** `tests/test_chat_conversations.py` drives 23 whole turns covering the
Sunday/Thursday conflict, the UTC-date trap, empty slots, ambiguity, "this game",
market-only and game-switching follow-ups, stale history, blocked model event ids,
research failure, prices-without-pick, undated research, the resolved event with no
posted props, event-keyed analysis caching, and the two degraded paths above. The OpenAI
orchestration path is covered with a mocked client, separately from the deterministic
router. The two regression tests for (5) and (6) were each confirmed to fail with the
fix reverted, so they discriminate rather than merely pass.
Full suite: 212 passing.

**Live verification (this pass).** The original failing turn was replayed through the
real endpoint with the *real* frontend payload shape (`message` + last-8 `history` +
`event_id`), with Las Vegas Raiders @ Los Angeles Chargers selected, asking
"whos gonna score a touchdown on the thursday night game":

- resolved to **Detroit Lions @ Buffalo Bills, Thu 17 Sep 20:15 EDT** (stored
  `2026-09-18T00:15Z` — Friday's UTC date), `source=explicit-request`
- the single tool call ran against that event id, not the selected one
- `validation: {ok: true}`; header rendered from validated data
- prices shown with no Official Pick (PASS), market favourite kept separate from the
  experimental model, and each implied probability matched its own quote
  (-275 -> 73.3%, -150 -> 60.0%, +120 -> 45.5%, +185 -> 35.1%)

Cost: **2 OpenAI requests, 0 Odds credits** (the props board was served from a snapshot
captured 21 minutes earlier), **<=1 paid web search**.

**Browser check not performed.** The Claude-in-Chrome extension is not connected in this
environment, so no rendered-DOM check was made. Verified instead at the contract level:
`/`, `/static/app.js`, `/static/style.css`, `/api/config`, `/api/dashboard?sport=NFL`,
`/api/slate`, `/api/providers`, `/api/event/{id}` and `/api/chat` all 200, `node --check`
passes on `app.js`, and the chat payload the UI sends was read out of `app.js` and
reproduced exactly. A visual UI regression would not be caught by this.

## Verification
```
.venv/bin/python -m pytest -q                 # 212 passed
.venv/bin/python -m compileall -q edgeterminal tools
node --check edgeterminal/web/app.js
bash -n tools/edgeterminal.sh
PYTHONPATH=$PWD .venv/bin/python tools/audit_local_integrity.py --report
```
New regression tests prove: adding an unrelated player cannot move another player's
probability; same-player Yes/No de-vig works; a missing No stays missing; a No from
another book does not supply it; zero/missing usage cannot manufacture an edge or a
model probability; probabilities and fair prices stay in bounds; first-TD quotes never
enter the anytime board; a Yes-only market browses but recommends nothing; player
research queries never attach the wrong team.

Quarantine exclusion was re-verified directly against the live database: 908 quarantined
odds rows, 0 leaked into `latest_odds`; 25 quarantined predictions excluded from
performance.

**Live vs mocked.** Regression suite is fully mocked. Live-verified against the real Odds
API for one event (Lions @ Bills, identity `OK`): parser sides, the 6.55-vs-1.00 finding,
end-to-end board output, and the deterministic chat rendering. The launchd plist was
validated with `plutil` against a stubbed `launchctl`; **no login service was installed**
and persistence across a real terminal close is therefore unverified.

## Actual API usage
- **Odds API: 6 credits** (three `event_props` calls at 2 credits each). Budget was 10.
  Last observed quota: remaining 213, used 287.
- **OpenAI: 4 billable requests** (two chat turns, each a tool call plus a composition).
  Budget was 3 — **over by one**. The first turn exposed the wrong-team research bug; the
  second surfaced the stale-process trap above. Further grounding work was then verified
  through the deterministic (no-key) path at zero cost.

## Remaining limitations
1. No book returns an anytime-TD No price, so no TD prop can currently qualify. The
   feature is a price browser plus an experimental model until that changes.
2. The TD model is uncalibrated and runs mostly on prior-season baselines (current-season
   nflverse coverage is ~2 weeks). It reads high on longshots.
3. Name collisions still resolve on name similarity, not roster ids; such rows are capped
   at LEAN and ambiguous names are excluded.
4. The LLM narration path is now verified against the corrected board for **one** live
   turn (see "Live verification" above): correct event, validated header, prices without
   a pick, and each implied probability matching its own quote. One turn is not coverage
   of the narration path — the deterministic no-key path remains the authority, and the
   mocked suite is what guards the invariants.
5. Always confirm with `./tools/edgeterminal.sh status` that the serving pid is current
   after pulling changes; an adopted older process serves older code until `restart`.
6. Persistence across terminal close is unverified (see above).

7. No rendered-DOM browser check was possible in this environment (see above), so a
   purely visual UI regression would not have been caught.
8. Event resolution reads whatever schedule is already stored. If the events table is
   stale, "Thursday night" resolves correctly against stale data and will be wrong about
   a rescheduled game; `get_schedule`/`resolve_event` do not force a refresh.
