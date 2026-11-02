# Full season record consolidation — November 2, 2026

## Approved scope

The user approved the plan in this chat on November 2. Work is isolated in
`edgeterminal-record-consolidation`, based on the approved portability commit
`9441e1781a554c22d311cf7bee8b3f7483eab590`. The running Mac checkout remains at
`8d1cc4d245acf1f1e35c595d158050152658a254`.

1. Present one season overview per sport across the saved live model versions.
2. Show actual model-version records and their recorded date ranges.
3. Move the overlapping NFL comparison and linear research records into an
   expandable history section. They are alternative forecasts, not extra wins
   or losses in the primary record.
4. Preserve historical rankings/replays as labeled provenance; never attribute
   a reconstructed forecast to today's active model.
5. Preserve weekly/daily history, pending results, and all immutable records.
6. Test reconciliation, attribution, pregame selection, disclosure and escaping;
   obtain independent code/security review before requesting commit approval.

No training, model parameter changes, new selection policy, provider quota
changes or grading rule changes are included. No service deployment, Windows
package/image update, credentials transfer, symlink conversion, final state
archive or migration cutover is included.

## Record interpretation

The NFL headline already spans the saved live model versions and supported
historical rankings/replays. Locked selections take precedence over historical
records for the same game. The old challenger and linear research predictions
overlap these games and must not be added to the headline totals.

NBA totals already span immutable prop locks and the selected pretipoff game
forecasts across versions. Version breakdowns describe their actual saved
models, not the version currently serving the application. NBA game component
versions must remain distinguishable from the enclosing prop/model version.

Win rates use wins divided by wins plus losses. Pushes, voids, pending,
unavailable lines and passes are not losses. Version cohorts cover different
dates and game mixes; their win rates are descriptive, not a controlled
comparison proving that an upgrade caused an improvement. Prop outcomes can
also be correlated within a player or game.

## Live audit

Read-only snapshot: **November 2, 2026, 12:09:51 PM EST**. The latest completed
genuine worker cycle finished at **12:07:56 PM**, successfully. Both source and
grading checks succeeded. NBA schedule coverage was checked at **12:00:40 PM**;
tonight's NFL game quotes were checked at **12:00:34 PM**. NFL Week 8 correctly
remains active through tonight's CHI–SEA game. Today's 15 NBA games have 75
locked player props.

| Statistical record | W–L–push | Win rate | Pending / other exclusions |
|---|---:|---:|---|
| NFL live props, all saved versions | 157–122 | 56.3% | 5 pending, 1 void |
| NFL current v2 live props | 32–23 | 58.2% | 5 pending |
| NFL headline props, including historical cohorts | 212–172 | 55.2% | 5 pending, 3 void |
| NFL winners | 60–44–1 | 57.7% | 1 pending |
| NFL spreads | 46–42–2 | 52.3% | 1 pending, 15 unavailable |
| NFL totals | 45–44 | 50.6% | 1 pending, 1 pass, 15 unavailable |
| NBA live props, all saved versions | 258–190 | 57.6% | 79 pending, 8 void |
| NBA current v2 props | 112–79 | 58.6% | 79 pending, 5 void |
| NBA winners | 58–34 | 63.0% | 15 pending |
| NBA spreads | 41–51 | 44.6% | 15 pending |
| NBA totals | 46–46 | 50.0% | 15 pending |

The NFL historical contribution is 57 archived-ranking selections (28–29)
plus 50 replay selections (27–21, two voids). These are not newly issued live
picks. The archived challenger is 81–60 with five voids and no pending results;
its overlapping alternative forecasts do not belong in the primary totals.

On **November 1**, NBA props went **10–8**, with two voids; winners **2–2**,
spreads **0–4**, totals **2–2**. NFL Sunday props went **36–24**, winners
**9–3**, spreads **4–8**, totals **5–6**, with one pass.

All five pending NFL props belong to tonight's CHI–SEA. NBA has 75 legitimately
pending props for today and four older unsupported props:

| Saved date | Recorded matchup | Saved player / market | Existing evidence |
|---|---|---|---|
| October 29 | MEM at POR | Robert Williams III, rebounds over 6.5 | `missing_player_stat` |
| October 30 | NYK at PHI | Jaylen Brown, PRA over 28.5 | `missing_player_stat` |
| October 30 | NYK at PHI | Jaylen Brown, points over 19.5 | `missing_player_stat` |
| October 30 | LAL at GSW | Ziaire Williams, points over 8.5 | `missing_player_stat` |

The Brown and Williams rows in particular require source/player/game identity
review. A complete game box score does not establish the missing player's
participation or a zero result. No player was substituted, no historical
selection was rewritten, and no unsupported result was forced or voided.

