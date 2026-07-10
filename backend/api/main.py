from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from .routers import trading, diagnostics, overview, bot_control, settings, performance, backtest  # add all

app = FastAPI(title="Upstox Trading Bot API", version="1.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update with your Vercel URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount all routers
app.include_router(trading.router, prefix="/api", tags=["trading"])
app.include_router(diagnostics.router, prefix="/api", tags=["diagnostics"])
app.include_router(overview.router, prefix="/api", tags=["overview"])
app.include_router(bot_control.router, prefix="/api", tags=["bot"])
app.include_router(settings.router, prefix="/api", tags=["settings"])
app.include_router(performance.router, prefix="/api", tags=["performance"])
app.include_router(backtest.router, prefix="/api", tags=["backtest"])

@app.get("/health")
def health():
    return {"status": "ok", "mode": "paper"}

@app.get("/")
def root():
    return {"message": "Upstox Bot API is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)