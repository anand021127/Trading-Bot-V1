# Scanner / Runtime QA Report — "RUNNING but no scan recorded"

Scope: paper mode, `TRADING_STRATEGY=V8_D_PULLBACK_ATM`. V8-D parameters/entry logic,
risk controls, kill switch and contract-metadata lot sizing are **unchanged**. No test
signals, synthetic market data or fake trades were introduced in any production path.

## Pipeline trace and where it stopped

UI Start → `POST /api/bot/start` → **(stop #1)** → paper_worker.py → `_tick` →
**(stop #2)** scanner arming → `PaperMarketScanner.scan_once` → candles → freshness →
expiry → option chain → V8-D → AI gate → risk → `ExecutionPipeline` → `PaperBroker` →
SQLite → dashboard/Copilot (**stop #3** — display).

| # | Defect (found by code trace) | Effect |
|---|---|---|
| 1 | `/api/bot/start` only set the `BotState` DB flag. Nothing in the API, `render.yaml` or the systemd unit ever spawned `paper_worker.py` (only the legacy Node bridge did). | Dashboard "RUNNING", **zero scan iterations**. |
| 2 | `_init_market_scanner()` ran once at worker start; with no token it set `disabled_no_token` and the loop then skipped scanning **silently forever**; a token appearing later was never picked up. Explicit token was also pinned into the client, so a rotated token 401'd until restart. | No scan record, no error. |
| 3 | "Scanner RUNNING" / "WebSocket STREAMING" came from the **API process's** LiveScanner and WS object, not the paper worker. | Header green while the worker never scanned. |
| 4 | Scan errors wrote only `paper_worker_last_scan`, never `..._detail`; detail was `json.dumps()[:2000]` (can be cut mid-token = corrupt JSON); a candle-fetch failure was recorded `scanned=False` ("did not run"). | Copilot: "No scan has been recorded" although scans ran/failed. |
| 5 | No scan schedule (Upstox called every 2 s tick); heartbeat only written after a whole tick, so a long scan made a live worker look dead. | Rate-limit risk, false "worker down". |
| 6 | `paper_trades_today` was a lifetime counter that never reset. | Latent: after N cumulative trades every later day rejected "daily limit". Now uses the runtime's day-scoped counter. |
| 7 | `pid_is_alive` treated zombie processes as alive (`os.kill(pid,0)`). | Dead worker reported alive (this is why `test_kill_stops_worker` failed at baseline). |
| 8 | `date.today()` (server-local) in the Upstox client. | Wrong trading day on non-IST hosts. |
| 9 | `backend/api/main.py` used `logging` without importing it. | NameError in a startup-recovery except path. |

## Fixes
- Start now spawns/verifies the worker (or fails honestly and rolls the flag back); API watchdog
  respawns a dead worker while the bot is flagged running (bounded 1/60 s, max 5, never under kill switch).
- Worker: scheduled scans (`PAPER_SCAN_INTERVAL_SEC`, default 5 s), scanner re-arm every 30 s,
  **every** iteration persisted as valid, size-bounded JSON with `seq`, IST time, session, candle
  count/freshness, expiry, option-chain size, selected contract, decision, rejection,
  risk/execution decision, duration, next-scan time, redacted error; exceptions logged + persisted
  + `paper_worker_last_error`; separate heartbeat thread that still goes stale if the loop hangs.
- One authority for status: `backend/paper/scan_state.py::compute_runtime_state` →
  STOPPED · STARTING · STARTED_WORKER_NOT_RESPONDING · RUNNING_SCANNING ·
  RUNNING_WAITING_FOR_MARKET · RUNNING_NO_SIGNAL · RUNNING_DATA_ERROR · RUNNING_SCANNER_ERROR.
  Exposed by `/api/bot/status`, `/api/overview`, `/api/bot/operations`, Copilot context, gate chain.
- Data/scanner failures are reported as such (V8-D "NOT_EVALUATED"), never as a strategy outcome.
- Frontend: Overview/Copilot show the real state; Copilot + Layout made responsive (sidebar collapses
  below 1024 px so phone "Desktop site" mode gets the drawer; no horizontal overflow; 44 px touch targets).

## Operating it
`GET /api/bot/status` → `runtime_state`, `runtime_summary`, `runtime.last_scan`.
Worker log: `data/paper_worker.log`. Expected outside market hours:
**RUNNING_WAITING_FOR_MARKET** (scan loop executing; not trading because market closed) — this is
distinct from **STARTED_WORKER_NOT_RESPONDING** / **RUNNING_SCANNER_ERROR** (loop not executing).

## Limits of this QA (honest)
- Verified against fakes and a real spawned worker process **without an Upstox token**; **not** against
  live Upstox or live market hours (no token/network in the QA sandbox). Real candle freshness,
  option-chain size and a live V8-D signal must be confirmed on your machine during 09:20–14:45 IST.
- Responsive check was static (source + compiled CSS); no headless browser could be installed, so no
  screenshots were rendered at phone widths.
