import { useCallback, useEffect, useRef, useState } from 'react'

type WsStatus = 'connecting' | 'connected' | 'error' | 'closed'

// Build WebSocket URL from the backend URL env var
// VITE_BACKEND_URL = https://upstox-bot-api.onrender.com
// → WS_URL         = wss://upstox-bot-api.onrender.com/api/ws
function buildWsUrl(): string {
  const raw = import.meta.env.VITE_BACKEND_URL?.replace(/\/+$/, '') ?? ''
  if (raw) {
    return raw.replace(/^https/, 'wss').replace(/^http/, 'ws') + '/api/ws'
  }
  // Local dev fallback
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

  const connect = useCallback(() => {
    try {
      setStatus('connecting')
      const ws = new WebSocket(WS_URL)
      socketRef.current = ws

      ws.onopen = () => setStatus('connected')

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
      clearTimeout(retryRef.current)
      socketRef.current?.close()
    }
  }, [connect])

  return { status, message, prices }
}
