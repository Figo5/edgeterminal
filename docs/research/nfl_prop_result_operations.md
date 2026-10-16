# NFL player-prop result operations

The Week 3 prop freeze remains immutable. This command only ingests result sources, appends research grades, and updates the separate forward report after a grade changes. It does not poll prop prices or issue picks.

## Commands

From `~/edgeterminal`:

```sh
.venv/bin/python -m edgeterminal.nfl_prop_result_ingest status
.venv/bin/python -m edgeterminal.nfl_prop_result_ingest sync
```

`status` is read-only and never calls a provider. `sync` makes no request before a frozen game's kickoff. Once a game begins, it retrieves that week's ESPN scoreboard once. Only after an ESPN final and four hours from the reported kickoff does it retrieve the nflverse weekly player-stat CSV. A missing or incomplete weekly game remains pending. No scheduler or recurring prop-price capture is added by this change.

For a previously downloaded result source, use exact bytes and identify where they came from:

```sh
.venv/bin/python -m edgeterminal.nfl_prop_result_ingest sync \
  --scoreboard-file /absolute/path/scoreboard.json \
  --scoreboard-source-url 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=2026&seasontype=2&week=3&limit=100' \
  --stats-file /absolute/path/stats_player_week_2026.csv \
  --stats-source-url 'https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv'
```

Supplied bytes without their source URL fail closed. The fetched or supplied bytes are stored by SHA-256 in `nfl_prop_grade_source_artifacts` before a grade is attempted. `nfl_prop_result_ingestions` records the exact source hash, game, source URL, retrieval time, and validation metadata. Both tables reject updates and deletes. A game must match its existing ESPN event mapping and home/away identities. Player-stat rows must match season, week, game and both teams, contain required columns, have unique GSIS IDs, and pass a minimum of 50 player rows with at least 20 for each team. This conservative check catches an obviously incomplete release; it cannot prove the publisher will never correct a statistic.

ESPN's [scoreboard API](https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard) supplies the operational final score and kickoff. The [nflverse player-stat release](https://github.com/nflverse/nflverse-data/releases) supplies GSIS-keyed player totals; nflverse describes these as calculated player statistics. These are timestamped research sources, not a bookmaker settlement record or an NFL-signed final result. For disputed results or participation, use the NFL [Game Book PDF](https://support.nfl.com/hc/en-us/articles/35869678028180-Game-Books) and a reviewed attestation. Official Game Books are available from NFL game centers. The command retains the source PDF bytes and the exact attestation bytes separately.

## Participation exceptions

A nonzero recorded game action supports participation. A missing row, all-zero row, or zero target stat without another recorded action remains pending. To resolve it, save the applicable NFL Game Book PDF locally and provide a JSON attestation. Example:

```json
{
  "canonical_game_id": "nfl-2026-reg-03-atl-gb",
  "reviewer": "name or initials",
  "reviewed_at": 1790380000,
  "source_url": "https://www.nfl.com/games/.../gamebook",
  "source_file": "/absolute/path/gamebook.pdf",
  "source_sha256": "64 lowercase hexadecimal characters",
  "players": [
    {"gsis_player_id": "00-0000000", "active_roster": false, "snap_played": false, "certified_no_stats": true}
  ]
}
```

Pass it with `sync --participation-file /absolute/path/attestation.json`. Only include player IDs from the frozen game. `certified_no_stats: true` permits an active, absent-row player to receive statistical zeros when the game-level player-stat source passed completeness checks. An inactive player has no statistical result and is recorded as such. `participation_class` in each grade distinguishes `confirmed_inactive`, `active_zero_no_snap`, and `participated_target_stat_zero`. If the Game Book cannot establish these facts, omit the player: unknown remains pending. The reviewer is responsible for checking the PDF against the attestation; the command verifies identity fields, file hash, timestamp, and internal consistency but cannot independently read every name in the PDF.

## Status and revisions

The JSON status view counts `not_started`, `in_progress`, `final_wait`, `awaiting_player_stats`, `ready_for_grading`, `participation_pending`, and `graded`, plus postponed/canceled/suspended exceptions. It includes per-game graded quote counts, players pending participation, source hashes, and changed-result revisions. A grade is appended only after the source bytes are archived and the four-hour wait passes. Identical sources and values add no revision. A changed final score or player statistic appends a grade revision with its previous revision and evidence SHA. The prior grade and original freeze remain unchanged.

When actual Week 3 results arrive, the existing prospective report is rewritten mechanically from the latest grades. Retrospective validation remains a separate section. Week 3 performance does not trigger model retuning, a threshold, promotion, staking, or picks. Live behavior remains `RESEARCH_ONLY` and PASS.
