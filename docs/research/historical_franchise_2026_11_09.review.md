# Historical NFL franchise identity — release follow-up

Status: isolated repair complete; code/security review findings and two automated code reviews reported no blocking issues. Additional commit and release await confirmation. Base/live release `e7ad7c54ca01fd6d1f5b615b70bbf8af3a1f1ae1`. The user approved and received the 51-file game-quality release. This follow-up repairs its historical-source validation requirement; it does not add a new model or change grading rules. The approved plan covers verified 2019-through-current history. Separate release confirmation remains required for the additional commit/release.

## Confirmed live outcome and defect

The application reloaded gracefully and is healthy. The first genuine scheduled cycle on the release succeeded in 38.28 seconds, including NFL top-five/challenger/game grading, NBA research, forecast updates and learning dispatch. The new NBA totals diagnostic completed automatically; NBA totals and NFL spread fitting jobs were queued normally. No service schedules were changed or learner interrupted.

Current NFL play-by-play was genuinely verified and published through Week 8. The subsequent weekly schedule preparation failed on `2019_01_DEN_OAK`: the current-name-only identity resolver does not accept the historic Oakland Raiders token. The immutable source job correctly reports the error while its last-good current PBP head remains available. Unsupported grades remain pending; the error is not suppressed.

The existing forward schedule is 516,321 bytes, SHA-256 `62e974e78fd0adf4ab40e8eae04d261f89ca60b03abb9ab7238d4380699c98f5`. Independent read-only inspection of both existing schedule copies found 2,143 selected regular-season rows across 2019–2026. The only unknown current-identity token is OAK in the 16 games from 2019; later rows use LV. Existing 2019 play-by-play validation also needs the same bounded identity treatment.

## Repair boundary

The fix adds a closed season-aware identity rule only inside the NFL source adapters. Raw provider game IDs and schedule rows accept OAK only for 2019; a raw 2019 LV identifier is refused. Canonical application IDs retain the stable franchise key `lv`. For play-by-play anchored to a genuine 2019 OAK raw game ID, the adapter supports either consistently raw OAK fields or consistently normalized LV fields across that game; the actual cache uses LV. Mixed representations, full-name/fuzzy substitutions, unknown teams and future OAK are refused. This provider-field distinction leaves the global team resolver unchanged.

Actual historical replay additionally exposed legitimate postseason END GAME rows above 600 seconds (2019 and 2021). The source verifier now recognizes a 900-second postseason overtime period while REG/PRE retain the 600-second bound. Nonzero regulation finals and out-of-bounds overtime clocks still fail. This validates source duration metadata; it does not change a game result, grading rule, model parameter or selected cohort. Preserve every raw source/cache byte, stored descriptor/pointer, snapshot, selection, freeze and grade. Current 2026 aggregate bytes must replay identically.

The shared global `nfl_identity.py`, provider normalizer, legacy research fitter and numerical model files remain unchanged, preserving their saved implementation fingerprints. Parameters, training cutoffs, quotas, publication/activation gates and grading rules remain unchanged. No source is replaced with synthetic data or silently skipped.

## Verification and release

Executed RED cases reproduce the original schedule refusal, the PBP normalization distinction and the postseason clock refusal. All **97 focused tests** pass. The complete offline regression suite passes **2,088 Python tests** in 90.56 seconds and all **30 JavaScript tests** pass. External sockets are denied, credentials removed, dotenv disabled and the default fixture database is owned by the worktree. Executable-line coverage is 88.81% for the source adapter and 83.70% for weekly preparation.

Actual read-only replay verifies all seven cached 2019–2025 PBP inputs (1,871 regular-season games in total) and all 2,143 selected schedule rows across 2019–2026. Every raw source and metadata hash remains unchanged. Both original 2026 aggregates reproduce byte-for-byte; the current published 121-game/32-team aggregate remains `102eb86f1e8ad159fe07e353d28238ef4d56f12415809aa5831e8c07676e679a`. The source ID, publication event and timestamp remain unchanged. No pointer is rewritten or proof silently resealed.

- Code/security review: no actionable findings. A separate 97-test replay and adversarial probes cover wrong-era, unknown, mixed-header/play/cross-row aliases and clock boundaries. Twelve protected implementation files match the base.
- Two automated code reviews reported CODE: PASS for the narrow four-file code/test repair. Target/interface hashes remained stable, with no tool use. The documented dual PBP representation is intentional; no broader release PASS is inferred from these scoped reviews.

The exact five-file diff and its adjacent release manifest bind the final review document, two adapters and their two regression files. The files are uncommitted and the running checkout remains at the approved e7ad7c5 release. No provider demonstrations, actual model fitting, service actions or live source/state edits were performed for this repair. The normal deployed learner continues separately; it must not be interrupted.

Before any confirmed follow-up release, recheck the exact live base and reviewed source hashes, managed source/learner inactivity, old v1 totals-candidate compatibility, and the independent per-season source-child lease/job boundary. Source children run outside the research lock; the managed workers being idle is insufficient. Never terminate a genuine child or learner. The deployed source head and saved history remain untouched; the normal worker should retry through its existing cadence.

Proposed follow-up commit: `fix(nfl): verify historical source identities and clocks`.

Release confirmation is required before commit. The additional fix will be
presented with its exact reviewed diff before requesting that confirmation.
