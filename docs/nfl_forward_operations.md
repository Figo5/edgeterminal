# NFL forward research operations

The live product remains `RESEARCH_ONLY` and PASS. The linear Week 3 freeze is
`reports/nfl_model_v2/forward_2026_week03.json`. Its predictions and original
market file are never regenerated. The local SQLite database stores separate,
append-only market provenance, capture attempts, model version links, and grade
revisions. These research records are not consumer betting signals.

## Local jobs and dependencies

Run commands from `/Users/giofiore/edgeterminal` with `.venv/bin/python`.
The Codex desktop scheduler, local Mac, and writable `data/edgeterminal.db`
must be available. ESPN schedule and odds retrieval also require network access.
The web server is not needed for these jobs. `ODDS_PROVIDER=espn` is configured;
the near-kickoff job uses the ESPN adapter directly and never calls Odds API.

| Job | Active local heartbeat | Command | Work |
| --- | --- | --- | --- |
| Daily research | `edgeterminal-nfl-forward-research`, 10:00 America/New_York daily | `.venv/bin/python -m edgeterminal.nfl_forward_weekly` | Refresh schedule and prior data, create a new week's freeze only before first kickoff, then grade due games and write the separate forward report. Existing freezes are left intact. |
| Near-kickoff market | `edgeterminal-near-kickoff-market-capture`, every 15 minutes (currently at :01, :16, :31, :46) | `.venv/bin/python -m edgeterminal.nfl_market_capture` | Fetch ESPN odds only if a canonical, mapped, active NFL game starts within the next 15 minutes. Accepted quotes and an outcome per attempted game are appended. |
| Postgame grading | Included in the daily job; may also be run manually | `.venv/bin/python -m edgeterminal.nfl_forward_grading sync-all --season 2026` | Final ESPN scores are eligible only at least four hours after the actual provider kickoff. Corrections append revisions and audit rows. |

The near-kickoff job reads the latest locally refreshed schedule; it does not
refresh ESPN events on every 15-minute run. If a kickoff changes after the
daily refresh, its capture window may be missed. A move of over four hours
from the frozen kickoff needs manual identity review before grading. A smaller
move still waits four hours from the updated kickoff. Postponed, canceled, and
suspended games do not grade. A tie has no binary Brier contribution.

## Missing observations

If the Mac sleeps or the scheduler does not run, no attempt row is fabricated.
After kickoff, capture health shows a missed window if no valid timestamped
near-close quote was stored. If the job runs while offline or ESPN times out,
an append-only `error` attempt names the exception; no price is invented. If
ESPN replies without a valid quote, the attempt is `no_quote`. A later quote
cannot be backdated into the missed window. Repeated successful retrievals are
separate observations with separate local quote IDs; the original frozen source
quote ID remains linked to its immutable snapshot.

Generate the research-only health and integrity artifacts:

```sh
.venv/bin/python -m edgeterminal.nfl_capture_health reports/nfl_model_v2/forward_2026_week03.json --output reports/nfl_model_v2/capture_health_2026_week03.json
.venv/bin/python -m edgeterminal.nfl_forward_integrity reports/nfl_model_v2/forward_2026_week03.json --output reports/nfl_model_v2/week03_integrity_2026.json
```

Capture health reports expected windows, attempted and successful captures,
misses, latest observation ages, and source per game. A quote is called an
observed near-close quote only when it was actually fetched within 15 minutes
before kickoff. This does not establish that a wager was executable at that
price. The integrity audit reads the ESPN schedule endpoint, never Odds API.

## Grading and version history

`nfl_research_grades` stores each completed score as revision 1 or higher;
`nfl_research_grade_audit` links corrections to prior revisions. Duplicate
grades append nothing. Frozen market sidecars and source attestations are
created only when the freeze is first registered. Missing prices remain
missing; they cannot be added retroactively after registration. SQLite triggers
reject updates and deletes of these research records. The original JSON freeze
is also checked against its Git artifact SHA before an integrity report passes.

`nfl_research_model_versions` stores a model name, version, code commit,
training cutoff, feature hash, model/config hash, and first eligible week.
`nfl_research_model_freeze_links` binds each immutable freeze SHA to a version.
Status and last eligible week are recorded as append-only
`nfl_research_model_version_events`. Register future candidates with distinct
version names; preserve the Week 3 link to linear research version `linear`.
No version is promoted by this registry.

`reports/nfl_model_v2/forward_performance_2026.json` is prospective only.
Retrospective development results remain separate. Neither report authorizes
official picks.

## Reproducibility bundles for Week 4 onward

Every new weekly freeze first copies `games.csv.gz` and all 2019-through-current
season play-by-play archives into a private staging directory. Training reads
these copies, never the mutable cache paths. The final bundle lives at
`data/nflverse/forward_bundles/<bundle_id>/`; this directory is excluded from
Git because the historical archives are roughly 127 MB per weekly bundle.
Retain and back up the entire directory together with the freeze JSON and
SQLite attestation. A manifest alone cannot reproduce a model if the copied
data or serialized estimator is lost.

The bundle format is version 1:

| Artifact | Content |
| --- | --- |
| `inputs/*.csv.gz` | Exact raw schedule and play-by-play bytes used for training. |
| `training_rows.pkl` | Exact derived pre-cutoff training rows. |
| `estimators.joblib` | Fitted margin, total, and win-probability estimators; its SHA-256 is the future freeze's `model_hash`. Load only from trusted local storage. |
| `hyperparameters.json` | Each fitted estimator's full `get_params(deep=True)` configuration. |
| `feature_config.json` | Ordered feature names, groups, numeric type, and feature schema version. |
| `environment.json` | Python implementation/version, executable, platform, and installed package names/versions. |
| `code/` | Python source and requirements files as they existed at sealing. A full Git commit SHA is also recorded; relevant uncommitted code refuses a new freeze. |
| `market.json` | Frozen quote fields plus the exact selected odds rows or immutable source snapshot payload. Missing quotes are recorded as missing. |
| `predictions.json` | Byte-identical prediction artifact linked to the published weekly freeze. |
| `manifest.json`, `seal.json` | Per-artifact sizes/SHA-256, cutoff, training count, code commit, model/feature hashes, freeze SHA, and manifest SHA. |

The runner writes and hashes every artifact, checks the sealing time against
the first kickoff, moves the completed directory into place, makes it read
only, and only then publishes a separately copied, byte-identical prediction
file at the public freeze path without replacing any existing file.
If copying, fitting, serialization, hashing, or pre-kickoff sealing fails, no
freeze file is published. A sealed but unpublished orphan can remain after a
late process failure; it is not a valid freeze. Registration of a Week 4 or
later freeze verifies every artifact and writes an immutable bundle attestation
in SQLite. A missing or changed bundle is a hard registration error.

Months later, verify the retained bundle with:

```sh
.venv/bin/python -m edgeterminal.nfl_repro_bundle reports/nfl_model_v2/forward_2026_week04.json
```

**Week 3 is a permanent legacy limitation.** Its original `games.csv.gz`
bytes are absent from the local cache, which now has a different SHA-256. Do
not synthesize those bytes, add a retrospective bundle, or rewrite its freeze,
hash, market records, or audit trail. The Week 3 predictions and source quote
snapshots remain verifiable, but an exact input-to-fit replay is unavailable.
