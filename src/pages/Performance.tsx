import { useState, useCallback } from 'react'
import { fetchPerformance } from '../api/endpoints'
import { usePolling } from '../hooks/usePolling'
import { formatCurrency, formatPercent, pnlColor } from '../utils/formatters'
import type { PerformanceResponse } from '../types'

function MetricRow({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex items-center justify-between py-2 border-b border-[#1e2d45] last:border-0">
      <span className="text-xs text-slate-500">{label}</span>
      <span className={`text-xs font-semibold ${color ?? 'text-white'}`}>{value}</span>
    </div>
  )
}

export default function Performance() {
  const [data, setData] = useState<PerformanceResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await fetchPerformance({
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
      })
      setData(res)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load performance data')
    } finally {
      setLoading(false)
    }
  }, [dateFrom, dateTo])

  usePolling(load, 60000)

  const m = data?.metrics
  const equity = data?.equity_curve ?? data?.performance?.map(p => ({ date: p.date, value: p.equity })) ?? []
  const monthly = data?.monthly_returns ? Object.entries(data.monthly_returns) : []
  const maxEq = equity.length > 0 ? Math.max(...equity.map(e => e.value)) : 1
  const minEq = equity.length > 0 ? Math.min(...equity.map(e => e.value)) : 0

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold text-white">Performance Analytics</h1>
          <p className="text-xs text-slate-500 mt-0.5">Historical performance metrics and charts</p>
        </div>
        <div className="flex items-center gap-2">
          <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
            className="bg-[#141b2d] border border-[#1e2d45] rounded-lg px-3 py-2 text-xs text-slate-300 focus:outline-none focus:border-blue-600/50" />
          <span className="text-slate-600 text-xs">to</span>
          <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
            className="bg-[#141b2d] border border-[#1e2d45] rounded-lg px-3 py-2 text-xs text-slate-300 focus:outline-none focus:border-blue-600/50" />
        </div>
      </div>

      {loading ? (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-10 text-center text-slate-500 text-sm">Loading performance data...</div>
      ) : error ? (
        <div className="bg-red-950/30 border border-red-800/50 rounded-xl p-5 text-red-300 text-sm">{error}</div>
      ) : (
        <>
          {/* Metrics grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* Returns */}
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-3">Returns</div>
              <MetricRow label="Net Profit" value={formatCurrency(m?.net_profit)} color={pnlColor(m?.net_profit)} />
              <MetricRow label="Net Profit %" value={formatPercent(m?.net_profit_pct)} color={pnlColor(m?.net_profit_pct)} />
              <MetricRow label="Best Day" value={formatCurrency(m?.best_day)} color="text-emerald-400" />
              <MetricRow label="Worst Day" value={formatCurrency(m?.worst_day)} color="text-red-400" />
              <MetricRow label="Avg Daily P&L" value={formatCurrency(m?.avg_daily_pnl)} color={pnlColor(m?.avg_daily_pnl)} />
            </div>

            {/* Trades */}
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-3">Trade Stats</div>
              <MetricRow label="Total Trades" value={String(m?.total_trades ?? '—')} />
              <MetricRow label="Win Rate" value={m?.win_rate != null ? `${m.win_rate.toFixed(1)}%` : '—'} color={(m?.win_rate ?? 0) >= 40 ? 'text-emerald-400' : 'text-red-400'} />
              <MetricRow label="Profit Factor" value={m?.profit_factor?.toFixed(2) ?? '—'} color={(m?.profit_factor ?? 0) >= 1.5 ? 'text-emerald-400' : 'text-red-400'} />
              <MetricRow label="Expectancy (R)" value={m?.expectancy_r?.toFixed(2) ?? '—'} color={pnlColor(m?.expectancy_r)} />
              <MetricRow label="Avg Win R" value={m?.avg_win_r != null ? `+${m.avg_win_r.toFixed(2)}R` : '—'} color="text-emerald-400" />
            </div>

            {/* Risk */}
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-3">Risk Metrics</div>
              <MetricRow label="Max Drawdown" value={m?.max_drawdown_pct != null ? `${m.max_drawdown_pct.toFixed(2)}%` : '—'} color={(m?.max_drawdown_pct ?? 0) < 5 ? 'text-emerald-400' : 'text-red-400'} />
              <MetricRow label="Sharpe Ratio" value={m?.sharpe_ratio?.toFixed(2) ?? '—'} color={(m?.sharpe_ratio ?? 0) >= 1 ? 'text-emerald-400' : 'text-amber-400'} />
              <MetricRow label="Sortino Ratio" value={m?.sortino_ratio?.toFixed(2) ?? '—'} color={(m?.sortino_ratio ?? 0) >= 1 ? 'text-emerald-400' : 'text-amber-400'} />
              <MetricRow label="Calmar Ratio" value={m?.calmar_ratio?.toFixed(2) ?? '—'} />
              <MetricRow label="Avg Loss R" value={m?.avg_loss_r != null ? `${m.avg_loss_r.toFixed(2)}R` : '—'} color="text-red-400" />
            </div>

            {/* Targets */}
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-3">Live Targets</div>
              {[
                { label: 'Win Rate ≥ 40%', val: m?.win_rate, target: 40, fmt: (v: number) => `${v.toFixed(1)}%` },
                { label: 'Profit Factor ≥ 1.5', val: m?.profit_factor, target: 1.5, fmt: (v: number) => v.toFixed(2) },
                { label: 'Max DD < 5%', val: m?.max_drawdown_pct, target: 5, invert: true, fmt: (v: number) => `${v.toFixed(1)}%` },
                { label: 'Sharpe ≥ 1.0', val: m?.sharpe_ratio, target: 1, fmt: (v: number) => v.toFixed(2) },
              ].map(item => {
                const pass = item.val != null ? (item.invert ? item.val < item.target : item.val >= item.target) : false
                return (
                  <div key={item.label} className="flex items-center justify-between py-2 border-b border-[#1e2d45] last:border-0">
                    <div className="flex items-center gap-1.5">
                      <span className={pass ? 'text-emerald-400' : 'text-red-400'}>{pass ? '✓' : '✗'}</span>
                      <span className="text-xs text-slate-500">{item.label}</span>
                    </div>
                    <span className={`text-xs font-semibold ${pass ? 'text-emerald-400' : 'text-red-400'}`}>
                      {item.val != null ? item.fmt(item.val) : '—'}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>

          {/* Equity Curve */}
          {equity.length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-white mb-4">Equity Curve</h2>
              <div className="flex items-end gap-0.5 h-40">
                {equity.map((pt, i) => {
                  const h = maxEq > minEq ? ((pt.value - minEq) / (maxEq - minEq)) * 100 : 50
                  const prev = i > 0 ? equity[i - 1].value : pt.value
                  const isUp = pt.value >= prev
                  return (
                    <div key={i} className="flex-1 flex flex-col justify-end h-full" title={`${pt.date}: ${formatCurrency(pt.value)}`}>
                      <div
                        className={`w-full rounded-t transition-all ${isUp ? 'bg-emerald-500/60' : 'bg-red-500/60'}`}
                        style={{ height: `${Math.max(h, 1)}%` }}
                      />
                    </div>
                  )
                })}
              </div>
              <div className="flex justify-between text-[10px] text-slate-600 mt-1">
                <span>{equity[0]?.date} · {formatCurrency(equity[0]?.value)}</span>
                <span>{equity[equity.length - 1]?.date} · {formatCurrency(equity[equity.length - 1]?.value)}</span>
              </div>
            </div>
          )}

          {/* Monthly Returns */}
          {monthly.length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-white mb-4">Monthly Returns</h2>
              <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
                {monthly.map(([month, ret]) => (
                  <div key={month} className={`rounded-lg p-2.5 border text-center ${ret >= 0 ? 'bg-emerald-950/20 border-emerald-800/40' : 'bg-red-950/20 border-red-800/40'}`}>
                    <div className="text-[10px] text-slate-500">{month}</div>
                    <div className={`text-xs font-bold mt-0.5 ${pnlColor(ret)}`}>{formatPercent(ret)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {!m && !loading && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-10 text-center text-slate-500 text-sm">
              No performance data yet. Complete at least one trade in paper trading mode.
            </div>
          )}
        </>
      )}
    </div>
  )
}
