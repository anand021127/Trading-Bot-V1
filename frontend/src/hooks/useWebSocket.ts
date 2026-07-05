import { useCallback, useEffect, useRef, useState } from 'react'

type WsStatus = 'connecting' | 'connected' | 'error' | 'closed'

<<<<<<< HEAD
const meta = import.meta as unknown as { env: Record<string, string> }
const backendUrl = (meta.env.VITE_BACKEND_URL ?? '').replace(/\/+$/, '').replace(/\/api$/, '')
const wsBase = backendUrl
  ? backendUrl.replace(/^http/, 'ws')
  : `${typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss' : 'ws'}://${typeof window !== 'undefined' ? window.location.host : 'localhost:8000'}`

const WS_URL = `${wsBase}/api/ws`
=======
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
>>>>>>> c7c1b38 (Updated version-4.0)

export function useWebSocket() {
  const [status, setStatus] = useState<WsStatus>('closed')
  const [message, setMessage] = useState<unknown>(null)
<<<<<<< HEAD
  const [prices, setPrices] = useState<Record<string, { ltp: number; change_pct: number; volume: number }>>({})
  const socketRef = useRef<WebSocket | null>(null)
  const retryTimer = useRef<ReturnType<typeof setTimeout>>()
=======
  const [prices, setPrices] = useState<Record<string, WsPricePayload>>({})
  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout>>()
>>>>>>> c7c1b38 (Updated version-4.0)

  const connect = useCallback(() => {
    try {
      setStatus('connecting')
      const ws = new WebSocket(WS_URL)
      socketRef.current = ws

      ws.onopen = () => setStatus('connected')

<<<<<<< HEAD
      ws.onmessage = (ev: MessageEvent) => {
        try {
          const data = JSON.parse(ev.data as string) as {
            type?: string
            payload?: { prices?: Record<string, { ltp: number; change_pct: number; volume: number }> }
          }
          setMessage(data)
          if (data?.type === 'price_update' && data?.payload?.prices) {
            setPrices((prev) => ({ ...prev, ...data.payload!.prices }))
          }
        } catch {
          setMessage(ev.data)
=======
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
>>>>>>> c7c1b38 (Updated version-4.0)
        }
      }

      ws.onerror = () => setStatus('error')
<<<<<<< HEAD

      ws.onclose = () => {
        setStatus('closed')
        retryTimer.current = setTimeout(connect, 4000)
      }
    } catch {
      setStatus('error')
      retryTimer.current = setTimeout(connect, 5000)
=======
      ws.onclose = () => {
        setStatus('closed')
        retryRef.current = setTimeout(connect, 4000)
      }
    } catch {
      setStatus('error')
      retryRef.current = setTimeout(connect, 5000)
>>>>>>> c7c1b38 (Updated version-4.0)
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
<<<<<<< HEAD
      clearTimeout(retryTimer.current)
=======
      clearTimeout(retryRef.current)
>>>>>>> c7c1b38 (Updated version-4.0)
      socketRef.current?.close()
    }
  }, [connect])

<<<<<<< HEAD
  return { status, message, prices, websocketUrl: WS_URL }
=======
  return { status, message, prices }
>>>>>>> c7c1b38 (Updated version-4.0)
}
