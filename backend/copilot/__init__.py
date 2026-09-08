"""AI Trading Copilot — a conversational/decision layer built ON TOP of the
existing bot, not a replacement for any of it.

Everything numerical (prices, indicators, P&L, risk state) is computed by
the existing deterministic services and returned as-is by backend/copilot/
tools.py. The LLM layer (backend/copilot/llm_adapter.py) only explains and
converses — it is never the source of a number, and it cannot place or
modify an order. Trade ideas become a TradePlan (trade_plan.py) that must
pass the EXISTING RiskManager/PositionSizer before anything happens.

Package layout:
  tools.py           - read-only functions wrapping existing services (the
                        "get_*" tool list from the spec)
  trade_plan.py       - TradePlan dataclass + deterministic validation
  decision_engine.py  - turns tool output into a structured MarketAnalysis
                        and WAIT/SKIP/TRADE decision (no LLM required)
  llm_adapter.py      - pluggable local-LLM interface + a zero-dependency
                        rule-based fallback that needs no model at all
  conversational.py   - routes a natural-language question to tools, then
                        to the LLM adapter for explanation only
  diagnostics.py       - run_full_diagnostics(), reuses HealthMonitor +
                         the existing diagnostics router's checks
  shadow_logger.py     - logs TradePlans + hypothetical outcomes
  alerts.py            - state-change-based structured alerts
  config.py            - COPILOT_ENABLED / COPILOT_MODE, off by default
"""
