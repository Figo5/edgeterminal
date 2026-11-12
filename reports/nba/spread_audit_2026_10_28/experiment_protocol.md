# Offline spread-model experiment protocol

Locked before calculating any candidate metrics in this experiment.

The existing October 27 assessment rejected a ridge over recent scores and
points allowed. This experiment tests a different structural hypothesis:
opponent-adjusted team net strength, estimated from prior complete game
results with an unpenalized home intercept and ridge-penalized team effects.

Fixed grid: team-effect ridge penalties **5, 20, 80**, crossed with exponential
time half-lives **none, 30, 60 days**. A simpler comparison shrinks the existing
baseline margin toward its fixed 2.2-point home advantage at weights **0.5,
0.75, 1.0**. No other grid will be added after results are viewed. The rating
does not add the baseline rest adjustment; the unpenalized home intercept and
team effects comprise the entire alternative margin prediction.

Each family's settings are selected by January 2026 margin MAE, then RMSE,
then name for ties. The overall research candidate is likewise selected using
January only. Full-season ratings are a rolling estimate of current state:
each target's fit uses only complete games tipped at least four hours before
the target. Hyperparameters remain fixed after January selection. Baseline
eligibility requires ten earlier games per team, identically for all models.

February alone fits a prespecified logistic winner link with C=1 and an
intercept, separately for each selected point model and the baseline. It is
not used to change the selected point model or to select among probability
methods. Report both this calibrated link and the original normal link with
SD=11.6. March–April is the assessment set; the October 27 report already
examined its labels, so it is retrospective evidence and is not a pristine
independent holdout. Earlier assessment games may update the rolling ratings
for later targets, subject to the same four-hour lag, as baseline histories do.

No October 2026 outcome may fit or select settings or calibrate probabilities.
If a separate October 20–27 forward diagnostic is produced, settings must be
locked first and October 28 excluded. No historical sportsbook lines exist
for the historical assessment, so it measures margin errors and home-winner
Brier only, with no synthetic ATS claim. Any October diagnostic is explicitly
a retrospective reconstruction, not a saved prospective forecast.

Sources are verified archived local schedule response bytes only. Archive
receipt times are later than the historical games; this evaluates a
chronological reconstruction of historical final scores, not evidence that
those archived snapshots existed at historical forecast time. No current
roster, injury, or lineup features enter. All database access is read-only;
all output belongs under this report directory. No production deployment,
saved forecast change, grade change, or model-artifact change is authorized.
