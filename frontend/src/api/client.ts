import axios, { type AxiosError } from 'axios'

// Production backend URL on Oracle Cloud DuckDNS
const PROD_BACKEND_URL = 'https://upstoxbot-anand.duckdns.org'

function resolveBaseUrl(): string {
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

const BACKEND_URL = resolveBaseUrl()

export const api = axios.create({
  baseURL: BACKEND_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

/**
 * The ONE place the WebSocket URL is derived. It uses the same backend base as REST, so a
 * deployment that reaches the API through the production fallback (e.g. a Vercel frontend with
 * no VITE_BACKEND_URL) also reaches the WebSocket there. (Two private copies used to fall back
 * to the page's own host instead, so REST worked while the WebSocket silently pointed at a
 * host that cannot serve it.)
 */
export function buildWsUrl(): string {
  if (BACKEND_URL) {
    return BACKEND_URL.replace(/^https/, 'wss').replace(/^http/, 'ws') + '/api/ws'
  }
  const proto = typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss' : 'ws'
  const host = typeof window !== 'undefined' ? window.location.host : 'localhost'
  return `${proto}://${host}/api/ws`
}

/** Optional operator token for state-changing endpoints (backend CONTROL_TOKEN). Kept in this
 *  browser only (localStorage); never baked into the bundle. */
export const CONTROL_TOKEN_KEY = 'control_token'
export function getControlToken(): string {
  try { return localStorage.getItem(CONTROL_TOKEN_KEY) ?? '' } catch { return '' }
}
export function setControlToken(token: string): void {
  try {
    if (token) localStorage.setItem(CONTROL_TOKEN_KEY, token)
    else localStorage.removeItem(CONTROL_TOKEN_KEY)
  } catch { /* storage unavailable (private mode) */ }
}

api.interceptors.request.use(config => {
  const token = getControlToken()
  if (token) config.headers.set('X-Control-Token', token)
  return config
})

/** Minimal axios-error-like shape used for narrowing (no `any`). */
interface HttpErrorLike { response?: { status?: number; data?: unknown } }

/** 401 from the backend's CONTROL_TOKEN guard (NOT an expired Upstox token). */
export function isControlTokenError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const httpErr = err as HttpErrorLike
  return httpErr.response?.status === 401 &&
    JSON.stringify(httpErr.response?.data || '').toLowerCase().includes('control token')
}

export function isAuthError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  if (isControlTokenError(err)) return false      // a rejected operator token is not an Upstox expiry
  const httpErr = err as HttpErrorLike
  const status = httpErr.response?.status
  const detail = JSON.stringify(httpErr.response?.data || '')
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
  return (err as HttpErrorLike).response?.status === 404
}

api.interceptors.response.use(
  (r) => r,
  (err: AxiosError) => {
    if (isControlTokenError(err)) {
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('control:token_required'))
      }
    } else if (isAuthError(err)) {
      console.warn('[Auth] Upstox token is expired or invalid (HTTP 401 / UDAPI100050)')
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('upstox:auth_expired'))
      }
    }
    console.error('API Error:', err.response?.data ?? err.message)
    return Promise.reject(err)
  },
)

export default api
