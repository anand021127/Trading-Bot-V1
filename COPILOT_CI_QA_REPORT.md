# Copilot / CI / Backtest-Parity QA Report

## 1. CI — repo hygiene + secret scan
Root causes in `.github/workflows/ci.yml`:
1. tracked-file regex `\.env\.` matched the safe template `.env.example`;
2. packaging check flagged any file whose name merely *contains* `.env`;
3. credential-literal scan used `grep -L` (lists files **without** a match — inverted).

Fix: one tested implementation, `scripts/repo_hygiene_check.py` (`--tracked`, `--zip`, `--paths`).
Allowed: exactly `.env.example` (`ALLOWED_TEMPLATES`, never a blanket `.env.*`).
Rejected: `.env`, `.env.local/.production/.development/.staging/.test`, `*.env`, token/credential JSON,
SQLite/DB (+ `-wal/-shm`), `*.log`, `*.pem/*.key/*.p12`, `id_rsa*`; plus credential literals, JWT-shaped
tokens and private-key blocks in content. The scan is **not** disabled. `scripts/make_final_zip.py` and
`scripts/audit_final_zip.py` now use the same classifier (`make_final_zip` previously only excluded the
exact name `.env`, so `.env.production` would have shipped). Tests: `backend/tests/test_repo_hygiene.py`.
The lint step is no longer `continue-on-error`.

## 2. Copilot async test
`test_submit_returns_job_id_immediately` asserted the POST status was `queued/thinking`; a fast provider is
already `completed`. It also polled *outside* the `with patch(...)` block, so the worker thread could resolve the
real adapter after patch teardown (a second race). Now: POST must be 202 + `job_id` + **no answer** (any of
queued/thinking/completed accepted), polling stays inside the patches, and cases A (slow) / B (instant) /
C (typed failure) / D (cancel) are deterministic (events, not sleeps). No production code was slowed or changed.
The existing cancel test's `sleep(0.2)` was replaced by waiting for provider entry. 25× under CPU load: 0 failures.

## 3. Copilot UI
Chat-first (`frontend/src/pages/Copilot.tsx`): ≥1024px chat ~70% | compact, independently-scrolling Live Context
~30%; <1024px chat is full width and context is a bottom-sheet dialog. Composer pinned to the chat panel; Enter
sends, Shift+Enter newline, Send/Stop/Clear/Retry, thinking indicator, typed errors, six quick prompts, ARIA
labels, ~44px touch targets. Live Context (`components/copilot/LiveContext.tsx`) renders the authoritative
`GET /api/copilot/context` in 10 compact sections (detail collapsed) and shows mismatches/errors prominently
only when present. The Copilot provider (chat LLM) and the *AI Trading Decision engine* are labelled separately.
Async handling: a POST that already says `completed` triggers an immediate status fetch with no fake "Thinking…".
Also fixed: **Clear** now starts a new server session (the old id used to be reused).
Frontend tests: Vitest + Testing Library (`npm test`, 28 tests) incl. mutation-checked cases A–D.

## 4. Backtest + strategy audit
Evidence: uploaded 171-trade CSV recomputed independently — P&L arithmetic exact (gross, fees, slippage, net);
SL floor 28%/cap 40% and 1.5R target match the documented spec on every trade; lot sizes follow exchange
revisions from contract metadata (NIFTY 75→65, BANKNIFTY 35→30, …); no entry outside 09:20–14:45, no exit after
15:15, no expiry before entry, no overlapping same-underlying positions; expiry weekdays correct per index.
File name `…nifty50_banknifty_finnifty__3_…` = first 3 symbols + "+3" (six symbols) — not a bug.

**One genuine defect found and fixed (backtest only):** V8-D's `max_daily_trades` gate was evaluated against a
snapshot of `trades_opened_today` taken before any position on the bar opened. When several symbols signalled on
the *same bar*, all passed and the backtest opened more trades than the cap (reproduced: 6 trades, cap 3).
Paper and live fill sequentially (each fill increments `trades_today` before the next evaluation), so they were
already correct. The backtest now re-asks the **strategy** with the up-to-date count when the counter moved; the
cap remains only in the strategy (no constant duplicated). Impact on the supplied CSV: none (12 same-bar entries,
none exceeded the cap). V8-D parameters/entry logic, risk, sizing, PaperTradingRuntime/PaperBroker,
ExecutionPipeline/OrderManager, worker and market-data code are **unchanged**.
Parity tests: `backend/tests/test_strategy_mode_parity.py` (identical parameters across the three construction
paths, identical rejection text at the cap in paper vs backtest, cap resets daily, deterministic under symbol
order). Verified to fail on the original engine (4 tests) and pass on the fix.

Observations, deliberately **not** changed (no defect, no spec violation): 57% of trades enter on expiry day
(spec: nearest expiry); the daily strategy cap is 3 while the risk config may say 20 (the strategy cap binds
first in all three modes); the ratchet-then-check within one bar is pessimistic by design.
Not possible offline: a real-data backtest re-run (the repo has underlying candles only; option contracts require
Upstox data and inventing prices is prohibited).
