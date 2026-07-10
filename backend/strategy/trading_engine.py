"""Mock Trading Engine for Render deployment (no circular imports)."""
from pathlib import Path
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.append(str(Path(__file__).parent.parent.parent))

# Lazy imports to avoid circular dependency
def get_update_scanner_state():
    from backend.api.routers.trading import update_scanner_state
    return update_scanner_state

def get_update_ws_health():
    from backend.api.routers.diagnostics import update_ws_health
    return update_ws_health

@dataclass
class BotState:
    """Thread-safe bot running state (mock)."""
    _running = False
    _kill_switch = False
    _start_time: Optional[datetime] = None
    _stop_reason: str = ""

    @classmethod
    def start(cls) -> None:
        cls._running = True
        cls._kill_switch = False
        cls._start_time = datetime.now(timezone.utc)
        cls._stop_reason = ""

    @classmethod
    def stop(cls, reason: str = "Manual stop") -> None:
        cls._running = False
        cls._stop_reason = reason

    @classmethod
    def kill(cls, reason: str = "Emergency kill switch") -> None:
        cls._kill_switch = True
        cls._running = False
        cls._stop_reason = reason

    @classmethod
    def reset_kill(cls) -> None:
        cls._kill_switch = False

    @classmethod
    def is_running(cls) -> bool:
        return cls._running and not cls._kill_switch

    @classmethod
    def status(cls) -> Dict[str, Any]:
        return {
            "running": cls._running,
            "kill_switch_active": cls._kill_switch,
            "start_time": cls._start_time.isoformat() if cls._start_time else None,
            "stop_reason": cls._stop_reason,
            "uptime_seconds": int((datetime.now(timezone.utc) - cls._start_time).total_seconds()) if cls._start_time and cls._running else 0,
        }

# Mock scanner
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