from fastapi import APIRouter, Depends
from typing import Dict, Any
import time
from datetime import datetime

router = APIRouter()

# Global scanner state - in real app use dependency or DB, here in-memory for demo
scanner_state = {
    "scanner_running": False,
    "current_symbol": None,
    "last_scan_time": None,
    "indicator_results": {},
    "rejected_reasons": {},
    "confidence_score": 0.0,
    "scanned_symbols": []
}

@router.get("/scanner/status")
def get_scanner_status():
    """Get current scanner status."""
    return {
        "scanner_running": scanner_state["scanner_running"],
        "current_symbol": scanner_state["current_symbol"],
        "last_scan_time": scanner_state["last_scan_time"],
        "scanned_count": len(scanner_state["scanned_symbols"]),
        "confidence_score": scanner_state["confidence_score"],
    }

# Mock update function - in real trading_engine call this
def update_scanner_state(symbol: str, running: bool = True, results: Dict = None, reason: str = None):
    scanner_state["scanner_running"] = running
    scanner_state["current_symbol"] = symbol
    scanner_state["last_scan_time"] = datetime.now().isoformat()
    if results:
        scanner_state["indicator_results"][symbol] = results
    if reason:
        scanner_state["rejected_reasons"][symbol] = reason
    if symbol not in scanner_state["scanned_symbols"]:
        scanner_state["scanned_symbols"].append(symbol)
