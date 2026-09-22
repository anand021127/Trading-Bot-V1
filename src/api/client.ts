import axios, { type AxiosError } from 'axios'

// Production backend URL on Oracle Cloud DuckDNS
export const PROD_BACKEND_URL = 'https://upstoxbot-anand.duckdns.org'

export function resolveBaseUrl(): string {
  // If explicitly configured via VITE_BACKEND_URL
  const envUrl = import.meta.env.VITE_BACKEND_URL?.replace(/\/+$/, '')
  if (envUrl && envUrl !== 'undefined' && envUrl !== 'null') {
    return envUrl
  }

  // If running in local dev / sandbox container (where express/vite proxy handles /api)
  if (typeof window !== 'undefined') {
    const host = window.location.hostname
    if (
      host === 'localhost' ||
      host === '127.0.0.1' ||
      host.includes('ais-dev') ||
      host.includes('ais-pre') ||
      host.includes('run.app') ||
      host.includes('duckdns.org')
    ) {
      return ''
    }
  }

  // Deployed external frontend (e.g. Vercel production) -> target production DuckDNS backend
  return PROD_BACKEND_URL
}

export function buildWsUrl(): string {
  const base = resolveBaseUrl()
  if (base) {
    return base.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:') + '/api/ws'
  }
  const proto = typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = typeof window !== 'undefined' ? window.location.host : 'localhost:3000'
  return `${proto}//${host}/api/ws`
}

const BACKEND_URL = resolveBaseUrl()

export const api = axios.create({
  baseURL: BACKEND_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

export function isAuthError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const anyErr = err as any
  const status = anyErr.response?.status
  const detail = JSON.stringify(anyErr.response?.data || '')
  return (
    status === 401 ||
    detail.includes('UDAPI100050') ||
    detail.includes('Invalid token') ||
    detail.includes('AUTH_EXPIRED') ||
    detail.includes('Token invalid or expired')
  )
}

export function isNotFoundError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const anyErr = err as any
  return anyErr.response?.status === 404
}

api.interceptors.response.use(
  (r) => r,
  (err: AxiosError) => {
    if (isAuthError(err)) {
      console.warn('[Auth] Upstox token is expired or invalid (HTTP 401 / UDAPI100050)')
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('upstox:auth_expired'))
      }
    }
    console.error('API Error:', err.response?.data ?? err.message)
    return Promise.reject(err)
  },
)

export function formatApiError(err: unknown, fallback = 'Request failed'): string {
  if (!err || typeof err !== 'object') return fallback
  const anyErr = err as any
  const status = anyErr.response?.status as number | undefined
  const detail = anyErr.response?.data?.detail ?? anyErr.response?.data?.message ?? anyErr.response?.data
  const detailText = typeof detail === 'string' ? detail : (detail && typeof detail === 'object' && detail.message) ? String(detail.message) : ''
  if (anyErr.code === 'ECONNABORTED' || String(anyErr.message || '').toLowerCase().includes('timeout')) {
    return 'The request timed out. If a long job is running, use status polling instead of waiting on a single request.'
  }
  if (status === 400) return detailText || 'Request was rejected (missing token, invalid dates, or invalid strategy).'
  if (status === 401) return 'Upstox token is missing or expired. Open Settings and generate a new token.'
  if (status === 403) return 'Not authorized to perform this action.'
  if (status === 404) return detailText || 'Resource not found.'
  if (status === 409) return detailText || 'A job is already running.'
  if (status === 422) return detailText || 'Validation failed.'
  if (status === 429) return 'Rate limited. Wait and retry.'
  if (status === 500) return 'Backend error. Check server logs; this is not a frontend timeout.'
  if (status === 502 || status === 503 || status === 504) return 'Backend unreachable or restarting.'
  if (!anyErr.response) return 'Backend unreachable. Check that the API host is running and CORS allows this origin.'
  return detailText || anyErr.message || fallback
}

export default api
