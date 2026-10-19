# Automatic participation checks

The built-in worker's existing six-hour grading pass now checks official
participation sources when a locked top-five selection lacks sufficient player
statistics. Page visits reuse that worker and its cadence; they do not force
additional source calls. No Codex automation is needed.

After ESPN confirms the final score and the existing four-hour postgame delay
has elapsed, the grader:

1. Uses the frozen GSIS identity, prior player-stat names, and the exact week's
   nflverse roster to match the full name, team and unique jersey number.
2. Discovers the Game Book link on the NFL's game page. Only approved HTTPS
   NFL/club hosts and the specific nflverse roster release are fetched.
3. Checks the PDF's teams, date and final score against the verified game.
   Explicit Not Active entries establish inactivity. Did Not Play entries or
   recorded playtime establish active participation status. For missing stats,
   the final passing, rushing and receiving tables must reconcile their totals;
   passing completions and yards must also agree with receiving totals.
4. For a reserve player absent from the Game Book, checks the official club's
   season transaction table. A unique explicit IR placement before game day,
   without a later player transaction, must corroborate the weekly reserve
   identity. Same-day or conflicting transactions require review.

Absence from a feed or PDF alone never means zero, inactive or void. Ambiguous
names, incomplete tables, source failures and conflicting stats leave the
affected selection pending. About & data shows the unresolved participation
check and its scheduled retry. Confirmed active zero production uses the frozen
line to determine win/loss/push; confirmed inactivity is void in the statistical
research record. Sportsbook settlement remains a separate interpretation.

Exact source bytes, URLs, hashes, parser version and automatic evidence manifests
are archived in the existing immutable grading-source tables. A six-hour cache
shares successful and failed requests across freezes. Repeated runs are
idempotent; new verified corrections append revisions. Existing reviewed Game
Book attestations are revalidated and reused.

These checks do not change selections, model parameters, training cutoff,
historical freezes, PASS or RESEARCH_ONLY. They never request Odds API or spend
SportsGameOdds quota.
