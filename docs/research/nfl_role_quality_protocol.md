# NFL role and actual-line quality protocol

The `nfl-props-quality-v2` adapter proposes updates to one composite NFL prop
model. It continues to choose five distinct player/stat selections per game with
`top-five-confidence-v1`. The pinned opportunity model, original parameter pins,
saved forecasts, grades and immutable freezes are unchanged by this adapter.
A proposal is research, not evidence of better future results.

## Sourced opportunity features

`nfl_prop_roles.py` uses columns present in archived nflverse player-week CSVs:
attempts, carries, targets, passing/receiving air yards and reported air-yard
share. It adds last-game opportunity, three-game variation, player opportunity
share computed from the same team/game rows, share change, team opportunity,
current-team history counts and explicit air-yard/share observation counts.
Missing player rows do not become invented zero-opportunity games. A transfer
without observations on the target team gets an explicit zero coverage count.
There are no inferred injury replacements, snap counts, route counts or guessed
starters. All players' observations from a week enter history together after
that week's feature rows have been built. Target labels and later team totals
cannot enter that week's inputs.

The point scorer is a fixed standardized Ridge with alpha 100 and explicit JSON
coefficients. It retains all original features and adds the sourced role inputs.
No model-family or alpha search uses the evaluation results.

## Chronological boundaries

For a holdout starting in week W:

1. Fit coefficients on complete weeks before W-2 using a receipt-backed snapshot
   obtained before W-2 begins.
2. Fit residual uncertainty and actual-line over/under calibration on W-2.
   Actual quote and result receipts must exist before W-1 starts. Calibration is
   per market, shrunk toward identity, with both outcomes and minimum independent
   game/sample coverage. Push mass remains unchanged.
3. Choose changed markets only on W-1 actual main lines with results observed
   before W begins. Require at least 20 decided rows and 10 games per changed
   market, nonregressing point/error metrics and the shared probability margin.
   Fit, calibration, market-choice and outer game identities remain disjoint.
4. Evaluate the prechosen composite on the latest three completed whole weeks,
   using exact frozen prior-input byte hashes. The registry checks the independent
   untouched-game/date requirements and statistical improvement gates.

The builder preflights the registry's consumed-holdout ledger before reading
outer result labels. A consumed cohort waits for a wholly fresh window. A sparse
or unqualified inner cohort waits instead of tuning against outer outcomes.
Markets with insufficient role fit/residual coverage retain their exact incumbent
configuration, so a bye week with fewer than 30 qualifying QB observations does
not block supported receiver or rushing updates. Missing actual-line calibration
or inner quote cohorts return an explicit coverage wait before fitting or reading
outer labels. Existing exact JSON configurations remain untouched for retained adaptive markets.
Retained pinned baseline markets continue through their original nonlinear
scorer; the complete original projected row and its identity pins stay intact.

## Full selection-policy evidence

The common cohort is every pregame frozen offer for each historically selected
game, not just the original five selected props. Archived grades cover all
frozen quotes where official results are known, so the new model can be tested
when it would choose a different player, market, book or side.

The replay records the complete frozen quote-ID universe, pregame eligible IDs,
selection time and every pregame exclusion. It rechecks paired current main-line
sources, the original 24-hour freshness limit and timestamped availability
observations as of the original selection. It then applies the original ranker
to both scorers. A model may choose only an actually observed side quote.

Any unresolved or void **eligible** offer excludes the whole game for both models;
coverage reasons remain in the report. This is conservative and can delay a
proposal. No individual nonparticipant or losing offer is removed after the
result to improve a policy score.

The shared registry requires five distinct player/stat selections for each model
on the same games, sufficient independent games and dates, no selected-policy win
count decrease, no probability/log-loss regression, and the existing tighter
paired statistical gates. Decided-result evaluation uses conditional side
probability given no push for both models. Displayed live probabilities retain
unconditional over/under/push mass.

`verify_policy_universe(group, artifact=...)` independently rebuilds the exact
source universe, as-of eligibility, archived feature inputs, prior incumbent and
candidate scores, side choices and top-five membership during activation and
nightly verification. Each selected row also passes the registry's official
quote/grade/source check. Changing a supplied score or dropping an offer fails.
The source/feature/selector/calibrator code hashes accompany the artifact.

Role-only, calibration-only and combined outer metrics are descriptive ablations;
they never choose changed markets. The calibration-only ablation keeps original
membership and sides. Automatic activation still belongs to the common registry.
No page load fits a model or fetches a provider, and empty installations wait
without requiring local source files.

## Verification scope

Synthetic tests exercise source reconstruction, prior-only feature construction,
whole-week separation, consumed-cohort preflight, actual-quote deduplication,
new-player top-five replay, forged-policy rejection, conditional push-aware
scores and exact retained-baseline dispatch. They do not establish accuracy gains
on live games. No live candidate has been fitted, registered or activated as part
of this isolated implementation.
