# PRODUCTION RUNBOOK — Trading-Bot-V1

Operating assumption: PAPER MODE. Timezone: IST (Asia/Kolkata). DB:
`data/trading_bot.db` (WAL). Worker heartbeat keys live in the `settings` table.

## 0. Operations dashboard & control plane (PHASE 5.3)

The one-screen operational view is the **Operations** page (`/operations`,
served by `GET /api/bot/operations`) — mode, strategy, AI, broker, market
session, API/data health, reconciliation state+age, kill switch, and the
LIVE readiness verdict with exact blocked reasons, refreshed every 5 s.

Server-side controls (all guarded by `CONTROL_TOKEN` when set — see
`backend/api/control_auth.py`):

* `POST /api/bot/ai-toggle` `{"enabled": true|false}` — flips the AI trading
  decision layer at runtime (DB override read EVERY scan tick; no restart).
  AI ON never bypasses hard risk.
* `POST /api/bot/mode` `{"mode": "paper"|"live"}` — PAPER is always allowed;
  LIVE is evaluated by `backend/execution/live_gate.py` against REAL state
  (auth, funds, instrument master, reconciliation fresh-OK, risk config,
  kill switch clear, strategy == V8_D_PULLBACK_ATM, all six underlyings
  resolvable). LIVE BLOCKED lists the exact failing checks. Even when
  LIVE READY, arming execution still requires a worker restart with
  `TRADING_MODE=live` — two-step arming, a UI click alone never arms live.
* `POST /api/bot/kill` / `/api/bot/reset-kill` — unchanged (§11 runbook below).

AI scan-budget: `AI_DECISION_BUDGET_SECONDS` (default 10) bounds how long a
scan may block on one AI decision; a slow decision returns typed WAIT and
resolves via setup dedup on a later tick. `AI_DECISION_MAX_TOKENS=64` bounds
inference worst-case.


Fast triage one-liners:

```bash
systemctl status upstox-bot
curl -s localhost:8000/api/bot/status | python3 -m json.tool
tail -50 data/paper_worker.log
sqlite3 data/trading_bot.db "SELECT key,value FROM settings WHERE key IN
 ('paper_worker_heartbeat','paper_worker_pid','paper_worker_status','persistent_kill_level');"
```

Heartbeat thresholds: age < 20 s = healthy; 20–60 s = degraded/watch;
> 60 s = treat worker as dead.

---

## 1. Worker dies (heartbeat stale / pid dead)

1. Read `data/paper_worker.log` tail for the crash (`Fatal:`, `Traceback`).
2. Open positions are safe in SQLite (`positions` table) — the worker hydrates
   them on next start (SL/target/lot restored).
3. Restart: dashboard **Start**, or `POST /api/bot/start` (idempotent;
   duplicate start is refused). Lock file `*.paper_worker.lock` is cleared
   automatically when the PID is dead.
4. Verify: `/api/bot/status` → `worker_alive true`; log shows
   `Paper ledger hydrate: restored N position(s)` (N ≥ 0) — this is the
   post-incident self-heal, not an error.
5. If it crash-loops: STOP. Check `paper_worker_last_error` setting; engage the
   kill switch (`/api/bot/kill`) if a position is open and you cannot recover
   the worker within the session; positions still square off at EOD only when
   the worker runs — a dead worker cannot square off. **A dead worker with an
   open paper position must be revived before 15:15 IST or manually squared off.**

## 2. API dies (dashboard down)

* Trading continues: the paper worker is an independent process and reads
  BotState/kill from SQLite, not from the API.
* Restart with `sudo systemctl restart upstox-bot.service`.
* You cannot Start/Stop the worker while the API is down — use §1 manual spawn.

## 3. Token expires / invalid (401 UDAPI100050)

* Symptoms: `market_data` health shows `AUTHENTICATION_FAILED`; log lines
  `AUTH_FAILURE ... reason=expired`; scanner entries show no-data.
* Paper fills do NOT need the token, but market-driven entries and EOD marks do.
  Without a token the bot will not enter new trades (fail closed) and exits fall
  back to the last valid recorded mark.
* Fix: Settings → reconnect Upstox (OAuth v3 approval flow) → the new token
  propagates to engine/WS without restart (`invalidate_old_token_references`).
