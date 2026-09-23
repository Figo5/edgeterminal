# Claude Opus 5.5 review gates

Both reviews used Claude Code CLI on 2026-09-23. These verdicts concern the
research-only local product, not a profitable or deployable betting policy.

## Gate 1 — data, model and replay

Initial verdict: **APPROVE_WITH_FIXES**. The review identified neutral-site
handling, tied-game probability scoring, baseline comparability, uncertainty
intervals, spread sign conventions, return caveats and replay provenance as
items to correct or disclose. The follow-up verdict remained
**APPROVE_WITH_FIXES** for a research-only diagnostic. The code and report were
updated, all three seasons were replayed, and `replay_manifest.json` now pins
the source commit, commands, seed and 14 source/data hashes.

The reviewer did not approve an NFL pick threshold. The 2023–2025 seasons are
seen retrospective development data, and the candidate loses to the reference
closing market on margin and total MAE in each season.

## Gate 2 — product, policy and current-week UI

Initial verdict: **REJECT**. The review found paths that could still promote
NFL player props, imply shadow grading existed, misstate why a game was PASS,
and confuse model edge with a price comparison. These were corrected. The
follow-up verdict was **APPROVE_WITH_FIXES** for the research-only product,
with no blocking findings. The remaining quote timestamp, displayed book
count, best-prop tool, default prop policy and thin-sample copy issues were
subsequently corrected and covered by the 269-test suite and browser checks.

## Release boundary

Readiness is **RESEARCH_ONLY**. After the second review, an append-only forward
tracker was added. On 2026-09-23 it froze 16 Week 3 projections with spread
and total reference lines; zero games had final scores to grade. A focused
Claude review of this tracker returned **APPROVE_WITH_FIXES**, with no blocking
findings. Its immutability, chat caveat, current-model selection, result-sync
and late-line findings were addressed, with focused regression tests. A final
focused follow-up returned **APPROVE** for RESEARCH_ONLY. This
does not upgrade readiness before real final scores are graded. The multi-book
Odds API credential currently returns HTTP 401; the fallback market feed
exposes one DraftKings book per game. No official NFL plays or profitability
claims are supported.
