import { useCallback, useEffect, useRef, useState } from 'react'

export type WsStatus = 'connecting' | 'connected' | 'error' | 'closed'

// Build WebSocket URL from the backend URL env var
function buildWsUrl(): string {
  const raw = import.meta.env.VITE_BACKEND_URL?.replace(/\/+$/, '') ?? ''
  if (raw) {
    return raw.replace(/^https/, 'wss').replace(/^http/, 'ws') + '/api/ws'
  }
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/api/ws`
}

const WS_URL = buildWsUrl()

export interface WsPricePayload {
  ltp: number
  change_pct: number
  volume: number
}

export function useWebSocket() {
  const [status, setStatus] = useState<WsStatus>('closed')
  const [message, setMessage] = useState<unknown>(null)
  const [prices, setPrices] = useState<Record<string, WsPricePayload>>({})
  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout>>()
  const pingRef = useRef<ReturnType<typeof setInterval>>()

  const connect = useCallback(() => {
    if (typeof window === 'undefined' || !navigator.onLine) {
      setStatus('closed')
      return
    }

    try {
      setStatus('connecting')
      const wsUrl = buildWsUrl()
      const ws = new WebSocket(wsUrl)
      socketRef.current = ws

      ws.onopen = () => {
        setStatus('connected')
        // Send keep-alive ping every 20s to prevent proxy idle dropouts
        clearInterval(pingRef.current)
        pingRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            try {
              ws.send(JSON.stringify({ type: 'ping', timestamp: Date.now() }))
            } catch {
              // ignore
            }
          }
        }, 20000)
      }

      ws.onmessage = ({ data }: MessageEvent<string>) => {
        try {
          const parsed = JSON.parse(data) as {
            type?: string
            payload?: { prices?: Record<string, WsPricePayload> }
          }
          setMessage(parsed)
          if (parsed.type === 'price_update' && parsed.payload?.prices) {
            setPrices((prev) => ({ ...prev, ...parsed.payload!.prices }))
          }
        } catch {
          setMessage(data)
        }
      }

      ws.onerror = () => setStatus('error')
      ws.onclose = () => {
        setStatus('closed')
        clearInterval(pingRef.current)
        retryRef.current = setTimeout(connect, 4000)
      }
    } catch {
      setStatus('error')
      retryRef.current = setTimeout(connect, 5000)
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      clearInterval(pingRef.current)
      clearTimeout(retryRef.current)
      socketRef.current?.close()
    }
  }, [connect])

  return { status, message, prices }
}
