# Zero BUY signals / zero paper trades — audit, root causes, fixes

V8-D parameters and entry logic are **unchanged** (`backend/strategy/strategies/v8d_strategy.py` is byte-identical to
the version you uploaded). No filter was loosened, no test signal enabled, no synthetic data used.

## 1. Is V8-D "genuinely selective" or is something broken?
Measured on the repo's REAL 5-minute candles (a full year, 6 symbols; `scripts/v8d_signal_audit.py`), evaluating every
completed bar in the 09:20–14:45 entry window with the corrected RSI:

| symbol | bars | valid setups | setups / bar | days with ≥1 setup | first-failing condition when NO_SIGNAL |
|---|---|---|---|---|---|
| NIFTY50 | 15,178 | 385 | 2.5 % | 105 / 233 | trend 73 %, RSI 14 %, reversal 10 %, pullback 3 % |
| BANKNIFTY | 11,152 | 392 | 3.5 % | 96 / 172 | trend 66 %, reversal 16 %, RSI 13 %, pullback 5 % |
| FINNIFTY | 11,152 | 353 | 3.2 % | 90 / 172 | trend 68 %, RSI 14 %, reversal 14 %, pullback 5 % |
| MIDCPNIFTY | 11,154 | 547 | 4.9 % | 118 / 172 | trend 59 %, reversal 18 %, RSI 16 %, pullback 7 % |
| SENSEX | 11,042 | 242 | 2.2 % | 72 / 170 | trend 76 %, RSI 13 %, reversal 9 %, pullback 3 % |
| BANKEX | 11,042 | 398 | 3.6 % | 106 / 170 | trend 66 %, reversal 16 %, RSI 13 %, pullback 6 % |

* The **trend** filter (EMA20/EMA50 separation > 0.15 %, price on the right side of EMA50) is the binding condition
  (it passes on only ~15 % of bars). The pullback band passes ~97 % of the time — it is not a blocker.
* Valid setups are **not rare in real data** (~1–2 per symbol per day on average), so a day with *zero* signals while
  scanning all six symbols would be unusual — whereas on **NIFTY50 alone** about 55 % of days have none.
* An independent check (textbook Wilder RSI, pandas EMA/ATR) shows EMA20/EMA50/ATR are correct and RSI had a bug (below).

## 2. Root causes found (all proven by tests; "real bug?" = yes for each)
| # | Finding | Effect | Fix |
|---|---|---|---|
| 1 | **Paper scanned ONE symbol** (`PAPER_UNDERLYING`, default NIFTY50) although the strategy/backtest universe is six; nothing showed it. | The dominant reason for "no signals": 1 of 6 symbols evaluated. | `PAPER_UNDERLYINGS` (default **all six**), per-symbol isolation + cadence, one shared runtime/AI gate/daily cap. `PAPER_UNDERLYINGS=NIFTY50` restores single-symbol. |
| 2 | **RSI off-by-one**: `rsi()[-1]` was the RSI of the *previous* candle (verified against textbook Wilder, 0.0 difference once aligned). | V8-D read a one-bar-stale RSI. On real data the bug produced **37 % more setups** (3,177 vs 2,317 across the six symbols), so fixing it makes V8-D *stricter*, not looser. Backtests run before this fix used the lagged RSI — re-run them. | `backend/indicators/rsi.py` (shared by backtest, paper, live, AI). |
| 3 | **Forming candle was evaluated** (backtest evaluates completed bars). | Intrabar flicker on a half-built reversal candle; live ≠ backtest. | Completed-candle feed decided from the candle *timestamp* (`backend/strategy/candle_utils.py`); `EVAL_FORMING_CANDLE=1` restores. |
| 4 | **Option quote age was the underlying candle's age**, fed to the contract validator ("reject if > 30 s"). | A genuine BUY passed only ~10 % of the time (first 30 s of a bar) — and **0 %** with completed candles. Found while proving "valid setup → BUY → execution" on real data. | Quote age = age of the option-chain snapshot at submit time (the 30 s limit is unchanged; a slow AI path can still be refused). |
| 5 | **API-process `LiveScanner` could submit paper entries directly** (`runtime.submit_entry`), bypassing the AI Trading Decision gate and racing the worker. | An AI-bypassing second execution path. | Removed: the scanner is signal-only (`execution_status=SIGNAL_ONLY`); the paper worker is the single executor. AST test forbids `submit_entry` there. |
| 6 | `no_trade:NO_SIGNAL` was labelled **SIGNAL_REJECTED** by Copilot (Overview said NO SIGNAL). | Two authorities disagreed; NO_SIGNAL looked like a rejection. | One taxonomy `derive_outcome`: NO_SIGNAL · SIGNAL_REJECTED · AI_REJECTED · RISK_REJECTED · EXECUTION_REJECTED · FILLED (+ MARKET_CLOSED / DATA_ERROR / SCANNER_ERROR); execution shows **NOT ATTEMPTED** when no order was built; final = NO TRADE / FILLED. |
| 7 | Option Scanner EMA / RSI / Volume = N/A for every symbol. | `ema_status`/`rsi_status`/`volume_status` were never assigned in the backend. | Filled from the real V8-D diagnostics (same indicators/thresholds). Volume = "Not used" (V8-D has no volume condition; index candles carry volume 0). Unavailable → N/A with a stated reason. |
| 8 | Copilot "AI provider timed out" looked like the trading AI. | Confusing; a slow chat model also hid the real reason. | Labels are "Copilot AI provider …" with a "separate from the AI Trading Decision gate" note; "why didn't the bot trade / latest rejection" is answered deterministically from the scan record (no model call). |

