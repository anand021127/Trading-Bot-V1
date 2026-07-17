import { useState } from 'react'
import {
  TrendingUp, TrendingDown, Activity, Shield, AlertTriangle,
  RefreshCw, Play, Square, Zap, Wifi, WifiOff
} from 'lucide-react'
import { fetchOverview } from '../api/endpoints'
import { useWebSocket } from '../hooks/useWebSocket'
import { usePolling } from '../hooks/usePolling'
import MetricCard from '../components/MetricCard'
import { formatCurrency, formatPercent, formatR, formatTime, formatDuration, pnlColor } from '../utils/formatters'
import type { OverviewData, Position } from '../types'
import api from '../api/client'
import toast from 'react-hot-toast'

interface BotStatus {
  running: boolean
  kill_switch_active: boolean
  start_time: string | null
  stop_reason: string
  uptime_seconds: number
  mode: string
}

export default function Overview() {
  const [data, setData]           = useState<OverviewData | null>(null)
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState<string | null>(null)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const { status: wsStatus, prices } = useWebSocket()

  const load = async () => {
    try {
      const [overview, status] = await Promise.all([
        fetchOverview(),
        api.get<BotStatus>('/api/bot/status').then(r => r.data).catch(() => null),
      ])
      setData(overview)
      if (status) setBotStatus(status)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load overview')
    } finally {
      setLoading(false)
    }
  }

  usePolling(load, 10000)

  const botAction = async (action: 'start' | 'stop' | 'kill' | 'reset-kill') => {
    setActionLoading(action)
    try {
      const res = await api.post<{ success: boolean; message: string; warning?: string }>(
        `/api/bot/${action}`
      )
      if (res.data.success) {
        toast.success(res.data.message)
        if (res.data.warning) toast(res.data.warning, { icon: '⚠️', duration: 8000 })
      } else {
        toast.error(res.data.message)
      }
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Action failed')
    } finally {
      setActionLoading(null)
    }
  }

  const confirmKill = () => {
    if (window.confirm('⚠️ EMERGENCY KILL SWITCH\n\nThis will IMMEDIATELY stop ALL trading.\nYou must manually reset before trading can resume.\n\nAre you sure?')) {
      botAction('kill')
    }
  }

  const pnl = data?.daily_pnl
  const cap = data?.capital
  const stats = data?.today_stats
  const risk = data?.risk_status
  const positions = data?.open_positions ?? []
  const sys = data?.system
  const isRunning = botStatus?.running ?? false
  const isKilled  = botStatus?.kill_switch_active ?? false

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
      {/* Page header with bot status */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold text-white">Live Overview</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {sys?.market_open ? '🟢 Market Open' : '🔴 Market Closed'} ·
            Mode: <span className="text-slate-400 font-medium">{(sys?.mode ?? 'paper').toUpperCase()}</span>
          </p>
        </div>

        {/* BOT CONTROLS */}
        <div className="flex items-center gap-2 flex-wrap justify-end">
          {/* Bot status indicator */}
          <div className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium ${
            isKilled  ? 'bg-red-950/40 border-red-800/50 text-red-400' :
            isRunning ? 'bg-emerald-950/40 border-emerald-800/50 text-emerald-400' :
                        'bg-slate-800/40 border-slate-700/50 text-slate-400'
          }`}>
            <div className={`w-1.5 h-1.5 rounded-full ${
              isKilled ? 'bg-red-500' : isRunning ? 'bg-emerald-400 animate-pulse' : 'bg-slate-500'
            }`} />
            {isKilled ? 'KILLED' : isRunning ? 'RUNNING' : 'STOPPED'}
          </div>

          {/* Start button */}
          {!isRunning && !isKilled && (
            <button
              onClick={() => botAction('start')}
              disabled={actionLoading !== null}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white text-xs font-medium rounded-lg transition-colors"
            >
              {actionLoading === 'start' ? <RefreshCw size={12} className="animate-spin" /> : <Play size={12} />}
              Start Bot
            </button>
          )}

          {/* Stop button */}
          {isRunning && (
            <button
              onClick={() => botAction('stop')}
              disabled={actionLoading !== null}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white text-xs font-medium rounded-lg transition-colors"
            >
              {actionLoading === 'stop' ? <RefreshCw size={12} className="animate-spin" /> : <Square size={12} />}
              Stop Bot
            </button>
          )}

          {/* Reset kill switch */}
          {isKilled && (
            <button
              onClick={() => botAction('reset-kill')}
              disabled={actionLoading !== null}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-xs font-medium rounded-lg transition-colors"
            >
              Reset Kill Switch
            </button>
          )}

          {/* Emergency kill */}
          {!isKilled && (
            <button
              onClick={confirmKill}
              disabled={actionLoading !== null}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-red-700 hover:bg-red-800 disabled:opacity-50 text-white text-xs font-medium rounded-lg transition-colors border border-red-600"
              title="Emergency Kill Switch"
            >
              <Zap size={12} />
              KILL
            </button>
          )}
        </div>
      </div>

      {/* Kill switch warning */}
      {isKilled && (
        <div className="flex items-start gap-2.5 bg-red-950/40 border border-red-700/60 rounded-xl px-4 py-3">
          <Zap size={14} className="text-red-400 flex-shrink-0 mt-0.5" />
          <div>
            <div className="text-sm font-semibold text-red-300">Emergency Kill Switch is ACTIVE</div>
            <div className="text-xs text-red-400/70 mt-0.5">All trading is stopped. Click "Reset Kill Switch" to resume.</div>
          </div>
        </div>
      )}

      {/* Connection status bar */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {[
          { label: 'Bot Engine',    ok: isRunning && !isKilled,  val: isKilled ? 'KILLED' : isRunning ? 'Running' : 'Stopped' },
          { label: 'Live Updates',  ok: wsStatus === 'connected', val: wsStatus === 'connected' ? 'Connected' : 'Offline' },
          { label: 'Market',        ok: sys?.market_open ?? false, val: sys?.market_open ? 'Open' : 'Closed' },
          { label: 'Risk Status',   ok: risk?.status === 'ACTIVE', val: risk?.status ?? 'UNKNOWN' },
        ].map(item => (
          <div key={item.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-lg px-3 py-2 flex items-center gap-2">
            <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${item.ok ? 'bg-emerald-400' : 'bg-red-400'}`} />
            <div>
              <div className="text-[10px] text-slate-500">{item.label}</div>
              <div className={`text-xs font-medium ${item.ok ? 'text-emerald-400' : 'text-red-400'}`}>{item.val}</div>
            </div>
          </div>
        ))}
      </div>

      {/* System health — real Upstox v3 feed status, universe, scanner (item #8) */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {[
          {
            label: 'Market Data Feed', ok: sys?.websocket_connected ?? false,
            val: sys?.websocket_status ?? 'unknown',
          },
          {
            label: 'API Health', ok: sys?.api_health === 'ok',
            val: sys?.api_health ?? 'unknown',
          },
          {
            label: 'Configured Universe', ok: (data?.universe?.watching_count ?? 0) > 0 && (data?.scanner?.is_running ?? false),
            val: `${data?.universe?.watching_count ?? 0} symbols (${data?.universe?.mode ?? 'STOCKS'})${data?.scanner?.is_running ? '' : ' — scanner stopped'}`,
          },
          {
            label: 'Scanner', ok: data?.scanner?.is_running ?? false,
            val: data?.scanner?.currently_analyzing ? `Analyzing ${data.scanner.currently_analyzing}` : (data?.scanner?.is_running ? 'Idle' : 'Stopped'),
          },
        ].map(item => (
          <div key={item.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-lg px-3 py-2 flex items-center gap-2">
            <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${item.ok ? 'bg-emerald-400' : 'bg-amber-400'}`} />
            <div className="min-w-0">
              <div className="text-[10px] text-slate-500">{item.label}</div>
              <div className={`text-xs font-medium truncate ${item.ok ? 'text-emerald-400' : 'text-amber-400'}`}>{item.val}</div>
            </div>
          </div>
        ))}
      </div>

      {data?.scanner?.last_signal && (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-lg px-4 py-2.5 flex items-center gap-3">
          <Zap size={14} className="text-blue-400 flex-shrink-0" />
          <div className="text-xs text-slate-300">
            <span className="font-medium text-white">Last signal:</span>{' '}
            {data.scanner.last_signal.symbol} — {data.scanner.last_signal.strategy_name}{' '}
            <span className={data.scanner.last_signal.signal === 'BUY' ? 'text-emerald-400' : 'text-slate-400'}>
              {data.scanner.last_signal.signal}
            </span>{' '}
            ({data.scanner.last_signal.confidence.toFixed(0)}% confidence)
          </div>
        </div>
      )}

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
          title="Risk Meter"
          value={`${(risk?.daily_loss_used_pct ?? 0).toFixed(1)}% used`}
          sub={`${risk?.trades_used ?? 0}/${risk?.max_trades ?? 4} trades · ${risk?.consecutive_losses ?? 0} consec. losses`}
          valueColor={(risk?.daily_loss_used_pct ?? 0) > 80 ? 'text-red-400' : (risk?.daily_loss_used_pct ?? 0) > 50 ? 'text-amber-400' : 'text-emerald-400'}
          icon={<Shield size={14} />}
        />
      </div>

      {/* Open Positions */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#1e2d45]">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            Active Trades
            <span className="text-xs bg-blue-600/20 text-blue-400 px-1.5 py-0.5 rounded-full border border-blue-600/25">
              {positions.length}
            </span>
          </h2>
          <div className={`flex items-center gap-1.5 text-xs ${wsStatus === 'connected' ? 'text-emerald-400' : 'text-slate-500'}`}>
            {wsStatus === 'connected' ? <Wifi size={11} /> : <WifiOff size={11} />}
            {wsStatus === 'connected' ? 'Live' : 'No feed'}
          </div>
        </div>

        {positions.length === 0 ? (
          <div className="text-center py-10 text-slate-500">
            <Activity size={28} className="mx-auto mb-2 opacity-30" />
            <p className="text-sm">No active trades</p>
            <p className="text-xs mt-1 text-slate-600">{isRunning ? 'Bot is watching market for signals' : 'Start the bot to begin trading'}</p>
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
                {positions.map((pos: Position) => {
                  const ep = pos.entry_price ?? pos.average_price ?? 0
                  const livePrice = prices[pos.symbol]?.ltp ?? pos.current_price ?? ep
                  const livePnl = (livePrice - ep) * pos.quantity
                  const livePct = ep ? ((livePrice - ep) / ep) * 100 : 0
                  const initSl = pos.initial_stop ?? 0
                  const liveR = initSl && ep ? livePnl / ((ep - initSl) * pos.quantity) : null
                  return (
                    <tr key={pos.symbol} className={`border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235] ${livePnl >= 0 ? 'bg-emerald-950/5' : 'bg-red-950/5'}`}>
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

      {/* Risk dashboard */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
          <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Shield size={13} className="text-blue-400" /> Risk Dashboard
          </h2>
          <div className="space-y-3">
            {[
              { label: 'Daily Loss Used', val: risk?.daily_loss_used_pct ?? 0, color: (risk?.daily_loss_used_pct ?? 0) > 80 ? 'bg-red-500' : (risk?.daily_loss_used_pct ?? 0) > 50 ? 'bg-amber-500' : 'bg-emerald-500' },
            ].map(item => (
              <div key={item.label}>
                <div className="flex justify-between text-xs mb-1"><span className="text-slate-500">{item.label}</span><span className="text-slate-300">{item.val.toFixed(1)}%</span></div>
                <div className="h-1.5 bg-[#1e2d45] rounded-full overflow-hidden">
                  <div className={`h-full rounded-full transition-all ${item.color}`} style={{ width: `${Math.min(item.val, 100)}%` }} />
                </div>
              </div>
            ))}
            <div>
              <div className="flex justify-between text-xs mb-1"><span className="text-slate-500">Trades Used</span><span className="text-slate-300">{risk?.trades_used ?? 0}/{risk?.max_trades ?? 4}</span></div>
              <div className="flex gap-1">
                {Array.from({ length: risk?.max_trades ?? 4 }).map((_, i) => (
                  <div key={i} className={`flex-1 h-1.5 rounded-full ${i < (risk?.trades_used ?? 0) ? 'bg-blue-500' : 'bg-[#1e2d45]'}`} />
                ))}
              </div>
            </div>
            <div>
              <div className="flex justify-between text-xs mb-1"><span className="text-slate-500">Consecutive Losses</span><span className={(risk?.consecutive_losses ?? 0) >= 2 ? 'text-red-400' : 'text-slate-300'}>{risk?.consecutive_losses ?? 0}/3</span></div>
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
          </div>
        </div>

        {/* Uptime card */}
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
          <h2 className="text-sm font-semibold text-white mb-3">Bot Information</h2>
          <div className="space-y-2 text-xs">
            {[
              { label: 'Status', val: isKilled ? 'KILLED' : isRunning ? 'Running' : 'Stopped', color: isKilled ? 'text-red-400' : isRunning ? 'text-emerald-400' : 'text-slate-400' },
              { label: 'Mode', val: (sys?.mode ?? 'paper').toUpperCase(), color: 'text-amber-400' },
              { label: 'Uptime', val: botStatus?.uptime_seconds ? `${Math.floor(botStatus.uptime_seconds / 60)}m ${botStatus.uptime_seconds % 60}s` : '—', color: 'text-slate-300' },
              { label: 'Stop Reason', val: botStatus?.stop_reason || '—', color: 'text-slate-400' },
              { label: 'Market', val: sys?.market_open ? 'Open' : 'Closed', color: sys?.market_open ? 'text-emerald-400' : 'text-slate-400' },
              { label: 'Trend Bias', val: data?.trend_bias ?? 'NEUTRAL', color: data?.trend_bias === 'BULLISH' ? 'text-emerald-400' : 'text-slate-400' },
            ].map(item => (
              <div key={item.label} className="flex justify-between items-center py-1 border-b border-[#1e2d45] last:border-0">
                <span className="text-slate-500">{item.label}</span>
                <span className={`font-medium ${item.color}`}>{item.val}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
