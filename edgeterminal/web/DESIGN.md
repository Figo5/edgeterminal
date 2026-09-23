# EdgeTerminal NFL interface

## Context

- Artifact: local NFL research terminal for scanning a full weekly slate and
  opening one matchup at a time.
- Direction: restrained sportsbook research ledger. A dark canvas supports
  score and price comparison without implying a live trading feed.
- Dials: DESIGN_VARIANCE=3, MOTION_INTENSITY=1, VISUAL_DENSITY=4.
- Signature move: each game aligns projected score, model numbers and observed
  market prices in three calm horizontal bands. Status stays visible.

## Typography

- Body: platform system sans for legibility in a local utility.
- Numbers and timestamps: platform monospace with tabular figures.
- Base: 14px / 1.5. Main heading: 27px / 1.2. Game scores: 23px.

## Color

| Role | OKLCH target | Current CSS token |
| --- | --- | --- |
| bg | `oklch(0.16 0.02 255)` | `--bg` |
| surface | `oklch(0.20 0.025 255)` | `--panel` |
| fg | `oklch(0.88 0.02 255)` | `--text` |
| muted | `oklch(0.62 0.02 255)` | `--dim` |
| border | `oklch(0.29 0.025 255)` | `--line2` |
| accent | `oklch(0.77 0.15 80)` | `--accent` |
| accent-fg | `oklch(0.20 0.03 80)` | primary-button text |
| success | `oklch(0.72 0.15 155)` | `--good` |
| warning | `oklch(0.73 0.13 65)` | `--warn` |
| error | `oklch(0.67 0.18 20)` | `--bad` |

## Spacing and shape

- Base unit: 4px. Board padding is 16px, grid gap 12px, page inset 24px.
- Radius: 4px for controls and panels.
- Elevation: borders and surface lightness only; no card shadows.

## Iconography and motion

- The existing square brand mark is the only decorative symbol. Text labels
  identify every action and state.
- Hover and focus change borders. Reduced-motion preferences suppress the
  inherited chat loading animation.

## Verification

- Automated source and API tests plus `node --check` run on 2026-09-23.
- Wide (1440×900) and narrow (375×812) browser render review completed on
  2026-09-23. Both keep the board within the viewport; the detailed market
  table scrolls horizontally inside its panel on narrow screens. Captures are
  stored in `reports/nfl_model_v1/screenshots/`.
- The pre-existing stylesheet contains older small type and hex colors outside
  this board system; these are legacy detail and chat styles, not new tokens.
