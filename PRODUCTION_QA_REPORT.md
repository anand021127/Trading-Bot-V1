# PRODUCTION QA REPORT — Trading-Bot-V1

Date: 2026-09-24 · Runner: true `pytest` (the repo-root `pytest.py` shim was
shadowing the real pytest package and was renamed — see audit C5)

## 1. Headline numbers

| Metric | Before hardening | After hardening |
|---|---|---|
| Collected | 629 | 651 |
| **Passed** | 620 | **650** |
| **Failed** | 11 | **0** |
| **Errors** | 8 | **0** |
| Skipped | 0 | 0 |
| New regression tests | — | +22 (`test_production_hardening_regression.py`) |

Full-suite evidence: `python -m pytest backend/tests -q --tb=no -p no:cacheprovider -o addopts=`
→ `650 passed, 1 warning in 111.55s`.

## 2. Regression tests added (22) — mapped to production bugs

| Test | Bug class covered |
|---|---|
| `test_restart_restores_realized_equity_and_daily_counters` | C2 — restart reset risk limits |
| `test_restart_new_day_resets_daily_counters_but_keeps_equity` | C2 — day-rollover semantics |
| `test_restart_restores_position_risk_state_from_extra` | C3 — SL/target/lot lost on restart |
| `test_exit_after_restart_uses_restored_stop_and_persists_once` | C3 + duplicate-exit guard |
| `test_manual_exit_queue_deduplicates_and_executes_once` | C4 — fake exit endpoint |
| `test_manual_exit_for_unknown_position_is_safe_noop` | C4 — unknown symbol safety |
| `test_trading_router_exit_endpoint_queues_when_runtime_attached` | C4 — endpoint actually works |
| `test_paper_place_never_reaches_real_upstox_place_order` | paper/live contamination |
| `test_offline_guard_blocks_real_upstox_place_order_even_when_called_directly` | H4 — offline guard |
| `test_order_intent_insert_is_idempotent_under_retry` | C9 — retry overwrote intent |
| `test_duplicate_signal_100x_never_creates_two_positions` | idempotency ×100 |
| `test_sqlite_wal_and_busy_timeout_applied` | H1 — SQLITE_BUSY |
| `test_close_is_idempotent_and_releases_file` | H3 — leaked handles |
| `test_daily_counters_atomic_increment` | H2 — atomic counters |
| `test_pid_liveness_current_process_true` | C6 — Windows PID probe |
| `test_pid_liveness_bogus_and_invalid` | C6 |
| `test_worker_status_reports_spawned_worker_alive` | C6 — false "dead" report |
| `test_worker_started_after_eod_refuses_new_entries` | startup-after-EOD |
| `test_startup_before_market_open_does_not_trade_via_cutoff_rule` | pre-open determinism |
| `test_reconcile_orphan_broker_position_fails_closed` | fail-closed reconcile |
| `test_reconcile_self_heals_ledger_orphan_by_hydration` | the NSE_FO\|69780 incident class |
| `test_eod_square_off_closes_restored_position_at_valid_mark` | EOD after restart |

## 3. Failure-injection coverage (offline)

* Duplicate signal ×100 → 100/100 rejected, exactly one position/one trade row.
* Order-intent retry overwrite → impossible (`INSERT OR IGNORE`).
* Broker order-id clear attempt → blocked (monotonic attribution).
* Expired/missing token at paper entry → rejected (existing + hardened tests).
* Broker positions API failure → `broker_positions_unavailable` fail-closed (existing).
* Ledger↔broker quantity mismatch → `STOP_NEW_ENTRIES` latched (existing).
* Broker-only orphan → fail closed (new).
* Ledger-only orphan (restart incident) → self-heals via hydration (new).
* Worker spawn / duplicate start / stop / kill (subprocess, real) → 10/10 e2e tests.
* EOD cutoff rejection at 15:30 IST start → `EOD_CUTOFF`.
* Stale candles (>900 s), future candles, insufficient bars → scan refuses (existing).
* Corrupt OHLC → rejected before strategy evaluation (existing).
* Contract metadata missing lot size → `INVALID_LOT_SIZE` rejection (existing).
* Notional above allocation cap → rejected (proved live during this pass: the
  regression fixture itself was rejected at ₹19,305 > ₹18,000 before being fixed).
* `TRADING_BOT_OFFLINE_TESTS=1` → `UpstoxClient._get` and `place_order` both raise
  before any socket use; mocked `requests.Session.post` proves zero network writes.

## 4. Restart / recovery drills

* Paper worker subprocess lifecycle: spawn → heartbeat → duplicate-start guard →
  SIGTERM stop → kill switch → reset (`test_paper_worker_e2e.py`, 6 tests) — all pass.
* Position hydration after restart with full risk state (new).
* Equity/counters restore after restart (new) + new-day rollover (new).
* Kill switch persistence across a brand-new DB handle (existing).
* Node bridge status/start/stop/kill/reset round-trip (existing, 4 tests).

## 5. Paper-safety verification

* Zero calls to real Upstox order placement from any paper path — proven by
  mocking `UpstoxClient.place_order`, `Session.post`, `Session.request` with
  trip-wires while executing entry + intraday exit end-to-end.
* Paper reconcile never contacts Upstox positions (`PaperBroker` type enforced).
* Strategy gate: any strategy other than `V8_D_PULLBACK_ATM` refuses to start
  the paper runtime, refuses pipeline submission, refuses scanner evaluation.

## 6. Live-safety verification (static + mocked; live NOT enabled)

* `bot_control.start` refuses `TRADING_MODE=live` (tested).
* `node_bridge.cmd_start` refuses live (tested).
* `worker_manager` preflight refuses live (tested via bridge).
* `TradingEngine` live construction refuses `paper_mode=False` mismatch and
  requires explicit product (existing tests).
* Live-order path additionally blocked by the offline guard under test env.

## 7. Frontend verification

* `npm ci` clean install: 325 packages, registry-clean lockfile (no private URLs).
* `npm run build` (Vite production): success, ~31.6 s, dist emitted.
* `npm run typecheck` (`tsc --noEmit`): clean.
* Contract checks: paper positions now expose SL/target/lot + `paper_runtime_attached`;
  manual exit endpoint performs a real queue+execute; health exposes worker PID,
  heartbeat age, pipeline_ok, broker auth, market-feed state.

## 8. Clean-install verification

* Backend: CI installs `backend/requirements.txt` on Python 3.11 (`.github/workflows/python-tests.yml`)
  and runs true pytest. Local suite executed against Python 3.14 too — 650 pass.
* Frontend: clean `npm ci` + build + typecheck verified locally on Node 22.
* `.env.example` rewritten: safe paper defaults, precedence rules, test switches documented.

## 9. Deployment checks

* New `deploy/systemd/upstox-bot.service` (paper-mode enforced, hardened unit).
* Nginx config present (`deploy/nginx/upstoxbot.conf`, TLS + WS upgrade).
* Vercel frontend config present (`frontend/vercel.json`, SPA rewrites + API proxy).
* Deployment command sequence documented in PRODUCTION_DEPLOYMENT.md.

## 10. Known-good environment notes

* Windows dev machine: PID liveness and handle-hygiene fixes make the suite green
  here too; worker e2e tests pass (~72 s).
* pytest must be invoked with `-p no:cacheprovider` against OneDrive-synced paths
  to avoid watcher contention (documented in runbook troubleshooting).
