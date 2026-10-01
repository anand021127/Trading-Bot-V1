/** Honest, UI-ready view of the decision pipeline for the LATEST scan.
 *  Produced by the backend (backend/paper/scan_state.py::build_pipeline) from the
 *  persisted scan record + worker heartbeat — never from a flag. */
export interface PipelineView {
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
  execution: string          // FILLED (PAPER) | REJECTED | ERROR | NO TRADE
  execution_detail?: string | null
  scan_seq?: number | null
  scan_time_ist?: string | null
  summary?: string
}