The audit independently recalculated **92 NBA game outcomes** using selected
pregame forecasts and saved lines, and **448 numerical prop outcomes**: zero
calculation mismatches. This verifies settlement arithmetic, not the accuracy
of every historical identity or the correctness of unverified missing stats.

Private reproducible evidence is under `output/record-audit-2026-11-02/`:
`audit_readonly.py`, `AUDIT.json`, original view fixtures and candidate parity
fixtures. The auditing process used a consistent read transaction, SQLite
`query_only`, and blocked networking. It did not run refresh, grade, fit or
provider functions.

## Improvements supported by the evidence

1. **NBA spread accuracy needs priority.** The current live game component is
   8–16 on spreads, versus 41–51 across the full record. Its dates and sample
   differ from the older component. The adaptive learner has not activated a
   candidate; its latest recorded gate has 24 of 60 fresh validation games.
   Assess margin error, home/away bias, saved-line timing and market comparison
   on untouched games before promoting a replacement. These are proposed
   follow-up analyses, not parameter changes in this release.
2. **Probability calibration remains weak.** NBA v2's 191 decided props average
   76.5% confidence but win 58.6%; Brier score is 0.2716. NFL historical-inclusive
   confidence bands also overstate observed wins. Calibrate with held-out,
   game-grouped, market-specific evidence and keep original saved probabilities
   fixed. A winning percentage alone does not establish profitability.
3. **Resolve historical source identities explicitly.** Investigate the four
   unsupported NBA props against authoritative participation evidence; retain
   pending outcomes until a supported, append-only resolution exists. Do not
   manufacture grades to remove pending rows.
4. **Keep learning from delaying ordinary refresh.** Morning diagnostics and
   retuning recorded approximately **1 hour 57 minutes of wall time**
   (06:14:44–08:12:14 EST). Learning currently runs synchronously inside the
   shared refresh lock. These timestamps do not prove uninterrupted CPU time,
   but they require investigation before Windows cutover: its controller has a
   900-second timeout. Consider bounded fitting or an isolated job with verified
   input/output handoff, while preserving promotion and single-writer guards.

The automatic learning system retained the current models when prior proposals
failed probability/market validation. New quality proposals are waiting for
fresh evidence. No automatic process can guarantee a continuously rising win
rate, and this change does not tune on the reported outcomes.

## Implementation and validation

Additive `model_versions` summaries expose live prop and game records, source
model/component identities, sample counts and recorded date ranges. NFL props
use the saved locked row's model, NBA props use each item's immutable forecast,
and games use the already selected pregame forecast. NFL legacy market versions
remain attributable separately rather than being copied from the first market.
Unknown identities/dates remain unknown. Historical reconstructions retain their
existing evidence labels. No primary selection/grade calculation changed.

The NFL archived comparison and linear research sections now live inside a
closed native disclosure. Both sports retain one season overview, then live
version records and existing weekly/daily history. Dynamic version/component
labels are HTML escaped.

Browser validation also exposed the NBA summary cards' missing existing metric
class, which made their labels and scores run together on a phone-width view.
The renderer now uses the established metric markup and CSS, with no new
stylesheet or dependency. The date column says “Saved record dates” because
prop cohorts use their lock time and game cohorts use forecast save time.

| Verification | Actual result |
|---|---|
| Backend RED, before production edits | 13 intended failures for missing version summaries |
| Final backend suite | 115 offline tests passed, including 17 new cases |
| Frontend RED, before production edits | 15 passed, 6 intended failures |
| Frontend mobile-metric / date-label RED | Intended markup and wording failures before the follow-up fix |
| Final JavaScript suite | 23 passed |
| Final focused independent Python review | 140 passed |
| Independent final new/existing record cases | 30 passed |
| Candidate against actual live stored records | Headline, weekly/daily totals unchanged; every version summary reconciles; zero DB changes |
| New aggregation helper executable-line coverage | NFL 26/26, NBA 29/29 (100%; not branch or repository coverage) |
| Shared NBA renderer line coverage | 93.46%; new version-renderer lines covered; not branch/repository coverage |
| Stored-data browser previews | Desktop and 390px phone viewport; native history disclosure; no page overflow or invalid values |

Independent native code/security review found no blockers. It checked saved
version attribution, missing-line exclusion, no duplicate counting, preserved
evidence, hosted-access compatibility, HTML escaping and absence of source or
state mutations. JavaScript and browser results and the final reviewed hashes
are captured in the accompanying review artifacts before commit confirmation.

Tests use temporary databases and blocked networking. TDD checkpoint commits
were deferred to respect the orchestration requirement to obtain explicit
pre-commit approval. No full-repository test result is claimed.

The isolated branch includes the already approved portability fix; neither that
fix nor this feature has been installed into the running Mac checkout. The
existing Windows package/image remain at the earlier `8d1cc4d` release. No live
service was stopped or reloaded. Windows package/image work and migration
cutover remain separate.
