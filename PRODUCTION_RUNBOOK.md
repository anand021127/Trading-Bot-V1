# PRODUCTION RUNBOOK — Trading-Bot-V1

Operating assumption: PAPER MODE. Timezone: IST (Asia/Kolkata). DB:
`data/trading_bot.db` (WAL). Worker heartbeat keys live in the `settings` table.

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
