"""REST endpoints for live index option-chain analysis."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.broker.upstox_client import UpstoxAPIError, UpstoxClient
from backend.config.universe_config import VALID_OPTION_INDICES
from backend.market_data.option_chain import summarize_chain

router = APIRouter()


@router.get("/chain")
async def get_option_chain(
    underlying: str = Query(..., description="Supported index underlying"),
    expiry: Optional[str] = Query(None, description="ISO expiry; nearest live expiry when omitted"),
) -> Dict[str, Any]:
    name = underlying.upper()
    if name not in VALID_OPTION_INDICES:
        raise HTTPException(status_code=400, detail={"message": "Unsupported index underlying", "allowed": VALID_OPTION_INDICES})
    client = UpstoxClient()
    token = client.access_token
    if not token:
        return {
            "underlying": name,
            "expiry": None,
            "spot": None,
            "contracts": [],
            "summary": {},
            "status": "AUTH_REQUIRED",
            "message": "Upstox access token is not configured. Please add access token in Settings.",
        }
    selected_expiry = expiry or client.get_nearest_expiry(name)
    if not selected_expiry:
        return {
            "underlying": name,
            "expiry": None,
            "spot": None,
            "contracts": [],
            "summary": {},
            "status": "DATA_UNAVAILABLE",
            "message": "No upcoming expiry available. NSE market may be closed or token expired.",
        }
    try:
        contracts = client.get_option_chain(name, selected_expiry)
        quote = client.get_live_quote(name)
    except UpstoxAPIError as exc:
        if exc.status_code == 401:
            return {
                "underlying": name,
                "expiry": selected_expiry,
                "spot": None,
                "contracts": [],
                "summary": {},
                "status": "AUTH_EXPIRED",
                "message": "Upstox access token has expired. Please re-authenticate in Settings.",
            }
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    spot = quote.get("ltp") or None
    summary = summarize_chain(name, selected_expiry, contracts, spot=spot)
    return {
        "underlying": name,
        "expiry": selected_expiry,
        "spot": spot,
        "contracts": contracts,
        "summary": summary.to_dict(),
    }
