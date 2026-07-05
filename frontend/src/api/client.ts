import axios from 'axios'

// In Vite, import.meta.env is available. The TS errors are due to missing vite/client types.
// We cast to any to work around the tsconfig issue.
const meta = import.meta as unknown as { env: Record<string, string> }
const rawUrl = (meta.env.VITE_BACKEND_URL ?? '').replace(/\/+$/, '')
const BASE_URL = rawUrl || ''

export const api = axios.create({
  baseURL: BASE_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.response.use(
  (r) => r,
  (err: unknown) => {
    const message =
      typeof err === 'object' && err !== null && 'message' in err
        ? (err as { message: string }).message
        : 'Unknown error'
    console.error('API Error:', message)
    throw err
  },
)

export default api
