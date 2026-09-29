# PHASE B FINAL REPORT — COPILOT FULL BOT CONTEXT + CONFIGURATION/INTEGRATION FORENSIC FIX

Date: 2026-09-29 · Branch: `main` · Working tree only — **no commit, no push, no deploy** (per instruction; review and commit manually). A production ZIP artifact was produced on explicit request — see §20.

---

## 1. Root causes (forensic — every one confirmed in code before fixing)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | Overview showed ₹1,00,000 after saving ₹20,000 | `load_settings()` was env-only; every consumer snapshotted it at import time (`overview`, `bot_control`, `performance`, `trading_engine` module-level `settings = load_settings()`; `PaperTradingRuntime` read `os.environ["TRADING_CAPITAL"]` directly; `paper_worker` had an env equity fallback). Settings-UI saves went to the SQLite `settings_blob` but **nothing at runtime read it**. | ONE resolver: `backend/config/runtime_config.py` — effective = Settings-DB blob deep-merged over env defaults, resolved per call with a 5 s TTL, invalidated on Settings PUT. All consumers migrated. |
| 2 | Max trades 20 saved → bot stopped at 3 | Same as #1 (`MAX_TRADES_PER_DAY=3` env default shadowed the saved blob). | Same resolver; RiskManager/PositionSizer/`build_authoritative_risk_config` now see the saved value. |
| 3 | Copilot: "I'm not aware… / Decisions measured: 0 / no scan result recorded yet" | Copilot context was intent-routed and thin; ai_decision router had its own ad-hoc reason mapping (drift risk); no single authoritative state source. | New `backend/copilot/full_context.py` (one canonical builder) + `GET /api/copilot/context`; chat context always merges the full context. |
| 4 | Performance tab "Network Error" | `DatabaseManager.init_db()` never created `performance_snapshots`; `list_performance_snapshots()` queried it → 500. | Table added to DDL; `list_performance_snapshots` self-heals on legacy DBs (recreates missing table); endpoint returns honest empty payload (`metrics: null`). |
| 5 | Operations 404s | Frontend called `/bot/operations` etc.; backend mounts `/api/bot/*`; API client baseURL is empty in same-origin deploys. | All paths prefixed (`/api/bot/operations`, `/api/bot/ai-toggle`, `/api/bot/mode`, `/api/bot/kill`); regression test greps the frontend for bare `/bot/` paths. |
| 6 | Settings UI capped max trades at 10 | `max={10}` on the input. | `max={20}`. |
| 7 | Operators couldn't see *where* config came from | No provenance anywhere. | `get_config_sources()` per-key labels (`sqlite_settings` / `env_<VAR>` / `default`); exposed in `GET /api/settings/` (`config_sources`), Overview (`capital.source = "runtime_config"`), Operations `runtime_config`, Copilot context. |
| 8 | Saved-vs-runtime mismatch undetectable | — | `detect_config_mismatches()` compares env defaults vs saved blob; shown as a warning banner (Operations + Copilot context `configuration_mismatches`). Distinguishes unreadable DB (warn) from fresh-empty DB (no warn). |
| 9 | "Why didn't we trade?" answers drifted per route | Reason→stage mapping lived in the ai_decision router. | New single authority `backend/copilot/gate_chain.py`; `/api/ai-decision/why-not-traded` delegates (legacy `breakdown` contract preserved). |
| 10 | Backtest numbers in Copilot risked being hardcoded | Context had no live link to the job store. | `_backtest_summary` reads `backend.backtest.job_store.job_store.get_latest()` — dynamic, honest empty state; frozen numbers live only in `strategy_context.BACKTEST_COMPARISON` (labeled historical). |
| 11 | Paper START could succeed with no real executor (found during full-suite verification) | Offline fallback stub in `_paper_runtime_from_app()` looked like a runtime to `start_bot`. | Stub flagged `_synthetic_db_stub`; `start_bot` refuses on it (isinstance-guarded so MagicMock tests keep their contract). |

---

## 2. Files changed

**New backend (3):** `backend/config/runtime_config.py` · `backend/copilot/gate_chain.py` · `backend/copilot/full_context.py`
**New tests (5):** `backend/tests/test_runtime_config_parity.py` · `test_copilot_context_phase_b.py` · `test_gate_chain_phase_b.py` · `test_operations_performance_phase_b.py` · `test_strategy_context_freeze_phase_b.py`

**Edited backend:** `database/db_manager.py` (snapshots DDL + self-heal + `insert_performance_snapshot`) · `strategy/trading_engine.py` (effective config + legacy-mutation compat shim) · `paper/paper_runtime.py` · `paper/paper_worker.py` · `api/routers/{bot_control,settings,overview,performance,ai_decision,copilot}.py` · `copilot/llm_adapter.py` (7-rule READ-ONLY system prompt with gate-chain guidance) · `copilot/strategy_context.py` (`_risk_state` authoritative + provenance)

