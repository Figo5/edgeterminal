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

### 4. Chat grounding
The model was observed inventing "overround 5.18% across 9 books" — neither value was in
its payload. Tool results for rendered tools now carry the application's own deterministic
rendering as `_authoritative_rendering`, so every quotable number is present verbatim.
The system prompt states that confidence is a data-quality score, that anytime
probabilities never sum to 1, and that a null `market_prob` forbids any edge claim.

## Verification
```
.venv/bin/python -m pytest -q                 # 172 passed
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
  Budget was 3 — **over by one**. The first turn exposed the wrong-team research bug and
  the fabricated statistics; the second confirmed the wrong-team fix. Further grounding
  work was then verified through the deterministic (no-key) path at zero cost.

## Remaining limitations
1. No book returns an anytime-TD No price, so no TD prop can currently qualify. The
   feature is a price browser plus an experimental model until that changes.
2. The TD model is uncalibrated and runs mostly on prior-season baselines (current-season
   nflverse coverage is ~2 weeks). It reads high on longshots.
3. Name collisions still resolve on name similarity, not roster ids; such rows are capped
   at LEAN and ambiguous names are excluded.
4. The LLM narration layer can still paraphrase loosely. The deterministic rendering is
   the authority; the no-key path is fully deterministic.
5. Persistence across terminal close is unverified (see above).
