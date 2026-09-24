# EdgeTerminal design system

## Context
- Artifact / audience / primary action: NFL projection product for readers comparing frozen model estimates with observed markets. Scan the weekly board, inspect a matchup and player props, then review PAPER and research records.
- Aesthetic direction: custom near-black financial interface. Native system typography, neutral surfaces, and precise numeric alignment put projections ahead of chrome.
- Dials: DESIGN_VARIANCE=3, MOTION_INTENSITY=1, VISUAL_DENSITY=7.
- Signature move: a consistent two-row **Market / Model** comparison on each game, with scores and win probabilities above it.

## Typography
- Display / body: `-apple-system`, `BlinkMacSystemFont`, `SF Pro Text`, `SF Pro Display`, `Inter`, sans-serif. The system stack is deliberate because the brief calls for an Apple-like native product without proprietary font files.
- Numerals: the same system font with tabular figures. `SF Mono`/Menlo is reserved for hashes and raw source details.
- Type scale: 11, 12, 14, 16, 20, 29, 39, 48–74 px; ratio is roughly 1.25 for primary steps.
- Base: 14 px / 1.48; headings use tight tracking, metadata uses normal tracking except short uppercase labels.

## Color

| Role | Value | Use |
|---|---|---|
| bg | `oklch(0.105 0 0)` | page |
| surface | `oklch(0.145 0 0)` | matchup and market units |
| surface-raised | `oklch(0.18 0 0)` | hover and selected controls |
| fg | `oklch(0.96 0 0)` | model numbers and headings |
| secondary | `oklch(0.72 0 0)` | supporting text |
| muted | `oklch(0.56 0 0)` | timestamps and labels |
| border | `oklch(0.275 0 0)` | 1 px dividers |
| accent | `oklch(0.93 0 0)` | focus and active state, not a colored theme |
| success | `oklch(0.75 0.075 153)` | true positive status only |
| warning | `oklch(0.76 0.075 78)` | stale/source warnings |
| error | `oklch(0.72 0.075 25)` | true negative status only |

## Spacing & shape
- Base unit: 4 px. Product rows use 16–24 px spacing; the page starts 40 px below the header on desktop.
- Radii: 14 px for meaningful cards, 8 px for controls and small labels.
- Shadow: none. Elevation comes from lightness and one border.

## Iconography
- No decorative icon library. Text labels and one directional arrow carry navigation.

## Motion
- State changes only. No looping or entrance animation.
- Reduced motion: all animations and transitions disabled by media query.

## Boundaries
- All projections, probabilities, lines, confidence values, availability, freezes, PAPER positions, and PASS designations arrive from existing read-only endpoints. This document governs presentation only.

## Verification
- Viewports checked: 1440 × 900 and 375 × 812. The in-app browser rendered the 1440 px layout and reported no overflow; its visible screenshot surface was capped at 969 px, so complete desktop captures use a separate 969 × 900 viewport.
- Date: 2026-09-24.
- Method: rendered browser screenshots in `reports/ui/neutral/`, desktop and mobile overflow checks, `tests/test_nfl_ui_scope.py`, and `tests/product_ui.test.js`.
- Hard-no scan: neutral palette, no gradient, no glow, no gauges, no nested metric cards, no decorative status animation.

## Drift log
- None.
