import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react'
import api from '../api/client'

export type ConnectionState =
  | 'CONNECTED'
  | 'MARKET_CLOSED'
  | 'DEGRADED'
  | 'AUTH_EXPIRED'
  | 'RECONNECTING'
  | 'BACKEND_UNAVAILABLE'
  | 'OFFLINE'

export interface WsPricePayload {
  ltp: number
  change_pct: number
  volume: number
}

interface ConnectionContextType {
  status: ConnectionState
  isMarketOpen: boolean
  isAuthExpired: boolean
  isBackendReachable: boolean
  isWsConnected: boolean
  prices: Record<string, WsPricePayload>
  lastHeartbeat: Date | null
  setAuthExpired: (expired: boolean) => void
  reportApiSuccess: () => void
  reportApiFailure: (err: unknown) => void
  reconnect: () => void
}

const ConnectionContext = createContext<ConnectionContextType | null>(null)

function checkIsMarketOpen(): boolean {
  const now = new Date()
  const istOffset = 5.5 * 60 * 60 * 1000
  const istTime = new Date(now.getTime() + (now.getTimezoneOffset() * 60000) + istOffset)
  const day = istTime.getDay()
  if (day === 0 || day === 6) return false // Sunday or Saturday
  const hours = istTime.getHours()
  const minutes = istTime.getMinutes()
  const totalMin = hours * 60 + minutes
  return totalMin >= 9 * 60 + 15 && totalMin <= 15 * 60 + 30
}

