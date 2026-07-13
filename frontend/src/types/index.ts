export interface Trade {
  id: string
  trade_id?: string
  symbol: string
  mode?: 'paper' | 'live'
  side?: string
  entry_time?: string
  exit_time?: string | null
  entry_price?: number
  exit_price?: number | null
  quantity: number
  price?: number
  timestamp?: string
  initial_stop?: number
  final_stop?: number
  exit_reason?: string | null
  gross_pnl?: number | null
  net_pnl?: number | null
  pnl?: number | null
  brokerage?: number | null
  stt?: number | null
  pnl_r?: number | null
  trade_duration_min?: number | null
  stage_at_exit?: number | null
  orb_high?: number
  orb_low?: number
  atr_at_entry?: number
  rsi_at_entry?: number
  choppiness_at_entry?: number
  volume_ratio?: number
  ema20_at_entry?: number
  ema50_at_entry?: number
  trend_bias?: string
  max_favorable?: number | null
  max_adverse?: number | null
  conditions_checked?: Record<string, boolean> | null
}

export interface Position {
  symbol: string
  entry_time: string
  average_price?: number
  entry_price?: number
  side?: string
  quantity: number
  unrealized_pnl: number
  current_price?: number
  initial_stop?: number
  trailing_stop?: number
  stage?: 1 | 2 | 3 | 4
  unrealized_pnl_pct?: number
  unrealized_r?: number
  time_in_trade_min?: number
  orb_high?: number
  orb_low?: number
  trend_bias?: string
  mode?: string
}

export interface LiveQuote {
  symbol: string
  ltp: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  change: number
  change_pct: number
  timestamp: string
  market_closed?: boolean
}

export interface RiskStatus {
  is_trading_allowed?: boolean
  status: string
  consecutive_losses?: number
  daily_loss_used_pct?: number
  trades_used?: number
  max_trades?: number
  stop_reason?: string | null
  daily_pnl?: number
  daily_pnl_pct?: number
}

export interface WatchlistItem {
  symbol: string
  status: string
  orb_high?: number
  orb_low?: number
  trend_bias?: string
  last_price?: number
  skip_reason?: string
}

export interface SystemStatus {
  last_candle_seconds_ago?: number | null
  websocket_connected?: boolean
  websocket_status?: string
  active_frontend_connections?: number
  api_health?: string
  last_api_call?: string
  mode?: string
  market_open?: boolean
}

export interface UniverseSummary {
  mode: string
  watching_count: number
}

export interface ScannerSummary {
  is_running: boolean
  currently_analyzing?: string | null
  last_signal?: {
    symbol: string
    strategy_name: string
    signal: string
    confidence: number
    entry_reason: string
  } | null
}

export interface OverviewData {
  daily_pnl: { amount: number; pct: number }
  capital: { total: number; available: number; used: number; buffer: number }
  today_stats: { total_trades: number; wins: number; losses: number; win_rate: number; net_pnl?: number }
  risk_status: RiskStatus
  trend_bias?: string
  open_positions?: Position[]
  watchlist?: WatchlistItem[]
  system?: SystemStatus
  universe?: UniverseSummary
  scanner?: ScannerSummary
}

export interface PerformanceMetrics {
  total_trades?: number
  win_rate?: number
  avg_win_r?: number
  avg_loss_r?: number
  profit_factor?: number
  expectancy_r?: number
  max_drawdown_pct?: number
  sharpe_ratio?: number
  sortino_ratio?: number
  calmar_ratio?: number
  net_profit?: number
  net_profit_pct?: number
  best_day?: number
  worst_day?: number
  avg_daily_pnl?: number
}

export interface PerformanceResponse {
  metrics?: PerformanceMetrics
  performance?: Array<{
    date: string
    net_pnl: number
    trades_count: number
    win_rate: number
    equity: number
  }>
  equity_curve?: Array<{ date: string; value: number }>
  monthly_returns?: Record<string, number>
}

export interface DiagnosticResult {
  test_name: string
  status: 'PASS' | 'FAIL' | 'RUNNING' | 'PENDING'
  response_time_ms?: number | null
  details?: string
  error?: string | null
}

export interface DiagnosticsResponse {
  results?: DiagnosticResult[]
  passed?: number
  failed?: number
  available_tests?: string[]
}

export interface PaperChecklistItem {
  value: number | boolean
  target?: number
  pass: boolean
}

export interface PaperStatus {
  days_active?: number
  days_required?: number
  is_ready?: boolean
  checklist?: {
    win_rate_ok?: PaperChecklistItem
    profit_factor_ok?: PaperChecklistItem
    max_drawdown_ok?: PaperChecklistItem
    logs_complete?: PaperChecklistItem
    orb_filter_ok?: PaperChecklistItem
    choppiness_filter_ok?: PaperChecklistItem
    time_window_ok?: PaperChecklistItem
  }
  daily_history?: Array<{
    date: string
    trades: number
    wins: number
    losses: number
    net_pnl: number
    drawdown: number
    trend_bias: string
  }>
}

