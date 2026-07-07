"""API routers package — exports all router instances."""
from .alerts      import router as alerts_router
from .backtest    import router as backtest_router
from .bot_control import router as bot_control_router
from .diagnostics import router as diagnostics_router
from .overview    import router as overview_router
from .performance import router as performance_router
from .settings    import router as settings_router
from .trading     import router as trading_router
from .websocket   import router as websocket_router

__all__ = [
    "alerts_router",
    "backtest_router",
    "bot_control_router",
    "diagnostics_router",
    "overview_router",
    "performance_router",
    "settings_router",
    "trading_router",
    "websocket_router",
]