**Edited frontend:** `src/pages/{Operations,Settings,Overview,Copilot}.tsx` · `src/api/endpoints.ts` · `src/types/{operations,index}.ts`

---

## 3. Configuration — before vs after

**Before:** Settings UI → SQLite blob (dead end). Runtime → `TRADING_CAPITAL`/`MAX_TRADES_PER_DAY` env or hardcoded defaults at import. Result: UI said ₹20,000/20, runtime traded ₹1,00,000/3.

**After (ONE rule, project-wide):** `effective = Settings-DB blob over env defaults`, resolved per call (5 s TTL; Settings PUT invalidates immediately).

Consumers migrated: TradingEngine (RiskManager, PositionSizer, `evaluate_configured_strategy`), PaperTradingRuntime (`build_authoritative_risk_config`; strategy risk% stays frozen 2.5 % for V8-D parity), paper_worker equity fallback, Overview, bot_control (`_settings_now()`), performance R-normalizations, Copilot context, strategy_context `_risk_state`.

Capital definitions in Overview stay distinct: `total` = STARTING · `current` = CURRENT EQUITY (`paper_equity_snapshot.realized_equity`, `null` until persisted, with `equity_source`) · `used` = positions notional · `available` = equity − used · `buffer`. `capital.source = "runtime_config"`.

**Precedence note (verified by tests):** saved-blob > in-process legacy override > env default. The legacy module-global `settings` object remains importable and mutation-compatible for tests/CLI, but production code paths never mutate it, so the blob always wins in production.

## 4. Context architecture (ONE authoritative builder)

`backend/copilot/full_context.py → build_full_context()` — sections: bot · configuration · market · data_health · websocket · scanner · latest_signal · latest_decision · latest_rejection (+ gate_chain) · today · positions · recent_trades (bounded 20) · risk · execution · reconciliation · broker · AI · copilot · backtest (dynamic) · mismatches · errors. Every section carries `source` (LIVE_RUNTIME / DATABASE / CONFIGURATION / BACKTEST / BROKER / WEBSOCKET / SCANNER / RISK_MANAGER) and `as_of`; the whole payload passes the secret-guard redaction layer. `GET /api/copilot/context` serves it; the chat pipeline merges it into every prompt; the Copilot UI renders `BotContextCards` (collapsible cards, 15 s polling) and can answer from real state.

Honest gaps enforced by tests: no signal → `"No actionable V8-D signal has been recorded."` (never "I'm not aware…"); scanner/WS/reconciliation unavailable → explicit reason, never UNKNOWN→HEALTHY.

## 5. Endpoints (new/changed)

- `GET /api/copilot/context` — **new**, full authoritative context (read-only).
- `GET /api/settings/` — now returns `config_sources`.
- `PUT /api/settings/` — writes the blob to the resolver-bound DB, invalidates the cache, returns `effective_immediately: true` + `worker_restart_note`.
- `GET /api/bot/operations` — adds `runtime_config` block (effective capital/max-trades + source labels).
- `GET /api/performance` — 200 on empty DB (no more 500); safe snapshot handling.
- `GET /api/ai-decision/why-not-traded` — delegates to gate_chain (legacy contract preserved).
- `/api/bot/ai-toggle|mode|kill|reset-kill` — persist against real settings even when the API runs offline (DB-backed stub).

## 6. Operations fix

Frontend → `/api/bot/*` everywhere; Operations page shows runtime capital/max-trades with source labels, a config-mismatch banner, and the same authoritative numbers Overview shows (cross-checked by `test_operations_runtime_config_consistency`).

## 7. Performance fix

`performance_snapshots` table created by `init_db()`; legacy DBs self-heal on first access; `insert_performance_snapshot` provided; endpoint degrades honestly (`metrics: null`, empty curve) instead of erroring. R-multiple normalizations use effective capital.

## 8. "Why didn't we trade?" — single authority

`backend/copilot/gate_chain.py` maps every recorded reason to one stage taxonomy and emits the full chain MARKET → DATA → V8-D SIGNAL → AI DECISION → HARD RISK → POSITION SIZING → CONTRACT VALIDATION → EXECUTION PIPELINE → BROKER/PAPER EXECUTION → RECONCILIATION, each gate `OK / REJECTED / NOT_EVALUATED / NOT_ATTEMPTED / SKIPPED / UNKNOWN`, plus `stage`, `human_summary`, `recorded_at`, `age_seconds`.

