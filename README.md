# Trading-Bot-V1 — Local Development (Windows / VS Code)

Upstox **options** paper-trading bot with FastAPI backend + React (Vite) frontend.

| Setting | Default |
|---------|---------|
| **TRADING_MODE** | `paper` (safe — no live Upstox order placement) |
| **TRADING_STRATEGY** | `V8_D_PULLBACK_ATM` (production paper strategy) |
| **UPSTOX_ORDER_PRODUCT** | `I` (intraday) |

**Live trading is not enabled by default.** Paper mode never calls the real Upstox order-placement API.

---

## A. Windows prerequisites

1. **Windows 10 or 11**
2. **Python 3.11+** — https://www.python.org/downloads/  
   - During install: check **“Add python.exe to PATH”**
3. **Node.js 18+ (LTS)** — https://nodejs.org/  
   - Includes `npm`
4. **VS Code** — https://code.visualstudio.com/  
   - Recommended extensions: Python, Pylance, ESLint
5. **Git** (optional) — only if you version the extracted folder yourself

Open a **new** PowerShell or Command Prompt after installing Python/Node so `python` and `npm` are on PATH.

```powershell
python --version
npm --version
```

---

## B. Extract and open in VS Code

1. Extract `Trading-Bot-V1-LOCAL-FINAL.zip` to a path **without spaces** if possible, e.g.  
   `C:\dev\Trading-Bot-V1`
2. In VS Code: **File → Open Folder…** → select that folder (the one that contains `backend/`, `frontend/`, `README.md`).

---

## C. Python setup (backend)

From the **project root** (folder that contains `backend/`):

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
```

If activation fails in PowerShell:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\activate
```

---

## D. Node / npm setup (frontend)

**Production frontend path is `frontend/`** (not the optional root `src/` / `server.ts` stack).

```powershell
cd frontend
npm ci
```

If `npm ci` fails (lockfile mismatch), use:

```powershell
npm install
```

Then return to root:

```powershell
cd ..
```

---

## E. Environment configuration (`.env`)

```powershell
copy .env.example .env
```

Edit `.env` in VS Code. **Required local defaults are already set:**

```env
TRADING_MODE=paper
TRADING_STRATEGY=V8_D_PULLBACK_ATM
UPSTOX_ORDER_PRODUCT=I
DATABASE_PATH=data/trading_bot.db
```

### Upstox token (market data / quotes)

Leave empty for pure offline tests. For live **market data** (quotes, option chain, historical candles) paste a token from the Upstox developer app:

```env
UPSTOX_ACCESS_TOKEN=
UPSTOX_CLIENT_ID=
UPSTOX_CLIENT_SECRET=
UPSTOX_REDIRECT_URI=
```

**Do not set `TRADING_MODE=live` unless you fully understand the risk.** Paper mode is the supported default.

Frontend API base (optional; defaults work for local):

```env
VITE_BACKEND_URL=http://127.0.0.1:8000
```

---

## F. Start backend

From project root, with venv active:

```powershell
.\.venv\Scripts\activate
$env:PYTHONPATH = "."
python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000 --reload
```

Health check: open http://127.0.0.1:8000/health or http://127.0.0.1:8000/docs

---

## G. Start frontend

In a **second** terminal:

```powershell
cd frontend
npm run dev
```

Vite is configured for **port 3000** (`http://127.0.0.1:3000`).  
CORS allows `http://localhost:3000` and `http://127.0.0.1:3000`.

---

## H. Running tests

From project root, venv active:

```powershell
.\.venv\Scripts\activate
$env:PYTHONPATH = "."
$env:TRADING_MODE = "paper"
$env:TRADING_STRATEGY = "V8_D_PULLBACK_ATM"
$env:OFFLINE = "1"
$env:ALLOW_LIVE_UPSTOX = "0"

python run_all_tests.py
```

Or run specific files with true pytest:

```powershell
python -m pytest backend\tests\test_paper_exit_pnl_risk.py -q
python -m pytest backend\tests\test_position_recovery.py -q
```

Official style (if pytest is installed in the venv):

```powershell
python -m pytest backend\tests -q
```

---

## I. Running backtests

1. Start the backend (section F).
2. Open the UI → **Backtest**.
3. Select strategy **V8-D Pullback ATM** (default).
4. Choose symbols / date range / interval.
5. Run. Results report strategy identity, coverage, and validity status  
   (`VALID` / `ZERO_TRADES` / `INCONCLUSIVE` / `INVALID_DATA`).

