# NBA October 27, 2026 — independent source and grading review

Outcome: **9 wins, 11 losses, no pushes, voids or pending selections.** The losses are genuine statistical outcomes under the saved sides and lines. No grading or player-identity defect was found.

## Scope and method

Offline, read-only audit of all four October 27 games and all twenty saved `nba-research-2.0.1` prop selections. No provider requests, target tests, model fitting, forecast/lock/grade updates or live configuration changes occurred. SQLite was opened with `mode=ro`.

- Recomputed each exact forecast payload SHA-256 and five-item lock SHA-256.
- Checked every referenced source has HTTP 200, matching archived body SHA-256 and receipt no later than the pregame forecast. The lock and quote observation times precede tipoff.
- Renormalized raw SportsGameOdds capture 9216 against the exact ESPN rosters referenced by each saved forecast. Each selected name, ESPN identity, team, market, side, line, price and bookmaker has one exact matching normalized offer.
- Parsed the exact final-box-score artifact referenced by each latest grade. ESPN game IDs, teams and final scores match the saved game; both team totals equal summed player points (`box_complete=True`).
- All twenty selected player IDs are unique in their respective final boxes, with matching names/teams and explicit played status. Actual points, rebounds, assists and threes match their parsed columns; PRA equals the three component totals.
- Independently recomputed each win/loss from actual stat, saved side and saved line. All twenty agree with the persisted grade. Zero-valued winning threes selections have proof of play, not missing statistics.

**47 archived artifacts verified.** ESPN is the actual result source; this does not claim independent NBA-league-source corroboration or independent re-execution of a game. Current 2026 team assignments were checked against the archived sources rather than assumed from older rosters.

## Per-selection readback

| Game | Player | Market/side/line | Actual | Minutes | Grade | Final source |
|---|---|---|---:|---:|---|---|
| 401909877 | Derrick White | pra under 24.5 | 28 | 33 | loss | 10440 |
| 401909877 | Derrick White | assists under 5.5 | 7 | 33 | loss | 10440 |
| 401909877 | Keon Ellis | assists under 2.5 | 4 | 25 | loss | 10440 |
| 401909877 | Keon Ellis | threes under 1.5 | 0 | 25 | win | 10440 |
| 401909877 | Jayson Tatum | assists under 7.5 | 8 | 42 | loss | 10440 |
| 401909878 | Ausar Thompson | threes under 0.5 | 0 | 25 | win | 10438 |
| 401909878 | John Collins | assists under 1.5 | 1 | 27 | win | 10438 |
| 401909878 | Paul Reed | rebounds under 8.5 | 9 | 26 | loss | 10438 |
| 401909878 | Cade Cunningham | points under 23.5 | 19 | 36 | win | 10438 |
| 401909878 | Duncan Robinson | pra over 16.5 | 14 | 33 | loss | 10438 |
| 401909879 | Precious Achiuwa | points over 10.5 | 4 | 14 | loss | 10439 |
| 401909879 | Keldon Johnson | points over 10.5 | 14 | 22 | win | 10439 |
| 401909879 | Domantas Sabonis | assists under 6.5 | 8 | 37 | loss | 10439 |
| 401909879 | Julian Champagnie | threes under 2.5 | 8 | 25 | loss | 10439 |
| 401909879 | Stephon Castle | assists over 6.5 | 5 | 27 | loss | 10439 |
| 401909880 | Sandro Mamukelashvili | threes over 0.5 | 3 | 24 | win | 10472 |
| 401909880 | Ziaire Williams | points over 8.5 | 11 | 29 | win | 10472 |
| 401909880 | Collin Sexton | points over 10.5 | 9 | 14 | loss | 10472 |
| 401909880 | Sandro Mamukelashvili | points over 7.5 | 17 | 24 | win | 10472 |
| 401909880 | Jrue Holiday | pra over 16.5 | 29 | 30 | win | 10472 |

## Result evidence

| Artifact | Game | SHA-256 |
|---:|---|---|
| 10440 | [nba-2026-401909877](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401909877) | `a19c3c244003f5c7326782cd934eb39374dc9d784673bd9f8d0c32f1e94c16fc` |
| 10438 | [nba-2026-401909878](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401909878) | `b21b664ccb4ada6600acf0af8efcd675eda20f5d568875d54fc3d4c0553c7074` |
| 10439 | [nba-2026-401909879](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401909879) | `cd36641b44afa44565f4ab39518693f3c70725ec5bdcc7de7cdb91181db3b66a` |
| 10472 | [nba-2026-401909880](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401909880) | `14cd716ffb89a5f234af63d650546650653ae13e235fd933b604357c636a126d` |

## Interpretation

Derrick White's two losing props are the same player/game exposure; both final statistics are correct (28 PRA and 7 assists in 33 minutes). Keon Ellis's 4 assists, Tatum's 8 assists, Sabonis's 8 assists and Champagnie's 8 threes are also correct in the archived boxes. Achiuwa and Sexton each played 14 minutes, consistent with genuine opportunity misses rather than misgrading. The companion error audit should distinguish minutes and production-rate forecast misses; this review does not select model changes from these twenty outcomes.

Reproduction script and full row evidence for this maintenance audit were retained locally at `/tmp/nba-oct27-grading-audit.py` and `/tmp/nba-oct27-grading-audit.json`. Those scratch files are not part of the product or model artifact.
