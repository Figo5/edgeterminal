# NFL projection surface

This page uses the existing EdgeTerminal dark palette and type scale. The visible order is: weekly matchup, projected score, observed market price, model probability, and player props. Research operations live behind the Research navigation item.

## Components

- Game cards use a two-column desktop grid and one mobile column. Scores carry the strongest visual weight; market and model lines are separated by rules.
- Matchup pages place the projected score first, then moneyline, spread, total, and the full qualified prop board. PAPER uses a small badge only when an immutable position exists.
- Confidence means the stored model probability for the displayed outcome. It is not a recommendation score.
- Stale prices remain visible only as *last observed* pregame quotes with timestamps. ESPN fallback is named explicitly.
- Mobile uses vertical cards and wraps prop facts; it does not require horizontal scrolling for the primary board.

## Tokens

Existing `style.css` variables remain authoritative: `--bg`, `--panel`, `--panel2`, `--line`, `--text`, `--dim`, `--accent`, and `--accent2`. Product components use 5–6 px radii, 10–14 px gaps, and the existing system sans/monospace pairing.

## Boundaries

The browser only calls read-only product and research endpoints on page load. The background scheduler owns ingestion. PASS, RESEARCH_ONLY, research freezes, model parameters, and PAPER policy are not presentation state.

## Verification

On 2026-09-24, browser review covered 1440 × 900 and 375 × 812 viewports. The NFL board, ATL–GB matchup, qualified prop rows, Paper Portfolio, Performance, and Research rendered. The 375 px board and prop view had no document overflow. Captures are in `reports/ui/`.
