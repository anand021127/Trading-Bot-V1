# Upstox Algorithmic Trading Bot

FastAPI backend (Render) + React frontend (Vercel) + SQLite

---

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js 18+
- Upstox trading account with API access

---

## Deployment

### Step 1 — Push to GitHub
```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/trading-bot.git
git push -u origin main
```

### Step 2 — Deploy Backend to Render

1. Go to https://render.com → New → Web Service
2. Connect your GitHub repo
3. Set these **exact** values:
   - **Root Directory:** ` ` (leave blank — uses repo root)
   - **Build Command:** `pip install -r backend/requirements.txt`
   - **Start Command:** `uvicorn backend.api.main:app --host 0.0.0.0 --port $PORT`
   - **Python Version:** 3.11
4. Add a **Disk**: Mount path `/data`, Size 1 GB
5. Add all environment variables from `.env.example`
6. Deploy

Your backend URL will be: `https://upstox-bot-api.onrender.com`

### Step 3 — Deploy Frontend to Vercel

1. Go to https://vercel.com → New Project → Import your GitHub repo
2. Set these **exact** values in Vercel dashboard:
   - **Framework Preset:** Vite
   - **Root Directory:** `frontend`  ← THIS IS CRITICAL
   - **Build Command:** `npm run build`
   - **Output Directory:** `dist`
   - **Install Command:** `npm install`
3. Add environment variables:
   - `VITE_BACKEND_URL` = `https://upstox-bot-api.onrender.com`
4. Deploy

Your frontend URL will be: `https://trading-bot-v1-snowy.vercel.app`

### Step 4 — Update CORS in backend/config/settings.yaml
```yaml
api:
  cors_origins:
    - "https://trading-bot-v1-snowy.vercel.app"
    - "http://localhost:5173"
```
Commit and push — Render auto-redeploys.

### Step 5 — Upstox API v3 Semi-Automated Authentication & Notifier Webhook Setup

1. **Configure Notifier Webhook in Upstox Developer Console**:
   - Log in to https://developer.upstox.com/
   - Open your App Settings
   - Under **Notifier Webhook Endpoint**, configure:
     ```
     https://ais-dev-hd7gghhtwxalr5b44w6atc-860902825628.asia-southeast1.run.app/api/webhooks/upstox-token-notifier
     ```
     *(Or your production backend URL `/api/webhooks/upstox-token-notifier`)*

2. **One-Tap Daily Push Approval (v3)**:
   - In the bot dashboard → **Settings** → **"Request Token Approval (Push)"**
   - Upstox dispatches a push notification to your Upstox Mobile App and WhatsApp.
   - Tap **"Approve"** on your mobile device.
   - Upstox immediately delivers the access token to the Notifier Webhook.
   - The token is securely verified, saved to persistent SQLite storage, and the trading engine/WebSocket feeds are automatically initialized.

3. **Legacy OAuth Dialog (v2 Fallback)**:
   - The classic OAuth authorization dialog remains fully supported via **"Generate Token via OAuth Dialog"**.

---

## Local Development

### Backend
```bash
cd /path/to/project
pip install -r backend/requirements.txt
cp .env.example .env  # fill in your values
uvicorn backend.api.main:app --reload
# API: http://localhost:8000
# Docs: http://localhost:8000/docs
```

### Frontend
```bash
cd frontend
npm install
cp .env.example .env.local
# Set VITE_BACKEND_URL=http://localhost:8000
npm run dev
# Dashboard: http://localhost:5173
```

---

## Project Structure

```
├── backend/
│   ├── api/
│   │   ├── main.py          # FastAPI app
│   │   ├── websocket.py     # WebSocket manager
│   │   └── routers/         # API endpoints
│   ├── config/
│   │   └── settings.yaml    # All configuration
│   ├── database/            # SQLite models
│   ├── indicators/          # EMA, ATR, RSI, Choppiness
│   ├── strategy/            # ORB, exit logic
│   ├── risk/                # Position sizing, trailing stop
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/           # 8 dashboard pages
│   │   ├── components/      # Shared UI components
│   │   ├── api/             # Backend API calls
│   │   ├── hooks/           # WebSocket, polling
│   │   └── types/           # TypeScript types
│   ├── index.html           # Vite entry point
│   ├── vercel.json          # Vercel SPA routing
│   └── package.json
├── render.yaml              # Render deployment
└── .env.example             # Environment variable template
```
