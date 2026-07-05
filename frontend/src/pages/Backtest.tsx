import { useState } from 'react'
import { Play, BarChart2, RefreshCw, AlertTriangle } from 'lucide-react'
import { runBacktest } from '../api/endpoints'
import { formatCurrency, formatPercent, pnlColor } from '../utils/formatters'
import type { BacktestResponse } from '../types'

const NIFTY50 = [
  'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','KOTAKBANK','LT','SBIN','AXISBANK',
  'BHARTIARTL','ITC','ASIANPAINT','MARUTI','HCLTECH','SUNPHARMA','WIPRO','TITAN','ULTRACEMCO','BAJFINANCE',
  'NESTLEIND','TECHM','NTPC','POWERGRID','ONGC','JSWSTEEL','TATASTEEL','HINDALCO','TATAMOTORS',
  'BAJAJFINSV','DRREDDY','CIPLA','DIVISLAB','APOLLOHOSP','ADANIENT','ADANIPORTS','COALINDIA',
  'BPCL','EICHERMOT','HEROMOTOCO','INDUSINDBK','SBILIFE','HDFCLIFE','GRASIM','TATACONSUM',
  'UPL','BRITANNIA','SHREECEM','BAJAJ-AUTO','M&M',
]

export default function Backtest() {
  const [startDate, setStartDate] = useState('2024-01-01')
  const [endDate, setEndDate] = useState('2024-12-31')
  const [capital, setCapital] = useState('500000')
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK'])
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<BacktestResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const toggleSymbol = (sym: string) => {
    setSelectedSymbols(prev =>
      prev.includes(sym) ? prev.filter(s => s !== sym) : [...prev, sym]
    )
  }

  const handleRun = async () => {
    if (!startDate || !endDate || selectedSymbols.length === 0) {
      setError('Please fill in all fields and select at least one symbol.')
      return
    }
    setRunning(true)
    setError(null)
    setResult(null)
    try {
      const res = await runBacktest({
        start_date: startDate,
        end_date: endDate,
        capital: Number(capital),
        symbols: selectedSymbols,
        commission_pct: 0.0003,
        slippage_pct: 0.0001,
      })
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Backtest failed. Make sure the backend is running.')
    } finally {
      setRunning(false)
    }
  }

  const metrics = result ? [
    { label: 'Total Trades', value: String(result.total_trades ?? '—') },
    { label: 'Win Rate', value: result.win_rate != null ? `${result.win_rate.toFixed(1)}%` : '—', color: (result.win_rate ?? 0) >= 40 ? 'text-emerald-400' : 'text-red-400' },
    { label: 'Profit Factor', value: result.profit_factor?.toFixed(2) ?? '—', color: (result.profit_factor ?? 0) >= 1.5 ? 'text-emerald-400' : 'text-red-400' },
    { label: 'Net Profit', value: formatCurrency(result.net_profit ?? 0), color: pnlColor(result.net_profit) },
    { label: 'Max Drawdown', value: result.max_drawdown_pct != null ? `${result.max_drawdown_pct.toFixed(2)}%` : '—', color: (result.max_drawdown_pct ?? 0) < 5 ? 'text-emerald-400' : 'text-red-400' },
    { label: 'Sharpe Ratio', value: result.sharpe_ratio?.toFixed(2) ?? '—', color: (result.sharpe_ratio ?? 0) >= 1 ? 'text-emerald-400' : 'text-amber-400' },
    { label: 'Avg Win R', value: result.avg_win_r != null ? `+${result.avg_win_r.toFixed(2)}R` : '—', color: 'text-emerald-400' },
    { label: 'Avg Loss R', value: result.avg_loss_r != null ? `${result.avg_loss_r.toFixed(2)}R` : '—', color: 'text-red-400' },
  ] : []

  const monthlyReturns = result?.monthly_returns ? Object.entries(result.monthly_returns) : []
  const equityCurve = result?.equity_curve ?? []
  const maxEq = equityCurve.length > 0 ? Math.max(...equityCurve.map(e => e.value)) : 1
  const minEq = equityCurve.length > 0 ? Math.min(...equityCurve.map(e => e.value)) : 0

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold text-white">Backtest</h1>
        <p className="text-xs text-slate-500 mt-0.5">Test strategy on historical data with real transaction costs</p>
      </div>

      {/* Input form */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5 space-y-4">
        <h2 className="text-sm font-semibold text-white">Configuration</h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">Start Date</label>
            <input
              type="date" value={startDate}
              onChange={e => setStartDate(e.target.value)}
              className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50"
            />
          </div>
          <div>
            <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">End Date</label>
            <input
              type="date" value={endDate}
              onChange={e => setEndDate(e.target.value)}
              className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50"
            />
          </div>
          <div>
            <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">Capital (₹)</label>
            <input
              type="number" value={capital}
              onChange={e => setCapital(e.target.value)}
              className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50"
              min="10000" step="10000"
            />
          </div>
        </div>

        <div>
          <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-2">
            Symbols ({selectedSymbols.length} selected)
          </label>
          <div className="flex flex-wrap gap-1.5 max-h-36 overflow-y-auto pr-1">
            {NIFTY50.map(sym => (
              <button
                key={sym}
                onClick={() => toggleSymbol(sym)}
                className={`text-[10px] px-2 py-1 rounded border font-medium transition-colors ${
                  selectedSymbols.includes(sym)
                    ? 'bg-blue-600/25 text-blue-300 border-blue-600/50'
                    : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044] hover:text-slate-300'
                }`}
              >
                {sym}
              </button>
            ))}
          </div>
        </div>

        {error && (
          <div className="flex items-start gap-2 bg-red-950/30 border border-red-800/50 rounded-lg p-3 text-xs text-red-300">
            <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />
            {error}
          </div>
        )}

        <button
          onClick={handleRun}
          disabled={running || selectedSymbols.length === 0}
          className="flex items-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors"
        >
          {running ? <><RefreshCw size={14} className="animate-spin" /> Running Backtest...</> : <><Play size={14} /> Run Backtest</>}
        </button>
      </div>

      {/* Results */}
      {result && (
        <div className="space-y-4">
          {/* Metrics */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {metrics.map(m => (
              <div key={m.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-3">
                <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">{m.label}</div>
                <div className={`text-lg font-bold ${m.color ?? 'text-white'}`}>{m.value}</div>
              </div>
            ))}
          </div>

          {/* Equity Curve */}
          {equityCurve.length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-white mb-4 flex items-center gap-2"><BarChart2 size={14} className="text-blue-400" /> Equity Curve</h2>
              <div className="flex items-end gap-0.5 h-32">
                {equityCurve.map((pt, i) => {
                  const h = maxEq > minEq ? ((pt.value - minEq) / (maxEq - minEq)) * 100 : 50
                  const isUp = i === 0 || pt.value >= equityCurve[i - 1].value
                  return (
                    <div key={i} className="flex-1 flex flex-col justify-end h-full" title={`${pt.date}: ${formatCurrency(pt.value)}`}>
                      <div className={`w-full rounded-t ${isUp ? 'bg-emerald-500/70' : 'bg-red-500/70'}`} style={{ height: `${Math.max(h, 2)}%` }} />
                    </div>
                  )
                })}
              </div>
              <div className="flex justify-between text-[10px] text-slate-600 mt-1">
                <span>{equityCurve[0]?.date}</span>
                <span>{formatCurrency(equityCurve[equityCurve.length - 1]?.value)}</span>
              </div>
            </div>
          )}

          {/* Monthly Returns */}
          {monthlyReturns.length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-white mb-4">Monthly Returns</h2>
              <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
                {monthlyReturns.map(([month, ret]) => (
                  <div key={month} className={`rounded-lg p-2.5 border text-center ${ret >= 0 ? 'bg-emerald-950/20 border-emerald-800/40' : 'bg-red-950/20 border-red-800/40'}`}>
                    <div className="text-[10px] text-slate-500">{month}</div>
                    <div className={`text-xs font-bold mt-0.5 ${pnlColor(ret)}`}>{formatPercent(ret)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Trade log */}
          {result.trade_log && result.trade_log.length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
              <div className="px-4 py-3 border-b border-[#1e2d45]">
                <h2 className="text-sm font-semibold text-white">Trade Log ({result.trade_log.length} trades)</h2>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-500 border-b border-[#1e2d45]">
                      {['Symbol','Entry','Exit','Entry Price','Exit Price','Qty','Net P&L','R'].map(h => (
                        <th key={h} className="text-left px-3 py-2 font-medium whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.trade_log.slice(0, 50).map((t, i) => (
                      <tr key={i} className={`border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235] ${(t.net_pnl ?? 0) >= 0 ? 'bg-emerald-950/5' : 'bg-red-950/5'}`}>
                        <td className="px-3 py-2 font-semibold text-white">{t.symbol}</td>
                        <td className="px-3 py-2 text-slate-400">{t.entry_time?.slice(0, 10)}</td>
                        <td className="px-3 py-2 text-slate-400">{t.exit_time?.slice(0, 10)}</td>
                        <td className="px-3 py-2">₹{(t.entry_price ?? 0).toFixed(2)}</td>
                        <td className="px-3 py-2">{t.exit_price ? `₹${t.exit_price.toFixed(2)}` : '—'}</td>
                        <td className="px-3 py-2 text-slate-300">{t.quantity}</td>
                        <td className={`px-3 py-2 font-semibold ${pnlColor(t.net_pnl)}`}>{formatCurrency(t.net_pnl)}</td>
                        <td className={`px-3 py-2 font-medium ${pnlColor(t.pnl_r)}`}>{t.pnl_r != null ? `${t.pnl_r >= 0 ? '+' : ''}${t.pnl_r.toFixed(2)}R` : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {result.trade_log.length > 50 && (
                  <div className="px-3 py-2 text-xs text-slate-500 text-center border-t border-[#1e2d45]">
                    Showing first 50 of {result.trade_log.length} trades. Export CSV for full list.
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
