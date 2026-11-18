# NFL provider suffix repair — release review

November 3, 2026. Base `f69d4f38b7770a1952eeec64f232b11d68dd7a88`.
Branch `fix/nfl-provider-suffix`, isolated worktree
`/Users/giofiore/edgeterminal-nfl-identity-suffix`. The plan was confirmed.
Implementation is complete; commit and local release await confirmation.

## Problem and resulting behavior

An actual guarded update request archived SportsGameOdds batch
`b53fdbd2-c4ff-42e9-9527-1b1111ede7e9`. Two previously bound provider IDs now
have added generational suffixes: Kenneth Walker becomes Kenneth Walker III,
and Brian Robinson becomes Brian Robinson Jr. The existing exact-name guard
aborts future NFL quote capture even though their IDs and first/last names agree.

The repair adds a local compatibility check only at that existing-name guard.
When a name differs, it requires an explicit matching provider player ID,
matching complete core names, substantive matching structured first/last fields,
and compatible recognized suffixes. Missing/present suffixes and equivalent
suffix punctuation are supported. Changed core names, abbreviations, conflicting
explicit suffixes, repeated suffixes, one-token cores, missing/contradictory
structured fields and suffix-only surnames fail closed. Different provider IDs
are never merged by name. Existing event, team, odd-ID and pregame guards remain.

Original alias/player rows retain their stored names and canonical IDs. Incoming
names remain on new quote rows and original HTTP bytes remain archived. Past
quotes, selections, freezes and grades are not rewritten. The broader model
feature normalizer is unchanged. No model parameter, cutoff, probability logic,
grading rule, quota, official-pick setting or acceptance gate changes.

## Validation and review

- **243 offline Python tests passed** across suffix cases, existing capture and
  source handling, frozen projection/roles, result ingestion/grading, historical
  rehearsal, challenger, the NFL learning registry, refresh, records
  and product views. No UI source changed; no new JavaScript test claim is made.
- TDD: six genuine capture-to-projection failures before the initial fix;
  two malformed-name admissions before complete-core/repeated-suffix guards;
  one suffix-only-surname admission before the structured-field guard.
  Focused final run: 92 passed. These counts overlap the broader run.
- Tests exercise paired quote projection through frozen fixture serving,
  unchanged canonical/GSIS identity and fingerprint, exact mocked HTTP bytes,
  populated historical grade/freeze preservation, and repeat-capture safety.
  No production model is fitted or activated.
- Helper executable-line coverage: **20/21 = 95.24%**. This is scoped line
  coverage, not branch or whole-module/repository coverage.
- A separate read-only check confirmed the production pin and all **21 pinned
  model feature hashes** are unchanged. This parser is outside the current
  model feature seals and learned NFL code catalogs; no seal is relaxed.
- Full real archived-response rehearsal against temporary state succeeds:
  **15 mapped games and 558 supported quote rows**, all original alias/player
  rows preserved, zero network requests and zero live database changes.
- Walker and Robinson have no eligible offers in that archived response.
  Their missing lines remain absent. The rehearsal does not claim five picks
  for every game or fabricate offers. Positive frozen-fixture cases separately
  prove incoming suffix names flow to quotes and projections when offers exist.
- Code/security review checked the exact source and test hashes, separately
  passed 172 offline tests and repeated the real-data rehearsal. No remaining
  critical/high/medium findings. `git diff --check` passes.

Receipts, RED/GREEN logs, the exact raw-response hash, model-seal check and
rehearsal results were recorded with the review evidence. Test runs used
temporary DB/cache state with networking disabled. Earlier rehearsal script argument/offer assumptions
were corrected and are not represented as product-regression RED evidence.

## Results and live state checked today

November 2 NBA: props **39–26 with 10 voids**, winners **11–4**, spreads **10–5**,
totals **7–8**. NFL Monday: props **3–2**; winner and spread correct, total lost.
Main NBA/NFL completed-game props are fully graded; no past NBA game forecast
was left pending. All 513 decided NBA selections matched final identities and
scores in a read-only source audit. NBA season props: 297–216 with 22 voids.

The original site and automatic services stay on `f69d4f3` during development.
Fresh source-worker cycles succeeded after the overnight gap. NBA learning
automatically recovered from its earlier error and completed with `waiting`:
more separate calibration/selection/validation evidence is required before an
update. Subsequent NBA/NFL diagnostics and NFL retuning checks also completed
successfully under the normal single-runner policy. The identity error still
blocks new NFL capture until this repair is
approved and released; a healthy worker tick is not used to conceal it.

After confirmation: commit only the four enumerated files, release that exact
commit locally, and retry actual missing-board capture through existing cooldown
and quota guards. Verify supported selections, source health and preservation of
historical evidence. Unsupported markets still wait. Do not create a final state
archive, transfer credentials, materialize migration symlinks or perform Windows
cutover. The Mac must remain awake for local automatic work.

## Release review

Proposed commit: `fix(nfl): accept verified provider name suffixes`.
Four files: parser, new focused test module, reviewed plan and this review.
The exact diff and hash manifest were recorded with the review evidence.

Commit requires confirmation after presenting the diff summary and proposed
messages. No staging, commit, installation or live identity edit has occurred
for this repair.
