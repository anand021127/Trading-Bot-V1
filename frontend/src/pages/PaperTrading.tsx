import { useState, useCallback } from 'react'
import { CheckCircle, XCircle, Clock, TrendingUp, AlertTriangle } from 'lucide-react'
import { fetchPaperStatus, fetchLivePositions } from '../api/endpoints'
import { usePolling } from '../hooks/usePolling'
import { formatCurrency, pnlColor } from '../utils/formatters'
import type { PaperStatus, LivePositionDetail } from '../types'

interface ChecklistItemProps {
  label: string
  currentValue: string
  target?: string
  pass: boolean
}

function ChecklistItem({ label, currentValue, target, pass }: ChecklistItemProps) {
  return (
    <div className={`flex items-center justify-between p-3 rounded-lg border ${pass ? 'border-emerald-800/40 bg-emerald-950/15' : 'border-red-800/40 bg-red-950/15'}`}>
      <div className="flex items-center gap-2.5">
        {pass ? (
          <CheckCircle size={15} className="text-emerald-400 flex-shrink-0" />
        ) : (
          <XCircle size={15} className="text-red-400 flex-shrink-0" />
        )}
        <span className="text-xs text-slate-300">{label}</span>
      </div>
      <div className="text-right">
        <span className={`text-xs font-semibold ${pass ? 'text-emerald-400' : 'text-red-400'}`}>{currentValue}</span>
        {target && <div className="text-[10px] text-slate-600">target: {target}</div>}
      </div>
    </div>
  )
}

