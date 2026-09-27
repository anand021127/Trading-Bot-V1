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