Stage renames vs the old router (documented; tests updated): `stale_candles → STALE_DATA` (was STALE_OR_INSUFFICIENT_DATA) · `no_trade: → SIGNAL_REJECTED` (was V8D_REJECTED) · `rejected:INSUFFICIENT_EQUITY → SIZING_REJECTED` (was INSUFFICIENT_EQUITY).

## 9. Health honesty

WebSocket: `streaming=True` ⇒ healthy; connected-but-not-streaming ⇒ explicitly "NOT healthy" with stale-data reasons. Reconciliation: `NEVER_CHECKED` is a distinct state with an honest note (never folded into HEALTHY). Scanner: unavailable state carries a reason. No UNKNOWN→HEALTHY path exists (tests pin all three).

## 10. Backtest

Copilot backtest section is dynamic from the durable job store (`get_latest()`), shows stored rejection-reason counts, and reports unavailable-with-reason when nothing is stored. The §23 example numbers (171 trades / ₹45,397) are provably absent (grep test) — the frozen historical comparison lives only in `strategy_context.BACKTEST_COMPARISON`, clearly labeled.

## 11. Security / secret review

- Copilot remains strictly READ-ONLY: no order endpoints, no settings mutation, no broker calls, no code execution, no kill-switch/mode/broker writes from any Copilot path.
- Context passes the shared redaction guard; tests plant `upstox_access_token` / `control_token` values and assert they never appear in `build_full_context()` output or the HTTP response body.
- System prompt (llm_adapter) states the 7 READ-ONLY rules including "never modify settings".
- Email/SMTP stays removed: repo-wide scan finds zero `smtplib`/SMTP references outside tests.

## 12. V8-D freeze proof

- `git diff --stat backend/strategy/strategies/v8d_strategy.py` → **empty** (untouched in working tree).
- EOL-normalized sha256: **`d468cc110401e3b2`** (matches frozen value; 19,688 normalized bytes).
- No strategy parameters, entry logic, option-selection, or risk philosophy changed anywhere in this phase; WS lifecycle fix and live gate untouched.

## 13. Test counts

- Full backend suite (`python run_all_tests.py`): **1056 passed, 0 failed** (Phase-A baseline 977 + 79 new Phase-B tests).
- Repo-root integration suite: **30 passed** (baseline 30).
- 5 Phase-B files re-verified after all fixes: **79 passed**.
- During verification, 15 full-suite regressions were found and fixed at root cause (not by weakening tests): engine effective-config vs legacy test mutations (compat shim), `start_bot` vs synthetic DB stub (refuse), Settings-PUT blob leak into the shared process DB (PUT now writes the resolver-bound DB), a cwd-fragile `open()` in a static-guard test (`__file__`-relative), and blob precedence saved-blob > in-process > env.

## 14. Frontend build

`npm run build` (frontend): **success** in ~17 s — `tsc` clean; Copilot bundle 33.01 kB (gzip 9.28 kB) including the new context cards. `npx tsc --noEmit` passed earlier with EXIT:0.

## 15. Limitations / known notes

- `current` equity is `null` until the paper worker persists `paper_equity_snapshot` (by design — no fabricated numbers).
- Config-mismatch warnings compare env defaults vs saved blob; they do not diff a *running engine instance* constructed before a save (a restart or next construction picks up new values; TTL is 5 s).
- The backtest summary reflects the latest stored job only (no multi-job aggregation).
- Gate-chain `age_seconds` is wall-clock from `recorded_at`; a stale record is labeled old, not hidden.
- `run_all_tests.py` remains the canonical runner (thin pytest wrapper); no second framework exists.

## 16. Manual deploy steps (for the user — nothing was deployed)

1. Review the working tree; commit (`backend/…`, `frontend/…`, `PHASE_B_FINAL_REPORT.md`).
2. `cd frontend && npm run build` → deploy `dist/` with the backend (same-origin).
3. Deploy backend; on start, lifespan binds the shared DatabaseManager to the resolver and `init_db()` creates/repairs `performance_snapshots` automatically.
4. Optional: set `DATABASE_PATH` explicitly in the environment; otherwise `data/trading_bot.db` is used.
5. No env changes are required for the fix to work — saved Settings now simply win, as the UI always implied.

## 17. Post-deploy curl verification (proves ₹20,000 / 20 reach RUNTIME)

