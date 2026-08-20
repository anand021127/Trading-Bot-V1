Frontend scaffold (Vite + React)

Files created under `frontend/`:
- `package.json` — scripts: `dev`, `build`, `preview`
- `index.html`, `src/main.jsx`, `src/App.jsx`, `src/App.css`
- `README.md` — run/install instructions

CI:
- `.github/workflows/frontend.yml` — installs deps and builds on push/PR to main

Quick start (from project root):

```bash
cd frontend
npm install
npm run dev
```

If your backend is deployed at `https://upstox-bot-api.onrender.com`, create `frontend/.env` with:

```
VITE_BACKEND_URL=https://upstox-bot-api.onrender.com
```

Vercel deployment:
- Vercel will detect the project and run `npm run build` in `frontend/`.
- Ensure `VITE_BACKEND_URL` is set in Vercel environment variables.
