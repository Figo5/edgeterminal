# October 28, 2026 NBA spread grading audit

RESEARCH_ONLY / PASS.

The saved original-comparison record is genuinely **3 won, 9 lost**. All 12 independently recomputed results match the saved grades; 0 integrity failures were found.

This offline audit verified 65 archived source body hashes, 12 game forecast hashes, 12 separately saved spread-comparison payload hashes and all grade-revision hash chains. Every referenced input was HTTP 200 and received by its record time; all forecasts and comparison observations precede stored tipoff. Final event IDs, numeric team IDs, ESPN abbreviations, home/away placement and scores match the database. Each final score also reconciles to period totals, player points, box totals and 2×FGM + 3PM + FTM.

All game projections use **nba-research-1.1.0**, fingerprint `79026f8a1c09b06e247bd5e2c0335d86f8930c92c416679299dadfc479cf4da4`. Their original payloads contain no spread. `comparison_market()` subsequently attaches the first immutable pretipoff `spread-line` record; this remains fixed even when `kv` market prices move. The prop model upgrade to 2.0.1 did not regenerate these game projections.

Pick cushion = home score − away score + pick line for a home pick; away score − home score + pick line for an away pick. Positive wins; negative loses. Projection and actual margins below both use home − away.

| Matchup | Original pick | Projected home margin | Actual home margin | Cover cushion | Result | Forecast age (h) | Line source | Final source |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| MIA@ATL | ATL -3.5 | +9.60 | -12 | -15.5 | lost | 47.81 | 9917 | 11558 |
| WAS@CLE | CLE -10.5 | +10.60 | +10 | -0.5 | lost | 47.81 | 9917 | 11559 |
| CHA@DET | CHA +7.5 | +3.97 | +8 | -0.5 | lost | 47.81 | 9917 | 11560 |
| PHI@IND | IND +1.5 | -0.90 | -5 | -3.5 | lost | 47.81 | 8439 | 11561 |
| ORL@TOR | TOR -4.5 | +8.35 | +30 | +25.5 | won | 45.71 | 9917 | 11557 |
| NYK@CHI | CHI +7.5 | -5.40 | -6 | +1.5 | won | 45.55 | 9917 | 11688 |
| HOU@MIL | MIL +8.5 | -7.83 | -32 | -23.5 | lost | 45.54 | 9917 | 11555 |
| GSW@MIN | MIN -3.5 | +8.67 | -5 | -8.5 | lost | 45.54 | 9917 | 11689 |
| DEN@NOP | NOP +4.5 | -3.53 | -13 | -8.5 | lost | 45.54 | 9917 | 11556 |
| SAS@UTA | UTA +12.5 | -9.03 | -6 | +6.5 | won | 46.54 | 10067 | 11687 |
| OKC@DAL | OKC -5.5 | -7.43 | +24 | -29.5 | lost | 47.04 | 10067 | 11686 |
| LAL@LAC | LAC +5.5 | +1.32 | -16 | -10.5 | lost | 46.95 | 10067 | 11685 |

Findings that affect interpretation and future work:

- Forecasts were 45.54–47.81 hours old at tipoff; comparison lines were 23.41–44.55 hours old. Preserve these originals, but evaluate a separately versioned near-tipoff snapshot process so new injuries, rest and recent games can inform future forecasts.
- Four projected advantages were under one point: CLE 0.10, IND 0.60, MIL 0.67 and NOP 0.97. All lost. The system currently forces a side for every available line. Test a prespecified abstention rule on chronological holdouts; do not select its threshold from this slate.
- CLE −10.5 and CHA +7.5 both lost by half a point. IND +1.5 is also correctly a loss despite PHI being the model's straight-up winner; spread and moneyline picks are different calculations.
- Diagnostic counterfactual only: the latest cached lines differ in 7 games. Regrading the same original picks at the latest lines produces 5–7 because CLE −9.5 and IND +7.5 cover. Recomputing both picks and grades at the latest lines produces 6–6, additionally changing LAC +5.5 to LAL +1.5. These are hypothetical records at different quotes and must not overwrite the genuine original 3–9 record.
- Saved sportsbook names are `Unknown` for 10 games, although all 12 original raw offers name DraftKings, provider ID 100. `parse_market()` only reads `provider.displayName`, which is absent in those 10 offers. A future metadata-only fix can fall back to `provider.name`; prices, lines and grades are correct.
- Slate margin MAE was 12.64 points versus 11.17 for the original market margin. This is descriptive evidence from 12 games, not a validated improvement rule.
- There are no `spreads` rows in `nba_quote_observations`; game lines are preserved by the immutable forecast payload and its archived source hash. Future audit metadata should expose both forecast time and comparison time and freeze a selected side/line/source reference explicitly.

Original line source artifacts: 8439 (PHI@IND), 9917 (MIA@ATL, WAS@CLE, CHA@DET, ORL@TOR, NYK@CHI, HOU@MIL, GSW@MIN, DEN@NOP), and 10067 (SAS@UTA, OKC@DAL, LAL@LAC). The companion JSON contains complete source URLs, receipts, SHA-256 values and per-game checks. The ESPN JSON `close` field means the observed offer at that archived receipt; it is not asserted to be the final closing line.

Reproduce from the repository root:

```sh
python3 reports/nba/spread_audit_2026_10_28/grading_audit.py
```

The script opens the production database read-only and writes only grading_audit.json and grading_audit.md in its report directory. No production database, model, source capture, forecast, lock or existing report was modified; no Odds API was used.
