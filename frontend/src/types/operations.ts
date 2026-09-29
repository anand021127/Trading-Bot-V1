/** PHASE 5.3 §30 — shared control-plane payload types. */

export type Operations = {
  mode: string
  strategy: string
  broker: string
  market: { status?: string; note?: string; error?: string }
  api_health: Record<string, unknown>
  data_health: Record<string, unknown>
  reconciliation: {
    state?: string
    age_seconds?: number | null
    detail?: Record<string, unknown>
    error?: string
  }
  kill_switch: { level?: string; triggered?: boolean }
  ai: {
    enabled?: boolean
    override?: string
    worker_layer_state?: string | null
    provider?: string
    model?: string
    base_url?: string
    timeout_seconds?: number
  }
  live_readiness: {
    ready?: boolean
    blocked_reasons?: string[]
    checks?: Record<string, { ok: boolean; detail: unknown }>
  }
  runtime_config?: {
    available?: boolean
    config_source?: string
    capital?: {
      starting_capital?: number
      current_equity?: number | null
      max_allocation_per_trade?: number
      cash_buffer_pct?: number
      source?: string
    }
    risk?: {
      max_risk_per_trade_pct?: number
      max_daily_loss_pct?: number
      max_trades_per_day?: number
      max_concurrent_positions?: number
      max_trades_source?: string
    }
    mode?: string
    mode_source?: string
    sources?: Record<string, string>
    mismatches?: Array<{ key?: string; saved_value?: unknown; runtime_value?: unknown; message?: string; severity?: string }>
  }
  bot_running?: boolean
  generated_at?: string
}

export type AiToggleResponse = {
  success: boolean
  ai_enabled?: boolean
  message: string
}

export type ModeSwitchResponse = {
  success: boolean
  mode?: string
  ready?: boolean
  blocked_reasons?: string[]
  checks?: Record<string, { ok: boolean; detail: unknown }>
  message: string
}
