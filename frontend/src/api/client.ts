import axios, { type AxiosError } from 'axios'

<<<<<<< HEAD
// In Vite, import.meta.env is available. The TS errors are due to missing vite/client types.
// We cast to any to work around the tsconfig issue.
const meta = import.meta as unknown as { env: Record<string, string> }
const rawUrl = (meta.env.VITE_BACKEND_URL ?? '').replace(/\/+$/, '')
const BASE_URL = rawUrl || ''

export const api = axios.create({
  baseURL: BASE_URL,
=======
// VITE_BACKEND_URL is set in Vercel env vars pointing to your Render backend URL
// e.g. https://upstox-bot-api.onrender.com
const BACKEND_URL = import.meta.env.VITE_BACKEND_URL?.replace(/\/+$/, '') ?? ''

export const api = axios.create({
  baseURL: BACKEND_URL,
>>>>>>> c7c1b38 (Updated version-4.0)
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.response.use(
  (r) => r,
<<<<<<< HEAD
  (err: unknown) => {
    const message =
      typeof err === 'object' && err !== null && 'message' in err
        ? (err as { message: string }).message
        : 'Unknown error'
    console.error('API Error:', message)
    throw err
=======
  (err: AxiosError) => {
    console.error('API Error:', err.response?.data ?? err.message)
    return Promise.reject(err)
>>>>>>> c7c1b38 (Updated version-4.0)
  },
)

export default api
