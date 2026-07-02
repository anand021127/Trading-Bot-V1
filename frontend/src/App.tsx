import { useEffect, useRef, useState } from 'react'

interface CapitalSnapshot {
  total: number
  available: number
  used: number
  buffer: number
}

interface RiskStatus {
  is_trading_allowed: boolean
  status: string
  consecutive_losses: number
  daily_loss_used_pct: number
  trades_used: number
  max_trades: number
  stop_reason: string | null
}

interface OverviewResponse {
  status: string
  daily_pnl: { amount: number; pct: number }
  capital: CapitalSnapshot
  today_stats: { total_trades: number; wins: number; losses: number; win_rate: number }
  risk_status: RiskStatus
  trend_bias: string
  open_positions: Array<Record<string, unknown>>
  watchlist: Array<Record<string, unknown>>
  system: {
    last_candle_seconds_ago: number
    websocket_connected: boolean
    last_api_call: string
    mode: string
    market_open: boolean
  }
}

export default function App() {
  const [overview, setOverview] = useState<OverviewResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [socketStatus, setSocketStatus] = useState('disconnected')
  const [socketMessage, setSocketMessage] = useState<unknown>(null)
  const [socketRetry, setSocketRetry] = useState(0)
  const reconnectTimer = useRef<number | null>(null)
  const backendUrl = import.meta.env.VITE_BACKEND_URL || '/api'

  useEffect(() => {
    fetch(`${backendUrl}/overview`)
      .then((res) => res.json())
      .then(setOverview)
      .catch((err) => setError(err.message))
  }, [backendUrl])

  useEffect(() => {
    const url = backendUrl.startsWith('http')
      ? `${backendUrl.replace(/^http/, 'ws')}/ws`
      : `${window.location.origin}${backendUrl}/ws`

    let socket: WebSocket | null = null

    const connect = () => {
      setSocketStatus('connecting')
      socket = new WebSocket(url)

      socket.onopen = () => setSocketStatus('connected')
      socket.onmessage = (event) => {
        try {
          setSocketMessage(JSON.parse(event.data))
        } catch {
          setSocketMessage(event.data)
        }
      }
      socket.onerror = () => setSocketStatus('error')
      socket.onclose = () => {
        setSocketStatus('closed')
        reconnectTimer.current = window.setTimeout(() => {
          setSocketRetry((retry) => retry + 1)
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
  }, [backendUrl, socketRetry])

  return (
    <div className="app">
      <header>
        <h1>Upstox Trading Bot Dashboard</h1>
      </header>
      <main>
        <section className="card">
          <h2>Overview</h2>
          {error ? (
            <div className="error">Error: {error}</div>
          ) : overview ? (
            <div>
              <p>
                <strong>Status:</strong> {overview.status}
              </p>
              <p>
                <strong>Mode:</strong> {overview.system.mode}
              </p>
              <p>
                <strong>Daily PnL:</strong> {overview.daily_pnl.amount.toFixed(2)} ({overview.daily_pnl.pct}%)
              </p>
              <p>
                <strong>Open Positions:</strong> {overview.open_positions.length}
              </p>
              <p>
                <strong>Risk Status:</strong> {overview.risk_status.status}
              </p>
            </div>
          ) : (
            <div>Loading overview...</div>
          )}
        </section>
        <section className="card">
          <h2>Live Feed</h2>
          <p>
            <strong>WebSocket:</strong> {socketStatus}
          </p>
          <div className="ws-message">
            <strong>Last message:</strong>
            <pre>{socketMessage ? JSON.stringify(socketMessage, null, 2) : 'Waiting for updates...'}</pre>
          </div>
        </section>
        <section className="card">
          <h2>Quick Stats</h2>
          {overview ? (
            <div className="grid">
              <div className="stat-card">
                <div className="stat-label">Capital Total</div>
                <div className="stat-value">₹{overview.capital.total.toLocaleString()}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Available</div>
                <div className="stat-value">₹{overview.capital.available.toLocaleString()}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Used</div>
                <div className="stat-value">₹{overview.capital.used.toLocaleString()}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Buffer</div>
                <div className="stat-value">₹{overview.capital.buffer.toLocaleString()}</div>
              </div>
            </div>
          ) : (
            <p>Loading stats...</p>
          )}
        </section>
      </main>
    </div>
  )
}
