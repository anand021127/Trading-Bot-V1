from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse
from typing import Optional

app = FastAPI()


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/settings/token-callback")
async def token_callback(code: Optional[str] = Form(None)):
    # Minimal stub: in the real app this would exchange the code for tokens
    if not code:
        return JSONResponse({"detail": "missing code"}, status_code=400)
    return {"status": "received", "code": code}
