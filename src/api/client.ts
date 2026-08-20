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

export default api
