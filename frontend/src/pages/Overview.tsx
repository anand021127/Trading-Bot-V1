import { useEffect, useState } from 'react'
import { fetchOverview } from '../api/endpoints'
import MetricCard from '../components/MetricCard'
import StatusBadge from '../components/StatusBadge'
import { formatCurrency, formatPercent } from '../utils/formatters'
import { OverviewData } from '../types'
import { useWebSocket } from '../hooks/useWebSocket'
import { usePolling } from '../hooks/usePolling'

export default function Overview() {
  const [overview, setOverview] = useState<OverviewData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const { status: socketStatus, message: socketMessage } = useWebSocket()

  const refreshOverview = async () => {
    try {
      const data = await fetchOverview()
      setOverview(data)
    } catch (err) {
      if (err instanceof Error) {
        setError(err.message)
      } else {
        setError('Unable to load overview')
      }
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    refreshOverview()
  }, [])

  usePolling(refreshOverview, 15000)

  return (
    <div className="space-y-8">
      <div className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">Live trading dashboard</p>
            <h1 className="mt-3 text-4xl font-semibold text-white">Professional trading insights</h1>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={overview?.risk_status.status ?? 'DISCONNECTED'} />
            <div className="rounded-3xl bg-white/5 px-4 py-3 text-sm text-white/70">
              WebSocket: {socketStatus}
            </div>
          </div>
        </div>
      </div>

      {isLoading ? (
        <div className="rounded-[2rem] border border-white/10 bg-card p-8 text-center text-white/70">Loading overview...</div>
      ) : error ? (
        <div className="rounded-[2rem] border border-red-500/20 bg-red-950/40 p-8 text-red-200">{error}</div>
      ) : overview ? (
        <div className="grid gap-8 xl:grid-cols-[1.5fr_1fr]">
          <div className="space-y-8">
            <div className="grid gap-6 md:grid-cols-2">
              <MetricCard label="Daily P&L" value={formatCurrency(overview.daily_pnl.amount)} delta={formatPercent(overview.daily_pnl.pct)} description="Real-time profit and loss for the trading day." />
              <MetricCard label="Available capital" value={formatCurrency(overview.capital.available)} description="Balance available for new trades." />
              <MetricCard label="Used capital" value={formatCurrency(overview.capital.used)} description="Capital currently allocated to open positions." />
              <MetricCard label="Buffer" value={formatCurrency(overview.capital.buffer)} description="Reserved capital buffer for risk control." />
            </div>

            <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p className="text-sm uppercase tracking-[0.18em] text-white/50">Strategy health</p>
                  <h2 className="mt-2 text-2xl font-semibold text-white">Risk & performance</h2>
                </div>
                <StatusBadge status={overview.risk_status.status} />
              </div>

              <div className="mt-6 grid gap-4 sm:grid-cols-2">
                <div className="rounded-3xl bg-white/5 p-4 text-sm text-white/75">
                  <div className="font-semibold text-white">Win rate</div>
                  <div className="mt-2 text-2xl">{formatPercent(overview.today_stats.win_rate)}</div>
                </div>
                <div className="rounded-3xl bg-white/5 p-4 text-sm text-white/75">
                  <div className="font-semibold text-white">Trades today</div>
                  <div className="mt-2 text-2xl">{overview.today_stats.total_trades}</div>
                </div>
              </div>

              <div className="mt-6 grid gap-4 sm:grid-cols-2">
                <div className="rounded-3xl bg-white/5 p-4 text-sm text-white/75">
                  <div className="font-semibold text-white">Open positions</div>
                  <div className="mt-2 text-2xl">{overview.open_positions.length}</div>
                </div>
                <div className="rounded-3xl bg-white/5 p-4 text-sm text-white/75">
                  <div className="font-semibold text-white">Watchlist items</div>
                  <div className="mt-2 text-2xl">{overview.watchlist.length}</div>
                </div>
              </div>
            </div>

            <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
              <p className="text-sm uppercase tracking-[0.18em] text-white/50">Active positions</p>
              <div className="mt-4 grid gap-4 sm:grid-cols-2">
                {overview.open_positions.length ? (
                  overview.open_positions.slice(0, 4).map((position) => (
                    <div key={position.symbol} className="rounded-3xl bg-white/5 p-4">
                      <div className="flex items-center justify-between gap-2 text-sm text-white/60">
                        <span>{position.symbol}</span>
                        <span>{position.side.toUpperCase()}</span>
                      </div>
                      <div className="mt-3 text-xl font-semibold text-white">{position.quantity} @ {formatCurrency(position.average_price)}</div>
                      <div className="mt-2 text-sm text-white/60">Unrealized P&L: {formatCurrency(position.unrealized_pnl)}</div>
                    </div>
                  ))
                ) : (
                  <div className="rounded-3xl bg-white/5 p-6 text-sm text-white/70">No open positions yet. Execute the first paper trade to begin live tracking.</div>
                )}
              </div>
            </div>
          </div>

          <div className="space-y-6">
            <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p className="text-sm uppercase tracking-[0.18em] text-white/50">System status</p>
                  <h2 className="mt-2 text-2xl font-semibold text-white">Backend health</h2>
                </div>
                <div className="rounded-3xl bg-white/5 px-3 py-2 text-sm text-white/80">{overview.system.websocket_connected ? 'WebSocket live' : 'Polling'}</div>
              </div>

              <div className="mt-6 space-y-4 text-sm text-white/70">
                <div className="flex justify-between gap-4 border-b border-white/10 pb-3">
                  <span>Last candle age</span>
                  <span>{overview.system.last_candle_seconds_ago}s</span>
                </div>
                <div className="flex justify-between gap-4 border-b border-white/10 pb-3">
                  <span>API last call</span>
                  <span>{new Date(overview.system.last_api_call).toLocaleTimeString()}</span>
                </div>
                <div className="flex justify-between gap-4 pt-3">
                  <span>Mode</span>
                  <span>{overview.system.mode}</span>
                </div>
              </div>
            </div>

            <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
              <p className="text-sm uppercase tracking-[0.18em] text-white/50">Strategy bias</p>
              <h2 className="mt-3 text-2xl font-semibold text-white">{overview.trend_bias}</h2>
              <p className="mt-3 text-sm text-white/70">High-level directional bias for the active strategy.</p>
            </div>

            <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
              <p className="text-sm uppercase tracking-[0.18em] text-white/50">Latest WebSocket message</p>
              <pre className="mt-3 max-h-40 overflow-auto rounded-3xl bg-white/5 p-4 text-sm text-white/70">{socketMessage ? JSON.stringify(socketMessage, null, 2) : 'No live updates yet.'}</pre>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
