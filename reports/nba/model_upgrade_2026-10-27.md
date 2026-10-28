# NBA active model upgrade — October 27, 2026

PASS / RESEARCH_ONLY. New predictions use nba-research-2.0.1; saved predictions remain unchanged. The game forecast component is unchanged.

All fitting and calibration precede the original October 1 cutoff. This is retrospective historical validation; March–April results informed the addition of a generic February probability gate, so these are not independent future-performance results. Current-roster sampling has survivorship bias. Synthetic thresholds are not sportsbook lines.

| Market | Historical forecasts | MAE before | MAE after | CRPS before | CRPS after | Probability law |
|---|---:|---:|---:|---:|---:|---|
| assists | 6094 | 1.455 | 1.433 | 1.035 | 1.013 | parametric |
| points | 6094 | 4.933 | 4.842 | 3.497 | 3.438 | parametric |
| pra | 6094 | 6.445 | 6.294 | 4.591 | 4.482 | parametric |
| rebounds | 6094 | 1.959 | 1.938 | 1.390 | 1.367 | empirical |
| threes | 6094 | 0.935 | 0.932 | 0.644 | 0.636 | parametric |

Minutes MAE: 5.336 → 5.032. The opportunity method uses pooled per-minute production multiplied by separately forecast minutes. Coefficients and per-market probability methods are frozen.

Artifact SHA-256: `16026a8ae369bd66805e1ad7ea5a9d58e867cac142a2027b471c9a4c1a0a772f`. The JSON report and model artifact contain source and serving implementation hashes.

Activation uses 2.0.1 because an intermediate worker registration reserved 2.0.0 during construction. No 2.0.0 forecasts were saved; fitted coefficients and historical metrics are unchanged.
