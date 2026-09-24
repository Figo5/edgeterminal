# EXPERIMENTAL_PAPER_V1

`PAPER / EXPERIMENTAL / NOT A REAL BETTING RECOMMENDATION`

This is a prospective mock accounting policy. It does not change the NFL product's `RESEARCH_ONLY`/`PASS` state or create real wagers. The immutable policy payload is [`data/research/experimental_paper_v1.json`](../../data/research/experimental_paper_v1.json). Registration stores a SHA-256 of canonical sorted JSON. Every new position stores that hash and the policy version; changing V1 content under the same name fails. A later policy must use a new version. Existing PAPER ledger rows are never changed.

Registered canonical policy SHA-256: `a9ea06f9430bb860678a919ace2fbe9738e9c89d2369fd2284a7d520f70d0fc7`.

## Selection rule, fixed before results

- Five markets: passing yards, rushing yards, rushing attempts, receptions, receiving yards. Main lines only. No touchdowns, longest props, parlays, game lines, or moneyline.
- Six supported books: FanDuel, DraftKings, BetMGM, Caesars, ESPN BET, Bovada. Names are stored in their normalized forms.
- A frozen, hash-verified prop projection must match its immutable source quote, canonical provider-event mapping, player alias registry, team/opponent identity, player name, line, side, price, book, observation time, and kickoff. Projection, valid side probability, and positive uncertainty are required. A sourced `available`/`active`/`healthy`/`cleared` status for the player must be no more than **six hours old** at creation; unresolved status skips the quote.
- Quote must be observed before position creation and kickoff, no more than **900 seconds old** at creation, and have American odds from **-200 through +200**. The projection must be at least **0.01** units on the selected side of the line. Its side probability must exceed the probability implied by the quoted price by at least **0.25** (25 percentage points). This is a crude, unadjusted comparison to the book's single-side price, not a validated edge or expected-profit estimate.
- A player's market can enter once per game. Among qualifying books, the newest frozen quote is considered first; a competing quote skipped after selection gets `competing_quote_player_market_already_selected` in the append-only skip ledger. Source identity and availability failures receive their own skip reasons.
- Starting PAPER bankroll **$1,000**; each position is a flat **$10** (one unit). Maximum **3** positions per game, **10** per Eastern-time slate day, **10** open positions, and **10%** of current PAPER bankroll in open stakes. A single stake cannot exceed **1%** of current bankroll. No Kelly, parlays, loss chasing, or stake increases.

## Why 0.25

The archived pregame Week 3 prop freeze captured at **2026-09-23 15:14:15 EDT** contains 1,746 main-line quote sides across the five markets. Using only frozen projections and quoted prices, requiring the correct directional model-line sign and -200 to +200 odds gives these descriptive counts:

| Minimum model minus implied probability | Quote sides | Distinct game/player/market pairs |
| --- | ---: | ---: |
| 0.10 | 300 | 80 |
| 0.15 | 182 | 49 |
| 0.20 | 91 | 28 |
| **0.25** | **30** | **10** |

The 0.25 rule reduces the archived slate to a manageable ten distinct candidates before quote age and availability checks; three are in one game, two each in two games, and the rest are spread across three games. These counts were **not** calculated from Week 3 outcomes. The cutoff is arbitrary and conservative for workload control. It is frozen for V1; results from one week will not trigger retuning.

At the activation audit on **2026-09-24**, **zero** Week 3 quote sides were within the required 15-minute age window, so **zero positions qualify at that moment**. Nine of the ten archived distinct candidates had a recorded available status at the audit time; availability can change. An old frozen quote does not become fresh because a later market observation exists. V1 waits for a newly frozen projection paired with a recent quote. It will not invent a quote or re-freeze Week 3.

Week 3's prop freeze metadata lacks a serialized fitted estimator hash. For that legacy freeze, `model_hash` on a PAPER position would be a SHA-256 research signature of its model version and frozen metadata, rather than a hash of fitted weights. This limitation is reported explicitly and is not repaired by modifying the freeze. Future prop freezes should include a fitted-model hash to provide stronger provenance.