## 3. What every scan now exposes (per symbol, persisted + API + UI)
EMA20 · EMA50 · EMA separation · RSI · ATR14 (informational: V8-D stops use the *option* ATR) · price · pullback band (CE/PE) ·
trend / pullback / RSI / reversal for **both** CE and PE (with the numbers) · closest side + binding condition · option-chain size ·
ATM strike + CE/PE contract + premium · final decision + reason · AI / risk / execution state · candle completeness.
`explain_pullback` is not a second strategy: a parity test asserts, over every window of a year of real candles for all six
symbols, that its decision and values equal the strategy's own output.

## 4. Proof (tests)
* `test_rsi_indicator.py` — RSI == textbook on real data for all six symbols.
* `test_v8d_diagnostics.py` — parity with the strategy on real windows; valid real setup → BUY (CE and PE); invalid → NO_SIGNAL with the exact
  failed condition; each of trend / pullback / RSI / reversal identified when it is the only failure (real windows).
* `test_v8d_signal_to_execution.py` — real V8-D, real candles, **each of the six symbols**: valid setup → BUY → AI → risk → paper fill;
  invalid setup → NO_SIGNAL → AI not called, `submit_entry` never called, no trade; forming candle excluded; all six scanned independently.
* `test_outcome_states_and_copilot.py` — distinct states, Copilot==pipeline, deterministic why-no-trade, Copilot timeout cannot hide/masquerade,
  quote-age regression (and a genuinely stale quote is still rejected), single execution path, Option Scanner values.
* `test_pipeline_frontend_contract.py` + Vitest — backend-generated pipeline fixture == what the UI renders, value for value.
Mutation-checked: restoring the old RSI, the old quote-age, or the old `no_trade` classification makes these fail.

## 5. Unchanged / not claimed
Unchanged: V8-D strategy file, risk, execution (`ExecutionPipeline`), orders, broker, `PaperTradingRuntime`, `PaperBroker`, kill switch,
position sizing, AI fail-safe, live-order gates. `trading_engine.py` changed only inside `evaluate_configured_strategy` (the
scanner/Copilot *display* evaluator: completed-candle feed + additive diagnostics). The strategy's docstring still describes older
thresholds (RSI 40–60, band ×1.002); the code (CE RSI 45–62, PE 38–55, band 0.985–1.003) is what backtest/paper/live run — left as is.
**Not verified here:** live Upstox responses (whether the intraday API returns the forming bar — hence the timestamp-based rule),
live option-chain contents for SENSEX/BANKEX (BSE_FO), and a real market session. Each symbol's data problems are isolated and shown per symbol.
