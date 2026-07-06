import { useState } from 'react'
import { TrendingUp, TrendingDown, Activity, Shield, AlertTriangle, RefreshCw } from 'lucide-react'
import { fetchOverview } from '../api/endpoints'
import { useWebSocket } from '../hooks/useWebSocket'
import { usePolling } from '../hooks/usePolling'
import MetricCard from '../components/MetricCard'
import StatusBadge from '../components/StatusBadge'
import { formatCurrency, formatPercent, formatR, formatTime, formatDuration, pnlColor } from '../utils/formatters'
import type { OverviewData } from '../types'

export default function Overview() {
  const [data, setData] = useState<OverviewData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const { status: wsStatus, prices } = useWebSocket()

  const load = async () => {
    try {
      const d = await fetchOverview()
      setData(d)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load overview')
    } finally {
      setLoading(false)
    }
  }

  usePolling(load, 15000)

  const pnl = data?.daily_pnl
  const cap = data?.capital
  const stats = data?.today_stats
  const risk = data?.risk_status
  const positions = data?.open_positions ?? []
  const watchlist = data?.watchlist ?? []
  const sys = data?.system

  if (loading) return (
    <div className="flex items-center justify-center h-64">
      <div className="flex items-center gap-3 text-slate-400">
        <RefreshCw size={18} className="animate-spin" />
        <span className="text-sm">Loading dashboard...</span>
      </div>
    </div>
  )

  if (error) return (
    <div className="bg-red-950/30 border border-red-800/50 rounded-xl p-6 text-red-300 text-sm">
      <div className="font-medium mb-1">Failed to load overview</div>
      <div className="text-red-400/70">{error}</div>
      <button onClick={load} className="mt-3 text-xs text-red-300 hover:text-red-200 underline">Retry</button>
    </div>
  )

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Live Overview</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {sys?.market_open ? '🟢 Market Open' : '🔴 Market Closed'} ·
            Bot Mode: <span className="text-slate-400">{(sys?.mode ?? 'paper').toUpperCase()}</span>
          </p>
        </div>
        <StatusBadge status={risk?.status ?? 'ACTIVE'} size="md" />
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <MetricCard
          title="Today's P&L"
          value={formatCurrency(pnl?.amount ?? 0)}
          sub={`${formatPercent(pnl?.pct ?? 0)} · ${stats?.total_trades ?? 0} trades`}
          valueColor={pnlColor(pnl?.amount ?? 0)}
          icon={pnl?.amount && pnl.amount >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
        />
        <MetricCard
          title="Available Capital"
          value={formatCurrency(cap?.available ?? 0)}
          sub={`Used: ${formatCurrency(cap?.used ?? 0)}`}
          icon={<Activity size={14} />}
        />
        <MetricCard
          title="Win Rate Today"
          value={`${(stats?.win_rate ?? 0).toFixed(1)}%`}
          sub={`${stats?.wins ?? 0}W · ${stats?.losses ?? 0}L`}
          valueColor={(stats?.win_rate ?? 0) >= 50 ? 'text-emerald-400' : 'text-slate-300'}
        />
        <MetricCard
          title="Risk Status"
          value={risk?.status ?? 'ACTIVE'}
          sub={`${risk?.consecutive_losses ?? 0} losses · ${(risk?.daily_loss_used_pct ?? 0).toFixed(1)}% limit`}
          valueColor={risk?.status === 'ACTIVE' ? 'text-emerald-400' : risk?.status === 'PAUSED' ? 'text-amber-400' : 'text-red-400'}
          icon={<Shield size={14} />}
        />
      </div>

      {/* Open Positions */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#1e2d45]">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            Open Positions
            <span className="text-xs bg-blue-600/20 text-blue-400 px-1.5 py-0.5 rounded-full border border-blue-600/25">
              {positions.length}
            </span>
          </h2>
          <StatusBadge status={wsStatus} size="sm" />
        </div>

        {positions.length === 0 ? (
          <div className="text-center py-10 text-slate-500">
            <Activity size={28} className="mx-auto mb-2 opacity-30" />
            <p className="text-sm">No open positions — bot is watching the market</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-[#1e2d45]">
                  {['Symbol','Entry Time','Entry Price','Current','Chg%','Qty','Init SL','Curr SL','Stage','Unrealised P&L','R','Duration'].map(h => (
                    <th key={h} className="text-left px-3 py-2 font-medium whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.map((pos) => {
                  const ep = pos.entry_price ?? pos.average_price ?? 0
                  const livePrice = prices[pos.symbol]?.ltp ?? pos.current_price ?? ep
                  const livePnl = (livePrice - ep) * pos.quantity
                  const livePct = ep ? ((livePrice - ep) / ep) * 100 : 0
                  const initSl = pos.initial_stop ?? 0
                  const liveR = initSl && ep ? livePnl / ((ep - initSl) * pos.quantity) : null
                  return (
                    <tr key={pos.symbol} className={`border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235] transition-colors ${livePnl >= 0 ? 'bg-emerald-950/5' : 'bg-red-950/5'}`}>
                      <td className="px-3 py-2.5 font-semibold text-white">{pos.symbol}</td>
                      <td className="px-3 py-2.5 text-slate-400">{formatTime(pos.entry_time)}</td>
                      <td className="px-3 py-2.5">₹{ep.toFixed(2)}</td>
                      <td className="px-3 py-2.5 font-medium text-white">₹{livePrice.toFixed(2)}</td>
                      <td className={`px-3 py-2.5 font-medium ${pnlColor(livePct)}`}>{formatPercent(livePct)}</td>
                      <td className="px-3 py-2.5 text-slate-300">{pos.quantity}</td>
                      <td className="px-3 py-2.5 text-red-400">₹{initSl.toFixed(2)}</td>
                      <td className="px-3 py-2.5 text-amber-400">₹{(pos.trailing_stop ?? initSl).toFixed(2)}</td>
                      <td className="px-3 py-2.5">
                        <span className="text-[10px] bg-blue-950/40 text-blue-400 border border-blue-800/40 px-1.5 py-0.5 rounded">S{pos.stage ?? 1}</span>
                      </td>
                      <td className={`px-3 py-2.5 font-semibold ${pnlColor(livePnl)}`}>{formatCurrency(livePnl)}</td>
                      <td className={`px-3 py-2.5 font-medium ${pnlColor(liveR)}`}>{formatR(liveR)}</td>
                      <td className="px-3 py-2.5 text-slate-400">{formatDuration(pos.time_in_trade_min)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Bottom row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Watchlist */}
        <div className="lg:col-span-2 bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-[#1e2d45]">
            <h2 className="text-sm font-semibold text-white">Today's Watchlist</h2>
          </div>
          {watchlist.length === 0 ? (
            <div className="px-4 py-6 text-xs text-slate-500 text-center">Watchlist builds before market open (9:00 AM)</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-slate-500 border-b border-[#1e2d45]">
                    {['Symbol','Status','ORB High','ORB Low','Bias','Last Price'].map(h => (
                      <th key={h} className="text-left px-3 py-2 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {watchlist.map(w => (
                    <tr key={w.symbol} className="border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235]">
                      <td className="px-3 py-2.5 font-semibold text-white">{w.symbol}</td>
                      <td className="px-3 py-2.5"><StatusBadge status={w.status} /></td>
                      <td className="px-3 py-2.5 text-slate-300">{w.orb_high ? `₹${w.orb_high.toFixed(2)}` : '—'}</td>
                      <td className="px-3 py-2.5 text-slate-300">{w.orb_low ? `₹${w.orb_low.toFixed(2)}` : '—'}</td>
                      <td className="px-3 py-2.5"><StatusBadge status={w.trend_bias ?? 'NEUTRAL'} /></td>
                      <td className="px-3 py-2.5 font-medium text-white">
                        {prices[w.symbol]?.ltp ? `₹${prices[w.symbol].ltp.toFixed(2)}` : w.last_price ? `₹${w.last_price.toFixed(2)}` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Risk panel */}
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4 space-y-4">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Shield size={13} className="text-blue-400" /> Risk Dashboard
          </h2>

          {[
            { label: 'Daily Loss Used', val: risk?.daily_loss_used_pct ?? 0, max: 100, color: (risk?.daily_loss_used_pct ?? 0) > 80 ? 'bg-red-500' : (risk?.daily_loss_used_pct ?? 0) > 50 ? 'bg-amber-500' : 'bg-emerald-500' },
          ].map(item => (
            <div key={item.label}>
              <div className="flex justify-between text-xs mb-1.5">
                <span className="text-slate-500">{item.label}</span>
                <span className="text-slate-300">{item.val.toFixed(1)}%</span>
              </div>
              <div className="h-1.5 bg-[#1e2d45] rounded-full overflow-hidden">
                <div className={`h-full rounded-full transition-all ${item.color}`} style={{ width: `${Math.min(item.val, 100)}%` }} />
              </div>
            </div>
          ))}

          <div>
            <div className="flex justify-between text-xs mb-1.5">
              <span className="text-slate-500">Trades Used</span>
              <span className="text-slate-300">{risk?.trades_used ?? 0}/{risk?.max_trades ?? 4}</span>
            </div>
            <div className="flex gap-1">
              {Array.from({ length: risk?.max_trades ?? 4 }).map((_, i) => (
                <div key={i} className={`flex-1 h-1.5 rounded-full ${i < (risk?.trades_used ?? 0) ? 'bg-blue-500' : 'bg-[#1e2d45]'}`} />
              ))}
            </div>
          </div>

          <div>
            <div className="flex justify-between text-xs mb-1.5">
              <span className="text-slate-500">Consecutive Losses</span>
              <span className={(risk?.consecutive_losses ?? 0) >= 2 ? 'text-red-400' : 'text-slate-300'}>{risk?.consecutive_losses ?? 0}/3</span>
            </div>
            <div className="flex gap-1">
              {[0, 1, 2].map(i => (
                <div key={i} className={`flex-1 h-1.5 rounded-full ${i < (risk?.consecutive_losses ?? 0) ? 'bg-red-500' : 'bg-[#1e2d45]'}`} />
              ))}
            </div>
          </div>

          {risk?.stop_reason && (
            <div className="flex items-start gap-2 bg-red-950/30 border border-red-800/40 rounded-lg p-2.5">
              <AlertTriangle size={13} className="text-red-400 flex-shrink-0 mt-0.5" />
              <p className="text-xs text-red-300">{risk.stop_reason}</p>
            </div>
          )}

          <div className="flex items-center justify-between text-xs pt-1 border-t border-[#1e2d45]">
            <span className="text-slate-500">15-min Bias</span>
            <StatusBadge status={data?.trend_bias ?? 'NEUTRAL'} />
          </div>
        </div>
      </div>

      {/* System status */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[
          { label: 'WebSocket', value: wsStatus === 'connected' ? 'Connected' : 'Disconnected', ok: wsStatus === 'connected' },
          { label: 'Last Candle', value: sys?.last_candle_seconds_ago != null ? `${sys.last_candle_seconds_ago}s ago` : '—', ok: (sys?.last_candle_seconds_ago ?? 999) < 120 },
          { label: 'Market', value: sys?.market_open ? 'Open' : 'Closed', ok: sys?.market_open ?? false },
          { label: 'Mode', value: (sys?.mode ?? 'paper').toUpperCase(), ok: true },
        ].map(item => (
          <div key={item.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-lg px-3 py-2.5 flex items-center gap-2.5">
            <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${item.ok ? 'bg-emerald-400' : 'bg-red-400'}`} />
            <div>
              <div className="text-[10px] text-slate-500">{item.label}</div>
              <div className="text-xs font-medium text-slate-200">{item.value}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
