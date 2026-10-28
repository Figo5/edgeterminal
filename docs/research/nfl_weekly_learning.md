# NFL weekly model updates

`edgeterminal.nfl_learning` is a serving facade and an explicit offline trainer.
The original `nfl_prop_model` and all existing forecast/selection bytes stay
unchanged. No provider request occurs in this adapter.

The candidate is a fixed StandardScaler/Ridge model (alpha 100), exported to
plain JSON coefficients in the existing opportunity feature units. Its
architecture, features, split lengths and probability smoothing are declared
before evaluation. It does not search parameters against a recent losing day.

The latest three completed regular-season weeks with fully resolved selected
games form the validation window. Two preceding whole weeks supply residual
calibration; the point estimate fits only earlier weeks. A minimum of 30
calibration player rows per market is required. Rounded empirical outcomes
retain explicit integer-line push mass, with one half-observation on each tail.
The limited calibration sample is one reason promotion also needs the much
stricter common registry gates.

Training and calibration read exact local source snapshots with receipt times
before their respective cutoffs. Historical file digests must have appeared in
an existing immutable forecast. Official result archives retain their original
database capture receipts. Current-season game coverage before each cutoff
must be complete. Newer downloaded results cannot retroactively become a
training source for an earlier holdout.

Validation uses only actually locked main-line selections and official final
grades, excluding pushes and voids. Both candidate and current baseline are
evaluated on the same quote, side and line, using each forecast's exact archived
prior-only feature inputs. The stored old model prediction is not substituted
for the current active model. A baseline already trained inside that validation
window must wait for a fresh untouched window.

`build_candidate(now=None)` returns a complete artifact/evaluation/source
proposal or a `waiting` reason. The registry independently rechecks sources,
quote identities, result revisions, disjoint game sets and chronology, and
recomputes paired MAE, Brier score and log loss. Its sample coverage,
per-market nonregression, minimum Brier improvement and game-group confidence
gates decide whether this candidate becomes active. A rejection leaves the
current model active; the candidate is not refitted on validation labels after
evaluation.

`project_current`, `model_info`, `load_artifact` and `seal_forward` preserve the
legacy call surface. New adaptive freezes additionally record the exact model
candidate, artifact digest, fitting week boundary, fitting cutoff and
calibration cutoff. Serving never fits a model or rewrites a saved result.
`nightly_diagnostics` reports current verified holdout coverage without provider
requests. Everything remains `PASS` and `RESEARCH_ONLY`.
