import { useState } from 'react'
import { Play, BarChart2, RefreshCw, AlertTriangle, Info, TrendingUp, TrendingDown } from 'lucide-react'
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

const STRATEGIES = [
  { id: 'orb',      label: 'ORB Strategy',      desc: 'Opening Range Breakout with volume + ATR stop' },
  { id: 'ema',      label: 'EMA Crossover',      desc: 'EMA 20/50 crossover with RSI + trailing stop' },
  { id: 'combined', label: 'Combined Strategy',  desc: 'ORB signals only during EMA bullish trend' },
]

export default function Backtest() {
  const [startDate, setStartDate]             = useState('2024-01-01')
  const [endDate, setEndDate]                 = useState('2024-12-31')
  const [capital, setCapital]                 = useState('500000')
  const [strategy, setStrategy]               = useState('orb')
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(['RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK'])
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
        commission_pct: 0.0003, slippage_pct: 0.0001,
        strategy,
      } as any)
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Backtest failed. Make sure backend is running.')
    } finally {
      setRunning(false)
    }
  }

  const r = result as any
  const totalTrades    = r?.total_trades    ?? 0
  const winningTrades  = r?.winning_trades  ?? 0
  const losingTrades   = r?.losing_trades   ?? 0
  const winRate        = r?.win_rate        ?? 0
  const profitFactor   = r?.profit_factor   ?? 0
  const netProfit      = r?.net_profit      ?? 0
  const maxDD          = r?.max_drawdown_pct ?? 0
  const sharpe         = r?.sharpe_ratio    ?? 0
  const avgWinR        = r?.avg_win_r       ?? 0
  const avgLossR       = r?.avg_loss_r      ?? 0
  const totalCharges   = r?.total_charges   ?? 0
  const slippageImpact = r?.slippage_impact ?? 0
  const equityCurve    = r?.equity_curve    ?? []
  const monthlyReturns = r?.monthly_returns ?? {}
  const tradeLog       = r?.trade_log       ?? []
  const dataSource     = r?.data_source     ?? ''
  const message        = r?.message         ?? ''
  const bestTrade      = r?.best_trade
  const worstTrade     = r?.worst_trade
  const strategyUsed   = r?.strategy_used   ?? strategy

  const maxEq = equityCurve.length > 0 ? Math.max(...equityCurve.map((e: any) => e.value ?? 0)) : 1
  const minEq = equityCurve.length > 0 ? Math.min(...equityCurve.map((e: any) => e.value ?? 0)) : 0

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold text-white">Backtest</h1>
        <p className="text-xs text-slate-500 mt-0.5">Test strategies on historical data with real transaction costs</p>
      </div>

      {/* Config */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5 space-y-4">
        <h2 className="text-sm font-semibold text-white">Configuration</h2>

        {/* Strategy selector */}
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

        <div>
          <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-2">
            Symbols ({selectedSymbols.length} selected)
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
          {running ? <><RefreshCw size={14} className="animate-spin" /> Running {STRATEGIES.find(s=>s.id===strategy)?.label}...</>
                   : <><Play size={14} /> Run Backtest</>}
        </button>
      </div>

      {/* Results */}
      {result && (
        <div className="space-y-4">
          {/* Notice */}
          {message && (
            <div className={`flex items-start gap-2 rounded-xl border px-4 py-3 text-xs ${
              dataSource === 'synthetic'
                ? 'bg-amber-950/20 border-amber-800/40 text-amber-300'
                : 'bg-emerald-950/20 border-emerald-800/40 text-emerald-300'
            }`}>
              <Info size={13} className="flex-shrink-0 mt-0.5" />{message}
            </div>
          )}

          {totalTrades === 0 ? (
            <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-8 text-center">
              <BarChart2 size={28} className="mx-auto mb-2 text-slate-600" />
              <div className="text-sm text-slate-400 font-medium">No signals generated</div>
              <div className="text-xs text-slate-600 mt-1">Try ORB strategy or a longer date range.</div>
            </div>
          ) : (
            <>
              {/* Core metrics */}
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                {[
                  { label: 'Total Trades',   value: String(totalTrades),              color: 'text-white' },
                  { label: 'Win Rate',        value: `${winRate.toFixed(1)}%`,         color: winRate >= 40 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Profit Factor',   value: profitFactor.toFixed(2),          color: profitFactor >= 1.5 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Net Profit',      value: formatCurrency(netProfit),        color: pnlColor(netProfit) },
                  { label: 'Max Drawdown',    value: `${maxDD.toFixed(2)}%`,           color: maxDD < 5 ? 'text-emerald-400' : 'text-red-400' },
                  { label: 'Sharpe Ratio',    value: sharpe.toFixed(2),               color: sharpe >= 1 ? 'text-emerald-400' : 'text-amber-400' },
                  { label: 'Avg Win R',       value: `+${avgWinR.toFixed(2)}R`,       color: 'text-emerald-400' },
                  { label: 'Avg Loss R',      value: `${avgLossR.toFixed(2)}R`,       color: 'text-red-400' },
                  { label: 'Total Charges',   value: formatCurrency(totalCharges),    color: 'text-slate-400' },
                  { label: 'Slippage Impact', value: formatCurrency(slippageImpact),  color: 'text-slate-400' },
                ].map(m => (
                  <div key={m.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-3">
                    <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">{m.label}</div>
                    <div className={`text-base font-bold ${m.color}`}>{m.value}</div>
                  </div>
                ))}
              </div>

              {/* Wins / Losses bar */}
              <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
                <div className="flex items-center justify-between text-xs mb-2">
                  <span className="text-emerald-400">{winningTrades} wins</span>
                  <span className="text-slate-500">{totalTrades} total trades via {strategyUsed.toUpperCase()}</span>
                  <span className="text-red-400">{losingTrades} losses</span>
                </div>
                <div className="h-2 bg-red-900/40 rounded-full overflow-hidden">
                  <div className="h-full bg-emerald-500 rounded-full"
                    style={{ width: `${totalTrades > 0 ? (winningTrades/totalTrades*100) : 0}%` }} />
                </div>
              </div>

              {/* Best & Worst trades */}
              {(bestTrade || worstTrade) && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {bestTrade && (
                    <div className="bg-emerald-950/20 border border-emerald-800/40 rounded-xl p-4">
                      <div className="flex items-center gap-2 mb-2">
                        <TrendingUp size={13} className="text-emerald-400" />
                        <span className="text-xs font-semibold text-emerald-400">Best Trade</span>
                      </div>
                      <div className="text-white font-bold">{bestTrade.symbol}</div>
                      <div className="text-emerald-400 text-sm font-semibold">{formatCurrency(bestTrade.net_pnl)}</div>
                      <div className="text-xs text-slate-500 mt-0.5">{bestTrade.exit_date} · +{bestTrade.pnl_r?.toFixed(2)}R</div>
                    </div>
                  )}
                  {worstTrade && (
                    <div className="bg-red-950/20 border border-red-800/40 rounded-xl p-4">
                      <div className="flex items-center gap-2 mb-2">
                        <TrendingDown size={13} className="text-red-400" />
                        <span className="text-xs font-semibold text-red-400">Worst Trade</span>
                      </div>
                      <div className="text-white font-bold">{worstTrade.symbol}</div>
                      <div className="text-red-400 text-sm font-semibold">{formatCurrency(worstTrade.net_pnl)}</div>
                      <div className="text-xs text-slate-500 mt-0.5">{worstTrade.exit_date} · {worstTrade.pnl_r?.toFixed(2)}R</div>
                    </div>
                  )}
                </div>
              )}

              {/* Equity curve */}
              {equityCurve.length > 1 && (
                <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
                  <h2 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
                    <BarChart2 size={14} className="text-blue-400" /> Equity Curve
                  </h2>
                  <div className="flex items-end gap-0.5 h-32">
                    {equityCurve.map((pt: any, i: number) => {
                      const h    = maxEq > minEq ? ((pt.value - minEq) / (maxEq - minEq)) * 100 : 50
                      const prev = i > 0 ? equityCurve[i - 1] : pt
                      const isUp = pt.value >= (prev as any).value
                      return (
                        <div key={i} className="flex-1 flex flex-col justify-end h-full"
                          title={`${pt.date || ''}: ${formatCurrency(pt.value)}`}>
                          <div className={`w-full rounded-t ${isUp ? 'bg-emerald-500/70' : 'bg-red-500/70'}`}
                            style={{ height: `${Math.max(h, 1)}%` }} />
                        </div>
                      )
                    })}
                  </div>
                  <div className="flex justify-between text-[10px] text-slate-600 mt-1">
                    <span>{(equityCurve[0] as any)?.date || ''} · {formatCurrency((equityCurve[0] as any)?.value)}</span>
                    <span>{(equityCurve[equityCurve.length-1] as any)?.date || ''} · {formatCurrency((equityCurve[equityCurve.length-1] as any)?.value)}</span>
                  </div>
                </div>
              )}

              {/* Monthly returns */}
              {Object.keys(monthlyReturns).length > 0 && (
                <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
                  <h2 className="text-sm font-semibold text-white mb-4">Monthly Returns</h2>
                  <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
                    {Object.entries(monthlyReturns).map(([month, ret]) => (
                      <div key={month} className={`rounded-lg p-2.5 border text-center ${
                        (ret as number) >= 0 ? 'bg-emerald-950/20 border-emerald-800/40' : 'bg-red-950/20 border-red-800/40'
                      }`}>
                        <div className="text-[10px] text-slate-500">{month}</div>
                        <div className={`text-xs font-bold mt-0.5 ${pnlColor(ret as number)}`}>{formatPercent(ret as number)}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Trade log */}
              {tradeLog.length > 0 && (
                <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-[#1e2d45] flex items-center justify-between">
                    <h2 className="text-sm font-semibold text-white">Trade Log</h2>
                    <span className="text-xs text-slate-500">{tradeLog.length} trades shown</span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-slate-500 border-b border-[#1e2d45]">
                          {['Symbol','Strategy','Entry','Exit','Entry ₹','Exit ₹','Qty','Gross','Charges','Net P&L','R','Reason'].map(h => (
                            <th key={h} className="text-left px-3 py-2 font-medium whitespace-nowrap">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {tradeLog.map((t: any, i: number) => (
                          <tr key={i} className={`border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235] ${(t.net_pnl ?? 0) >= 0 ? 'bg-emerald-950/5' : 'bg-red-950/5'}`}>
                            <td className="px-3 py-2 font-semibold text-white">{t.symbol}</td>
                            <td className="px-3 py-2">
                              <span className="text-[9px] px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-400 border border-blue-800/40 uppercase">{t.strategy}</span>
                            </td>
                            <td className="px-3 py-2 text-slate-400">{(t.entry_time || '').slice(0,10)}</td>
                            <td className="px-3 py-2 text-slate-400">{(t.exit_time  || '').slice(0,10)}</td>
                            <td className="px-3 py-2">₹{(t.entry_price ?? 0).toFixed(2)}</td>
                            <td className="px-3 py-2">₹{(t.exit_price  ?? 0).toFixed(2)}</td>
                            <td className="px-3 py-2 text-slate-300">{t.quantity}</td>
                            <td className={`px-3 py-2 font-medium ${pnlColor(t.gross_pnl)}`}>{formatCurrency(t.gross_pnl)}</td>
                            <td className="px-3 py-2 text-slate-500">{formatCurrency(t.charges)}</td>
                            <td className={`px-3 py-2 font-semibold ${pnlColor(t.net_pnl)}`}>{formatCurrency(t.net_pnl)}</td>
                            <td className={`px-3 py-2 font-medium ${pnlColor(t.pnl_r)}`}>
                              {t.pnl_r != null ? `${(t.pnl_r ?? 0) >= 0 ? '+' : ''}${(t.pnl_r ?? 0).toFixed(2)}R` : '—'}
                            </td>
                            <td className="px-3 py-2 text-slate-400">{(t.exit_reason || '').replace(/_/g,' ')}</td>
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
