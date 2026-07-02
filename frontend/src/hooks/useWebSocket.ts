import { useEffect, useRef, useState } from 'react'

type WebSocketStatus = 'connecting' | 'connected' | 'error' | 'closed'

const rawBackendUrl = import.meta.env.VITE_BACKEND_URL?.trim() || ''
const backendUrl = rawBackendUrl
  ? rawBackendUrl.replace(/\/+$|^(https?:\/\/[^/]+)$/, (match) => {
      if (/^https?:\/\//.test(match)) {
        return match.replace(/\/+$/, '')
      }
      return match.replace(/\/+$/, '')
    })
  : '/api'
const normalizedBackendUrl = rawBackendUrl
  ? rawBackendUrl.replace(/\/+$/, '').replace(/^(https?:\/\/[^/]+)$/, '$1/api')
  : '/api'

const websocketUrl = normalizedBackendUrl.startsWith('http')
  ? `${normalizedBackendUrl.replace(/^http/, 'ws')}/ws`
  : `${window.location.origin}${normalizedBackendUrl}/ws`

export function useWebSocket() {
  const [status, setStatus] = useState<WebSocketStatus>('closed')
  const [message, setMessage] = useState<unknown>(null)
  const retryRef = useRef(0)
  const reconnectTimer = useRef<number | null>(null)

  useEffect(() => {
    let socket: WebSocket | null = null

    const connect = () => {
      setStatus('connecting')
      socket = new WebSocket(websocketUrl)

      socket.onopen = () => setStatus('connected')
      socket.onmessage = (event) => {
        try {
          setMessage(JSON.parse(event.data))
        } catch {
          setMessage(event.data)
        }
      }
      socket.onerror = () => setStatus('error')
      socket.onclose = () => {
        setStatus('closed')
        reconnectTimer.current = window.setTimeout(() => {
          retryRef.current += 1
          connect()
        }, 3000)
      }
    }

    connect()

    return () => {
      if (reconnectTimer.current) {
        window.clearTimeout(reconnectTimer.current)
      }
      if (socket) {
        socket.close()
      }
    }
  }, [])

  return { status, message, websocketUrl, backendUrl }
}
