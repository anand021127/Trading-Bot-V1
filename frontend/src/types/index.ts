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
  last_candle_seconds_ago?: number
  websocket_connected?: boolean
  last_api_call?: string
  mode?: string
  market_open?: boolean
}

export interface OverviewData {
  daily_pnl: { amount: number; pct: number }
  capital: { total: number; available: number; used: number; buffer: number }
  today_stats: { total_trades: number; wins: number; losses: number; win_rate: number }
  risk_status: RiskStatus
  trend_bias?: string
  open_positions?: Position[]
  watchlist?: WatchlistItem[]
  system?: SystemStatus
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
    sender_email?: string
    recipient_email?: string
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

// Legacy aliases
export type AppSettingsResponse = Settings
export type HealthStatus = { status: string; mode?: string; timestamp?: string }
export type DiagnosticsResponse = { results?: DiagnosticResult[]; passed?: number; failed?: number }

export interface BacktestRequest {
  start_date: string
  end_date: string
  commission_pct?: number
  slippage_pct?: number
  symbols?: string[]
  capital?: number
}

export interface BacktestResponse {
  total_trades?: number
  win_rate?: number
  profit_factor?: number
  net_profit?: number
  max_drawdown_pct?: number
  sharpe_ratio?: number
  avg_win_r?: number
  avg_loss_r?: number
  trade_log?: Trade[]
  equity_curve?: Array<{ date: string; value: number }>
  monthly_returns?: Record<string, number>
}
