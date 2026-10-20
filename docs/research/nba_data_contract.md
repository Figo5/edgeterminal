# NBA data contract (ESPN public JSON)

Modules: `edgeterminal/nba_identity.py`, `edgeterminal/nba_data.py`, `edgeterminal/nba_store.py`.
Status: RESEARCH_ONLY. No odds endpoints, no official picks.

## Sources (probed live 2026-10-20)

| Kind | URL | Notes |
|---|---|---|
| scoreboard | `site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD&limit=200` | `dates` is the US-Eastern calendar date (9:30 PM ET games sit at 01:30Z the next day) |
| summary | `.../nba/summary?event={id}` | `header` = event; `boxscore.players[team].statistics[0]` has `keys`, `totals`, `athletes` |
| roster | `.../nba/teams/{espn_team_id}/roster` | current roster, `injuries[].status` as ESPN reports it |
| team stats | `.../nba/teams/{espn_team_id}/statistics[?season=YYYY]` | **`season` is the current label even when numbers are `requestedSeason`'s** (opening night returned 82 GP of 2025-26 labelled 2026-27). Parser reports `data_season_label` from `requestedSeason`. |
| player game log | `site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/gamelog[?season=YYYY]` | rows grouped by `seasonTypes[].displayName` ("2025-26 Regular Season"); played games only |

Unavailable at probe time: cdn.nba.com schedule/scoreboard (HTTP 403), stats.nba.com leaguegamelog (timeout).

## Identity

- Game: `nba-{season_start_year}-{espn_event_id}`; ESPN `season.year=2027` → `2026-27`, start year 2026.
- Accepted season types: 2 regular (incl. NBA Cup final, competition `CC`), 5 play-in, 3 postseason. Preseason (1) and All-Star are refused.
- Teams: canonical tricodes (GSW, NOP, NYK, SAS, UTA, WAS, ...). ESPN team id and abbreviation must agree.
- `tipoff_utc` ISO Z; `game_date_et` = America/New_York date of tipoff.

## Participation (box score)

| status | meaning |
|---|---|
| played | `didNotPlay=false`, numeric minutes and stats |
| dnp | `didNotPlay=true`, reason `COACH'S DECISION` or no/blank reason (never inferred inactive) |
| inactive | `didNotPlay=true`, an explicit other ESPN reason (injury text, REST). ESPN-derived, **not** the official NBA inactive list |
| unverified | listed as playing but minutes `--` (seen live: Mo Bamba, 401810837) |
| not_listed | player absent from box score. Never zero, never inactive |

ESPN's `active` flag is not participation (starters who played show `active=false`) and is ignored.
`box_complete` is true only for a final game whose team totals and summed player points equal the score (held for every game probed).

## Five-prop locks (store contract)

- Exactly five props, all from one game; at most one lock per game regardless of lock ID.
- Each selection carries a finite line in [0, 1000] and a `quote_id` whose game, player,
  market (`PROP_MARKETS[prop]`), side and line match exactly, with a finite integer American price.
- The quote must be the latest observation of that game/player/market/side at lock time, and
  `source received_at <= observed_at <= locked_at < stored tipoff`, source HTTP 200.
- Forecasts (lines optional) need HTTP 200 sources received no later than the forecast.
- "Main line" is a caller contract: quotes record no alt-line flag, so only main lines may be saved.

## Limitations

- `inactive` is an inference from ESPN reason text; graders should treat it like DNP (void), not as an official ruling.
- Team stats before games are played describe the previous season.
- This store's grade revisions cover locked props. The integrated `nba_ops` layer adds append-only game-grade revisions, including corrections that return to a previous outcome; `nba_games` retains the first final score.
- Explicit canceled-game voids are permitted. Postponed/suspended games stay pending, with slower retries after seven days.
- Live HTTP uses `providers.base.get_raw_meta`; rate limits are unknown, so callers should honour `nba_cadences`.
