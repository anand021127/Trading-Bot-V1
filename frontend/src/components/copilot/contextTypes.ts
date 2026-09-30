/** Shape of GET /api/copilot/context (the ONE authoritative backend context).
 *  Every field is optional: sections degrade to an honest "unavailable" state. */

export interface GateInfo { status?: string; detail?: unknown }

export interface GateChain {
  available?: boolean
  reason?: string
  stage?: string
  human_summary?: string
  signal?: string | null
  traded?: boolean
  scan_reason?: string
  final_reason?: string
  age_seconds?: number
  gates?: Record<string, GateInfo>
}

export interface CtxBot {
  running?: boolean
  mode?: string
  strategy?: string
  broker?: string
  market_open?: boolean | null
  kill_switch_active?: boolean
  runtime_state?: string | null
  runtime_label?: string | null
  runtime_summary?: string | null
  worker_alive?: boolean | null
  heartbeat_age_seconds?: number | null
  effective_running?: boolean
}

export interface CtxConfig {
  capital?: { starting_capital?: number | null; current_equity?: number | null; source?: string }
  risk?: {
    max_trades_per_day?: number | null
    max_trades_source?: string
    max_risk_per_trade_pct?: number | null
    max_daily_loss_pct?: number | null
  }
  strategy?: string
}

export interface CtxWebsocket {
  state?: string
  streaming?: boolean
  last_tick_age_seconds?: number | null
  market_data_status?: string
  health_interpretation?: string
}

export interface CtxScanner {
  available?: boolean
  reason?: string
  state_label?: string
  scanner_status?: string
  summary?: string
  scan_seq?: number | null
  last_scan_ist?: string | null
  last_scan_seconds_ago?: number | null
  scan_interval_seconds?: number | null
  worker_last_scan_reason?: string | null
  data_status?: string | null
  candle_count?: number | null
  candle_age_seconds?: number | null
  expiry?: string | null
  option_chain_count?: number | null
  error?: string | null
}

export interface CtxToday {
  available?: boolean
  reason?: string
  trades_today?: number
  configured_max_trades?: number | null
  trades_remaining?: number | null
  wins?: number
  losses?: number
  realized_pnl?: number | null
}

export interface CtxRisk {
  available?: boolean
  reason?: string
  trades_used?: number
  max_trades?: number | null
  daily_loss_used_pct?: number | null
  daily_pnl?: number | null
  consecutive_losses?: number | null
  stop_reason?: string | null
  note?: string
  state?: string
}

export interface CtxPosition {
  underlying?: string
  symbol?: string
  strike?: number | string
  option_type?: string
  quantity?: number
  average_price?: number
}

export interface CtxTrade {
  trade_id?: string | number
  underlying?: string
  symbol?: string
  strike?: number | string
  option_type?: string
  net_pnl?: number | string | null
}

export interface CtxBacktest {
  available?: boolean
  reason?: string
  config?: { start_date?: string; end_date?: string }
  summary?: {
    trades?: number
    net_pnl?: number
    win_rate_pct?: number
    profit_factor?: number
    max_drawdown_pct?: number
  }
}

export interface CtxAi {
  available?: boolean
  reason?: string
  enabled?: boolean
  provider?: string
  model?: string
  latency?: { latency_ms_median?: number | null; latency_ms_p95?: number | null }
  latest?: {
    available?: boolean
    reason?: string
    decision?: string
    symbol?: string
    confidence?: number | null
    reason_codes?: string[]
    latency_ms?: number | null
    age_seconds?: number | null
  }
}

export interface CtxMismatch { message?: string }

export interface CopilotContext {
  generated_at?: string
  bot?: CtxBot
  configuration?: CtxConfig
  market?: { session_status?: string }
  websocket?: CtxWebsocket
  scanner?: CtxScanner
  latest_rejection?: { reason?: string; gate_chain?: GateChain }
  latest_signal?: { available?: boolean; signal?: string; reason?: string; rejection_reasons?: string[] }
  today?: CtxToday
  positions?: { available?: boolean; reason?: string; count?: number; positions?: CtxPosition[] }
  recent_trades?: { available?: boolean; reason?: string; count?: number; trades?: CtxTrade[] }
  risk?: CtxRisk
  ai?: CtxAi
  backtest?: CtxBacktest
  configuration_mismatches?: CtxMismatch[]
  errors?: { available?: boolean; note?: string; recent?: unknown[] }
}

export type CtxLoad = 'loading' | 'ok' | 'error'
