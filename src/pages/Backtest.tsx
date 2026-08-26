import { useState, useEffect, useRef } from 'react'
import axios from 'axios'
import { Play, BarChart2, RefreshCw, AlertTriangle, Info, Download } from 'lucide-react'
import { runBacktest, getBacktestStatus, getBacktestResult, downloadBacktestResult } from '../api/endpoints'
import { formatCurrency, pnlColor } from '../utils/formatters'
import type { BacktestResponse } from '../types'

const INDICES = ['NIFTY50', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX']

const STRATEGIES = [
  { id: 'OPTION_PREMIUM', label: 'Option Premium', desc: 'Broker-resolved contract, premium momentum, VWAP, liquidity and expiry controls' },
]

const INTERVALS = [
  { id: '5minute',  label: '5 min' },
  { id: '15minute', label: '15 min' },
  { id: '30minute', label: '30 min' },
  { id: 'day',      label: 'Daily' },
]

const today = new Date()
const defaultEndDate = today.toISOString().slice(0, 10)
const defaultStartDate = new Date(today.getFullYear() - 1, today.getMonth(), today.getDate())
  .toISOString().slice(0, 10)

function strategyParam(id: string): string[] { return [id] }

export default function Backtest() {
  const [startDate, setStartDate]             = useState(defaultStartDate)
  const [endDate, setEndDate]                 = useState(defaultEndDate)
  const [capital, setCapital]                 = useState('100000')
  const [strategy, setStrategy]               = useState('OPTION_PREMIUM')
  const [interval, setInterval]               = useState('5minute')
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(['NIFTY50'])
  const [running, setRunning]                 = useState(false)
  const [result, setResult]                   = useState<BacktestResponse | null>(null)
  const [error, setError]                     = useState<string | null>(null)
  const [taskId, setTaskId]                   = useState<string | null>(null)
  const [downloading, setDownloading]         = useState<'csv' | 'json' | null>(null)
  const [progress, setProgress]               = useState<{ phase?: string; symbol?: string; symbol_index?: number; total_symbols?: number; bar_index?: number; total_bars?: number; symbols_fetched?: number } | null>(null)
  const [tradeFilter, setTradeFilter]         = useState<'ALL' | 'WINS' | 'LOSSES'>('ALL')
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current) }, [])

  const toggleSymbol = (sym: string) =>
    setSelectedSymbols(prev => prev.includes(sym) ? prev.filter(s => s !== sym) : [...prev, sym])

  const handleRun = async () => {
    if (selectedSymbols.length === 0) { setError('Select at least one index underlying.'); return }
    setRunning(true); setError(null); setResult(null); setProgress(null); setTaskId(null)
    try {
      const start = await runBacktest({
        start_date: startDate, end_date: endDate,
        capital: Number(capital), symbols: selectedSymbols,
        interval, strategies: strategyParam(strategy),
      })
      setTaskId(start.task_id)
      pollTask(start.task_id)
    } catch (e: unknown) {
      const detail = axios.isAxiosError(e) ? e.response?.data?.detail : undefined
      setError(typeof detail === 'string' ? detail : (e instanceof Error ? e.message : 'Backtest failed.'))
      setRunning(false)
    }
  }

  const handleDownload = async (format: 'csv' | 'json') => {
    if (!taskId || !result) return
    setDownloading(format)
    setError(null)
    try {
      await downloadBacktestResult(taskId, format)
    } catch (e: unknown) {
      const detail = axios.isAxiosError(e) ? e.response?.data?.detail : undefined
      setError(typeof detail === 'string' ? detail : 'Could not download the backtest result.')
    } finally {
      setDownloading(null)
    }
  }

  const pollTask = (taskId: string) => {
    const tick = async () => {
      try {
        const status = await getBacktestStatus(taskId)
        if (status.status === 'completed') {
          const finalResult = await getBacktestResult(taskId)
          setResult(finalResult)
          setRunning(false)
          setProgress(null)
          return
        }
        if (status.status === 'failed') {
          setError(status.error || 'Backtest failed.')
          setRunning(false)
          setProgress(null)
          return
        }
        setProgress(status.progress ?? null)
        pollRef.current = setTimeout(tick, 1000)
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Lost connection while polling backtest progress.')
        setRunning(false)
        setProgress(null)
      }
    }
    tick()
  }

  const r = result
  const tradesTaken   = r?.trades_taken ?? r?.trades_executed ?? r?.trade_log?.length ?? 0
  const tradeLog      = r?.trade_log ?? []
  const winningTrades = r?.winning_trades ?? tradeLog.filter(t => (t.net_pnl ?? 0) > 0).length
  const losingTrades  = r?.losing_trades ?? tradeLog.filter(t => (t.net_pnl ?? 0) < 0).length
  const accuracy      = tradesTaken > 0 ? (winningTrades / tradesTaken) * 100 : (r?.accuracy_pct ?? 0)
  
  const grossProfit = tradeLog.length > 0 
    ? tradeLog.filter(t => (t.gross_pnl ?? 0) > 0).reduce((acc, t) => acc + (t.gross_pnl ?? 0), 0)
    : (r?.net_profit && r.net_profit > 0 ? r.net_profit : 0)
  const grossLoss = tradeLog.length > 0
    ? Math.abs(tradeLog.filter(t => (t.gross_pnl ?? 0) < 0).reduce((acc, t) => acc + (t.gross_pnl ?? 0), 0))
    : 0

  const profitFactor  = r?.profit_factor ?? (grossLoss > 0 ? grossProfit / grossLoss : (grossProfit > 0 ? 999 : 0))
  const netProfit     = r?.net_profit ?? (grossProfit - grossLoss - (r?.total_charges ?? 0))
  const netProfitPct  = r?.net_profit_pct ?? (Number(capital) > 0 ? (netProfit / Number(capital)) * 100 : 0)
  const maxDD         = r?.max_drawdown_pct ?? 0
  const totalCharges  = r?.total_charges ?? tradeLog.reduce((acc, t) => acc + (t.charges ?? 0), 0)

  const winningPnLTotal = tradeLog.filter(t => (t.net_pnl ?? 0) > 0).reduce((acc, t) => acc + (t.net_pnl ?? 0), 0)
  const losingPnLTotal = Math.abs(tradeLog.filter(t => (t.net_pnl ?? 0) < 0).reduce((acc, t) => acc + (t.net_pnl ?? 0), 0))
  const avgWin = winningTrades > 0 ? winningPnLTotal / winningTrades : 0
  const avgLoss = losingTrades > 0 ? losingPnLTotal / losingTrades : 0
  const expectancy = tradesTaken > 0 ? netProfit / tradesTaken : 0

  const equityCurve   = r?.equity_curve ?? []
  const candlesScanned = r?.total_candles_scanned ?? 0
  const signalsGenerated = r?.signals_generated ?? 0
  const rejectedTotal  = r?.rejected_signals_total_count ?? 0
  const rejectionReasons = r?.rejection_reason_counts ?? {}
  const skippedSymbols = r?.skipped_symbols ?? []
  const dataSource     = r?.data_source ?? ''

  const eqValues = equityCurve.map(e => e.equity)
  const maxEq = eqValues.length > 0 ? Math.max(...eqValues) : 1
  const minEq = eqValues.length > 0 ? Math.min(...eqValues) : 0

  const filteredTrades = tradeLog.filter(t => {
    if (tradeFilter === 'WINS') return (t.net_pnl ?? 0) > 0
    if (tradeFilter === 'LOSSES') return (t.net_pnl ?? 0) < 0
    return true
  })

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold text-white">Backtest</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Runs the option-premium strategy against broker-resolved contracts and real historical premium data.
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
            Historical premium interval
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
          <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-2">Index underlyings</label>
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

        {running && progress && (
          <div className="text-xs text-slate-400 bg-[#0f1628] border border-[#1e2d45] rounded-lg p-3">
            {progress.phase === 'fetching_data' && (
              <span>Fetching real historical data — {progress.symbols_fetched ?? 0}/{progress.total_symbols ?? '?'} symbols done...</span>
            )}
            {progress.phase === 'processing' && (
              <span>
                Processing {progress.symbol} ({progress.symbol_index}/{progress.total_symbols}) —
                bar {progress.bar_index}/{progress.total_bars}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Results */}
      {result && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2 bg-[#141b2d] border border-[#1e2d45] rounded-xl px-4 py-3">
            <div className="text-xs text-slate-400">Export the complete result, including the summary and trade log.</div>
            <div className="flex gap-2">
              <button onClick={() => handleDownload('csv')} disabled={!taskId || downloading !== null}
                className="flex items-center gap-1.5 rounded-lg border border-[#2a3a56] px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-[#1a2235] disabled:cursor-not-allowed disabled:opacity-50">
                {downloading === 'csv' ? <RefreshCw size={13} className="animate-spin" /> : <Download size={13} />} CSV
              </button>
              <button onClick={() => handleDownload('json')} disabled={!taskId || downloading !== null}
                className="flex items-center gap-1.5 rounded-lg border border-[#2a3a56] px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-[#1a2235] disabled:cursor-not-allowed disabled:opacity-50">
                {downloading === 'json' ? <RefreshCw size={13} className="animate-spin" /> : <Download size={13} />} JSON
              </button>
            </div>
          </div>
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

          {/* Signals & Rejections Overview Card */}
          <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-center">
              <div className="bg-[#0f1628] border border-[#1e2d45] rounded-lg p-3">
                <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-0.5">Signals Generated</div>
                <div className="text-xl font-bold text-blue-400">{signalsGenerated}</div>
              </div>
              <div className="bg-[#0f1628] border border-[#1e2d45] rounded-lg p-3">
                <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-0.5">Trades Executed</div>
                <div className="text-xl font-bold text-emerald-400">{tradesTaken}</div>
              </div>
              <div className="bg-[#0f1628] border border-[#1e2d45] rounded-lg p-3">
                <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-0.5">Rejected</div>
                <div className="text-xl font-bold text-amber-400">{rejectedTotal}</div>
              </div>
            </div>
          </div>

          {/* Rejection Breakdown Panel */}
          {Object.keys(rejectionReasons).length > 0 && (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                  <AlertTriangle size={14} className="text-amber-400" />
                  Rejection Breakdown
                </h2>
                <span className="text-xs text-slate-500 font-medium">
                  {rejectedTotal} total rejection evaluation(s)
                </span>
              </div>
              <div className="space-y-2">
                {Object.entries(rejectionReasons)
                  .sort((a, b) => b[1] - a[1])
                  .map(([reason, count]) => {
                    const pct = rejectedTotal > 0 ? ((count / rejectedTotal) * 100).toFixed(1) : '0.0'
                    return (
                      <div key={reason} className="bg-[#0f1628] border border-[#1e2d45] rounded-lg p-2.5">
                        <div className="flex items-center justify-between text-xs mb-1">
                          <span className="text-slate-300 font-medium">{reason}</span>
                          <span className="text-slate-400 font-semibold ml-2 whitespace-nowrap">
                            {count} ({pct}%)
                          </span>
                        </div>
                        <div className="h-1.5 bg-[#141b2d] rounded-full overflow-hidden">
                          <div
                            className="h-full bg-amber-500/70 rounded-full"
                            style={{ width: `${Math.min(100, Math.max(2, parseFloat(pct)))}%` }}
                          />
                        </div>
                      </div>
                    )
                  })}
              </div>
            </div>
          )}

          {tradesTaken === 0 ? (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-8 text-center">
              <BarChart2 size={28} className="mx-auto mb-2 text-slate-600" />
              <div className="text-sm text-slate-400 font-medium">No trades taken in this window</div>
              <div className="text-xs text-slate-600 mt-1">
                {signalsGenerated > 0
                  ? `${signalsGenerated} signal(s) were generated but not all conditions passed — see rejection reasons above.`
                  : 'No strategy conditions were fully met. Try a longer date range, a different interval, or a trending symbol.'}
              </div>
            </div>
          ) : (
            <>
              {/* Comprehensive Summary Metrics */}
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-2.5">
                {[
                  { label: 'Total Trades',    value: String(tradesTaken),                                               color: 'text-white' },
                  { label: 'Winning Trades',  value: String(winningTrades),                                             color: 'text-emerald-400' },
                  { label: 'Losing Trades',   value: String(losingTrades),                                              color: 'text-red-400' },
                  { label: 'Win Rate',        value: `${accuracy.toFixed(1)}%`,                                         color: accuracy >= 40 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Gross Profit',    value: formatCurrency(grossProfit),                                       color: 'text-emerald-400' },
                  { label: 'Gross Loss',      value: formatCurrency(grossLoss),                                         color: 'text-red-400' },
                  { label: 'Net P&L',         value: `${formatCurrency(netProfit)} (${netProfitPct >= 0 ? '+' : ''}${netProfitPct.toFixed(2)}%)`, color: pnlColor(netProfit) },
                  { label: 'Profit Factor',   value: profitFactor >= 99 ? '99.00+' : profitFactor.toFixed(2),           color: profitFactor >= 1.5 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Max Drawdown',    value: `${maxDD.toFixed(2)}%`,                                            color: maxDD < 5 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Total Charges',   value: formatCurrency(totalCharges),                                      color: 'text-slate-400' },
                  { label: 'Avg Win',         value: formatCurrency(avgWin),                                            color: 'text-emerald-400' },
                  { label: 'Avg Loss',        value: formatCurrency(avgLoss),                                           color: 'text-red-400' },
                  { label: 'Expectancy',      value: `${formatCurrency(expectancy)}/trade`,                             color: pnlColor(expectancy) },
                  { label: 'Candles Scanned', value: candlesScanned.toLocaleString(),                                   color: 'text-slate-400' },
                ].map(m => (
                  <div key={m.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-3">
                    <div className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1 truncate">{m.label}</div>
                    <div className={`text-sm font-bold ${m.color} truncate`}>{m.value}</div>
                  </div>
                ))}
              </div>

              {/* Win / Loss Ratio bar */}
              <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
                <div className="flex items-center justify-between text-xs mb-2">
                  <span className="text-emerald-400 font-semibold">{winningTrades} Wins ({tradesTaken > 0 ? ((winningTrades / tradesTaken) * 100).toFixed(1) : 0}%)</span>
                  <span className="text-slate-400">{tradesTaken} Total Closed Trades</span>
                  <span className="text-red-400 font-semibold">{losingTrades} Losses ({tradesTaken > 0 ? ((losingTrades / tradesTaken) * 100).toFixed(1) : 0}%)</span>
                </div>
                <div className="h-2.5 bg-red-900/40 rounded-full overflow-hidden flex">
                  <div className="h-full bg-emerald-500 rounded-l-full transition-all duration-500"
                    style={{ width: `${tradesTaken > 0 ? (winningTrades / tradesTaken * 100) : 0}%` }} />
                  <div className="h-full bg-red-500 rounded-r-full transition-all duration-500"
                    style={{ width: `${tradesTaken > 0 ? (losingTrades / tradesTaken * 100) : 0}%` }} />
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

              {tradeLog.length > 0 && (
                <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden shadow-lg">
                  <div className="px-4 py-3 border-b border-[#1e2d45] flex flex-wrap items-center justify-between gap-3 bg-[#111728]">
                    <div className="flex items-center gap-3">
                      <h2 className="text-sm font-bold text-white tracking-wide">Trade Log</h2>
                      <span className="text-xs px-2 py-0.5 rounded-full bg-[#1e2d45] text-slate-300 font-semibold">
                        {filteredTrades.length} / {tradeLog.length} trades
                      </span>
                    </div>

                    <div className="flex items-center gap-1.5 bg-[#0b101d] p-1 rounded-lg border border-[#1e2d45]">
                      <button
                        onClick={() => setTradeFilter('ALL')}
                        className={`text-xs px-2.5 py-1 rounded font-medium transition-all ${
                          tradeFilter === 'ALL'
                            ? 'bg-blue-600 text-white shadow'
                            : 'text-slate-400 hover:text-slate-200'
                        }`}
                      >
                        All ({tradeLog.length})
                      </button>
                      <button
                        onClick={() => setTradeFilter('WINS')}
                        className={`text-xs px-2.5 py-1 rounded font-medium transition-all ${
                          tradeFilter === 'WINS'
                            ? 'bg-emerald-600 text-white shadow'
                            : 'text-slate-400 hover:text-emerald-300'
                        }`}
                      >
                        Wins ({winningTrades})
                      </button>
                      <button
                        onClick={() => setTradeFilter('LOSSES')}
                        className={`text-xs px-2.5 py-1 rounded font-medium transition-all ${
                          tradeFilter === 'LOSSES'
                            ? 'bg-red-600 text-white shadow'
                            : 'text-slate-400 hover:text-red-300'
                        }`}
                      >
                        Losses ({losingTrades})
                      </button>
                    </div>
                  </div>

                  <div className="overflow-x-auto max-h-[600px] overflow-y-auto">
                    <table className="w-full text-xs text-left border-collapse">
                      <thead className="sticky top-0 z-10 bg-[#0f1628] border-b border-[#1e2d45]">
                        <tr className="text-slate-400 font-semibold text-[11px] uppercase tracking-wider">
                          <th className="px-3.5 py-3 whitespace-nowrap">Date / Time</th>
                          <th className="px-3 py-3 whitespace-nowrap">CE / PE</th>
                          <th className="px-3 py-3 whitespace-nowrap">ATM Strike</th>
                          <th className="px-3 py-3 whitespace-nowrap">Expiry</th>
                          <th className="px-3 py-3 whitespace-nowrap text-right">Entry ₹</th>
                          <th className="px-3 py-3 whitespace-nowrap text-right">Exit ₹</th>
                          <th className="px-3 py-3 whitespace-nowrap text-center">Qty</th>
                          <th className="px-3 py-3 whitespace-nowrap text-right">P&L ₹ (Gross)</th>
                          <th className="px-3 py-3 whitespace-nowrap text-right">Net P&L ₹</th>
                          <th className="px-3 py-3 whitespace-nowrap">Exit Reason</th>
                          <th className="px-3.5 py-3 whitespace-nowrap text-center">Result</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[#1a253a]">
                        {filteredTrades.map((t, i) => {
                          const isWin = (t.net_pnl ?? 0) > 0
                          const isLoss = (t.net_pnl ?? 0) < 0
                          const optType = t.option_type || (t.symbol.includes('CE') ? 'CE' : t.symbol.includes('PE') ? 'PE' : '')
                          const strikeVal = t.strike !== undefined && t.strike !== null && t.strike !== '' ? t.strike : ''
                          const formattedReason = (t.exit_reason || '')
                            .replace(/_/g, ' ')
                            .replace(/\b\w/g, l => l.toUpperCase())

                          return (
                            <tr
                              key={i}
                              className={`transition-colors hover:bg-[#182236] ${
                                isWin ? 'bg-emerald-950/10' : isLoss ? 'bg-red-950/10' : 'bg-slate-900/10'
                              }`}
                            >
                              <td className="px-3.5 py-2.5 whitespace-nowrap font-mono text-slate-300">
                                <div>{(t.entry_time || t.timestamp || '').slice(0, 16).replace('T', ' ')}</div>
                                {t.exit_time && (
                                  <div className="text-[10px] text-slate-500">
                                    Exit: {t.exit_time.slice(0, 16).replace('T', ' ')}
                                  </div>
                                )}
                              </td>

                              <td className="px-3 py-2.5 whitespace-nowrap">
                                {optType === 'CE' ? (
                                  <span className="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-900/40 text-emerald-300 border border-emerald-700/50">
                                    CE
                                  </span>
                                ) : optType === 'PE' ? (
                                  <span className="px-2 py-0.5 rounded text-[11px] font-bold bg-purple-900/40 text-purple-300 border border-purple-700/50">
                                    PE
                                  </span>
                                ) : (
                                  <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-slate-800 text-slate-400">
                                    SPOT
                                  </span>
                                )}
                              </td>

                              <td className="px-3 py-2.5 whitespace-nowrap font-mono text-slate-200">
                                {strikeVal ? `₹${Number(strikeVal).toLocaleString('en-IN')}` : '—'}
                              </td>

                              <td className="px-3 py-2.5 whitespace-nowrap font-mono text-slate-300">
                                {t.expiry || '—'}
                              </td>

                              <td className="px-3 py-2.5 whitespace-nowrap text-right font-mono text-slate-200">
                                ₹{Number(t.entry_price).toFixed(2)}
                              </td>

                              <td className="px-3 py-2.5 whitespace-nowrap text-right font-mono text-slate-200">
                                ₹{Number(t.exit_price).toFixed(2)}
                              </td>

                              <td className="px-3 py-2.5 whitespace-nowrap text-center text-slate-300 font-mono">
                                {t.quantity}
                              </td>

                              <td className={`px-3 py-2.5 whitespace-nowrap text-right font-mono font-medium ${pnlColor(t.gross_pnl)}`}>
                                {formatCurrency(t.gross_pnl)}
                              </td>

                              <td className={`px-3 py-2.5 whitespace-nowrap text-right font-mono font-bold ${pnlColor(t.net_pnl)}`}>
                                {formatCurrency(t.net_pnl)}
                              </td>

                              <td className="px-3 py-2.5 whitespace-nowrap text-slate-300">
                                <span className="px-2 py-0.5 rounded bg-[#1c283f] text-[11px] text-slate-300 border border-[#2b3c5c]">
                                  {formattedReason || '—'}
                                </span>
                              </td>

                              <td className="px-3.5 py-2.5 whitespace-nowrap text-center">
                                {isWin ? (
                                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
                                    WIN
                                  </span>
                                ) : isLoss ? (
                                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-red-500/20 text-red-300 border border-red-500/40">
                                    LOSS
                                  </span>
                                ) : (
                                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-slate-700/50 text-slate-400 border border-slate-600">
                                    FLAT
                                  </span>
                                )}
                              </td>
                            </tr>
                          )
                        })}
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
