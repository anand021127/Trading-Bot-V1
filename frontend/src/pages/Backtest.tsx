import { useState } from 'react'
import { Play, BarChart2, RefreshCw, AlertTriangle, Info } from 'lucide-react'
import { runBacktest } from '../api/endpoints'
import { formatCurrency, pnlColor } from '../utils/formatters'
import type { BacktestResponse } from '../types'

const NIFTY50 = [
  'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','KOTAKBANK','LT','SBIN','AXISBANK',
  'BHARTIARTL','ITC','ASIANPAINT','MARUTI','HCLTECH','SUNPHARMA','WIPRO','TITAN','ULTRACEMCO','BAJFINANCE',
  'NESTLEIND','TECHM','NTPC','POWERGRID','ONGC','JSWSTEEL','TATASTEEL','HINDALCO','TATAMOTORS',
  'BAJAJFINSV','DRREDDY','CIPLA','DIVISLAB','APOLLOHOSP','ADANIENT','ADANIPORTS','COALINDIA',
  'BPCL','EICHERMOT','HEROMOTOCO','INDUSINDBK','SBILIFE','HDFCLIFE','GRASIM','TATACONSUM',
  'UPL','BRITANNIA','SHREECEM','BAJAJ-AUTO','M&M',
]

// Item #5 — indices were missing from every symbol picker in this app.
const INDICES = ['NIFTY50', 'BANKNIFTY', 'SENSEX']

const STRATEGIES = [
  { id: 'ORB',       label: 'ORB Strategy',       desc: 'Opening Range Breakout, resets every trading day, volume + ATR stop' },
  { id: 'EMA_TREND', label: 'EMA Trend',           desc: 'EMA 20/50 trend + RSI + volume confirmation, ATR stop' },
  { id: 'BOTH',      label: 'Both (best signal)',  desc: 'Runs both strategies, takes whichever has higher confidence' },
]

const INTERVALS = [
  { id: '5minute',  label: '5 min' },
  { id: '15minute', label: '15 min' },
  { id: '30minute', label: '30 min' },
  { id: 'day',      label: 'Daily' },
]

function strategyParam(id: string): string[] | undefined {
  if (id === 'BOTH') return undefined // backend default runs both
  return [id]
}