* Verify: `/api/settings` broker status `CONNECTED`; WS health `LIVE`.

## 4. WebSocket disconnects

* The feed client auto-reconnects with bounded backoff; status is visible in
  `/api/health` → `websocket`.
* < 5 min: wait. Repeated 401 on reconnect: do §3.
* The scanner independently gates on candle freshness (`stale_candles_age_sec`),
  so trading is already fail-closed while disconnected. Do not force entries.

## 5. Reconciliation fails (`Reconcile not ok` / STOP_NEW_ENTRIES)

1. Read the mismatch in the log:
   `Paper position mismatch ledger={...} paper_broker={...}`.
2. Ledger-only keys (in `local`, absent from `remote`): the worker hydrates these
   automatically — if they keep reappearing, the hydration failed; check the
   startup log for `Paper startup hydrate deferred`.
3. Broker-only keys (in `remote` only): **fail closed by design.** This means
   in-memory state holds a position SQLite does not know about. Record both
   dicts, then restart the worker — after restart the broker is rebuilt purely
   from SQLite, so the orphan disappears and reconcile goes green.
4. Quantity mismatch on the same key: restart the worker (SQLite wins on start).
   If it persists, capture both values and file an incident — do not hand-edit.
5. `STOP_NEW_ENTRIES` latches the kill switch (`persistent_kill_level` in
   settings). After fixing, reset explicitly: `/api/bot/reset-kill`, then Start.

## 6. Database fails / locked / corrupt

* SQLITE_BUSY: should not occur (WAL + 30 s busy timeout). If seen, check for a
  second worker holding the lock file (`lsof data/trading_bot.db*`), stop
  duplicates.
* Disk full: free space, restart service.
* Corruption (rare): stop service, back up the whole `data/` dir, restore the
  latest `.backup` file (§11 of deployment guide), restart, verify reconcile.

## 7. Market data stale

* Log/scan reason `stale_candles_age_sec=...` → the scanner refuses entries
  (fail closed) — no action needed intraday except monitoring.
* Persisting beyond 30 min during market hours: check token (§3), check Upstox
  status, restart worker if the feed client wedged.

## 8. Position orphaned

* Paper: reconciliation handles both directions (§5). No manual DB edits.
* If EOD passed with a dead worker: start the worker; its first tick runs
  `run_eod()` → `EOD_SQUARE_OFF` at the last valid mark; verify a `PAPER_EXIT`
  line and `positions` table empty.
* Live (future): never resolve an orphan by re-placing orders by hand — follow
  the live-mode procedure in RELEASE_READINESS before live is ever enabled.

## 9. Server reboots

1. systemd auto-starts the API (`Restart=always`, `enable --now`).
2. Start the worker (dashboard Start) — it rehydrates open positions.
3. Verify §5/§8 outcomes; check `uptime` matches reboot window.
4. If the reboot happened after 15:15 IST: entries stay blocked by EOD_CUTOFF;
   open positions square off on the worker's first tick.

## 10. Deployment fails / rollback

* Keep the previous release ZIP. `sudo systemctl stop upstox-bot`, replace the
  application tree **preserving `data/` and `.env`**, `daemon-reload` if the unit
  changed, start, run the smoke checklist (deployment guide §13).
* Schema: migrations are additive (`positions.extra`, `daily_counters`); older
  code tolerates the new columns. Rollback is therefore safe.

## 11. Unexpected P&L appears

1. Pull the audit trail:
   `grep PAPER_EXIT data/paper_worker.log | tail -20` and the `trades` table
   (`SELECT id,symbol,quantity,price,exit_price,exit_reason,net_pnl FROM trades
   ORDER BY timestamp DESC LIMIT 20;`).
2. Cross-check quantity vs lot size (`extra.lot_size` on the position at entry).
3. Common causes (all tested): EOD square-off at last valid mark (never entry
   price unless no mark existed), slippage/fee model in `CostConfig`, restored
   position exiting at its restored SL.
4. If P&L implies a duplicate exit or duplicate position: run the duplicate-signal
   drill (`pytest backend/tests/test_production_hardening_regression.py -k
   duplicate -q`) and attach both `PAPER_AUDIT` signal_ids to the incident.

## 12. Duplicate order suspected

