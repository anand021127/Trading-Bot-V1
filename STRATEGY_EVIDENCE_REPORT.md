# Is V8-D broken, mis-configured, or simply without an edge? — evidence report

**Short answer.** Three separate things were true at once. (1) The bot could not trade for *implementation and
configuration* reasons (fixed in earlier rounds, plus one found now). (2) V8-D, as backtested on your real option data,
shows a **statistically significant negative expectancy** — so "it takes no trades" is not the real problem; taking the
trades would lose money. (3) Nothing I could test on the available real data qualifies as a better strategy, so **none was
substituted**: swapping in an unproven rule and calling it an improvement would be fabricating one.

## 1. Why the bot was not trading (implementation / configuration)
| Cause | Evidence | Status |
|---|---|---|
| **Capital too small for the lot sizes** (new) | With the dashboard's ₹20,000 only **2 of your 171 real backtest trades (1.2 %)** could be sized to one lot under V8-D's own caps (2.5 % risk, 18 % allocation). One lot needs ≈ ₹49k (NIFTY50) – ₹63k (BANKEX). Every valid setup is rejected "0 lots". The backtest ran with ₹100,000, which hid it. | Detected from the live chain every scan; red warning in the dashboard/Copilot with the exact ₹ needed per symbol; the rejection text now says so. **Action: raise `TRADING_CAPITAL` (paper money). The risk percentages are unchanged.** |
| Option quote age taken from the candle | ~90 % of genuine BUYs rejected "quote is stale" (0 % with completed candles) | Fixed (earlier round) |
| One symbol scanned | NIFTY50 alone: ~55 % of days have no setup on real 2024 data | Fixed: all six by default |
| RSI one candle stale | proven vs textbook Wilder | Fixed |
| Dashboard showed "0/20 trades" while V8-D stops at 3/day | strategy cap binds first | Shows the effective cap |

## 2. Does V8-D itself have an edge? (your real option-data backtest, 171 trades)
* Mean result **−0.157 R per trade** (t = −2.41); 95 % bootstrap CI **[−0.277, −0.030]** → the whole interval is below zero (≈ 99 % one-sided).
* **Before costs it is also negative: −0.145 R (CI [−0.265, −0.017])** — fees/slippage explain only ~8 % of the loss. The entry has no demonstrable edge.
* Win rate 26.9 % against a nominal break-even of 40 % (1.5R target / 1R stop). 66 stop-outs (−₹1,05,422) vs 25 target hits (+₹54,652); 67 trailing-stop exits and 13 square-offs net ≈ +₹5,400.
* Stable, not a one-off: −0.150 R (Oct 25–Mar 26) and −0.169 R (Apr–Sep 26). NIFTY50 ≈ break-even (+0.03 R, 62 trades); the other five lose. Expiry-day entries (57 % of trades): −0.21 R; other days −0.09 R — both negative, so avoiding expiry day would not create an edge.
* The dashboard now shows this verdict (`NEGATIVE_EXPECTANCY`, with the interval) beside the backtest P&L.
* Caveat: this backtest ran with the original (one-bar-stale) RSI; the corrected version cannot be re-run without option history, but the underlying-level test below shows the corrected entry is no better.

## 3. Search for a better strategy (real candles, fixed rules, pre-registered decision)
Harness: `scripts/strategy_research.py`; results: `analysis/strategy_research_results.json`. Real 5-minute candles, six symbols, 2024
(+ NIFTY50 Jan–Mar 2025 as untouched hold-out). Causal signals on completed bars, entry at the next open, identical bracket
(stop 1.5 ATR, target 1.5R, square-off 15:10), 1.5 bp round-trip cost, day-clustered bootstrap, time split, per-symbol split.
A candidate qualifies only if the Bonferroni-adjusted lower bound of net R is > 0, both halves are > 0, ≥ 4/6 symbols are > 0 and the hold-out is > 0.

| candidate | trades | gross R | net R | net 95 % CI | H1 | H2 | symbols + | hold-out | qualifies |
|---|---|---|---|---|---|---|---|---|---|
| random entry (control, 20 seeds) | 21,735 | +0.026 | −0.067 | [−0.084, −0.050] | −0.068 | −0.064 | 0 | −0.156 | — |
| V8-D original (stale RSI) | 986 | +0.024 | −0.047 | [−0.120, +0.027] | −0.034 | −0.081 | 2 | +0.057 | no |
| V8-D corrected | 820 | −0.010 | −0.081 | [−0.161, +0.001] | −0.099 | −0.032 | 1 | +0.047 | no |
| Opening-range breakout | 969 | +0.024 | −0.050 | [−0.127, +0.026] | +0.018 | −0.189 | 2 | −0.070 | no |
| EMA trend re-cross | 563 | +0.031 | −0.042 | [−0.135, +0.051] | −0.043 | −0.039 | 2 | −0.105 | no |
| Bollinger mean reversion | 1,302 | −0.071 | −0.152 | [−0.219, −0.086] | −0.190 | −0.072 | 0 | −0.141 | no |
| Morning momentum | 601 | +0.055 | −0.040 | [−0.132, +0.060] | −0.032 | −0.058 | 2 | +0.102 | no |
| Gap-and-go | 190 | −0.212 | −0.276 | [−0.432, −0.117] | −0.349 | −0.099 | 1 | −0.083 | no |

* The random-entry control averages +0.026 R gross (single draws range −0.09…+0.21: single-seed noise is as large as the effects being tested), so the simulator is unbiased.
* **No candidate qualifies.** Gross edges are indistinguishable from zero (two are significantly negative); a ~1.5 bp cost (≈ 0.1 R) turns every one net-negative.
* **Power:** with ~800–1,000 trades the 95 % CI half-width is ≈ 0.08 R, so only an edge of ≈ 0.12 R/trade or more is reliably detectable — about the size of the cost. One year of data cannot confirm (or rule out) a small edge.
* Option P&L (theta, gamma, spread) is **not** simulated: the repo has no historical option prices and inventing them is not acceptable. Underlying edge is necessary, not sufficient.

## 4. What this means / what to do
1. Raise the paper capital (≥ ₹100,000 reproduces the backtest's sizing) so valid signals are not discarded as "0 lots".
2. **Do not run V8-D live.** Its real-data backtest is significantly negative; the live-order gates stay closed and untouched.
3. Treat paper trading as data collection: with all six symbols, the corrected quote-age logic and an adequately funded account it will record real option premiums for every signal. That dataset (and ≥ 2–3 years of historical option chains) is what a genuine strategy search needs; the harness above is ready to extend to option-level P&L when that data exists.
4. A candidate should replace V8-D only after clearing the fixed rule in §3 on option-level data. Anything weaker would be curve-fitting to one year of noise.
