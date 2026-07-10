from pathlib import Path
import sys
sys.path.append(str(Path(__file__).parent.parent.parent))
from backend.api.routers.trading import update_scanner_state
from backend.api.routers.diagnostics import update_ws_health
# Mock strategy engine

def scan_instruments():
    """Mock scanner that updates state."""
    symbols = ["RELIANCE", "TCS", "HDFCBANK"]
    for sym in symbols:
        update_scanner_state(sym, running=True, results={"rsi": 65, "atr": 10}, reason=None)
        # simulate
        import time
        time.sleep(0.1)
    update_scanner_state("SCAN_COMPLETE", running=False)

# Call in background if needed
if __name__ == "__main__":
    scan_instruments()
