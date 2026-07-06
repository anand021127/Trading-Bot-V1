Setup Guide (condensed) — follow FILE2_YOUR_SETUP_GUIDE_v3.md

1) Local prerequisites
   - Install Python 3.11, Node.js (LTS), Git, and VS Code or Cursor.

2) Accounts you'll need
   - GitHub, Render (connect GitHub), Vercel (connect GitHub), Upstox developer app, Telegram bot token, Gmail app password.

3) Project skeleton (already in this repo)
   - backend/: Python backend and tests
   - frontend/: React app (created in later steps)

4) Quick local setup
   - Copy `.env.example` -> `.env` and fill values.
   - Create and activate a virtualenv, then install backend deps:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r backend/requirements.txt
```

5) Run tests (backend):

```bash
cd backend
python -m pytest tests/ -v
```

6) GitHub + Render
   - Initialize git, commit code, push to a private repository named `upstox-trading-bot`.
   - In Render: create a Web Service, connect repo, set Root Directory to `backend`, Build Command `pip install -r requirements.txt`, Start Command `uvicorn api.main:app --host 0.0.0.0 --port $PORT`.
   - Add environment variables in Render (use values from your `.env`).

7) Vercel
   - Create a Vercel project from your frontend repo (created later). Set `FRONTEND_URL` in Render when you have it.

8) Notes
   - The project loader expects a `.env` file in the repo root. Use `.env.example` as a template.
   - Many parts of the setup require actions you must complete in the browser (accounts, tokens, app registrations).

If you want, I can:
 - generate CI workflows (GitHub Actions) to run tests on push
 - create scripts to automate git init + remote setup
 - scaffold the frontend (Vite + React) and its tests
Tell me which of those to do next.
