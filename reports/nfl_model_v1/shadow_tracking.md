# Forward NFL shadow tracking

The local app freezes one projection per canonical NFL game on the **first**
app start, weekly board view, or refresh with an eligible prediction within
seven days before kickoff. This may be
several days before the game. The freeze records
model version, snapshot ID, score/margin/total/win probability, PASS
designation, kickoff, and a valid home spread and Over total quote captured
within the freshness window. It refuses demo data, terminal games, changed game
identity, and any prediction created after kickoff. Missing quotes remain
missing in the immutable record; they are not filled using later prices.

On app startup and each manual data refresh, the result sync requests the
historical ESPN week for games at least four hours past kickoff. It requires a
final status, exact canonical matchup, kickoff within four hours of the frozen
time, and nonnegative integer scores. It grades projected team-score error,
margin and total error, binary win probability, straight-up side, model-side
ATS and model-side total. A tie is excluded from Brier scoring. It uses the
same book's last validated quote captured within six hours before kickoff,
when present, for line-movement
diagnostics. These are hypothetical model-side points, not return estimates.

The app's SQLite connection protects `nfl_shadow_entries`,
`nfl_shadow_grades`, and `nfl_shadow_audit` against updates, deletes and
replacements. A separate raw SQLite connection with different pragma settings
can bypass this protection; the local database is not a tamper-proof external
ledger. A corrected ESPN final score appends a new grade
revision and an explicit audit row with both scores, source and time. The
performance API selects the latest revision while retaining the prior ones.
The Model record page reports frozen and graded counts, all projections,
official/lean counts (both zero under the PASS policy), weekly breakdowns,
and observed line movement separately from the historical replays.

The sync runs when the app starts or a user refreshes data. A local app that is
closed through kickoff cannot freeze a projection for that game; a closed app
can grade already-frozen games when it next starts. ESPN's result endpoint is
not guaranteed to be available or final immediately after four hours, so
ungraded games remain pending and are retried. Games still ungraded 24 hours
after their original kickoff are flagged for review, including cancellations
and material reschedules. No forward game has completed
in this 2026-09-23 acceptance check, so live grading remains unobserved.
