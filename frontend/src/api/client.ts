import axios from 'axios'

const rawBackendUrl = import.meta.env.VITE_BACKEND_URL || '/api'
const backendBaseUrl = rawBackendUrl.replace(/\/+$|^(https?:\/\/[^/]+)$/, (match) => {
  if (match.startsWith('http')) {
    return match.replace(/\/+$/, '')
  }
  return match
})

const baseURL = backendBaseUrl.startsWith('http')
  ? backendBaseUrl.replace(/\/+$/, '')
  : backendBaseUrl

const apiClient = axios.create({
  baseURL,
  headers: {
    Accept: 'application/json',
    'Content-Type': 'application/json',
  },
  withCredentials: true,
})

export default apiClient