export default function PaperTrading() {
  const [data, setData] = useState<PaperStatus | null>(null)
  const [positions, setPositions] = useState<LivePositionDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [d, p] = await Promise.all([
        fetchPaperStatus(),
        fetchLivePositions().catch(() => ({ positions: [] })),
      ])
      setData(d)
      setPositions(p.positions ?? [])
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load paper trading status')
    } finally {
      setLoading(false)
    }
  }, [])

  usePolling(load, 60000)

  const daysActive = data?.days_active ?? 0
  const daysRequired = data?.days_required ?? 20
  const isReady = data?.is_ready ?? false
  const progress = Math.min((daysActive / daysRequired) * 100, 100)
  const cl = data?.checklist

  const checklistItems = cl ? [
    { label: 'Win Rate > 40%', currentValue: cl.win_rate_ok?.value != null ? `${Number(cl.win_rate_ok.value).toFixed(1)}%` : '—', target: '40%', pass: cl.win_rate_ok?.pass ?? false },
    { label: 'Profit Factor > 1.5', currentValue: cl.profit_factor_ok?.value != null ? Number(cl.profit_factor_ok.value).toFixed(2) : '—', target: '1.5', pass: cl.profit_factor_ok?.pass ?? false },
    { label: 'Max Drawdown < 5%', currentValue: cl.max_drawdown_ok?.value != null ? `${Number(cl.max_drawdown_ok.value).toFixed(1)}%` : '—', target: '5%', pass: cl.max_drawdown_ok?.pass ?? false },
    { label: 'All trade logs complete', currentValue: cl.logs_complete?.pass ? 'Yes' : 'No', pass: cl.logs_complete?.pass ?? false },
    { label: 'ORB filter working correctly', currentValue: cl.orb_filter_ok?.pass ? 'Yes' : 'No', pass: cl.orb_filter_ok?.pass ?? false },
    { label: 'Choppiness filter blocking choppy days', currentValue: cl.choppiness_filter_ok?.pass ? 'Yes' : 'No', pass: cl.choppiness_filter_ok?.pass ?? false },
    { label: 'No trades outside time window', currentValue: cl.time_window_ok?.pass ? 'Yes' : 'No', pass: cl.time_window_ok?.pass ?? false },
  ] : []

  const history = data?.daily_history ?? []

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Paper Trading</h1>
          <p className="text-xs text-slate-500 mt-0.5">Monitor paper trading progress before going live</p>
        </div>
        <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-medium ${
          isReady ? 'border-emerald-800/50 bg-emerald-950/30 text-emerald-400' : 'border-amber-800/50 bg-amber-950/30 text-amber-400'
        }`}>
          {isReady ? <CheckCircle size={13} /> : <Clock size={13} />}
          {isReady ? 'Ready for Live' : 'Not Ready Yet'}
        </div>
      </div>

      {/* Progress Card */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-sm font-semibold text-white">Day {daysActive} of {daysRequired} Required</div>
            <div className="text-xs text-slate-500 mt-0.5">{daysRequired - daysActive > 0 ? `${daysRequired - daysActive} more days needed` : 'Minimum period complete'}</div>
          </div>
          <div className="text-2xl font-bold text-white">{progress.toFixed(0)}%</div>
        </div>
        <div className="h-2 bg-[#1e2d45] rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-500 ${progress === 100 ? 'bg-emerald-500' : 'bg-blue-500'}`}
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      {/* Open Positions — item #7 */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-[#1e2d45]">
          <h2 className="text-sm font-semibold text-white">Open Positions ({positions.length})</h2>
        </div>
        {positions.length === 0 ? (
          <div className="text-xs text-slate-600 text-center py-6">No open positions right now</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-[#1e2d45] bg-[#0f1628]/50">
                  {['Symbol','Strategy','Entry','Target','Stop Loss','Trailing SL','Current Price','Current P&L'].map(h => (
                    <th key={h} className="text-left px-3 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.map(p => (
                  <tr key={p.symbol} className="border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235]">
                    <td className="px-3 py-2.5 text-white font-medium">{p.symbol}</td>
                    <td className="px-3 py-2.5 text-slate-400">{p.strategy_used}</td>
                    <td className="px-3 py-2.5 text-slate-300">{formatCurrency(p.entry_price)}</td>
                    <td className="px-3 py-2.5 text-emerald-400">{p.target ? formatCurrency(p.target) : '—'}</td>
                    <td className="px-3 py-2.5 text-red-400">{formatCurrency(p.stop_loss)}</td>
                    <td className="px-3 py-2.5 text-amber-400">{formatCurrency(p.trailing_stop)}</td>
                    <td className="px-3 py-2.5 text-slate-300">{p.current_price != null ? formatCurrency(p.current_price) : <span className="text-slate-600">no live tick</span>}</td>
                    <td className={`px-3 py-2.5 font-semibold ${p.current_pnl != null ? pnlColor(p.current_pnl) : 'text-slate-600'}`}>
                      {p.current_pnl != null ? `${formatCurrency(p.current_pnl)} (${p.current_pnl_pct?.toFixed(2)}%)` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {loading ? (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-8 text-center text-slate-500 text-sm">Loading paper trading status...</div>
      ) : error ? (
        <div className="bg-amber-950/30 border border-amber-800/50 rounded-xl p-5">
          <div className="flex items-start gap-2 text-amber-300">
            <AlertTriangle size={14} className="flex-shrink-0 mt-0.5" />
            <div>
              <div className="text-xs font-medium">Could not load paper status</div>
              <div className="text-[11px] text-amber-400/70 mt-0.5">{error}</div>
              <div className="text-[11px] text-amber-400/70 mt-1">Make sure your backend is running and paper trading has been active.</div>
            </div>
          </div>
        </div>
      ) : (
        <>
          {/* Checklist */}
          <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
            <h2 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
              Live Readiness Checklist
              <span className="text-xs text-slate-500 font-normal">({checklistItems.filter(c => c.pass).length}/{checklistItems.length} passed)</span>
            </h2>
            {checklistItems.length === 0 ? (
              <div className="text-xs text-slate-600 text-center py-4">No checklist data yet — start paper trading first</div>
            ) : (
              <div className="space-y-2">
                {checklistItems.map(item => (
                  <ChecklistItem key={item.label} {...item} />
                ))}
              </div>
            )}
          </div>

          {/* Daily history */}
          {history.length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
              <div className="px-4 py-3 border-b border-[#1e2d45]">
                <h2 className="text-sm font-semibold text-white">Daily Performance History</h2>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-500 border-b border-[#1e2d45] bg-[#0f1628]/50">
                      {['Date','Trades','Wins','Losses','Net P&L','Drawdown','Bias'].map(h => (
                        <th key={h} className="text-left px-3 py-2 font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {history.map(d => (
                      <tr key={d.date} className={`border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235] ${d.net_pnl >= 0 ? 'bg-emerald-950/5' : 'bg-red-950/5'}`}>
                        <td className="px-3 py-2.5 text-slate-400">{d.date}</td>
                        <td className="px-3 py-2.5 text-slate-300">{d.trades}</td>
                        <td className="px-3 py-2.5 text-emerald-400">{d.wins}</td>
                        <td className="px-3 py-2.5 text-red-400">{d.losses}</td>
                        <td className={`px-3 py-2.5 font-semibold ${pnlColor(d.net_pnl)}`}>{formatCurrency(d.net_pnl)}</td>
                        <td className="px-3 py-2.5 text-slate-400">{d.drawdown?.toFixed(2)}%</td>
                        <td className="px-3 py-2.5">
                          <span className={`text-[9px] font-medium px-1.5 py-0.5 rounded border uppercase tracking-widest ${d.trend_bias === 'BULLISH' ? 'text-emerald-400 border-emerald-800/50 bg-emerald-950/30' : 'text-slate-400 border-slate-700/50 bg-slate-800/30'}`}>
                            {d.trend_bias}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Equity placeholder */}
          {history.length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-white mb-3">Paper Equity Curve</h2>
              <div className="flex items-end gap-1 h-20">
                {history.map((d, i) => {
                  const max = Math.max(...history.map(x => Math.abs(x.net_pnl)))
                  const h = max > 0 ? Math.abs(d.net_pnl) / max * 100 : 10
                  return (
                    <div key={i} className="flex-1 flex flex-col items-center justify-end h-full" title={`${d.date}: ${formatCurrency(d.net_pnl)}`}>
                      <div className={`w-full rounded-t ${d.net_pnl >= 0 ? 'bg-emerald-500' : 'bg-red-500'}`} style={{ height: `${h}%` }} />
                    </div>
                  )
                })}
              </div>
              <div className="flex justify-between text-[10px] text-slate-600 mt-1">
                <span>{history[0]?.date}</span>
                <span>{history[history.length - 1]?.date}</span>
              </div>
            </div>
          )}
        </>
      )}

      {/* Go Live section */}
      <div className={`rounded-xl p-5 border ${isReady ? 'bg-emerald-950/20 border-emerald-800/50' : 'bg-[#141b2d] border-[#1e2d45]'}`}>
        <div className="flex items-start gap-3">
          <TrendingUp size={18} className={isReady ? 'text-emerald-400 flex-shrink-0 mt-0.5' : 'text-slate-600 flex-shrink-0 mt-0.5'} />
          <div>
            <div className="text-sm font-semibold text-white mb-1">
              {isReady ? 'Ready to Switch to Live Trading' : 'Complete Paper Trading First'}
            </div>
            <div className="text-xs text-slate-400">
              {isReady
                ? 'All 7 checklist items have passed. Go to Settings to switch to live mode. Start with minimal capital.'
                : `You need ${daysRequired - daysActive} more days of paper trading and all 7 checklist items to pass before going live.`}
            </div>
            {isReady && (
              <div className="mt-3 text-xs text-amber-400 flex items-center gap-1.5">
                <AlertTriangle size={11} />
                Live trading involves real money. Only proceed if you fully understand the risks.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
