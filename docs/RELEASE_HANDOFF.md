# EdgeTerminal — NFL props v1 release handoff

## Baseline and branch
- Starting HEAD: `50dd64c` on `main` (NFL-only + nflverse + walk-forward backtest).
- All prop/research work was uncommitted on top of it; nothing was reset, cleaned or force-pushed.
- Work lands on branch **`nfl-props-v1`** as an audited checkpoint (not a production release). Not merged to `main`.

## Files and responsibilities
- `edgeterminal/integrity.py` — **new**. Event identity (`validate_provider_event`, `match_provider_event`), event-scoped versioned prop cache keys, player identity (`classify_player` → participant / mismatch / unresolved), model eligibility.
- `edgeterminal/props.py` — prop workflow (market fetch → usage → planner → bounded research features → model → market blend → state/units). Player verification, minimum-sample gate, prior-season baseline cap.
- `edgeterminal/analytics/usage.py` — nflverse play-by-play usage; `find_candidates` (abbreviated-name resolution, no unrestricted surname guessing).
- `edgeterminal/analytics/td.py` — TD/yardage models, multi-outcome no-vig normalisation.
- `edgeterminal/providers/theoddsapi.py` — on-demand event props, quota headers, validated payloads, versioned cache key.
- `edgeterminal/providers/registry.py` — provider routing; `event_props` resolves identity before any fetch.
- `edgeterminal/db.py` — `quarantine` table + `latest_odds(exclude_quarantined=True)`; `calibration._joined` skips quarantined predictions.
- `edgeterminal/research/*` — source reliability, TTL cache, bounded features, planner, provider (OpenAI web search → Tavily → Brave → DuckDuckGo).
- `tools/audit_local_integrity.py`, `tools/verify_live_props.py` — reproducible audit and live trace (the five temporary root `scripts_*.py` were removed).

## Root cause and fix
Prop requests sent the **stored local event id straight to the provider** and trusted the response. Completed games disappear from the Odds API feed (hence 404), and an id that no longer exists can resolve to another live game; nothing validated the returned matchup, kickoff or players. Responses were cached under `props:<local id>`.
Now: local and provider ids are separate; a provider event is matched on **exact canonical home/away + kickoff within tolerance**, nearest kickoff wins, two plausible matches are **AMBIGUOUS**, finished events are **EXPIRED** and never substituted; the payload must re-validate before it is cached or returned; cache keys are `props:v2:<hash(provider id + local id + matchup + kickoff + markets)>`. Players must resolve to **one identity on an event roster** or they are excluded.

## Tests and verification
```
.venv/bin/python -m pytest -q                     # 162 passed
.venv/bin/python -m pytest tests/test_event_integrity.py -q
PYTHONPATH=$PWD .venv/bin/python tools/audit_local_integrity.py --report
PYTHONPATH=$PWD .venv/bin/python tools/verify_live_props.py --market player_anytime_td
```
Covers: wrong upstream event, finished event 404/EXPIRED, same teams on two dates, ambiguity refusal, cross-event cache isolation, unrelated/ambiguous players, quarantine exclusion, missing usage, key redaction.

## Live evidence (sanitized)
- Requested `Detroit Lions @ Buffalo Bills`, kickoff 2026-09-18T00:15Z; resolved provider id `56e889768191`, identity `OK`.
- Market `player_anytime_td`: 33 participants, **3 mismatches** (players whose usage team is not in the game) and **13 unresolved** excluded; no Official Picks — prior-season-baseline rows are LEAN only.
- Finished event `Denver Broncos @ Kansas City Chiefs` → `EXPIRED`, 0 recommendations, explicit note.
- Local repair: 908 suspect prop rows and 25 predictions quarantined; database backed up to `data/backups/`.
- Cost: 2 requests for the final verification; diagnosis probes (before the state was known) used ~11 — over the 10-credit budget. Last observed quota: remaining 253, used 187.

## Remaining blockers
1. **TD multi-outcome normalisation still requires review** — proportional de-vig on a juiced, incomplete TD market is a first approximation; Claude should audit it before any release.
2. Name collisions: a same-surname-and-initial prior-season match can still stand in for a depth player; such rows are now capped at LEAN, but identity should be keyed on provider/roster ids.
3. Current-season nflverse coverage is thin (week 1 only), so most props run on labelled prior-season baselines.