function buildWsUrl(): string {
  const raw = import.meta.env.VITE_BACKEND_URL?.replace(/\/+$/, '') ?? ''
  if (raw) {
    return raw.replace(/^https/, 'wss').replace(/^http/, 'ws') + '/api/ws'
  }
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/api/ws`
}

export function ConnectionProvider({ children }: { children: React.ReactNode }) {
  const [isOnline, setIsOnline] = useState(() => (typeof navigator !== 'undefined' ? navigator.onLine : true))
  const [isMarketOpen, setIsMarketOpen] = useState(checkIsMarketOpen)
  const [isAuthExpired, setIsAuthExpired] = useState(false)
  const [isWsConnected, setIsWsConnected] = useState(false)
  const [isBackendReachable, setIsBackendReachable] = useState(true)
  const [isReconnecting, setIsReconnecting] = useState(false)
  const [prices, setPrices] = useState<Record<string, WsPricePayload>>({})
  const [lastHeartbeat, setLastHeartbeat] = useState<Date | null>(null)

  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout>>()
  const pingIntervalRef = useRef<ReturnType<typeof setInterval>>()
  const consecutiveFailuresRef = useRef(0)

  // Listen for browser online / offline
  useEffect(() => {
    const onOnline = () => {
      setIsOnline(true)
      connectWs()
    }
    const onOffline = () => {
      setIsOnline(false)
      setIsWsConnected(false)
    }

    window.addEventListener('online', onOnline)
    window.addEventListener('offline', onOffline)
    return () => {
      window.removeEventListener('online', onOnline)
      window.removeEventListener('offline', onOffline)
    }
  }, [])

  // Market hours ticker
  useEffect(() => {
    const timer = setInterval(() => {
      setIsMarketOpen(checkIsMarketOpen())
    }, 15000)
    return () => clearInterval(timer)
  }, [])

  // Connect WebSocket with Ping-Pong Keepalive
  const connectWs = useCallback(() => {
    if (typeof window === 'undefined') return
    if (!navigator.onLine) {
      setIsWsConnected(false)
      return
    }

    try {
      setIsReconnecting(true)
      const url = buildWsUrl()
      const ws = new WebSocket(url)
      socketRef.current = ws

      ws.onopen = () => {
        setIsWsConnected(true)
        setIsReconnecting(false)
        setIsBackendReachable(true)
        consecutiveFailuresRef.current = 0
        setLastHeartbeat(new Date())

        // Send keep-alive ping every 20 seconds to prevent Cloud Run/proxy idle timeouts
        clearInterval(pingIntervalRef.current)
        pingIntervalRef.current = setInterval(() => {
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
        setLastHeartbeat(new Date())
        try {
          const parsed = JSON.parse(data) as {
            type?: string
            payload?: { prices?: Record<string, WsPricePayload> }
          }
          if (parsed.type === 'price_update' && parsed.payload?.prices) {
            setPrices((prev) => ({ ...prev, ...parsed.payload!.prices }))
          }
        } catch {
          // ignore non-json messages
        }
      }

      ws.onerror = () => {
        setIsWsConnected(false)
      }

      ws.onclose = () => {
        setIsWsConnected(false)
        clearInterval(pingIntervalRef.current)
        // Automatic retry with exponential-like short delay
        clearTimeout(retryRef.current)
        retryRef.current = setTimeout(connectWs, 4000)
      }
    } catch {
      setIsWsConnected(false)
      clearTimeout(retryRef.current)
      retryRef.current = setTimeout(connectWs, 5000)
    }
  }, [])

  useEffect(() => {
    connectWs()
    return () => {
      clearInterval(pingIntervalRef.current)
      clearTimeout(retryRef.current)
      socketRef.current?.close()
    }
  }, [connectWs])

  // Periodic health check ping
  useEffect(() => {
    const healthInterval = setInterval(async () => {
      try {
        const res = await api.get('/api/health')
        if (res.data) {
          setIsBackendReachable(true)
          consecutiveFailuresRef.current = 0
          setLastHeartbeat(new Date())
        }
      } catch (err: any) {
        if (err?.response?.status === 401) {
          setIsAuthExpired(true)
        } else if (err?.code === 'ECONNABORTED' || !err?.response) {
          consecutiveFailuresRef.current += 1
          if (consecutiveFailuresRef.current >= 3) {
            setIsBackendReachable(false)
          }
        }
      }
    }, 25000)

    return () => clearInterval(healthInterval)
  }, [])

  const reportApiSuccess = useCallback(() => {
    setIsBackendReachable(true)
    consecutiveFailuresRef.current = 0
    setLastHeartbeat(new Date())
  }, [])

  const reportApiFailure = useCallback((err: any) => {
    if (err?.response?.status === 401 || err?.response?.data?.status === 'AUTH_EXPIRED') {
      setIsAuthExpired(true)
      return
    }
    consecutiveFailuresRef.current += 1
    if (consecutiveFailuresRef.current >= 3) {
      setIsBackendReachable(false)
    }
  }, [])

  // Derive consolidated connection status
  let status: ConnectionState = 'CONNECTED'
  if (!isOnline) {
    status = 'OFFLINE'
  } else if (isAuthExpired) {
    status = 'AUTH_EXPIRED'
  } else if (!isBackendReachable) {
    status = 'BACKEND_UNAVAILABLE'
  } else if (isReconnecting && !isWsConnected) {
    status = 'RECONNECTING'
  } else if (!isWsConnected) {
    status = 'DEGRADED' // HTTP is working, WS reconnecting
  } else if (!isMarketOpen) {
    status = 'MARKET_CLOSED'
  } else {
    status = 'CONNECTED'
  }

  const reconnect = useCallback(() => {
    consecutiveFailuresRef.current = 0
    setIsBackendReachable(true)
    connectWs()
  }, [connectWs])

  return (
    <ConnectionContext.Provider
      value={{
        status,
        isMarketOpen,
        isAuthExpired,
        isBackendReachable,
        isWsConnected,
        prices,
        lastHeartbeat,
        setAuthExpired: setIsAuthExpired,
        reportApiSuccess,
        reportApiFailure,
        reconnect,
      }}
    >
      {children}
    </ConnectionContext.Provider>
  )
}

export function useConnection() {
  const context = useContext(ConnectionContext)
  if (!context) {
    throw new Error('useConnection must be used within a ConnectionProvider')
  }
  return context
}
