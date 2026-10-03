/** Honest, UI-ready view of the decision pipeline for the LATEST scan.
 *  Produced by the backend (backend/paper/scan_state.py::build_pipeline) from the
 *  persisted scan record + worker heartbeat — never from a flag. */
export type Outcome =
  | 'NO_SIGNAL' | 'SIGNAL_REJECTED' | 'AI_REJECTED' | 'RISK_REJECTED' | 'EXECUTION_REJECTED'
  | 'FILLED' | 'MARKET_CLOSED' | 'DATA_ERROR' | 'SCANNER_ERROR' | 'NO_TRADE'

export interface V8dCondition { pass: boolean; detail: string }

export interface V8dSide {
  trend: V8dCondition | boolean
  pullback: V8dCondition | boolean
  rsi: V8dCondition | boolean
  reversal: V8dCondition | boolean
  all_pass?: boolean
  failed?: string[]
}

/** V8-D condition-level explanation (backend/strategy/v8d_diagnostics.py — same indicators/thresholds as the strategy). */
export interface V8dDiagnostics {
  evaluated?: boolean
  decision?: 'CE' | 'PE' | null
  decision_label?: string
  candle_count?: number
  ema20?: number | null
  ema50?: number | null
  ema_separation_pct?: number | null
  rsi?: number | null
  atr14_underlying?: number | null
  price?: { close?: number; open?: number; high?: number; low?: number; prev_high?: number; prev_low?: number }
  pullback_band?: { ce?: [number, number]; pe?: [number, number] }
  closest_side?: string
  binding_condition?: string
  failed?: string[]
  reason?: string
  strategy_decision?: string | null
  consistent?: boolean
  ce?: V8dSide
  pe?: V8dSide
}

export interface SymbolCoverage {
  symbol: string
  outcome?: Outcome | null
  reason?: string
  recorded_at_ist?: string | null
  candle_count?: number | null
  candle_complete?: boolean | null
  option_chain_count?: number | null
  v8d_label?: string | null
  binding?: string | null
  failed?: string[] | null
  ema20?: number | null
  ema50?: number | null
  sep_pct?: number | null
  rsi?: number | null
  ai_status?: string | null
  error?: string | null
}

export interface PipelineView {
  outcome?: Outcome | null   // what stopped this scan (NO_SIGNAL is never "rejected")
  final?: string             // NO TRADE | FILLED (PAPER)
  v8d?: V8dDiagnostics | null
  primary_symbol?: string | null
  symbols?: SymbolCoverage[]
  scanner: string            // RUNNING | STOPPED | STARTING | NOT RESPONDING | RUNNING — DATA ERROR …
  market: string             // LIVE | MARKET CLOSED | LIVE — ENTRY WINDOW CLOSED | UNKNOWN
  strategy: string
  latest_signal: string      // BUY CE | BUY PE | NO SIGNAL | NOT EVALUATED — MARKET CLOSED | NO SCAN YET …
  signal_detail?: string | null
  ai_decision: string        // APPROVED | REJECTED | WAIT (NO TRADE) | UNAVAILABLE — FAILED SAFE (NO TRADE) | DISABLED | NOT EVALUATED
  ai_reason?: string | null
  ai_confidence?: number | null
  ai_latency_ms?: number | null
  ai_enabled?: boolean | null
  risk_check: string         // PASS | REJECTED | NOT EVALUATED
  risk_detail?: string | null
  execution: string          // FILLED (PAPER) | REJECTED | ERROR | NOT ATTEMPTED
  execution_detail?: string | null
  scan_seq?: number | null
  scan_time_ist?: string | null
  summary?: string
}
