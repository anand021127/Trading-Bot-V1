import { useState, useCallback } from 'react'
import { Download, Filter, ChevronDown, ChevronUp, Search } from 'lucide-react'
import { fetchTrades, exportTradesCsv } from '../api/endpoints'
import { usePolling } from '../hooks/usePolling'
import StatusBadge from '../components/StatusBadge'
import {
  formatCurrency, formatDate, formatTime, formatDuration,
  formatR, pnlColor, pnlBg,
} from '../utils/formatters'
import type { Trade } from '../types'

const EXIT_REASONS = ['All', 'TRAILING_STOP_HIT', 'RSI_MOMENTUM_COLLAPSE', 'EMA20_CLOSE_BELOW', 'BEARISH_ENGULFING', 'TIME_FORCE_EXIT', 'DAILY_LOSS_LIMIT', 'MANUAL_EXIT']
const MODES = ['All', 'paper', 'live']

export default function TradeHistory() {
  const [trades, setTrades] = useState<Trade[]>([])
  const [summary, setSummary] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [totalCount, setTotalCount] = useState(0)

  // Filters
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [symbol, setSymbol] = useState('')
  const [mode, setMode] = useState('All')
  const [exitReason, setExitReason] = useState('All')

  const PAGE_SIZE = 20

  const load = useCallback(async () => {
    try {
      const res = await fetchTrades({
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        symbol: symbol || undefined,
        mode: mode === 'All' ? undefined : mode,
        exit_reason: exitReason === 'All' ? undefined : exitReason,
        page,
        page_size: PAGE_SIZE,
      })
      const list = res.trades ?? (Array.isArray(res) ? res as Trade[] : [])
      setTrades(list)
      setTotalCount(res.total_count ?? list.length)
      setSummary(res.summary ?? {})
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load trades')
    } finally {
      setLoading(false)
    }
  }, [dateFrom, dateTo, symbol, mode, exitReason, page])

  usePolling(load, 30000)

  const handleExport = async () => {
    try {
      const blob = await exportTradesCsv()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `trades_${new Date().toISOString().slice(0, 10)}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch { /* ignore */ }
  }

  const totalPages = Math.ceil(totalCount / PAGE_SIZE)

  const summaryCards = [
    { label: 'Total Trades', value: String(summary.total_trades ?? trades.length) },
    { label: 'Net P&L', value: formatCurrency(summary.total_net_pnl ?? 0), color: pnlColor(summary.total_net_pnl) },
    { label: 'Win Rate', value: summary.win_rate != null ? `${summary.win_rate.toFixed(1)}%` : '—' },
    { label: 'Profit Factor', value: summary.profit_factor != null ? summary.profit_factor.toFixed(2) : '—' },
  ]

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Trade History</h1>
          <p className="text-xs text-slate-500 mt-0.5">Complete log of all executed trades with indicators</p>
        </div>
        <button
          onClick={handleExport}
          className="flex items-center gap-1.5 px-3 py-2 bg-[#141b2d] border border-[#1e2d45] rounded-lg text-xs text-slate-300 hover:text-white hover:border-[#243044] transition-colors"
        >
          <Download size={13} /> Export CSV
        </button>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {summaryCards.map(c => (
          <div key={c.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-3">
            <div className="text-[10px] text-slate-500 uppercase tracking-widest">{c.label}</div>
            <div className={`text-lg font-bold mt-1 ${c.color ?? 'text-white'}`}>{c.value}</div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3 text-xs text-slate-400 font-medium">
          <Filter size={12} /> Filters
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <input
            type="date" value={dateFrom}
            onChange={e => { setDateFrom(e.target.value); setPage(1) }}
            className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-blue-600/50"
            placeholder="From date"
          />
          <input
            type="date" value={dateTo}
            onChange={e => { setDateTo(e.target.value); setPage(1) }}
            className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-blue-600/50"
          />
          <div className="relative">
            <Search size={12} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              value={symbol}
              onChange={e => { setSymbol(e.target.value.toUpperCase()); setPage(1) }}
              className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg pl-8 pr-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-blue-600/50"
              placeholder="Symbol"
            />
          </div>
          <select
            value={mode}
            onChange={e => { setMode(e.target.value); setPage(1) }}
            className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-blue-600/50"
          >
            {MODES.map(m => <option key={m} value={m}>{m === 'All' ? 'All Modes' : m}</option>)}
          </select>
          <select
            value={exitReason}
            onChange={e => { setExitReason(e.target.value); setPage(1) }}
            className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-blue-600/50"
          >
            {EXIT_REASONS.map(r => <option key={r} value={r}>{r === 'All' ? 'All Exit Reasons' : r.replace(/_/g, ' ')}</option>)}
          </select>
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-10 text-center text-slate-500 text-sm">Loading trades...</div>
      ) : error ? (
        <div className="bg-red-950/30 border border-red-800/50 rounded-xl p-6 text-red-300 text-sm">{error}</div>
      ) : trades.length === 0 ? (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-10 text-center text-slate-500 text-sm">
          No trades found. Start paper trading to see history here.
        </div>
      ) : (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-[#1e2d45] bg-[#0f1628]/50">
                  {['#','Date','Symbol','Mode','Entry Time','Entry Price','Exit Time','Exit Price',
                    'Qty','Duration','Init SL','Final SL','Stage','ORB H','ORB L',
                    'ATR','RSI','CI','Vol Ratio','EMA20','EMA50','Trend',
                    'Exit Reason','Gross P&L','Charges','Net P&L','P&L (R)','MFE','MAE',''].map(h => (
                    <th key={h} className="text-left px-2.5 py-2 font-medium whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.map((t, idx) => {
                  const id = t.trade_id ?? t.id
                  const isExpanded = expandedId === id
                  const netPnl = t.net_pnl ?? t.pnl
                  const charges = (t.brokerage ?? 0) + (t.stt ?? 0)
                  return (
                    <>
                      <tr
                        key={id}
                        className={`border-b border-[#1e2d45] last:border-0 hover:bg-[#1a2235] cursor-pointer transition-colors ${pnlBg(netPnl)}`}
                        onClick={() => setExpandedId(isExpanded ? null : id)}
                      >
                        <td className="px-2.5 py-2 text-slate-500">{(page - 1) * PAGE_SIZE + idx + 1}</td>
                        <td className="px-2.5 py-2 text-slate-400 whitespace-nowrap">{formatDate(t.entry_time ?? t.timestamp)}</td>
                        <td className="px-2.5 py-2 font-semibold text-white">{t.symbol}</td>
                        <td className="px-2.5 py-2"><StatusBadge status={(t.mode ?? 'paper').toUpperCase()} /></td>
                        <td className="px-2.5 py-2 text-slate-400 whitespace-nowrap">{formatTime(t.entry_time ?? t.timestamp)}</td>
                        <td className="px-2.5 py-2 text-white">₹{(t.entry_price ?? t.price ?? 0).toFixed(2)}</td>
                        <td className="px-2.5 py-2 text-slate-400 whitespace-nowrap">{formatTime(t.exit_time)}</td>
                        <td className="px-2.5 py-2">{t.exit_price ? `₹${t.exit_price.toFixed(2)}` : '—'}</td>
                        <td className="px-2.5 py-2 text-slate-300">{t.quantity}</td>
                        <td className="px-2.5 py-2 text-slate-400 whitespace-nowrap">{formatDuration(t.trade_duration_min)}</td>
                        <td className="px-2.5 py-2 text-red-400">{t.initial_stop ? `₹${t.initial_stop.toFixed(2)}` : '—'}</td>
                        <td className="px-2.5 py-2 text-amber-400">{t.final_stop ? `₹${t.final_stop.toFixed(2)}` : '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.stage_at_exit ?? '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.orb_high ? `₹${t.orb_high.toFixed(0)}` : '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.orb_low ? `₹${t.orb_low.toFixed(0)}` : '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.atr_at_entry?.toFixed(2) ?? '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.rsi_at_entry?.toFixed(1) ?? '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.choppiness_at_entry?.toFixed(1) ?? '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.volume_ratio?.toFixed(2) ?? '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.ema20_at_entry?.toFixed(2) ?? '—'}</td>
                        <td className="px-2.5 py-2 text-slate-400">{t.ema50_at_entry?.toFixed(2) ?? '—'}</td>
                        <td className="px-2.5 py-2"><StatusBadge status={t.trend_bias ?? 'NEUTRAL'} /></td>
                        <td className="px-2.5 py-2 text-slate-400 whitespace-nowrap">{(t.exit_reason ?? '—').replace(/_/g, ' ')}</td>
                        <td className={`px-2.5 py-2 font-medium ${pnlColor(t.gross_pnl)}`}>{formatCurrency(t.gross_pnl)}</td>
                        <td className="px-2.5 py-2 text-slate-500">{charges > 0 ? formatCurrency(charges) : '—'}</td>
                        <td className={`px-2.5 py-2 font-semibold ${pnlColor(netPnl)}`}>{formatCurrency(netPnl)}</td>
                        <td className={`px-2.5 py-2 font-medium ${pnlColor(t.pnl_r)}`}>{formatR(t.pnl_r)}</td>
                        <td className="px-2.5 py-2 text-emerald-400/70">{t.max_favorable ? `₹${t.max_favorable.toFixed(2)}` : '—'}</td>
                        <td className="px-2.5 py-2 text-red-400/70">{t.max_adverse ? `₹${t.max_adverse.toFixed(2)}` : '—'}</td>
                        <td className="px-2.5 py-2 text-slate-500">
                          {isExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr key={`${id}-detail`} className="border-b border-[#1e2d45] bg-[#0f1628]/50">
                          <td colSpan={30} className="px-4 py-4">
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                              <div>
                                <div className="text-slate-500 font-medium uppercase tracking-widest mb-2">Entry Conditions</div>
                                {t.conditions_checked ? (
                                  <div className="space-y-1">
                                    {Object.entries(t.conditions_checked).map(([k, v]) => (
                                      <div key={k} className="flex items-center gap-2">
                                        <span className={v ? 'text-emerald-400' : 'text-red-400'}>{v ? '✓' : '✗'}</span>
                                        <span className="text-slate-400">{k.replace(/_/g, ' ')}</span>
                                      </div>
                                    ))}
                                  </div>
                                ) : <span className="text-slate-600">No condition data</span>}
                              </div>
                              <div>
                                <div className="text-slate-500 font-medium uppercase tracking-widest mb-2">Trade Details</div>
                                <div className="space-y-1 text-slate-400">
                                  <div className="flex justify-between"><span>Trade ID</span><span className="font-mono text-slate-300 text-[10px]">{id?.slice(0, 16)}...</span></div>
                                  <div className="flex justify-between"><span>ORB Range</span><span className="text-slate-300">{t.orb_high && t.orb_low ? `₹${t.orb_low.toFixed(2)} – ₹${t.orb_high.toFixed(2)}` : '—'}</span></div>
                                  <div className="flex justify-between"><span>ATR at entry</span><span className="text-slate-300">{t.atr_at_entry?.toFixed(2) ?? '—'}</span></div>
                                  <div className="flex justify-between"><span>RSI at entry</span><span className="text-slate-300">{t.rsi_at_entry?.toFixed(1) ?? '—'}</span></div>
                                  <div className="flex justify-between"><span>Choppiness</span><span className={`${(t.choppiness_at_entry ?? 0) > 61.8 ? 'text-red-400' : 'text-emerald-400'}`}>{t.choppiness_at_entry?.toFixed(1) ?? '—'}</span></div>
                                  <div className="flex justify-between"><span>Volume Ratio</span><span className="text-slate-300">{t.volume_ratio?.toFixed(2) ?? '—'}×</span></div>
                                </div>
                              </div>
                              <div>
                                <div className="text-slate-500 font-medium uppercase tracking-widest mb-2">Exit Summary</div>
                                <div className="space-y-1 text-slate-400">
                                  <div className="flex justify-between"><span>Exit Reason</span><span className="text-slate-300">{(t.exit_reason ?? '—').replace(/_/g, ' ')}</span></div>
                                  <div className="flex justify-between"><span>Stage at Exit</span><span className="text-slate-300">Stage {t.stage_at_exit ?? '—'}</span></div>
                                  <div className="flex justify-between"><span>Max Gain (MFE)</span><span className="text-emerald-400">{t.max_favorable ? `₹${t.max_favorable.toFixed(2)}` : '—'}</span></div>
                                  <div className="flex justify-between"><span>Max Loss (MAE)</span><span className="text-red-400">{t.max_adverse ? `₹${t.max_adverse.toFixed(2)}` : '—'}</span></div>
                                  <div className="flex justify-between"><span>Net P&L</span><span className={`font-semibold ${pnlColor(netPnl)}`}>{formatCurrency(netPnl)}</span></div>
                                  <div className="flex justify-between"><span>P&L in R</span><span className={`font-semibold ${pnlColor(t.pnl_r)}`}>{formatR(t.pnl_r)}</span></div>
                                </div>
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </>
                  )
                })}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t border-[#1e2d45]">
              <span className="text-xs text-slate-500">{totalCount} total trades · Page {page} of {totalPages}</span>
              <div className="flex gap-2">
                <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}
                  className="px-3 py-1 text-xs bg-[#0f1628] border border-[#1e2d45] rounded-lg text-slate-300 disabled:opacity-40 hover:border-[#243044]">
                  Previous
                </button>
                <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page === totalPages}
                  className="px-3 py-1 text-xs bg-[#0f1628] border border-[#1e2d45] rounded-lg text-slate-300 disabled:opacity-40 hover:border-[#243044]">
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