* Paper: impossible by construction (idempotent intents + one position per
  instrument + 100× duplicate-drill test). Verify anyway:
  `SELECT signal_id,broker_order_id,status,created_at FROM order_intents ORDER BY created_at DESC LIMIT 20;`
  A repeated signal_id with `status=INTENT` and **one** trade row = correctly
  deduped. Two trade rows for one signal_id = genuine bug: capture
  `last_audit` + both trade ids, kill switch, file incident.
* Live (future): compare `order_intents.broker_order_id` against the broker's
  order book before any manual action; never "cancel everything" blindly.

---

### Escalation notes

* Kill switch levels: `STOP_NEW_ENTRIES` (auto, reconcile) → `FULL_SYSTEM_STOP`
  (dashboard Kill). Reset only after root cause understood.
* All trading decisions are reconstructible from `PAPER_AUDIT` /
  `PAPER_EXIT` log lines + `order_intents` + `trades`. Include these in every
  incident report.

### 0.1 Entry window (PHASE 5.3B — lifecycle parity, all paths)

New positions are accepted ONLY inside the entry window **09:20–14:45 IST**
(the `session_manager` policy). This is now enforced identically in all three
engines:

| Path | Gate | Outside-window behavior |
|---|---|---|
| Live (`trading_engine`) | calendar entry window (pre-existing) | no new entries; positions managed |
| Paper (`paper/market_scan_loop.py`) | `entry_window_closed:<HH:MM>` (NEW, fail-closed) | scan refused before evaluation; `submit_entry` still runs for square-off/monitoring |
| Backtest (`backtest/engine.py`) | `ENTRY_SESSION_RESTRICTED` (NEW; opt-out `enforce_entry_session_window=False`) | signal rejected and counted on the result |

Backtest result now also reports `entry_session_rejections`,
`positions_forced_expiry_closed`, `lifecycle_violations_prevented`. Any open
option position is force-closed no later than its ACTUAL contract expiry
(`EXPIRY_FORCED_CLOSE`, exit priced at the last observed option premium);
`BACKTEST_END` never overrides expiry. If a future backtest CSV shows these
counters non-zero, treat the underlying data/coverage problem as a blocker —
do not trade on it.


## Scanner runtime states (PHASE: scanner QA)
`GET /api/bot/status` → `runtime_state`. Meaning and action:

| State | Meaning | Action |
|---|---|---|
| STOPPED | Flag off / kill switch | Start |
| STARTING | Worker spawning, first scan pending | wait ≤ ~1 min |
| RUNNING_SCANNING / RUNNING_NO_SIGNAL | Scans executing; V8-D evaluated | none (NO_SIGNAL is a valid outcome) |
| RUNNING_WAITING_FOR_MARKET | Loop executing; market/entry window closed | none |
| RUNNING_DATA_ERROR | Scan ran but Upstox data/token unusable | fix token (Settings) — no restart needed, scanner re-arms ≤30 s |
| RUNNING_SCANNER_ERROR | Scan iteration raising / loop stalled | read `paper_worker_last_error`, `data/paper_worker.log` |
| STARTED_WORKER_NOT_RESPONDING | Flag says running, no live worker | Press Start again (repairs) — watchdog also respawns |


## Reading the Decision Pipeline (Overview + Copilot → Live context)
`GET /api/bot/status` → `runtime.pipeline` (also in `/api/overview`):

| Field | Values |
|---|---|
| scanner | RUNNING · STOPPED · STARTING · NOT RESPONDING · RUNNING — DATA/SCANNER ERROR |
| market | LIVE · MARKET CLOSED · LIVE — ENTRY WINDOW CLOSED |
| latest_signal | BUY CE/PE · NO SIGNAL (+ actual reason) · NOT EVALUATED — MARKET CLOSED/DATA PROBLEM/SCANNER ERROR |
| ai_decision | APPROVED · REJECTED · WAIT (NO TRADE) · UNAVAILABLE — FAILED SAFE · DISABLED · NOT EVALUATED |
| risk_check | PASS · REJECTED · NOT EVALUATED (AI stopped it earlier / no signal) |
| execution | FILLED (PAPER) · REJECTED · ERROR · NO TRADE |

`AI DISABLED` means `AI_DECISION_ENABLED=false` (or the Operations toggle is off): trades run on V8-D + risk controls only.