```bash
# a) Settings GET shows saved values + provenance
curl -s $BASE/api/settings/ | python -m json.tool
#   expect capital.total == 20000, risk.max_trades_per_day == 20,
#   config_sources["capital.total"] == "sqlite_settings"

# b) Operations runtime_config shows the RUNTIME sees them
curl -s $BASE/api/bot/operations | python -c "import sys,json; b=json.load(sys.stdin); \
print(b['runtime_config']['capital'], b['runtime_config']['risk'])"
#   expect starting_capital 20000.0, max_trades_per_day 20, source sqlite_settings

# c) Overview authoritative capital
curl -s $BASE/api/overview | python -c "import sys,json; c=json.load(sys.stdin)['capital']; \
print(c)"
#   expect total == 20000.0, source == "runtime_config"

# d) Copilot context (one payload, grounded)
curl -s $BASE/api/copilot/context | python -c "import sys,json; b=json.load(sys.stdin); \
print(b['bot']['strategy'], b['bot']['mode'], b['configuration']['capital']['starting_capital'], \
b['configuration']['risk']['max_trades_per_day'], b['backtest']['available'])"
#   expect V8_D_PULLBACK_ATM paper 20000.0 20 <True|False-with-reason>

# e) Why-not-traded delegates to the single gate-chain authority
curl -s $BASE/api/ai-decision/why-not-traded | python -m json.tool
#   expect stage/gates/human_summary keys present (or honest no-record state)
```

If (a) shows 20000/20 and (b)+(c) show 20000/20, the UI→SQLite→runtime chain is proven in production.

## 18. Acceptance checklist

- [x] ONE authoritative Copilot context (`full_context.py` + `/api/copilot/context`), UI cards live.
- [x] "I'm not aware…" eliminated — honest `NO_SIGNAL` / per-stage taxonomy instead.
- [x] Settings UI → SQLite → runtime parity (capital 20,000 & max trades 20 actually govern).
- [x] Overview capital authoritative + `source` label + distinct current/used/available/buffer.
- [x] Performance endpoint 200 (missing table fixed, self-healing).
- [x] Operations `/api/bot/*` paths (no 404s) + runtime_config block + mismatch banner.
- [x] Settings max-trades input max=20; save toast uses `worker_restart_note`.
- [x] Config-source visibility (`config_sources`) everywhere values are shown.
- [x] Gate-chain single authority with full MARKET→RECONCILIATION chain + human summary.
- [x] Backtest section dynamic from job store, never hardcoded.
- [x] 79 new tests; full suite 1056 passed; root suite 30 passed; frontend build OK.
- [x] V8-D frozen (empty diff + sha256 `d468cc110401e3b2`); email/SMTP absent; WS lifecycle & live gate untouched; Copilot read-only.
- [x] No commit, no push, no deploy. Production ZIP built and verified on request (§20).

## 19. Safety & scope compliance statement

No V8-D parameter, entry rule, option-selection rule, SL/TP logic, or risk philosophy was modified (§12 proof). No hard risk control is bypassed — effective config only changes *inputs already operator-controlled* (capital, max trades/day, loss limits) through the path the UI always wrote to. Copilot gained zero write capability; every new surface is GET-shaped, redacted, and bounded. The Upstox WebSocket lifecycle fix and the live safety gate are byte-for-byte untouched.

## 20. Production artifact (ZIP) — built and verified on request

**File:** `Trading-Bot-V1-PHASE-B-FINAL-PRODUCTION.zip` — 395 entries · ~4.8 MB · CRC integrity OK. Its sha256 is recorded in the adjacent `Trading-Bot-V1-PHASE-B-FINAL-PRODUCTION.sha256` file (checksums are kept outside the artifact because a file cannot contain its own hash).

Contents follow the existing `Trading-Bot-V1-FINAL-PRODUCTION.zip` convention (everything under a top-level `Trading-Bot-V1/` folder): source, tests, docs, `.env.example`, `data_cache/`, `real_data/`, `scripts/`, `deploy/`. Excluded: `.env`, live SQLite DBs (`data/` — stores auto-create their directories on first run), `node_modules`, `dist`, `__pycache__`, caches, logs, `analysis/`, any `.zip`.

**Verification of the artifact itself (not just the working tree):**
1. `zipfile.testzip()` CRC pass on every entry; forbidden-entry scan: NONE.
2. Byte-identity: sha256 of every zipped file equals the working tree — no stale or divergent content.
3. Extracted to a clean directory and the **full backend suite was run from the extraction: 1056 passed, 0 failed**; repo-root suite from the extraction: **30 passed**.
4. V8-D freeze holds inside the artifact: EOL-normalized sha256 `d468cc110401e3b2`.
5. Secret scan of all zipped content (JWT/access-token/secret patterns): the only hit is a synthetic **expired** test-JWT fixture in `test_infrastructure_hardening.py` (`exp` = 2020, `alg: none`) — not a credential. No `.env`, no tokens, no live DBs ship.

To deploy from the artifact: extract, `cd frontend && npm install && npm run build`, configure env from `.env.example`, run `python run_all_tests.py` once as a smoke gate, then start the API (uvicorn) — §16 applies unchanged.
