# PRODUCTION DEPLOYMENT — Trading-Bot-V1 (PAPER MODE)

Target: Ubuntu 22.04+ VPS (the existing Oracle Cloud host serving
`upstoxbot-anand.duckdns.org`). Frontend deploys to Vercel separately.

> **Mode safety:** every command below runs the system in PAPER MODE. The
> application refuses to start trading with `TRADING_MODE=live` — see
> RELEASE_READINESS.md before ever considering live (not supported).

## 1. Clean server deployment

```bash
# 1.1 System packages
sudo apt update && sudo apt install -y python3-venv python3-pip nginx git

# 1.2 Application user & directory
sudo useradd -m -s /bin/bash ubuntu || true
sudo mkdir -p /opt/trading-bot
sudo chown ubuntu:ubuntu /opt/trading-bot

# 1.3 Code (from the hardened ZIP — no build artifacts needed)
cd /opt/trading-bot
unzip ~/Trading-Bot-V1-PRODUCTION-HARDENED.zip -d /opt/trading-bot --strip-components=1 2>/dev/null \
  || unzip ~/Trading-Bot-V1-PRODUCTION-HARDENED.zip -d /opt/trading-bot

# 1.4 Virtualenv + backend dependencies
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r backend/requirements.txt
```

## 2. Environment configuration

```bash
cp .env.example .env
nano .env   # set UPSTOX_CLIENT_ID / SECRET / REDIRECT_URI; leave token empty
chmod 600 .env
```

Required minimum (paper):
`TRADING_MODE=paper`, `TRADING_STRATEGY=V8_D_PULLBACK_ATM`, `UPSTOX_ORDER_PRODUCT=I`.
The runtime refuses to start without these three being explicit.

## 3. Database initialization & directories

```bash
mkdir -p data logs
# The DB is created+MIGRATED automatically (WAL, extra column, daily_counters)
# on first API/worker start. To pre-create explicitly:
DATABASE_PATH=/opt/trading-bot/data/trading_bot.db ./venv/bin/python -c \
  "from backend.database.db_manager import DatabaseManager; DatabaseManager('/opt/trading-bot/data/trading_bot.db').close()"
```

## 4. systemd service

```bash
sudo cp deploy/systemd/upstox-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now upstox-bot.service
```

The unit pins `Environment=TRADING_MODE=paper` regardless of `.env`.

## 5. Nginx (TLS reverse proxy)

```bash
sudo cp deploy/nginx/upstoxbot.conf /etc/nginx/sites-available/upstoxbot
sudo ln -sf /etc/nginx/sites-available/upstoxbot /etc/nginx/sites-enabled/
# First time: obtain certs (certbot --nginx -d upstoxbot-anand.duckdns.org)
sudo nginx -t && sudo systemctl reload nginx
```

## 6. Start / stop / restart

```bash
sudo systemctl start  upstox-bot.service   # API only; worker starts via UI Start
sudo systemctl stop   upstox-bot.service
sudo systemctl restart upstox-bot.service  # safe: worker state is in SQLite
sudo systemctl status upstox-bot.service
```

## 7. Paper worker

Preferred: dashboard **Start** button (POST /api/bot/start → spawns worker with
lock-file guard). Manual CLI equivalent:

```bash
cd /opt/trading-bot
sudo -u ubuntu env DATABASE_PATH=/opt/trading-bot/data/trading_bot.db \
  TRADING_MODE=paper TRADING_STRATEGY=V8_D_PULLBACK_ATM UPSTOX_ORDER_PRODUCT=I \
  PYTHONPATH=/opt/trading-bot \
  nohup ./venv/bin/python backend/paper/paper_worker.py \
  >> data/paper_worker.log 2>&1 &
```

Verify: `curl -s localhost:8000/api/bot/status | python3 -m json.tool` →
`worker_alive: true`, `heartbeat_age_seconds < 20`, `pipeline_ok: true`.

## 8. Health verification

```bash
curl -s localhost:8000/health            # liveness (fast, in-memory)
curl -s localhost:8000/api/health        # components
curl -s localhost:8000/api/bot/status    # worker/pipeline/kill state
curl -s localhost:8000/api/paper/positions  # durable ledger view
```

`healthy` requires: API up + DB reachable + (if started) worker heartbeat fresh.
The UI shows DOWN/STOPPED honestly when the worker is dead — never fake RUNNING.

## 9. Log inspection

```bash
sudo journalctl -u upstox-bot -f          # API logs
tail -f /opt/trading-bot/data/paper_worker.log
grep PAPER_AUDIT  /opt/trading-bot/data/paper_worker.log | tail
grep PAPER_EXIT   /opt/trading-bot/data/paper_worker.log | tail
sqlite3 data/trading_bot.db "SELECT key,value FROM settings WHERE key LIKE 'paper_worker%';"
```

## 10. Restart / reboot / rollback

* **Server reboot:** systemd auto-starts the API; press Start (or §7) for the
  worker. Open paper positions rehydrate from SQLite (ledger → PaperBroker)
  including SL/target/lot — verified by regression tests.
* **Restart mid-position:** safe. See `test_restart_restores_position_risk_state_from_extra`.
* **Rollback:** keep the previous ZIP; stop service, replace tree (preserve
  `data/`!), restart. Schema migrations are additive; older code may ignore
  newer columns safely. Never delete `data/trading_bot.db*` blindly.

## 11. Backup & recovery

```bash
# Consistent backup (WAL-safe online backup)
sqlite3 data/trading_bot.db ".backup '/opt/backups/trading_bot_$(date +%F).db'"
# Restore
sudo systemctl stop upstox-bot
cp /opt/backups/trading_bot_YYYY-MM-DD.db data/trading_bot.db
sudo systemctl start upstox-bot
```

Also back up `.env` and `data/upstox_token.json` (600 perms, off-machine).

## 12. Frontend (Vercel)

```bash
cd frontend
npm ci && npm run build        # local verification; Vercel runs the same
# Vercel project root: frontend/  (uses frontend/vercel.json; /api/* proxied to
# https://upstoxbot-anand.duckdns.org) — set VITE_BACKEND_URL if self-hosting API elsewhere
```

## 13. Post-deploy smoke checklist

1. `/health` → `status: ok`.
2. Start bot → `/api/bot/status`: `worker_alive true`, `pipeline_ok true`.
3. `data/paper_worker.log` shows `ACTIVE STRATEGY: V8_D_PULLBACK_ATM`, `MODE: PAPER`,
   `RISK capital=... product=I`.
4. No `PAPER_AUDIT` entries outside market hours; `EOD_CUTOFF` after 15:15 IST.
5. Kill switch drill: `/api/bot/kill` → status shows `kill_switch_active true`,
   entries refused; `/api/bot/reset-kill` restores.
