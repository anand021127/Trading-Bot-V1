import api from './client'
import type {
  OverviewData, Trade, Position, LiveQuote,
  PerformanceResponse, DiagnosticResult, Settings,
  PaperStatus, BacktestRequest, BacktestResponse,
  HealthStatus, DiagnosticsResponse,
} from '../types'

// Health
export const fetchHealth = () =>
  api.get<HealthStatus>('/health').then(r => r.data)

// Overview
export const fetchOverview = () =>
  api.get<OverviewData>('/api/overview').then(r => r.data)

// Bot control
export const fetchBotStatus = () =>
  api.get('/api/bot/status').then(r => r.data)

export const startBot = () =>
  api.post('/api/bot/start').then(r => r.data)

export const stopBot = () =>
  api.post('/api/bot/stop').then(r => r.data)

export const killBot = () =>
  api.post('/api/bot/kill').then(r => r.data)

export const resetKillSwitch = () =>
  api.post('/api/bot/reset-kill').then(r => r.data)

// Trades
export const fetchTrades = (params?: {
  date_from?: string; date_to?: string; symbol?: string
  mode?: string; exit_reason?: string; page?: number; page_size?: number
}) => api.get<{ trades: Trade[]; total_count: number; summary: Record<string, number> }>(
  '/api/trades', { params }
).then(r => r.data)

export const fetchTradeById = (id: string) =>
  api.get<Trade>(`/api/trades/${id}`).then(r => r.data)

export const exportTradesCsv = (params?: Record<string, string>) =>
  api.get('/api/trades/export/csv', { params, responseType: 'blob' }).then(r => r.data)

// Positions
export const fetchPositions = () =>
  api.get<Position[]>('/api/positions').then(r => r.data)

export const exitPosition = (symbol: string) =>
  api.post(`/api/positions/${symbol}/exit`, { reason: 'MANUAL_EXIT' }).then(r => r.data)

// Prices
export const fetchLivePrices = () =>
  api.get<Record<string, LiveQuote>>('/api/prices/live').then(r => r.data)

export const fetchNifty50Prices = () =>
  api.get<Record<string, LiveQuote>>('/api/prices/nifty50').then(r => r.data)

// Performance
export const fetchPerformance = (params?: { date_from?: string; date_to?: string }) =>
  api.get<PerformanceResponse>('/api/performance', { params }).then(r => r.data)

// Paper trading
export const fetchPaperStatus = () =>
  api.get<PaperStatus>('/api/paper/status').then(r => r.data)

// Backtest
export const runBacktest = (params: BacktestRequest) =>
  api.post<BacktestResponse>('/api/backtest/run', params).then(r => r.data)

export const getBacktestStatus = (taskId: string) =>
  api.get(`/api/backtest/status/${taskId}`).then(r => r.data)

export const getBacktestResult = (taskId: string) =>
  api.get<BacktestResponse>(`/api/backtest/result/${taskId}`).then(r => r.data)

// Diagnostics
export const fetchDiagnostics = () =>
  api.get<DiagnosticsResponse>('/api/diagnostics').then(r => r.data)

export const runAllTests = () =>
  api.post<{ results: DiagnosticResult[]; passed: number; failed: number }>(
    '/api/diagnostics/run-all'
  ).then(r => r.data)

export const runSingleTest = (testName: string) =>
  api.post<DiagnosticResult>(`/api/diagnostics/test/${testName}`).then(r => r.data)

export const getTestHistory = () =>
  api.get('/api/diagnostics/history').then(r => r.data)

// Settings
export const fetchSettings = () =>
  api.get<Settings>('/api/settings').then(r => r.data)

export const updateSettings = (data: Partial<Settings>) =>
  api.put<{ saved: boolean; restart_required: boolean }>('/api/settings', data).then(r => r.data)

export const fetchEnvStatus = () =>
  api.get<Record<string, boolean>>('/api/settings/env-status').then(r => r.data)

export const regenerateToken = () =>
  api.post<{ auth_url: string }>('/api/settings/regenerate-token').then(r => r.data)
