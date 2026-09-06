import api from './client'
import { cachedFetch, TTL } from './cache'
import type {
  OverviewData, Trade, Position,
  PerformanceResponse, DiagnosticResult, Settings,
  PaperStatus, BacktestRequest, BacktestResponse,
  HealthStatus, DiagnosticsResponse,
  ScannerStatus, UniverseConfigResponse, LivePositionDetail,
} from '../types'

// Health
export const fetchHealth = () =>
  cachedFetch('health', () => api.get<HealthStatus>('/api/health').then(r => r.data), TTL.OVERVIEW)

// Overview — cached 10s, stale-while-revalidate
export const fetchOverview = () =>
  cachedFetch('overview', () => api.get<OverviewData>('/api/overview').then(r => r.data), TTL.OVERVIEW)

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

// Trades — cached 30s
export const fetchTrades = (params?: {
  date_from?: string; date_to?: string; symbol?: string
  mode?: string; exit_reason?: string; page?: number; page_size?: number
}) => {
  const key = `trades:${JSON.stringify(params ?? {})}`
  return cachedFetch(key,
    () => api.get<{ trades: Trade[]; total_count: number; summary: Record<string, number> }>(
      '/api/trades', { params }
    ).then(r => r.data),
    TTL.TRADES,
  )
}

export const fetchTradeById = (id: string) =>
  api.get<Trade>(`/api/trades/${id}`).then(r => r.data)

export const exportTradesCsv = (params?: Record<string, string>) =>
  api.get('/api/trades/export/csv', { params, responseType: 'blob' }).then(r => r.data)

// Positions
export const fetchPositions = () =>
  cachedFetch('positions', () => api.get<Position[]>('/api/positions').then(r => r.data), TTL.OVERVIEW)

export const exitPosition = (symbol: string) =>
  api.post(`/api/positions/${symbol}/exit`, { reason: 'MANUAL_EXIT' }).then(r => r.data)

// Prices — short cache, stale-while-revalidate keeps UI responsive
export const fetchLivePrices = () =>
  cachedFetch('prices:live',
    () => api.get('/api/prices/live').then(r => r.data),
    TTL.PRICES,
  )

export const fetchNifty50Prices = () =>
  cachedFetch('prices:nifty50',
    () => api.get('/api/prices/nifty50').then(r => r.data),
    TTL.PRICES,
  )

// Performance — cached 1m
export const fetchPerformance = (params?: { date_from?: string; date_to?: string }) => {
  const key = `performance:${JSON.stringify(params ?? {})}`
  return cachedFetch(key,
    () => api.get<PerformanceResponse>('/api/performance', { params }).then(r => r.data),
    TTL.PERFORMANCE,
  )
}

// Paper trading
export const fetchPaperStatus = () =>
  cachedFetch('paper:status',
    () => api.get<PaperStatus>('/api/paper/status').then(r => r.data),
    TTL.PAPER,
  )

export const fetchLivePositions = () =>
  cachedFetch('positions:live',
    () => api.get<{ positions: LivePositionDetail[] }>('/api/positions/live').then(r => r.data),
    TTL.PRICES,
  )

// Live Scanner (item #3)
export const fetchScannerStatus = () =>
  cachedFetch('scanner:status',
    () => api.get<ScannerStatus>('/api/scanner/status').then(r => r.data),
    TTL.SCANNER,
  )

export const triggerScanNow = () =>
  api.post('/api/scanner/scan-now').then(r => r.data)

// Trading Universe (item #4)
export const fetchUniverse = () =>
  cachedFetch('universe',
    () => api.get<UniverseConfigResponse>('/api/universe/').then(r => r.data),
    TTL.SETTINGS,
  )

export const updateUniverse = (body: Partial<UniverseConfigResponse>) =>
  api.put<UniverseConfigResponse>('/api/universe/', body).then(r => r.data)

// Backtest — no cache (user-triggered), async background task execution
export interface BacktestJobStatus {
  job_id: string
  task_id: string
  status: string
  progress_percent?: number
  current_symbol?: string
  completed_symbols?: number
  total_symbols?: number
  elapsed_seconds?: number
  estimated_remaining_seconds?: number | null
  current_phase?: string
  progress?: Record<string, unknown>
  error?: string | null
  error_details?: Record<string, unknown> | null
}

