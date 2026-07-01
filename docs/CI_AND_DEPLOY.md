Continuous Integration and Deployment

This file explains how to use the included helper scripts and CI templates.

1) Initialize and push the repo (one-time):

- Linux / macOS:
```bash
./scripts/init_repo.sh https://github.com/YOURUSERNAME/upstox-trading-bot.git
```

- Windows PowerShell:
```powershell
.\scripts\init_repo.ps1 -RemoteUrl https://github.com/YOURUSERNAME/upstox-trading-bot.git
```

2) GitHub Actions
- The workflow at `.github/workflows/python-tests.yml` runs backend tests on push
  and pull requests to `main`. It installs dependencies from `backend/requirements.txt`.

3) Render
- `render.yaml` is a template you can import into Render as an infra file, or use it
  as a reference. In the Render UI, create a Web Service with:
  - Root Directory: `backend`
  - Build Command: `pip install -r requirements.txt`
  - Start Command: `uvicorn api.main:app --host 0.0.0.0 --port $PORT`
- Add environment variables in the Render dashboard (keys match `.env.example`).

4) Vercel (frontend)
- `vercel.json` is a minimal template for deploying the frontend (assumes `frontend/` exists).

5) Notes
- Keep secrets out of the repo; use environment variables in Render and GitHub Secrets.
- CI uses Python 3.11 to match recommended runtime.
