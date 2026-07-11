"""API routers package — exports all router instances."""
from .alerts      import router as alerts_router
from .backtest    import router as backtest_router
from .bot_control import router as bot_control_router
from .diagnostics import router as diagnostics_router
from .overview    import router as overview_router
from .paper       import router as paper_router
from .performance import router as performance_router
from .scanner     import router as scanner_router
from .settings    import router as settings_router
from .strategy    import router as strategy_router
from .trading     import router as trading_router
from .universe    import router as universe_router
from .websocket   import router as websocket_router

__all__ = [
    "alerts_router",
    "backtest_router",
    "bot_control_router",
    "diagnostics_router",
    "overview_router",
    "paper_router",
    "performance_router",
    "scanner_router",
    "settings_router",
    "strategy_router",
    "trading_router",
    "universe_router",
    "websocket_router",
]
