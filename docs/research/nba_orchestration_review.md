# NBA implementation and verification — 2026-10-20

## Workstreams

- Data and storage: NBA identity, pure ESPN parsers, immutable source/forecast/quote/selection storage, fixture tests; a second pass tightened quote/source binding and one lock per game.
- Model and grading: fixed baseline game/player model, chronological evaluation, statistical grading, and tests; a second pass corrected explicit main-line provenance, confidence ranking, integer push probabilities, duplicate histories and the four-hour feature lag.
- Integration: cached source bootstrap, shared request guards, worker/API/UI integration, review of implementation changes, browser checks and regression verification.

Both implementation workstreams used isolated temporary worktrees. Their files and logs were preserved before removing the worktrees. The model workstream completed its edits and 57 tests; its process remained idle after writing documentation and was stopped after all five delivered files were verified identical to the integrated copies. No model or training parameter was changed after registration and the first forward forecasts.

## Review resolutions

A separate integration/security review reproduced three operational defects: the history-loading retry could be overwritten; one failed source could starve later work; requests could exhaust the NBA daily allowance before game day. These were corrected with persistent short retries, per-player/per-game failure isolation, a game-day capture window and a retry toward 45 minutes before tipoff. Empty provider responses still consume quota.

Further fixes require both current rosters, exclude ambiguous duplicate player identities without blocking unrelated games, scope NBA warnings to NBA status, preserve usable prior history after a failed recheck, append canceled-game voids, slow unresolved postgame retries, and save later comparison lines separately when the original forecast lacked one. Original forecasts and selections remain immutable. Source routes, same-origin protections, hosted previews and escaped UI output were reviewed without a security finding. A recheck's remaining medium finding was a missing schema initialization in a direct grading test; `grade_due()` now initializes its schema and that regression test passes.

Browser verification found ESPN prefixes totals with `o`/`u`; the parser now handles only the matching side prefix. Grading integration was exercised end to end for a win, push, played zero/loss, explicit DNP/void and missing player/pending, including repeat-run idempotence. Invalid HTTP-200 JSON cannot poison a long-lived cache.

## Verification

- Full Python suite: **706 passed**.
- JavaScript render/refresh suites: **15 passed**.
- Live built-in refresh completed successfully; all six NBA source/model/grade checks reported OK.
- Today's three games have **15 saved pretipoff selections**, captured in one SportsGameOdds request.
- Browser: NBA selector; today/tomorrow navigation; matchup and uncertainty disclosures; independent season record; source/evaluation page. Board and matchup fit a 390-pixel viewport without horizontal overflow; desktop checked at 1280 pixels.
- Existing NFL API remains Week 7 with 14 games and 20 selected props. All 13 original NFL prop freezes, 33 selection locks and 140,037 existing quote rows remained byte-for-byte intact. Protected Week 3 candidate and game-freeze file hashes are unchanged.

## Limits

The model is a descriptive, fixed baseline. Minutes use recent observed history; lineup/rotation/opponent effects and calibrated market probabilities have not been established. Current-roster historical evaluation has survivorship bias. No historical market-line win rate, edge or profitability is claimed. ESPN is identified as the source because NBA league endpoints were unavailable here. The built-in worker requires an awake, connected Mac and quota remains bounded. PASS / RESEARCH_ONLY; no official picks, stakes, bankroll, Odds API or additional automations were enabled.

Proposed commit: `feat(nba): add daily research and automatic grading`.
The pre-commit gate was confirmed with: “Commit the NBA changes”.
