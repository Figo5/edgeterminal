# NBA prop identity repair — October 29, 2026

Today's successful SportsGameOdds response contained available main lines but
player metadata had only provider player IDs and team IDs, without names. The
name-based ESPN roster join therefore rejected all player markets. The capture
cadence then waited for its scheduled near-tipoff retry.

The adapter now recovers missing names from prior immutable SportsGameOdds
artifacts, checking HTTP status, host/path, raw hash, NBA league, exact provider
ID and receipt time. Conflicting names remain excluded. Each current player must
still match exactly one current ESPN roster entry on the current event team.
Provider IDs are never converted into guessed names. Historical prices and
availability are never reused.

Each recovered selection saves its provider ID and exact identity source artifact
in the forecast's source manifest. Automatic learning replays those frozen proof
sources, checks their hash and receipt time, and matches the sourced main line.
It does not consult newer mutable identities to verify an old capture.

## Live verification

- A fresh guarded request at 14:51 Eastern returned 223 supported available offers.
- CLE @ ATL, SAC @ NOP and MEM @ POR each received five saved selections.
- All 15 saved quotes passed replay against their current raw quote source and
  declared identity evidence. The product API shows all three games locked.
- Prior NBA forecasts, quotes, locks, lock items and grades retain their hashes.
  Protected model files and historical NFL freezes are unchanged.
- Both the web app and the five-minute built-in worker were restarted. Model
  parameters, cutoff, PASS and RESEARCH_ONLY remain unchanged.
- The normal two-request rolling-day allowance was respected; both requests for
  this window are now used. No Odds API requests were made.

Detailed local verification was recorded with the release evidence.

## Validation

77 focused Python tests and 19 JavaScript tests pass, including fresh capture,
identity provenance persistence, automatic learning replay, and missing/future
proof rejection. A code/security review found no blocking issue.
The full Python run also found an unrelated existing failure in
`test_parlay_chat_uses_parlay_tool_without_openai_key`; the same failure was
reproduced on unmodified committed HEAD. No chat behavior was changed here.

## Windows source release

At pre-commit review, the existing Windows source bundle contains b6be680 and
predates this repair. Regenerate its bundle, manifest and ZIP only after the
scoped repair commit is confirmed, then verify a clean clone at the exact new
commit and compare the protected model and historical freeze bytes.

The owner has verified Ubuntu WSL2, Docker Linux x86_64, Compose and Git, with
224.8 GB free. These readiness checks do not establish that EdgeTerminal's
containers or phone access have been tested on Windows. The live Mac services
continue running; the final state archive and cutover remain pending.
