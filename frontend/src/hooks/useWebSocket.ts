import { useCallback, useEffect, useRef, useState } from 'react'

type WebSocketStatus = 'connecting' | 'connected' | 'error' | 'closed'

const meta = import.meta as unknown as { env: Record<string, string> }
const backendUrl = (meta.env.VITE_BACKEND_URL ?? '').replace(/\/+$/, '').replace(/\/api$/, '')
const wsBase = backendUrl
  ? backendUrl.replace(/^http/, 'ws')
  : `${typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss' : 'ws'}://${typeof window !== 'undefined' ? window.location.host : 'localhost:8000'}`

const WS_URL = `${wsBase}/api/ws`

export function useWebSocket() {
  const [status, setStatus] = useState<WebSocketStatus>('closed')
  const [message, setMessage] = useState<unknown>(null)
  const [prices, setPrices] = useState<Record<string, { ltp: number; change_pct: number; volume: number }>>({})
  const socketRef = useRef<WebSocket | null>(null)
  const retryTimer = useRef<ReturnType<typeof setTimeout>>()

  const connect = useCallback(() => {
    try {
      setStatus('connecting')
      const ws = new WebSocket(WS_URL)
      socketRef.current = ws

      ws.onopen = () => setStatus('connected')

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
        }
      }

      ws.onerror = () => setStatus('error')

      ws.onclose = () => {
        setStatus('closed')
        retryTimer.current = setTimeout(connect, 4000)
      }
    } catch {
      setStatus('error')
      retryTimer.current = setTimeout(connect, 5000)
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(retryTimer.current)
      socketRef.current?.close()
    }
  }, [connect])

  return { status, message, prices, websocketUrl: WS_URL }
}