export default function Backtest() {
  const [startDate, setStartDate]             = useState('2025-01-01')
  const [endDate, setEndDate]                 = useState('2025-12-31')
  const [capital, setCapital]                 = useState('100000')
  const [strategy, setStrategy]               = useState('BOTH')
  const [interval, setInterval]               = useState('5minute')
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(['RELIANCE', 'TCS', 'HDFCBANK'])
  const [running, setRunning]                 = useState(false)
  const [result, setResult]                   = useState<BacktestResponse | null>(null)
  const [error, setError]                     = useState<string | null>(null)

  const toggleSymbol = (sym: string) =>
    setSelectedSymbols(prev => prev.includes(sym) ? prev.filter(s => s !== sym) : [...prev, sym])

  const handleRun = async () => {
    if (selectedSymbols.length === 0) { setError('Select at least one symbol.'); return }
    setRunning(true); setError(null); setResult(null)
    try {
      const res = await runBacktest({
        start_date: startDate, end_date: endDate,
        capital: Number(capital), symbols: selectedSymbols,
        interval, strategies: strategyParam(strategy),
      })
      setResult(res)
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : (e instanceof Error ? e.message : 'Backtest failed.'))
    } finally {
      setRunning(false)
    }
  }

  const r = result
  const tradesTaken   = r?.trades_taken ?? 0
  const winningTrades = r?.winning_trades ?? 0
  const losingTrades  = r?.losing_trades ?? 0
  const accuracy      = r?.accuracy_pct ?? 0
  const profitFactor  = r?.profit_factor ?? 0
  const netProfit     = r?.net_profit ?? 0
  const netProfitPct  = r?.net_profit_pct ?? 0
  const maxDD         = r?.max_drawdown_pct ?? 0
  const totalCharges  = r?.total_charges ?? 0
  const equityCurve   = r?.equity_curve ?? []
  const tradeLog      = r?.trade_log ?? []
  const candlesScanned = r?.total_candles_scanned ?? 0
  const signalsGenerated = r?.signals_generated ?? 0
  const rejectedTotal  = r?.rejected_signals_total_count ?? 0
  const rejectionReasons = r?.rejection_reason_counts ?? {}
  const skippedSymbols = r?.skipped_symbols ?? []
  const dataSource     = r?.data_source ?? ''

  const eqValues = equityCurve.map(e => e.equity)
  const maxEq = eqValues.length > 0 ? Math.max(...eqValues) : 1
  const minEq = eqValues.length > 0 ? Math.min(...eqValues) : 0

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold text-white">Backtest</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Runs the real strategy engine against real Upstox historical candles — no synthetic data, ever.
        </p>
      </div>

      {/* Config */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5 space-y-4">
        <h2 className="text-sm font-semibold text-white">Configuration</h2>

        <div>
          <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-2">Strategy</label>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            {STRATEGIES.map(s => (
              <button key={s.id} onClick={() => setStrategy(s.id)}
                className={`text-left p-3 rounded-lg border transition-colors ${
                  strategy === s.id
                    ? 'bg-blue-600/20 border-blue-600/50 text-blue-300'
                    : 'bg-[#0f1628] border-[#1e2d45] text-slate-400 hover:border-[#243044]'
                }`}>
                <div className="text-xs font-semibold">{s.label}</div>
                <div className="text-[10px] text-slate-500 mt-0.5">{s.desc}</div>
              </button>
            ))}
          </div>
        </div>

        <div>
          <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-2">
            Candle Interval — ORB needs intraday data to compute a real opening range
          </label>
          <div className="flex gap-2">
            {INTERVALS.map(iv => (
              <button key={iv.id} onClick={() => setInterval(iv.id)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                  interval === iv.id
                    ? 'bg-blue-600/20 text-blue-400 border-blue-600/50'
                    : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044]'
                }`}>
                {iv.label}
              </button>
            ))}
          </div>
          {interval === 'day' && strategy !== 'EMA_TREND' && (
            <div className="text-[10px] text-amber-400 mt-1.5">
              ⚠ ORB's opening range needs intraday bars. On daily candles it will rarely find a valid signal.
            </div>
          )}
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">Start Date</label>
            <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
              className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
          </div>
          <div>
            <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">End Date</label>
            <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
              className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
          </div>
          <div>
            <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">Capital (₹)</label>
            <input type="number" value={capital} onChange={e => setCapital(e.target.value)} min="10000" step="10000"
              className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
          </div>
        </div>

        {interval !== 'day' && (endDate > addMonths(startDate, 2)) && (
          <div className="flex items-start gap-2 bg-amber-950/20 border border-amber-800/40 rounded-lg p-2.5 text-[11px] text-amber-300">
            <Info size={12} className="flex-shrink-0 mt-0.5" />
            Wide date range at intraday granularity means many chunked API calls per symbol — this can take a while.
          </div>
        )}

        <div>
          <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-2">Indices</label>
          <div className="flex flex-wrap gap-1.5 mb-3">
            {INDICES.map(sym => (
              <button key={sym} onClick={() => toggleSymbol(sym)}
                className={`text-[10px] px-2 py-1 rounded border font-medium transition-colors ${
                  selectedSymbols.includes(sym)
                    ? 'bg-purple-600/25 text-purple-300 border-purple-600/50'
                    : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044] hover:text-slate-300'
                }`}>
                {sym}
              </button>
            ))}
          </div>
          <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-2">
            Stocks ({selectedSymbols.length} selected)
          </label>
          <div className="flex flex-wrap gap-1.5 max-h-36 overflow-y-auto pr-1">
            {NIFTY50.map(sym => (
              <button key={sym} onClick={() => toggleSymbol(sym)}
                className={`text-[10px] px-2 py-1 rounded border font-medium transition-colors ${
                  selectedSymbols.includes(sym)
                    ? 'bg-blue-600/25 text-blue-300 border-blue-600/50'
                    : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044] hover:text-slate-300'
                }`}>
                {sym}
              </button>
            ))}
          </div>
        </div>

        {error && (
          <div className="flex items-start gap-2 bg-red-950/30 border border-red-800/50 rounded-lg p-3 text-xs text-red-300">
            <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />{error}
          </div>
        )}

        <button onClick={handleRun} disabled={running || selectedSymbols.length === 0}
          className="flex items-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors">
          {running ? <><RefreshCw size={14} className="animate-spin" /> Running backtest...</>
                   : <><Play size={14} /> Run Backtest</>}
        </button>
      </div>

      {/* Results */}
      {result && (
        <div className="space-y-4">
          {dataSource && (
            <div className="flex items-start gap-2 rounded-xl border px-4 py-3 text-xs bg-emerald-950/20 border-emerald-800/40 text-emerald-300">
              <Info size={13} className="flex-shrink-0 mt-0.5" />
              {r?.message ?? `Processed ${candlesScanned.toLocaleString()} real candles (${dataSource}), ${signalsGenerated} signal(s) generated, ${tradesTaken} trade(s) taken.`}
            </div>
          )}

          {skippedSymbols.length > 0 && (
            <div className="flex items-start gap-2 rounded-xl border px-4 py-3 text-xs bg-amber-950/20 border-amber-800/40 text-amber-300">
              <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />
              <div>
                {skippedSymbols.map(s => (
                  <div key={s.symbol}><strong>{s.symbol}</strong> skipped — {s.reason}</div>
                ))}
              </div>
            </div>
          )}

          {tradesTaken === 0 ? (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-8 text-center">
              <BarChart2 size={28} className="mx-auto mb-2 text-slate-600" />
              <div className="text-sm text-slate-400 font-medium">No trades taken in this window</div>
              <div className="text-xs text-slate-600 mt-1">
                {signalsGenerated > 0
                  ? `${signalsGenerated} signal(s) were generated but not all conditions passed — see rejection reasons below.`
                  : 'No strategy conditions were fully met. Try a longer date range, a different interval, or a trending symbol.'}
              </div>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {[
                  { label: 'Trades Taken',  value: String(tradesTaken),               color: 'text-white' },
                  { label: 'Accuracy',      value: `${accuracy.toFixed(1)}%`,          color: accuracy >= 40 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Profit Factor', value: profitFactor.toFixed(2),            color: profitFactor >= 1.5 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Net Profit',    value: `${formatCurrency(netProfit)} (${netProfitPct.toFixed(2)}%)`, color: pnlColor(netProfit) },
                  { label: 'Max Drawdown',  value: `${maxDD.toFixed(2)}%`,             color: maxDD < 5 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Total Charges', value: formatCurrency(totalCharges),       color: 'text-slate-400' },
                  { label: 'Candles Scanned', value: candlesScanned.toLocaleString(), color: 'text-slate-400' },
                  { label: 'Signals Generated', value: String(signalsGenerated),      color: 'text-slate-400' },
                ].map(m => (
                  <div key={m.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-3">
                    <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">{m.label}</div>
                    <div className={`text-base font-bold ${m.color}`}>{m.value}</div>
                  </div>
                ))}
              </div>

              <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
                <div className="flex items-center justify-between text-xs mb-2">
                  <span className="text-emerald-400">{winningTrades} wins</span>
                  <span className="text-slate-500">{tradesTaken} total trades</span>
                  <span className="text-red-400">{losingTrades} losses</span>
                </div>
                <div className="h-2 bg-red-900/40 rounded-full overflow-hidden">
                  <div className="h-full bg-emerald-500 rounded-full"
                    style={{ width: `${tradesTaken > 0 ? (winningTrades / tradesTaken * 100) : 0}%` }} />
                </div>
              </div>

              {equityCurve.length > 1 && (
                <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
                  <h2 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
                    <BarChart2 size={14} className="text-blue-400" /> Equity Curve
                  </h2>
                  <div className="flex items-end gap-0.5 h-32">
                    {equityCurve.map((pt, i) => {
                      const h = maxEq > minEq ? ((pt.equity - minEq) / (maxEq - minEq)) * 100 : 50
                      const prevVal = i > 0 ? equityCurve[i - 1].equity : pt.equity
                      const isUp = pt.equity >= prevVal
                      return (
                        <div key={i} className="flex-1 flex flex-col justify-end h-full"
                          title={`${pt.timestamp}: ${formatCurrency(pt.equity)}`}>
                          <div className={`w-full rounded-t ${isUp ? 'bg-emerald-500/70' : 'bg-red-500/70'}`}
                            style={{ height: `${Math.max(h, 1)}%` }} />
                        </div>
                      )
                    })}
                  </div>
                  <div className="flex justify-between text-[10px] text-slate-600 mt-1">
                    <span>{formatCurrency(equityCurve[0]?.equity)}</span>
                    <span>{formatCurrency(equityCurve[equityCurve.length - 1]?.equity)}</span>
                  </div>
                </div>
              )}

              {Object.keys(rejectionReasons).length > 0 && (
                <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
                  <h2 className="text-sm font-semibold text-white mb-3">
                    Why signals were rejected ({rejectedTotal} total)
                  </h2>
                  <div className="space-y-1.5">
                    {Object.entries(rejectionReasons).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([reason, count]) => (
                      <div key={reason} className="flex items-center justify-between text-xs">
                        <span className="text-slate-400">{reason}</span>
                        <span className="text-slate-500 font-medium">{count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {tradeLog.length > 0 && (
                <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-[#1e2d45] flex items-center justify-between">
                    <h2 className="text-sm font-semibold text-white">Trade Log</h2>
                    <span className="text-xs text-slate-500">{tradeLog.length} trades</span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-slate-500 border-b border-[#1e2d45]">
                          {['Symbol','Strategy','Entry','Exit','Entry ₹','Exit ₹','Qty','Gross','Charges','Net P&L','Confidence','Reason'].map(h => (
                            <th key={h} className="text-left px-3 py-2 font-medium whitespace-nowrap">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {tradeLog.map((t, i) => (
                          <tr key={i} className={`border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235] ${t.net_pnl >= 0 ? 'bg-emerald-950/5' : 'bg-red-950/5'}`}>
                            <td className="px-3 py-2 font-semibold text-white">{t.symbol}</td>
                            <td className="px-3 py-2">
                              <span className="text-[9px] px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-400 border border-blue-800/40 uppercase">{t.strategy}</span>
                            </td>
                            <td className="px-3 py-2 text-slate-400">{(t.entry_time || '').slice(0, 16).replace('T', ' ')}</td>
                            <td className="px-3 py-2 text-slate-400">{(t.exit_time || '').slice(0, 16).replace('T', ' ')}</td>
                            <td className="px-3 py-2">₹{t.entry_price.toFixed(2)}</td>
                            <td className="px-3 py-2">₹{t.exit_price.toFixed(2)}</td>
                            <td className="px-3 py-2 text-slate-300">{t.quantity}</td>
                            <td className={`px-3 py-2 font-medium ${pnlColor(t.gross_pnl)}`}>{formatCurrency(t.gross_pnl)}</td>
                            <td className="px-3 py-2 text-slate-500">{formatCurrency(t.charges)}</td>
                            <td className={`px-3 py-2 font-semibold ${pnlColor(t.net_pnl)}`}>{formatCurrency(t.net_pnl)}</td>
                            <td className="px-3 py-2 text-slate-400">{t.confidence?.toFixed(0)}%</td>
                            <td className="px-3 py-2 text-slate-400">{(t.exit_reason || '').replace(/_/g, ' ')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function addMonths(dateStr: string, months: number): string {
  const d = new Date(dateStr)
  d.setMonth(d.getMonth() + months)
  return d.toISOString().slice(0, 10)
}
