from pathlib import Path
import sys
sys.path.append(str(Path(__file__).parent.parent.parent))

# Lazy imports to avoid circular dependency
def get_update_scanner_state():
    from backend.api.routers.trading import update_scanner_state
    return update_scanner_state

def get_update_ws_health():
    from backend.api.routers.diagnostics import update_ws_health
    return update_ws_health

# Mock strategy engine
def scan_instruments():
    """Mock scanner that updates state."""
    symbols = ["RELIANCE", "TCS", "HDFCBANK"]
    update_scanner = get_update_scanner_state()
    for sym in symbols:
        update_scanner(sym, running=True, results={"rsi": 65, "atr": 10}, reason=None)
        import time
        time.sleep(0.1)
    update_scanner("SCAN_COMPLETE", running=False)

if __name__ == "__main__":
    scan_instruments()