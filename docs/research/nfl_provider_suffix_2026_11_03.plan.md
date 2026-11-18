# NFL provider name suffix — proposed repair

November 3, 2026. Standard scoped defect: external quote identity handling,
persistence compatibility and regression tests. Deployed base:
`f69d4f38b7770a1952eeec64f232b11d68dd7a88`.
Isolated worktree: `/Users/giofiore/edgeterminal-nfl-identity-suffix`.
No implementation or live identity changes have been made for this plan.

## Verified problem

The immutable SportsGameOdds alias `KENNETH_WALKER_III_1_NFL` stores
`Kenneth Walker`. The actual guarded update attempt archived batch
`b53fdbd2-c4ff-42e9-9527-1b1111ede7e9`: same provider ID, first name `Kenneth`,
last name `Walker`, full name `Kenneth Walker III`. The parser rejects the
suffix-only difference, blocking new NFL quote capture. Original IDs and names
must remain intact. No further demonstration provider request is needed.

A read-only audit of the full archived response also found `BRIAN_ROBINSON_1_NFL`
changing from `Brian Robinson` to `Brian Robinson Jr.`, with the same first/last
names and provider ID. No other stored-name differences occur in that response.
Both cases fall within the approved suffix-only repair.

## Ordered tasks

1. Reproduce the suffix-only capture failure against the current parser and
   persistence APIs in isolated temporary state.
2. Add a strict local compatibility check only for an already bound provider ID.
   Require the same complete core name; allow a recognized generational suffix
   to be present on only one rendering. Reject different explicit suffixes
   (Jr/Sr, II/III), abbreviations, changed core names and unrelated player IDs.
   Preserve current game/team/capture guards and require sufficient identity
   evidence. Do not reuse the broader model-pinned `_norm_name` helper.
3. Preserve immutable alias/player rows, original raw source bytes and every
   historical quote, forecast, selection, freeze and grade. Retain the incoming
   sourced full name on new quotes. Verify replay idempotency and the future
   capture-to-projection path without changing model math or grading rules.
4. Run offline positive, negative and immutability regressions and
   code/security review. Expected scope is `edgeterminal/nfl_props.py`, focused
   tests and research documentation. Recheck seals: this parser is outside the
   current model feature seals and NFL learned code catalogs.
5. Present the exact reviewed diff for separate commit/release confirmation.
   After confirmation, deploy only that repair and retry the actual missing-board
   capture through existing cadence and quota guards. Keep services live.

## Results and operational status

November 2 main results are complete: NBA props 39–26 with 10 voids, winners
11–4, spreads 10–5, totals 7–8. NFL Monday props 3–2; winner/spread won and
total lost. No completed-game NBA forecast or main NBA/NFL prop result remains
pending. All 513 decided NBA selections also passed a read-only comparison
against final player identities and game scores.

The NBA learner automatically recovered from its earlier error: the following
job completed successfully with `waiting`, retaining the model because more
separate calibration/selection/validation evidence is needed. Source and learner
services remain loaded. This repair does not alter learning settings or gates.

## Gates and exclusions

Plan confirmation is required before implementation code is written. Commit
separately requires confirmation.

No blanket staging, resets, quota changes, model tuning, forced grades,
historical backfills, Windows cutover, credential transfer, symlink
materialization or final state archive is part of this repair.
