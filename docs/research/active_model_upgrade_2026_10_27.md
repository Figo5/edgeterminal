# Active prop model upgrade — 2026-10-27

The user approved improving the active NBA and NFL models directly, without
creating another challenger. This adapts the approved improvement plan to one
active prop model per sport. No commit was requested.

## Work plan

1. Audit historical source coverage and the current serving/version contracts.
2. Fit and validate player opportunity and outcome distributions using data
   before each existing training cutoff. Keep all forward outcomes out of model
   fitting, parameter selection and acceptance decisions.
3. Activate versioned models for new pregame prop captures; preserve all already
   saved forecasts, five-prop locks, grades and original research evidence.
4. Retire automatic creation of comparison forecasts, while continuing to grade
   those already saved. Refresh current model evidence in About & data.
5. Independently review temporal integrity, artifact loading and serving; test
   end-to-end integration and verify that immutable records are unchanged.

## Scope and validation limits

The changes target player props. The existing NBA game-score alternative was
tested and rejected because it worsened validation error; see
[game evaluation](game_model_upgrade_evaluation_2026_10_27.md). NFL public game
forecasts use a separate existing calculation.

The NBA parameter cutoff remains 2026-10-01. The NFL fitting cutoff remains
before 2026 Week 3. Current prior-game observations may update inference inputs;
they do not refit parameters. The five-supported-markets policy, provider quotas,
automatic five-minute final grading, PASS and RESEARCH_ONLY remain in force.

Historical acceptance comparisons help select the deployment. They are not an
untouched prospective test. Synthetic thresholds measure distribution quality
without establishing accuracy against actual sportsbook lines. The future
record must retain its saved model identities to evaluate the upgrade honestly.

## Implemented result

- NBA active version: `nba-research-2.0.1`. Minutes are forecast from prior
  playing-time and role features, then combined with pooled per-minute
  production. Distribution methods are accepted per market using historical
  validation. Rebounds use empirical residual calibration; the other markets
  retain validated parametric distributions. Impossible negative NBA outcomes
  are censored to zero.
- NFL active version: `nfl-opportunity-empirical-2.0.0`. Additional opportunity,
  efficiency and role-change features improve historical MAE by 1.21–5.24%
  across five markets. Empirical distributions support asymmetric uncertainty
  and explicit push probability. See [NFL validation](nfl_active_prop_model_v2.md).
- Both fitted artifacts are fixed and integrity checked. Serving cannot train
  them or silently accept altered feature calculations. Existing source and
  provider guards remain in place.
- About & data identifies the active version and uses its frozen validation
  report. Per-prediction disclosures follow the saved probability method.
- Automatic future challenger capture has stopped. Existing comparison
  forecasts remain available and continue receiving final grades.

NBA March–April retrospective validation includes 6,094 observations per market.
Mean absolute error improves in all five markets: points 4.933→4.842, rebounds
1.959→1.938, assists 1.455→1.433, threes 0.935→0.932, and PRA 6.445→6.294.
Minutes MAE improves from 5.336 to 5.032. The fitted source sample covers players
on current rosters and has survivorship bias. Its full historical distribution
scores are saved in `reports/nba/model_upgrade_2026-10-27.json`.

Validation: 911 Python tests and 16 JavaScript tests passed. Review covers
source timing, training cutoffs, artifact/code integrity, probability
support, freeze metadata, unchanged saved forecasts and refresh integration.


## Activation

The built-in worker registered an intermediate NBA 2.0.0 while offline work was
underway. It saved no forecasts. The immutable registry correctly refused to
redefine it. Final NBA 2.0.1 uses a distinct version; the intermediate registry
entry remains intact. The final activation used the shared refresh lock, and
About & data now publishes the version-matched frozen report.


## Final verification and self-evaluation

The app was restarted, both active model identities were read back from its
HTTP API, and NBA's evaluation identity matches its active fingerprint.
The guarded built-in worker completed successfully. Product status is healthy
with no warnings; the LaunchAgent remains loaded with a 300-second interval.

The integrity comparison covered 579,796 pre-existing protected-table rows and
77 original files. All pre-existing forecasts, selection locks, grades and
research freezes remain unchanged. Four mutable NBA schedule/metadata rows
refreshed normally, and NBA serving source changed as authorized; these are
excluded from the immutable-evidence claim. No other changes were found.

| Axis | Score | Evidence and remaining limit |
| --- | --- | --- |
| Accuracy | 4/5 | Historical MAE and distribution scores improved, with retrospective acceptance and calibration-transfer limits explicitly disclosed. Future hit-rate improvement remains unproven. |
| Completeness | 4/5 | Both active prop models, versioning, automatic refresh, status and historical preservation are handled. Game mathematics remain unchanged after the tested NBA alternative regressed. |
| Clarity | 4/5 | About & data exposes the active version and saved-row probability methods. Detailed historical methodology requires reading the linked reports. |
| Actionability | 4/5 | Models are activated and the worker verified healthy. Fitting remains an explicit offline operation to preserve reproducibility. |
| Conciseness | 4/5 | User-facing changes stay compact; detailed reproducibility evidence is kept in research documents. |

Overall: 4.0/5. Highest-impact future improvements require more historical
seasons, timestamped rotation/lineup data, and actual historical sportsbook
lines. They are recorded as limits, not claimed as delivered capabilities.
Would the user agree: the active models are improved directly and remain
self-updating, while results already recorded retain their original meaning.
