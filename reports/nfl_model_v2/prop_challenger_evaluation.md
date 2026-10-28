# NFL player-prop challenger evaluation

Status: RESEARCH_ONLY / PASS. Separate shadow model; original selections are preserved.

Training cutoff: before 2026 Week 3. Blend chosen on 2024; comparison on 2025. No 2026 outcomes were used to choose parameters.

| Market | Usage blend | Baseline MAE | Challenger MAE | Baseline diagnostic Brier | Challenger diagnostic Brier | Offline gate |
|---|---:|---:|---:|---:|---:|---|
| passing_yards | 0% | 67.478 | 67.478 | 0.2130 | 0.2085 | Pass |
| receiving_yards | 25% | 18.696 | 18.660 | 0.2092 | 0.2015 | Pass |
| receptions | 25% | 1.408 | 1.403 | 0.2156 | 0.2115 | Pass |
| rushing_attempts | 25% | 2.940 | 2.932 | 0.2014 | 0.1941 | Pass |
| rushing_yards | 25% | 17.896 | 17.909 | 0.2048 | 0.1978 | Fail |

Brier diagnostics use synthetic prior-usage thresholds, not archived sportsbook lines. They do not establish a betting edge. MAE gains are small; rushing yards missed the accuracy gate.

Prospective snapshots: worker saves supported fresh pregame quotes to the shadow artifact directory. No additional provider requests. First snapshot per game determines the comparison record; later snapshots do not replace it.

Availability remains unresolved without a verified timestamped source. Rankings cap each player at two markets and deduplicate sides and player/stat pairs. Missing markets are not filled with unsupported picks.

Model manifest: /Users/giofiore/edgeterminal/data/research/prop_challenger/1791894691663023000/manifest.json
Manifest SHA-256: fa20ac46a9db24557f81a13f0e31e781556e020a9b6fcc7b4953c4a0ac52a368
Code commit: 6cdff7bfeffdf35dd4336c23f7117ed40c60a31b
