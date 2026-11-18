# NBA spread live update plan — October 29, 2026

The user authorized implementation of the spread audit's improvements using the established implementation workflow. They explicitly retained a selection for every supported game and asked for automatic updates while their Mac sleeps, then clarified that phone access is preferable. That authorization covers the model, serving, tests, and a reviewable hosting package; it does not select or purchase a cloud host.

Size: large. The existing approved improvement plan is the planning baseline,
amended to retain full selection coverage. Pre-commit confirmation remains
separate and follows concrete review.

## Ordered work

1. Add future-only append-only game forecast revisions. Continue updating inputs before tipoff; deterministically use the latest saved pretipoff revision and its own ESPN market snapshot for the record. Preserve original forecasts, all prior final grades, prop locks, NFL freezes, and pinned prop model bytes. Count each game once.
2. Refresh team schedules and rosters more frequently toward tipoff. Derive rest from complete schedule evidence and display missing/stale coverage. Archive exact selected rows and source receipts. Keep every supported spread selection, with honest uncertainty instead of an edge-based coverage filter.
3. Separate the NBA game component from the pinned prop component. Collect multiple historical seasons and final-box possession evidence with bounded public-source work. Record availability, without inventing player-impact coefficients. Share feature transforms across fitting and serving.
4. Add bounded scheduled game-model diagnostics and retuning. Require separate chronological fits and actual saved spread-line validation before promotion. Material margin/probability improvement, uncertainty checks, and unchanged supported-game coverage are gates. A failed candidate remains inactive; previous versions and cutoffs remain reproducible.
5. Make freshness and game-model status visible in the responsive UI. Add mobile home-screen metadata, reconnect refresh, and explicit Mac-local versus hosted-worker status. Do not cache live forecasts as an offline substitute.
6. Package an always-on deployment with one authoritative persistent data volume, one shared worker lock, five-minute worker cadence, health checks, secret isolation, and verified backup/restore. Prepare private HTTPS phone access and an explicit Mac-to-host cutover; never run independent local/cloud writers. No paid resource or public deployment until a host/access/cost is resolved.
7. Run focused integrity and temporal regressions, full Python/JavaScript tests, code/security/ML review, local live checks, and mobile browser checks. Report activated behavior, waiting model gates, deployment status, and limits accurately.

## Prediction and data contract

Entity: one regular-season NBA game, using immutable ESPN event/team identities. Target: final home score minus final away score; spread side follows that projection versus the exact saved ESPN home line. Labels: verified final sources and matching selected revision hashes; no retrospective forecasts or line replacement.

Source receipts must precede forecast as-of, which must precede actual stored tipoff. Scored history respects the existing four-hour feature lag. Schedule/roster availability and model training cutoffs remain distinct timestamps. Revisions carry component version/fingerprint, exact row and source manifests, and the predefined scoring policy.

Initial serving improvement: freshness and schedule-rest correctness, with the retained baseline numeric algorithm where a stronger fitted artifact has not passed gates. New historical features are hypotheses, not grounds for automatic activation. October 28 outcomes are not a tuning target. No unvalidated injury point penalties, forced model promotion, fewer-picks policy, official picks, Odds API, bankroll, or stakes.

## Workstreams

- NBA serving: revision policy/storage/selection, evidence validation, focused immutability tests.
- NBA model with automated review: separate game component/data/learning modules and chronological gates/tests.
- Deployment with automated review: always-on runtime, secret/state migration safeguards, backup/restore, hosting/phone runbook and tests.
- Integration: input freshness and schedule context, worker/API/UI integration, phone metadata, cross-module regressions, review reconciliation and live verification.

## Rollout and rollback

Apply schema changes additively under the existing research lock. Enroll scheduled future games only; old completed games remain on their original policy. Keep existing sources and original predictions accessible. Roll back future serving through a new explicit policy/version rather than rewriting immutable evidence. Hold new learned artifacts inactive until gates pass. The hosted rollout starts only after tested backup/restore, clean committed code, private access configuration and explicit single-host cutover.
