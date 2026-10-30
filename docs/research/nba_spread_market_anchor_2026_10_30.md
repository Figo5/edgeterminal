# NBA spread: chronological market-anchor evaluation

This change retains every supported-game selection and the current live fallback. It adds a trainable, versioned market ensemble to the existing game-component pipeline. It does not activate a model, replace an old forecast, change a grade, or alter pinned prop sources.

## Contracts and reuse

The new `nba_spread_market.py` contains pure quote validation, whole-date cohort partitioning, positive convex weight fitting, integer-margin cover probabilities, and four-way benchmarks. `nba_spread_model.project_game()` accepts an optional contemporaneous `market` snapshot. `nba_spread_learning` orchestrates source-bound replay and keeps its existing separate game registry and promotion thresholds.

Schema 1 `nba-spread-residual-v1` artifacts retain their prior numerical behavior. Schema 2 `nba-spread-market-v2` uses the same six-feature core plus an independently fitted `market_anchor`. The artifact records both `core_training_cutoff` and the later overall `training_cutoff`. Serving never fits a weight or calibrator.

The supplied ESPN line must have finite opposite home/away spreads, an exact source artifact ID and SHA-256, and `observed_at <= as_of < tipoff`. The publication path independently verifies the quote against its actual archived response. The anchor uses that exact line, not another bookmaker's line or a later closing quote. A missing supported line causes an explicit retained-baseline fallback, with the baseline's own version and fingerprint. A future or malformed supplied quote fails closed.

Projection metadata distinguishes the raw feature margin, raw published margin, market margin, observation time, fitted model weight, fallback, and whether anchoring was applied. The published anchored margin retains numerical precision so rounding a tiny negative edge to the line cannot silently flip its side. Display scores/margins may still be formatted to two decimals. Source-version metadata must describe the projection that was actually used, including fallback.

## Four independent chronological cohorts

The bounded reader requests at most 240 verified selected forecast/line/final snapshots. They retain the existing immutable policy, final hash-chain, original-line, source-identity, and receipt checks. Only snapshots after historical-input bootstrap completion qualify.

The minimum cohorts are:

| Cohort | Minimum whole games | Use |
| --- | ---: | --- |
| Anchor fit | 40 | Fit regularized convex model weights |
| Development | 20 | Choose a weight/regularization setting by margin MAE |
| Calibration | 20 | Fit the selected blend's residual standard deviation |
| Outer validation | 60 across seven dates | Existing actual-line promotion gates only |

The partition reserves entire eligible Eastern-date groups. A preceding phase's result receipt must precede the earliest next-phase forecast as-of; late labels are purged. Sorting tipoff timestamps alone is insufficient. These minimums can require more than 140 games after whole-date grouping and receipt purges.

The historical feature core is fitted before the **first anchor-fit forecast**, so its fitted coefficients cannot have seen fit/development/calibration target outcomes. The later overall cutoff remains before every outer-validation forecast. Calibration labels cannot alter core coefficients or blend choice; outer labels cannot alter either fitted weights or calibration.

Counterfactual extra-team/opponent features use the latest archived team sources actually received by each forecast as-of, with the shared feature cutoff. They do not use the mutable current history cache, nor freeze all opponent features at the much earlier coefficient cutoff. The actual capture's target-team input manifest is preserved. Every additional feature/possession source is recorded and its archived bytes and receipt are checked again before the proposal returns.

## What anchoring can establish

The point prediction is:

`anchored_margin = market_margin + weight × (feature_margin − market_margin)`

Weights are positive and no greater than one. The identity weight is always a candidate and wins exact ties. Other weights are fitted on the first cohort using regularized least squares toward the identity, with fixed penalties 0, 25, and 100. Nonpositive estimates are not serving candidates. Development chooses among these already fitted alternatives; calibration fits uncertainty afterward.

For the **same exact line**, this positive convex blend preserves the feature model's ATS side. It can improve point forecasts and cover calibration; anchoring alone cannot be described as improving the spread win count. Improvements in selected sides must come from the separately validated feature core. This identity does not apply when comparing different lines, times, or bookmakers.

The outer report compares the incumbent's actual saved projection, feature-only projection, centered market-only reference, and learned blend on the same games and original lines. The market-only point reference has no directional ATS pick against its own line; its centered cover reference is 0.5, not a claim to have inferred a de-vig probability from sportsbook prices. Integer lines retain explicit push mass, and conditional cover scores exclude pushes consistently for both compared models.

## Promotion and repeated-look controls

Existing minimums and materiality gates remain: 60 games/seven dates; margin MAE improvement of at least the larger of 0.25 point or 2%; cover Brier improvement of at least 0.002; paired calendar-day uncertainty intervals excluding deterioration; no lower ATS win count; and unchanged supported-game coverage. No threshold was lowered to make a proposal pass.

The former rolling holdout plus ten-new-games check could repeatedly inspect mostly the same outcomes. A new append-only consumption ledger seals every completed proposal's outer game IDs, dates, exact forecast hashes, and final-label hashes, whether accepted or rejected. Those games/dates cannot become outer validation again through a corrected label or family rename. Their labels may enter a later chronological fitting prefix. Candidate registration, holdout sealing, and the verdict are one transaction; duplicate consumption rolls back the entire new proposal. Schema-2 activation also requires a sealed holdout at the database boundary.

## Current evidence and scope

Read-only inspection on October 30 found **three qualifying final games, all from October 29**, and no registered or activated game candidate. This is insufficient for the outer gate, and also insufficient for the earlier market-fitting cohorts. There is **no newly validated performance improvement to claim**. The baseline remains live until independent future evidence earns activation.

No provider call, live database write, service change, registry activation, or pinned `nba_model.py`, `nba_prop_model.py`, or `pricing.py` edit was part of this track. Work is isolated in the quality-upgrades checkout.

Focused tests cover whole-date splits, delayed-label purging, strict source/quote timing, fit/development/calibration isolation, outer-target poisoning, schema compatibility, missing-market fallback, tiny-edge sign preservation, exact-line comparisons, source-bound feature replay, cached-source revalidation, rejected-cohort consumption, and atomic refusal of holdout reuse.
