import axios, { type AxiosError } from 'axios'

// VITE_BACKEND_URL is set in Vercel env vars pointing to your Render backend URL
// e.g. https://upstox-bot-api.onrender.com
const BACKEND_URL = import.meta.env.VITE_BACKEND_URL?.replace(/\/+$/, '') ?? ''

export const api = axios.create({
  baseURL: BACKEND_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.response.use(
  (r) => r,
  (err: AxiosError) => {
    console.error('API Error:', err.response?.data ?? err.message)
    return Promise.reject(err)
  },
)

export default api
