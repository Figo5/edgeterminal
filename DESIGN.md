# EdgeTerminal design

A compact weekly NFL board with the original dark EdgeTerminal theme. David Sasser's pages inform which projection fields are visible, while EdgeTerminal keeps its own appearance. Projected scores lead each matchup; winner, model spread and total, market context, and five ranked props follow in that order. Group games by kickoff day. Keep methods and operations under About & data.

The visible research ledger tracks five distinct player/market selections per game. It uses wins, losses, pushes, voids, and pending results. Money and position sizing are absent from the main navigation.

## Tokens

Colors use the original semantic OKLCH variables: near-black background (.105/0/0), surface (.145/0/0), raised surface (.18/0/0), bright text (.96/0/0), secondary (.72/0/0), borders (.275/0/0), restrained green/red results. Use native system sans and tabular numbers; monospace only for source hashes. Compact headings (29–40px), scores 48px, metadata 12–14px. Spacing follows 4/8/12/16/20/24/32/40/48/64. Corners: 8 and 14px. No shadows or decorative animation.

Season record shows cumulative and weekly winner/spread/total/prop results. Historical prop rankings are labeled Archived ranking. Missing archive weeks show Not recorded. Expandable history preserves original predictions, lines and outcomes; a native week selector filters history. No Research chat.

Desktop: two matchup columns, compact rows for five props. Mobile: one column, two-column stat rows, wrapped navigation, no horizontal page overflow. Keyboard focus is visible. Freshness always names an actual source timestamp. Pending and unavailable are explicit states.

## Local beta additions — 2026-10-06

Live pre-kickoff selections and archived rankings have separate headline and weekly records. Live probability checks exclude archived outcomes. Matchup explanations expand in place; original line and price remain primary, newer observed lines are secondary. Saved & account adds a local profile and inbox without bankroll, checkout or outbound alerts. An unread count appears in the navigation. Coverage and background timestamps form a compact strip. Mobile navigation wraps with no horizontal page overflow. Existing dark tokens and typography are preserved.
