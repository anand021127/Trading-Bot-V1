from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from .routers import trading, diagnostics  # will create

app = FastAPI(title="Upstox Trading Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(trading.router, prefix="/api", tags=["trading"])
app.include_router(diagnostics.router, prefix="/api", tags=["diagnostics"])

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
