# Prospective 2026 NFL research

Status: RESEARCH_ONLY; live product PASS.

These numbers exclude retrospective development games. See `research_results.json` for that separate analysis.

| Family | Graded | Margin MAE | Total MAE | Brier | ATS W-L-P | Totals W-L-P |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| linear | 32 | 7.734 | 11.045 | 0.223 | 18-13-1 | 16-16-0 |

Detailed week, edge-bucket, closing-market comparisons, sample counts, and bootstrap intervals are in the companion JSON file.
A close comparator requires a same-book quote observed within 15 minutes before kickoff. Without one, that comparison is unavailable. ESPN's `close` field alone does not establish a closing timestamp.
