export interface Trade {
  id: string
  trade_id?: string
  symbol: string
  side: string
  quantity: number
  price: number
  timestamp: string
  strategy?: string
  status?: string
  pnl?: number | null
  notes?: string | null
  mode?: 'paper' | 'live'
  entry_time?: string
  exit_time?: string | null
  entry_price?: number
  exit_price?: number | null
  initial_stop?: number
  final_stop?: number
  exit_reason?: string | null
  gross_pnl?: number | null
  net_pnl?: number | null
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
  average_price: number
  side: string
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

export interface OverviewPosition extends Position {
  entry_time: string
  average_price: number
  side: string
  symbol: string
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
  is_trading_allowed: boolean
  status: 'ACTIVE' | 'PAUSED' | 'STOPPED'
  consecutive_losses: number
  daily_loss_used_pct: number
  trades_used: number
  max_trades: number
  stop_reason: string | null
  daily_pnl: number
  daily_pnl_pct: number
}

export interface WatchlistItem {
  symbol: string
  status: 'WATCHING' | 'IN_TRADE' | 'SKIPPED'
  orb_high: number
  orb_low: number
  trend_bias: string
  last_price: number
  skip_reason?: string
}

export interface SystemStatus {
  last_candle_seconds_ago: number
  websocket_connected: boolean
  last_api_call: string
  mode: 'paper' | 'live' | 'backtest'
  market_open: boolean
}

export interface OverviewData {
  daily_pnl: { amount: number; pct: number }
  capital: { total: number; available: number; used: number; buffer: number }
  today_stats: { total_trades: number; wins: number; losses: number; win_rate: number }
  risk_status: RiskStatus
  trend_bias: string
  open_positions: OverviewPosition[]
  watchlist: WatchlistItem[]
  system: SystemStatus
}

export interface PerformanceSnapshot {
  date: string
  net_pnl: number
  trades_count: number
  win_rate: number
  equity: number
  created_at: string
}

export interface PerformanceResponse {
  performance: PerformanceSnapshot[]
  total_snapshots: number
}

export interface AppSettingsResponse {
  mode: string
  api_host: string
  api_port: number
  broker_base_url: string
  websocket_url: string
  frontend_url: string
  notifications: {
    email_enabled: boolean
    telegram_enabled: boolean
  }
}

export interface DiagnosticsResponse {
  status: string
  mode: string
  timestamp: string
  api_host: string
  api_port: number
  broker_base_url: string
  websocket_url: string
  frontend_url?: string
  upstox_redirect_uri?: string
}

export interface HealthStatus {
  status: string
  mode: string
  timestamp: string
}

export interface BacktestRequest {
  start_date?: string
  end_date?: string
  commission_pct?: number
  slippage_pct?: number
}

export interface BacktestResponse {
  status: string
  mode: string
  requested: BacktestRequest
  summary: {
    total_trades: number
    net_pnl: number
    win_rate: number
    start_date: string
    end_date: string
    ran_at: string
  }
}

export interface OrderExecutionResult {
  status: string
  order: {
    id: string
    symbol: string
    side: string
    quantity: number
    price: number
    order_type: string
    status: string
  }
}

export interface PerformanceMetrics {
  total_trades: number
  win_rate: number
  avg_win_r: number
  avg_loss_r: number
  profit_factor: number
  expectancy_r: number
  max_drawdown_pct: number
  sharpe_ratio: number
  sortino_ratio: number
  calmar_ratio: number
  net_profit: number
  net_profit_pct: number
  best_day: number
  worst_day: number
  avg_daily_pnl: number
}

export interface DiagnosticResult {
  test_name: string
  status: 'PASS' | 'FAIL' | 'RUNNING' | 'PENDING'
  response_time_ms: number | null
  details: string
  error: string | null
}

export interface PaperChecklistItem {
  value: number | boolean
  target?: number
  pass: boolean
}

export interface PaperChecklist {
  win_rate_ok: PaperChecklistItem
  profit_factor_ok: PaperChecklistItem
  max_drawdown_ok: PaperChecklistItem
  logs_complete: PaperChecklistItem
  orb_filter_ok: PaperChecklistItem
  choppiness_filter_ok: PaperChecklistItem
  time_window_ok: PaperChecklistItem
}

export interface Settings {
  mode: string
}
