# Model quality improvements — approved four-track scope

The owner approved the four-track proposal on October 30: improve player-role
forecasts, calibrate prop markets, extend NBA spread validation, and make
automatic updates selective. Implementation is isolated from the live checkout.
The orchestration plan gate is satisfied by that explicit approval; the final
scoped commit still requires separate confirmation.

## Iteration compact

- Goal: improve forward forecast accuracy and reliability without reducing the
  five supported prop selections per game or exposing a second live model.
- Decision owner: the owner approves scope and commits; deterministic validation
  gates decide whether a newly fitted version may replace an incumbent.
- Baseline: pinned NBA/NFL prop artifacts and the active NBA rest-aware game
  component. Saved selections, lines, grades and old model parameters are fixed.
- Primary metrics: paired actual-line Brier/log loss and point MAE; complete
  five-selection policy coverage/outcomes for any change to ranking or sides.
- Guardrails: whole-game/week chronology, source receipts and hashes, finite
  probabilities, integer push mass, market-level nonregression, sample minimums,
  unchanged-market exact parity, and verified rollback.
- Unacceptable mistakes: future-label leakage, using synthetic lines as actual
  sportsbook evidence, selecting winning markets from the outer holdout,
  fabricating lineup effects or past selections, or bypassing provider budgets.
- Constraints: one live model, existing built-in worker, no new paid services,
  Odds API, public challenger, official picks, stakes, migration or cutover.

## Ordered implementation slices

1. Add shared, deterministic per-market probability calibration and inner-only
   market selection. Preserve push probability; sparse evidence uses identity.
   Markets are chosen before the untouched outer evaluation.
2. Add NBA minutes-derived role/volatility features and predictive exposure
   uncertainty. Add NFL prior-week targets/carries/team shares/air-yards features
   from archived CSVs. No starter, routes or injury-effect claims without data.
3. Freeze complete future NBA capture contexts and available quote observations
   during ordinary guarded requests. Replay candidate and incumbent selections
   on the same pregame offer universe, with sourced finals. Never insert missing
   past live quotes/locks. Fixed-membership calibration can be tested separately;
   a new ranking must wait for full-policy evidence.
4. Extend NBA spreads with a trainable market anchor, a market-only benchmark,
   and separate chronological fit/development/calibration/outer periods. Preserve
   the existing 60-game/seven-date promotion gate. A positive convex market blend
   alone preserves the ATS side; improved point error is not a spread win claim.
5. Extend the append-only registry to accept selective updates only with exact
   unchanged-market parity, source-verified evidence and sufficient changed-market
   coverage. Retain global sample/uncertainty gates and reject unsupported ranking
   changes. Existing nightly diagnostics, weekly fitting and rollback stay in use.
6. Run regression, chronology, poisoning, push/coverage and train/serve parity
   tests; obtain independent ML/security reviews and reproducible evaluation.
   Ask for the scoped commit confirmation. Passing tests do not imply an artifact
   earned promotion; inadequate data remains an explicit waiting state.

## Data and evaluation boundaries

NBA currently has archived prior-minute/stat histories and roster context; it
does not have a validated historical starting-lineup impact dataset. NFL archives
contain attempts, targets, carries, shares and air yards, not snaps or routes.
The richer spread study has only three qualifying forward-policy finals so far.
These limitations are surfaced, never filled with guessed values.

Historical fit, inner method/market choice, calibration and outer validation
must have disjoint whole games/weeks. A label's actual availability must precede
the next stage's first prediction, including games on the same calendar day.
Retained markets dispatch to the original frozen scorer or exact prior adaptive
configuration; a nonlinear incumbent is never silently replaced by a Ridge fit.

Reference: scikit-learn's probability calibration guidance requires independent
calibration evidence and distinguishes reliability from other probability-score
components: https://scikit-learn.org/stable/modules/calibration.html.

## Orchestration

Codex coordinates registry/calibration integration. Separate NBA-prop, NFL-prop
and NBA-spread agents own disjoint implementation slices. Claude and OpenCode Go
are invoked for read-only architecture critiques; their actual outcomes are
recorded rather than assumed. Live services and baseline model files stay intact
while implementation is reviewed in the isolated worktree.
