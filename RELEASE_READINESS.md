# RELEASE READINESS — Trading-Bot-V1 (PAPER RELEASE)

Verdict per item: **PASS / FAIL / BLOCKED** with evidence. No vague claims.
Overall: **PASS for continued PAPER production operation.** LIVE trading:
**BLOCKED (by design — see §Live).**

## Backend quality

| Check | Status | Evidence |
|---|---|---|
| Full backend QA green | **PASS** | `650 passed, 0 failed, 0 errors, 0 skipped` (111.55 s) — true pytest after removing the `pytest.py` shadow |
| Real pytest in use (not the shim) | **PASS** | root `pytest.py` renamed `legacy_pytest_shim.py`; `pytest --version` → 9.1.1 from site-packages |
| New production regression suite | **PASS** | 22/22 in `backend/tests/test_production_hardening_regression.py` |
| Restart/recovery tests | **PASS** | position+extra hydration, equity/counters restore, new-day rollover, EOD-after-restart |
| Failure-injection tests | **PASS** | duplicate signal ×100; intent-retry overwrite; broker orphan; ledger orphan; API-failure paths |
| Worker lifecycle e2e | **PASS** | `test_paper_worker_e2e.py` 6/6 (spawn/duplicate-start/stop/kill/test-signal) |
| Node bridge round-trip | **PASS** | `test_node_bridge.py` 4/4 |
| Test isolation / offline enforcement | **PASS** | conftest + pytest.py shim envs: `TRADING_BOT_OFFLINE_TESTS=1`, token cleared, per-process DB |

## Paper / live safety

| Check | Status | Evidence |
|---|---|---|
| Paper mode cannot call real Upstox order placement | **PASS** | trip-wire mocks on `UpstoxClient.place_order` + `Session.post/request` → zero calls during full entry+exit flow; plus hard offline guard in `place_order` |
| Paper reconcile never touches Upstox | **PASS** | `PaperBroker` type enforced; `test_paper_reconcile_rejects_live_client` |
| Live START refused | **PASS** | `bot_control`, `node_bridge`, `worker_manager` all refuse `TRADING_MODE=live` (tested) |
| Live engine never uses PaperBroker | **PASS** | `RuntimeError` cross-check in `TradingEngine.__init__` (tested) |
| Unknown strategy fails closed | **PASS** | `require_paper_env`, pipeline identity check, scanner gate |
| Missing lot size / invalid qty / stale quote fail closed | **PASS** | `INVALID_LOT_SIZE`, `INVALID_QUANTITY`, quote-age>30 s, stale candles>900 s |
| Kill switch persists and blocks | **PASS** | `test_kill_switch_persists`, `test_kill_switch_survives_new_db_handle` |
| V8-D parameters unchanged | **PASS** | strategy file untouched this pass (git diff shows no change to `v8d_strategy.py` parameters) |

## Database

| Check | Status | Evidence |
|---|---|---|
| WAL + busy timeout | **PASS** | `test_sqlite_wal_and_busy_timeout_applied` |
| Atomic multi-step writes | **PASS** | `_transaction()` ctx; counters upsert-in-transaction test |
| Idempotent intents, monotonic order-id | **PASS** | `test_order_intent_insert_is_idempotent_under_retry` |
| Schema migration on existing DBs | **PASS** | additive `positions.extra` via `PRAGMA table_info` check; `daily_counters` CREATE IF NOT EXISTS |
| Connection hygiene | **PASS** | `close()` added; worker closes on stop; `test_close_is_idempotent_and_releases_file` |

## Market data / contracts / risk / EOD

| Check | Status | Evidence |
|---|---|---|
| Stale/future/corrupt candles rejected | **PASS** | `candles_are_fresh` tests; scan refuses |
| Intraday paper exits fire on real quotes | **PASS** | worker `_evaluate_open_exits` rewritten to `get_quote_by_instrument_key` (audit C1) + SL/target/trailing tests |
| EOD deterministic, never invents prices | **PASS** | EOD tests incl. restored-position close at provided mark |
| Startup-after-EOD cannot enter | **PASS** | `test_worker_started_after_eod_refuses_new_entries` |
| Holidays modeled | **FAIL (known gap)** | NSE holiday calendar not modeled; stale-data gate is the only protection (audit M3) |
| Token lifecycle fail-closed | **PASS** | token guard + tiered resolution tests (11/11) |

## Frontend

| Check | Status | Evidence |
|---|---|---|
| Clean install | **PASS** | `npm ci` → 325 packages, no registry errors, no private URLs in lockfile |
| Production build | **PASS** | `npm run build` success (31.6 s, dist emitted) |
| Typecheck | **PASS** | `tsc --noEmit` clean |
| API contract honesty | **PASS** | `/api/paper/positions` real ledger + `paper_runtime_attached`; exit endpoint queues for real; health shows worker truth |

## Deployment & packaging

| Check | Status | Evidence |
|---|---|---|
| systemd unit present (paper-enforced) | **PASS** | `deploy/systemd/upstox-bot.service` |
| Nginx TLS + WS config present | **PASS** | `deploy/nginx/upstoxbot.conf` |
| `.env.example` safe paper defaults | **PASS** | rewritten; no credentials; precedence documented |
| Docs: audit / QA / deploy / runbook | **PASS** | PRODUCTION_ENGINEERING_AUDIT.md, PRODUCTION_QA_REPORT.md, PRODUCTION_DEPLOYMENT.md, PRODUCTION_RUNBOOK.md |
| Secrets not committed | **PASS** | `.gitignore` covers `.env`, `*.db`, `data/`, tokens, logs; token no longer auto-written to `.env` |
| Historical data handling | **PASS** | caches under `real_data/`+`data_cache/` gitignored; loaders refuse missing data (`INVALID_DATA`) |
| Root duplicate frontend isolated | **PASS (documented)** | production frontend is `frontend/`; root tree only serves the dev gateway build (audit M1) — not deleted to avoid breaking root build script |
| Backend clean venv install | **PASS** | CI workflow installs requirements on 3.11; suite also green on local 3.14 |
| Final ZIP integrity | **PASS** | see final report (path, size, SHA256) |

## Remaining BLOCKED items

1. **LIVE trading — BLOCKED (permanent until a dedicated release).** No live
   order has ever been placed by this codebase; live gates are tested, but the
   live path is not exercised against the exchange. Enabling live requires a
   separate release: authenticated control plane, holiday calendar, live-order
   E2E in a broker sandbox, and operator sign-off.
2. **Control endpoints unauthenticated — BLOCKED for public exposure.** Keep the
   API behind VPN/Nginx allowlist (current deployment). Add auth before any
   internet-facing control surface.
3. **Holiday calendar — FAIL/gap** documented above; not blocking paper (stale
   gate protects), required before live.

## Sign-off summary

The objective "a professional team willing to operate it" is met for paper
operation: deterministic failure modes, durable state, honest health reporting,
traceable decisions (PAPER_AUDIT/PAPER_EXIT + order_intents), restart-safe risk
limits, and a QA suite that now fails the classes of bugs that recently hit
production (ledger/broker divergence, restart state loss).