export const runBacktest = (params: BacktestRequest) =>
  api.post<{ job_id: string; task_id: string; status: string; message: string }>('/api/backtest/jobs', params)
    .then(r => r.data)
    .catch(err => {
      // Fallback to /api/backtest/run if needed
      if (err.response?.status === 404) {
        return api.post<{ job_id: string; task_id: string; status: string; message: string }>('/api/backtest/run', params).then(r => r.data)
      }
      throw err
    })

export const getBacktestStatus = (taskIdOrJobId: string) =>
  api.get<BacktestJobStatus>(`/api/backtest/jobs/${taskIdOrJobId}`)
    .then(r => r.data)
    .catch(err => {
      if (err.response?.status === 404) {
        return api.get<BacktestJobStatus>(`/api/backtest/status/${taskIdOrJobId}`).then(r => r.data)
      }
      throw err
    })

export const fetchActiveBacktestJob = () =>
  api.get<{ active: boolean; job: BacktestJobStatus | null }>('/api/backtest/jobs/active').then(r => r.data)

export const cancelBacktestJob = (jobId: string) =>
  api.post<{ job_id: string; task_id: string; status: string; cancelled: boolean; message: string }>(
    `/api/backtest/jobs/${jobId}/cancel`,
  ).then(r => r.data)

export const getBacktestResult = (taskIdOrJobId: string) =>
  api.get<BacktestResponse>(`/api/backtest/jobs/${taskIdOrJobId}/result`)
    .then(r => r.data)
    .catch(err => {
      if (err.response?.status === 404) {
        return api.get<BacktestResponse>(`/api/backtest/result/${taskIdOrJobId}`).then(r => r.data)
      }
      throw err
    })

export const downloadBacktestResult = async (taskIdOrJobId: string, format: 'csv' | 'json' = 'csv') => {
  let response: any
  try {
    response = await api.get(`/api/backtest/jobs/${taskIdOrJobId}/download`, {
      params: { format },
      responseType: 'blob',
    })
  } catch (err: any) {
    if (err.response?.status === 404) {
      response = await api.get(`/api/backtest/download/${taskIdOrJobId}`, {
        params: { format },
        responseType: 'blob',
      })
    } else {
      throw err
    }
  }
  // Extract filename from Content-Disposition header
  const contentDisposition = response.headers['content-disposition'] ?? ''
  const filenameMatch = contentDisposition.match(/filename="?([^";\s]+)"?/)
  const filename = filenameMatch?.[1] ?? `backtest_${taskIdOrJobId}.${format}`

  // Create download link
  const url = window.URL.createObjectURL(new Blob([response.data]))
  const link = document.createElement('a')
  link.href = url
  link.setAttribute('download', filename)
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

// Bot health (full health model)
export const fetchBotHealth = () =>
  api.get('/api/health').then(r => r.data)

// Diagnostics — no cache (user-triggered)
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

// Settings — cached 5m
export const fetchSettings = () =>
  cachedFetch('settings',
    () => api.get<Settings>('/api/settings').then(r => r.data),
    TTL.SETTINGS,
  )

export const updateSettings = (data: Partial<Settings>) =>
  api.put<{ saved: boolean; restart_required: boolean }>('/api/settings', data).then(r => r.data)

export const fetchEnvStatus = () =>
  api.get<Record<string, boolean>>('/api/settings/env-status').then(r => r.data)

export const fetchAuthUrl = () =>
  api.get<{ auth_url: string; redirect_uri: string }>('/api/settings/auth-url').then(r => r.data)

export const regenerateToken = () =>
  api.get<{ auth_url: string; redirect_uri: string }>('/api/settings/auth-url')
    .then(r => r.data)
    .catch(() => api.post<{ auth_url: string; redirect_uri: string }>('/api/settings/regenerate-token').then(r => r.data))

export const disconnectToken = () =>
  api.post<{ status: string; message: string }>('/api/settings/disconnect-token').then(r => r.data)