**No synthetic candles.** Incomplete historical coverage is reported as data quality failure, not as a profitable result.

API example:

```powershell
# Requires backend running + token for historical data
curl -X POST http://127.0.0.1:8000/api/backtest/jobs -H "Content-Type: application/json" -d "{\"start_date\":\"2024-10-01\",\"end_date\":\"2024-10-05\",\"symbols\":[\"NIFTY50\"],\"interval\":\"5minute\",\"strategies\":[\"V8_D_PULLBACK_ATM\"],\"capital\":100000}"
```

---

## J. Paper mode

- Default `TRADING_MODE=paper`.
- Orders go through **ExecutionPipeline → PaperBroker** only.
- Real Upstox **order placement HTTP is never called** in paper mode (covered by automated tests).
- Exits: stop-loss, target, trailing stop, and EOD square-off use **mark/market price**, not entry price.
- Lot size comes from **contract metadata**; missing/invalid lot → safe rejection.
- Risk: max positions, max daily trades, daily loss, kill switch, insufficient equity.

---

## K. V8-D strategy (`V8_D_PULLBACK_ATM`)

Production paper strategy:

- EMA pullback / retest on the underlying index  
- ATM option selection  
- Dynamic SL / target (including ATR-aware stop when option ATR is present)  
- Risk and allocation caps from settings  

**Do not change entry parameters** unless you are deliberately researching a new variant. Research strategy `OPTION_PREMIUM` may appear in the Backtest UI but is **not** a silent fallback for paper/runtime.

---

## L. Historical data requirements

- Backtests and paper signal evaluation need real Upstox historical candles / expired-option data when online.
- This ZIP may **omit large bulk research CSVs / heavy caches** to keep size manageable.
- The application **does not substitute synthetic candles** when data is missing; runs are marked incomplete / invalid instead.
- Intentionally excluded from the ZIP (if present on a full research machine):  
  - `node_modules/`, `__pycache__/`, `.git/`, `.env`  
  - runtime `*.db`, `logs/`  
  - very large one-off research dumps under root (`option_behavior_obs_*.csv`, multi‑MB ablation CSVs) when not required to **run** the app  
- Keep any existing historical datasets on your own machine outside the ZIP; do not delete them.

Cache directories used at runtime (created automatically if missing):

- `data/` — SQLite  
- `data_cache/` / env `HISTORICAL_OPTIONS_CACHE_DIR` — option history cache  

---

## M. Safety

- **Default is paper.** Live order product and live mode require explicit configuration and are refused when misconfigured.
- No second execution path that bypasses risk / kill switch / limits.
- Strategy identity is explicit: empty strategy → error (no silent `OPTION_PREMIUM`).

---

## N. Troubleshooting

| Problem | What to try |
|---------|-------------|
| `ModuleNotFoundError: backend` | Set `$env:PYTHONPATH = "."` from project root |
| `npm` / `vite` not found | Reinstall Node LTS; reopen terminal; `cd frontend` |
| Frontend cannot call API | Backend on `:8000`; `VITE_BACKEND_URL=http://127.0.0.1:8000`; CORS allows `:3000` |
| WebSocket / quotes 401 | Fresh `UPSTOX_ACCESS_TOKEN` in `.env` / Settings UI |
| SQLite lock / path errors | Ensure `data\` exists; set `DATABASE_PATH=data\trading_bot.db` |
| Tests fail on import | Activate venv; `pip install -r backend\requirements.txt` |
| Backtest zero trades | Check validity status and rejection reasons — may be no signals or incomplete data |

---

## O. Project layout (local)

```
Trading-Bot-V1/
  backend/           # FastAPI, strategy, paper, risk, backtest
  frontend/          # React + Vite UI (PRIMARY frontend)
  docs/
  analysis/
  deploy/
  scripts/
  models/
  tests/             # optional top-level tests
  run_all_tests.py   # full-suite entry point (thin true-pytest wrapper)
  .env.example
  README.md
```

Optional / legacy at repo root: `src/`, `server.ts`, root `package.json` — **not** required for the standard local path above. Prefer **`frontend/`** only.

---

## P. QA snapshot (package build time)

See the release notes delivered with the ZIP for exact pass counts. Expected after a clean install:

- `python run_all_tests.py` → all tests passed  
- Paper exit / P&L / risk / lot / recovery / live-order-safety tests green  
- V8-D entry parameters **unchanged**  
- Frontend: `npm ci` + `npm run build` in `frontend/` (verify on your machine if network was limited at package time)
