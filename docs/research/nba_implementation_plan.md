# NBA research integration — approved 2026-10-20

## Scope and owners

The user approved adding NBA through the existing orchestration workflow, with Claude, OpenCode Go, and Codex participating. Claude owns NBA identity, ESPN data parsing, and immutable SQLite storage. OpenCode Go (kimi-k3) owns the fixed baseline model, chronological evaluation, and statistical grading. Codex owns source bootstrap, shared provider quota enforcement, refresh integration, HTTP boundaries, daily NBA interface, and end-to-end integration.

Each external agent works in an isolated temporary worktree. Neither agent may commit, read credentials, alter NFL research, or enable picks. Implement new fixture tests before behavior, cross-review the integrated implementation, resolve findings, then verify the real browser. The orchestration skill requires a concrete pre-commit review after implementation.

## Research contract

NBA uses an Eastern calendar slate and stable ESPN event/player IDs, with season 2026-27 distinct from NFL's weekly record. ESPN supplies schedule, game quotes, rosters, historical game logs, and final box scores with source URL, receipt time, and response hash. NBA CDN requests currently return HTTP 403 and the NBA Stats endpoint times out; these are not treated as successful official league observations.

Only sourced main player lines can create five distinct player/stat selections before tipoff. Missing histories, identities, quotes, participation, or final statistics remain unavailable/pending. Current rosters and timestamped availability are required; missing injury entries are not proof of health. Completed games cannot acquire new prospective selections. Saved forecasts, quote locks, model parameters, original training cutoff, and source bytes are immutable; result corrections append evidence.

Player markets: points, rebounds, assists, three-pointers made, and points+rebounds+assists. The baseline must expose minutes uncertainty and enough previous games, use past observations only, and publish real chronological holdout counts/errors without claiming a validated betting edge. Historical evaluation stays separate from the prospective season record. PASS / RESEARCH_ONLY; no bankroll, stakes, official picks, or Odds API.

## Refresh contract

Use the current built-in worker and shared lock, including when a page asks for due updates. Cache public sources and batch paid props across an NBA slate. NBA and NFL share SportsGameOdds request/object guards and provider cooldowns; NBA has an additional budget so daily games cannot exhaust NFL's allocation. Reads never trigger provider calls. Completed saved games are checked automatically and graded only from verified final box scores after the postgame delay. No Codex automation is enabled.

## Acceptance checks

- Daily NBA board, date navigation, matchup, season record and current source/refresh status.
- No fabricated data, post-tipoff locks, cross-league IDs, duplicated selections or counts.
- Missing row versus confirmed played zero versus explicit DNP covered by tests.
- Quota/cooldown, source failure, shared refresh lock and hosted API boundaries tested.
- Chronological evaluation and model version fingerprint visible.
- Actual UI verification on desktop and a narrow viewport; existing NFL tests and freeze hashes preserved.
