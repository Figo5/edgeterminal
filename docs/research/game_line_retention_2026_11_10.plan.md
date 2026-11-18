# NBA and NFL forecast line retention repair

Status: planning reviewed; implementation requires plan confirmation.

Base release: `4c691b64d882565c044b91d3858705cd518f7ce5`.
Isolated worktree: `/Users/giofiore/edgeterminal-line-retention`.
Size: large, because publication, source validation, records, grading and learning share this contract.

## Confirmed failure

NBA schedule ingestion can replace a previously priced market with a later empty market. The automatic revision producer projects and seals that empty market; the selected latest pretipoff revision supersedes earlier priced forecasts. NFL automatic revisions similarly query only currently fresh odds, then save an empty market when prices age out. Both season readers choose the latest forecast, so completed games lose their supported spread/total decisions.

The archived November 9 examples are Golden State–New Orleans and Buffalo–Minnesota. Scoped read-only observations were recorded at planning time; no production evidence was changed. Both have existing priced forecasts saved before the game, followed by empty-market forecasts. Markets can influence numerical projections, so attaching an older quote to a later unpriced projection after the result is known is not an acceptable repair.

## Required behavior

1. Preserve the last verified complete spread, total and moneyline groups independently when a later response omits a market. Replace a group only with a newer verified complete group for the same game, teams, kickoff and allowed source. Keep each group's book, quote/artifact identity, hash, original receipt and observation time. Never assemble opposite sides or line/price pairs from unrelated offers.
2. Resolve retained inputs before computing and sealing each future forecast. Store immutable per-market provenance and whether a quote was retained. Do not refresh a source timestamp by copying it. Retained comparison lines do not become fresh executable prices: existing live-price, market-anchor and quota guards remain in force.
3. Make the board, game grading, season record and learning readers agree on the exact saved forecast–line decision. Never choose a different forecast based on its result. Never accept a postgame quote or infer a missing line from a score, another matchup or an unrelated sportsbook.
4. Preserve all existing snapshots, predictions, freezes, quotes, grades, model artifacts and original policies byte-for-byte. Models, numerical parameters, cutoffs, activation thresholds and provider quotas stay unchanged.
5. For the confirmed GSW–NOP and BUF–MIN historical gaps, audit exact existing sealed, priced pregame forecast–line pairs using a deterministic pregame-time rule declared before reading outcomes. Preserve original selected forecasts, grades, policies and live/headline W–L, including their original unavailable markets. Any recovered pair and its result belongs only to a separately labeled append-only historical research section, with its own original forecast, projection, side, source and model attribution. Never merge recovered counts into the prospective headline, replace original grades, or make recoveries eligible for learner freezes, inner/outer cohorts, reservations or activation evidence. Inventory further historical gaps without silently expanding the recovery list. Insufficient or ambiguous evidence remains unavailable. Release review must show the exact historical audit/correction effects before any release.
6. Preserve the existing sealed NBA totals and NFL spread learning stages, code archives and permanently consumed evaluation windows. Before any affected capture reader runs, verify the old chain, archived code/source integrity and exact approved catalog delta; append one terminal context-change event idempotently, then let the normal offline learner own a compatible successor chain. Unknown code, source or protocol mismatches remain visible integrity errors. Preserve consumed cohorts and cooldowns; never reseal an old stage, weaken a fingerprint check, fit inside refresh or force activation. The current live models and automatic capture/grading continue.

## Ordered task list after confirmation

1. Add failing offline regression tests for both actual loss patterns, partial-market updates and expiration of current quote freshness; bind representative fixtures to the archived evidence.
2. Implement strict source-verified per-market retention and provenance, then integrate it before the NBA and NFL projection/publication boundaries. Recheck timing, identities and competing source heads at the existing write boundary.
3. Integrate a common saved-decision contract into board, grading, record and learner reads. Keep old format readers, immutable historical rows and existing enrolled NBA policy bytes/IDs supported; do not modify the existing policy under its old identifier. Verify explicit unavailable versus genuinely pending results. Source withdrawal/suspension, corruption and contradictory equal-time offers remain visible failures rather than successful fresh quotes.
4. Add the audited historical research section for the two confirmed old priced pairs only. Keep its immutable bindings/results separate from original grade revisions and original headline counts. Verify outcome-blind selection, distinct model attribution, idempotent recovery grading and no double counting. Explain that original missing-line live decisions remain unavailable rather than implying retroactive prospective picks.
5. Handle sealed-learning code-context compatibility without losing prior evidence or reusing evaluation windows. Verify ordinary worker scheduling, restart recovery and current-model service under both compatible and incompatible staged state.
6. Run focused tests, full Python/JavaScript regression checks and a socket-denied replay of the real archived failures on an owned database. Obtain code/security review.
7. Present the exact scoped diff, meaningful test results, historical recovery effects and proposed commit for release confirmation. Commit/release only after confirmation. Release at genuine idle learner and research-worker boundaries plus independent source-child leases: the prior unrelated-NBA-fit exception does not apply to this code-catalog change. Then verify a genuine automatic cycle, current source status and record reconciliation.

## Verification requirements

- Newer complete quotes supersede older quotes; empty responses and one missing market do not erase the other verified decisions.
- Spread pairs and total groups retain coherent book/offer provenance; equal-clock conflicts and ambiguous identity are rejected deterministically.
- Reject wrong game/team/kickoff, schedule changes, future receipts, posttipoff quotes, demo/unapproved providers, tampered source or payload hashes and unsafe artifact paths.
- Prove retention does not reset quote age or bypass live-price/anchor freshness.
- Recompute projection before sealing future retained-line snapshots; preserve the exact old paired projection for historical recovery.
- Outcome blindness: changing/removing final scores cannot change the selected recovery pair. Insufficient historical evidence remains unavailable.
- All board, grade, record and learner readers resolve the same forecast/line provenance; retrospective recoveries cannot silently enter prospective holdouts.
- Verify idempotent append-only grading/corrections, one count per game/market, and unchanged hashes of prior ledger rows and archived files.
- Verify staged learning compatibility, preserved burned dates/weeks and normal scheduling without starting a fit or provider demonstration during development.
- Preserve live service configuration, the 14 migration symlinks, existing dirty reports, PASS and RESEARCH_ONLY. No Windows cutover or final state archive.

## Review records

- Planner/code exploration and a safety review were recorded at planning time. The safety review's direction passes with the explicit context-transition and historical-recovery safeguards incorporated above.
- Two automated planning reviews supplied source/design advice with tools disabled. Both support prospective retention, strict original provenance and freshness separation. They oppose silently regrading the original historical live record; this plan keeps those selected forecasts/grades/headlines unchanged and limits recovery to separate research with explicit user approval and later grader review. The safety review found this tighter condition acceptable. The supplied excerpts did not cover every grading/caller path; full-source review and regression coverage are required before release confirmation. Suggestions for new retention TTL settings, broad fresh-only learning changes, carry-forward only after numerical inference, or flattening independently sourced groups into a single-source object are not adopted. Keep fresh projection/executable-price evidence distinguishable from retained comparison evidence, resolve their relationship before inference and sealing, and retain current freshness/eligibility rules. These planning opinions do not substitute for the later full implementation code/security review.

No application implementation, live source edit, provider demonstration, training, commit or service interruption has occurred during planning.