export interface Settings {
  mode?: string
  api_host?: string
  api_port?: number
  broker_base_url?: string
  websocket_url?: string
  notifications?: {
    email_enabled?: boolean
    telegram_enabled?: boolean
    sender_email?: boolean | string
    recipient_email?: boolean | string
  }
  capital?: { total: number; max_allocation_per_trade: number; cash_buffer: number }
  risk?: {
    max_risk_per_trade_pct: number
    max_daily_loss_pct: number
    max_trades_per_day: number
    max_concurrent_positions: number
    max_consecutive_losses: number
  }
  strategy?: {
    orb_window_start?: string
    orb_window_end?: string
    entry_window_start?: string
    entry_window_end?: string
    exit_all_by?: string
  }
  indicators?: {
    ema_fast?: number
    ema_slow?: number
    ema_trend?: number
    rsi_period?: number
    rsi_min?: number
    rsi_max?: number
    atr_period?: number
    choppiness_max?: number
    volume_multiplier?: number
  }
}

export type AppSettingsResponse = Settings
export type HealthStatus = { status: string; mode?: string; timestamp?: string }

export interface BacktestRequest {
  start_date?: string
  end_date?: string
  commission_pct?: number
  slippage_pct?: number
  stt_pct?: number
  symbols?: string[]
  capital?: number
  interval?: string          // 1minute|3minute|5minute|15minute|30minute|day
  strategies?: string[]      // EMA_TREND | ORB
  risk_pct_per_trade?: number
}

export interface BacktestTrade {
  symbol: string
  strategy: string
  entry_time: string
  exit_time: string
  entry_price: number
  exit_price: number
  quantity: number
  exit_reason: string
  gross_pnl: number
  net_pnl: number
  charges: number
  confidence: number
}

export interface RejectedSignalSample {
  symbol: string
  timestamp: string
  strategy: string
  reasons: string[]
}

export interface BacktestResponse {
  total_candles_scanned?: number
  signals_generated?: number
  trades_taken?: number
  winning_trades?: number
  losing_trades?: number
  accuracy_pct?: number
  profit_factor?: number
  net_profit?: number
  net_profit_pct?: number
  max_drawdown_pct?: number
  total_charges?: number
  equity_curve?: Array<{ timestamp: string; equity: number }>
  trade_log?: BacktestTrade[]
  rejected_signals_sample?: RejectedSignalSample[]
  rejected_signals_total_count?: number
  rejection_reason_counts?: Record<string, number>
  skipped_symbols?: Array<{ symbol: string; reason: string }>
  data_source?: string
  fetch_errors?: Array<{ symbol: string; error: string }>
  symbols_requested?: string[]
  date_range?: { start: string; end: string }
  interval?: string
  message?: string
}

// ── Live Scanner (item #3) ──────────────────────────────────────────────

export interface ScannerStrategyBreakdown {
  strategy_name: string
  symbol: string
  signal: string
  confidence: number
  entry_reason: string
  conditions: Record<string, boolean>
  conditions_passed: number
  conditions_total: number
  rejected_reasons: string[]
  indicators: Record<string, unknown>
}

export interface ScannerEntry {
  symbol: string
  ltp: number | null
  scanned_at: string
  ema_status: string
  rsi_value: number | null
  rsi_status: string
  atr: number | null
  volume_status: string
  trend: string
  decision: string
  signal: string
  confidence: number
  rejected_reasons: string[]
  strategy_breakdown: ScannerStrategyBreakdown[]
  error: string | null
}

export interface ScannerStatus {
  is_running: boolean
  currently_scanning: string | null
  last_full_pass_completed_at: string | null
  watching_count: number
  results: ScannerEntry[]
}

// ── Trading Universe (item #4) ──────────────────────────────────────────

export interface UniverseConfigResponse {
  mode: string
  index: string
  custom_symbols: string[]
  max_symbols: number
  option_indices: string[]
  resolved_symbols: string[]
  valid_modes: string[]
  valid_option_indices: string[]
  nifty50_constituents: string[]
}

// ── Live Positions detail (item #7) ─────────────────────────────────────

export interface LivePositionDetail {
  symbol: string
  strategy_used: string
  entry_price: number
  target: number
  stop_loss: number
  trailing_stop: number
  quantity: number
  current_price: number | null
  current_pnl: number | null
  current_pnl_pct: number | null
  mode: string
  entry_time: string | null
}
