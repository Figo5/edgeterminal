# NBA spread live release review — October 29, 2026

## Result

Every supported NBA spread keeps a selection. The worker now saves fresh, append-only game revisions before tipoff and grades the deterministically chosen latest pretipoff revision against its own saved ESPN line. Original forecasts, past results and prop locks remain intact. Live release verification saved **13 updated projections for 13 upcoming games**, with zero missing histories, zero revision errors and zero stale revisions at verification.

Schedules/rosters accelerate toward tipoff; final-hour schedule and game-price checks use a five-minute cadence. Rest comes from independently verified schedules rather than the last cached scored game. Missing/stale context remains visible while the prior supported forecast stays available. Publication checks the physical clock again after inference and after acquiring the database write lock, preventing delayed inference from backdating a post-tipoff forecast.

## Model and automatic learning

The active game component is `nba-game-live-1.2.0`, separate from the unchanged pinned prop component. Schedule-based rest uses the existing fixed adjustments. Availability is archived as context, without invented injury point values. Existing verified final boxes provide receipt-bound possession estimates; sparse historical coverage is disclosed rather than filled with guesses.

Sixty additional ESPN team-season archives were acquired and imported under the shared research lock, completing three historical seasons. Source hashes, owners, identities and actual receipt times are checked. Collection is bounded and does not use SportsGameOdds requests or Odds API.

The retrospective feature-model study improved held-year margin MAE from 11.7323 to 11.4946 points, approximately 2%. It is preliminary score-forecast evidence, not a spread hit-rate claim. The fitted candidate is **not active**: promotion requires at least 60 newly verified forecast/line/result pairs over seven dates, chronological fitting/calibration, material margin and cover-probability improvement, paired uncertainty checks and preserved coverage. The same failed proposal cannot be immediately retried without new games. Corrupt artifacts trigger a recorded fallback. See [model findings](../../reports/nba/spread_upgrade_2026_10_29/findings.md) for historical scope and limitations.

## Phone and always-on runtime

The responsive site includes home-screen metadata, uncached API reads and refresh on return/reconnection. A real 390-pixel browser check verified the board and About & data fit without horizontal scrolling; active game version, revision policy and local-worker dependency were visible, with no console errors. The local app was restarted to serve the reviewed code.

The always-on Docker package has one persistent authoritative state directory, shared five-minute worker/lock, separate secret files, private loopback-only web binding, clean committed-code guards, health checks and WAL-safe verified backup/restore. Linux worker status uses actual completed ticks rather than incorrectly requiring launchd. A stale/failed latest tick is never masked by an older successful one.

The user prefers free hosting. [The prepared Oracle guide](../../deploy/ORACLE_FREE.md) proposes one ARM A1 Free Tier VM, 1 OCPU/4 GB memory/50 GB boot disk, with private phone HTTPS access. The tested image is `linux/arm64`. Current Oracle limits and capacity/idle-reclamation caveats are linked in that guide. **No Oracle account, server, paid plan, public URL, remote upload, or local-to-cloud cutover has been created.** Account verification and available free capacity remain external prerequisites. The Mac remains the current authoritative host until a verified cutover.

## Review

- The implementation coordinated the plan, integration, source freshness, UI, tests and live verification.
- An automated read-only game-model architecture review covered model structure and evidence handling.
- Automated read-only infrastructure and code/security critiques covered deployment and implementation boundaries.
- Separate serving/model review checked cross-module source lineage, chronological boundaries, immutability and grading. Findings were reproduced and fixed: delayed physical writes, missing possession source IDs, mutable-cache availability, competitor identity conflicts, archived-input revalidation and cached training-source corruption.
- The deployment review resolved clean-code drift, source shadowing, secret separation, actual worker health, shutdown and state-timestamp preservation. See [deployment review](../../deploy/REVIEW.md).

## Verification

- Full Python suite: **1,082 passed**.
- JavaScript suites: **19 passed**.
- Deployment's focused tests: **21 passed**, included in the Python suite.
- Locked Linux image build, dependency consistency and Compose configuration passed. Isolated Docker smoke used dummy state and no network; web/worker health and blocked ingestion succeeded. It did not mount the live database or provider keys.
- Live bounded NBA refresh succeeded; all 13 upcoming games have revisions. Historical bootstrap is complete with no further requests needed; spread learning correctly waits for 60 new verified future-policy games.
- Integrity comparison preserved 242 past NBA forecast rows, 68 past game-grade rows at the snapshot boundary, all 68 prop locks and 340 locked items, plus seven protected source/freeze files. Immutable append-only result corrections may continue normally; no historical selection is reconstructed or replaced.
- Pinned `nba_model.py`, `nba_prop_model.py`, `pricing.py`, NBA analytics/manifest bytes and original NFL Week 3/Week 8 freeze bytes are unchanged. PASS / RESEARCH_ONLY remain in force; no additional automation or official picks were enabled.

Machine-readable release/immutability evidence is under `reports/nba/spread_upgrade_2026_10_29/`.

## Proposed commit

`feat(nba): refresh spread forecasts and prepare always-on mobile research`

Only reviewed implementation, tests, deployment files and concise review documents belong in the commit. Exclude provider keys, databases, raw historical archives, generated NFL reports, old pending research artifacts and unrelated output. The pre-commit gate was confirmed with: “Commit the reviewed changes”.
