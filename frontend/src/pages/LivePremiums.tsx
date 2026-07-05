import { useState, useCallback } from 'react'
import { RefreshCw, TrendingUp, TrendingDown, Search } from 'lucide-react'
import { fetchNifty50Prices } from '../api/endpoints'
import { useWebSocket } from '../hooks/useWebSocket'
import { usePolling } from '../hooks/usePolling'
import { formatVolume, pnlColor } from '../utils/formatters'
import type { LiveQuote } from '../types'

const NIFTY50 = [
  'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','KOTAKBANK','LT','SBIN','AXISBANK',
  'BHARTIARTL','ITC','ASIANPAINT','MARUTI','HCLTECH','SUNPHARMA','WIPRO','TITAN','ULTRACEMCO','BAJFINANCE',
  'NESTLEIND','TECHM','NTPC','POWERGRID','ONGC','JSWSTEEL','TATASTEEL','HINDALCO','TATAMOTORS','M&M',
  'BAJAJFINSV','DRREDDY','CIPLA','DIVISLAB','APOLLOHOSP','ADANIENT','ADANIPORTS','COALINDIA','BPCL','EICHERMOT',
  'HEROMOTOCO','INDUSINDBK','SBILIFE','HDFCLIFE','GRASIM','TATACONSUM','UPL','BRITANNIA','SHREECEM','BAJAJ-AUTO',
]

export default function LivePremiums() {
  const [quotes, setQuotes] = useState<Record<string, LiveQuote>>({})
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [flashMap, setFlashMap] = useState<Record<string, 'up' | 'down'>>({})
  const { prices: wsQuotes, status } = useWebSocket()

  const load = useCallback(async () => {
    try {
      const data = await fetchNifty50Prices()
      setQuotes(prev => {
        const flashes: Record<string, 'up' | 'down'> = {}
        Object.entries(data).forEach(([sym, q]) => {
          if (prev[sym] && q.ltp !== prev[sym].ltp) {
            flashes[sym] = q.ltp > prev[sym].ltp ? 'up' : 'down'
          }
        })
        if (Object.keys(flashes).length) {
          setFlashMap(flashes)
          setTimeout(() => setFlashMap({}), 600)
        }
        return data
      })
    } catch { /* use WS data */ }
    finally { setLoading(false) }
  }, [])

  usePolling(load, 5000)

  // Merge REST data with WebSocket prices
  const merged: Record<string, Partial<LiveQuote>> = { ...quotes }
  Object.entries(wsQuotes).forEach(([sym, q]) => {
    if (merged[sym]) {
      merged[sym] = { ...merged[sym], ltp: q.ltp, change_pct: q.change_pct, volume: q.volume }
    }
  })

  const filtered = NIFTY50.filter(s => !search || s.includes(search.toUpperCase()))
  const connected = status === 'connected'

  const topGainers = Object.entries(merged)
    .filter(([, q]) => q.change_pct != null)
    .sort((a, b) => (b[1].change_pct ?? 0) - (a[1].change_pct ?? 0))
    .slice(0, 3)

  const topLosers = Object.entries(merged)
    .filter(([, q]) => q.change_pct != null)
    .sort((a, b) => (a[1].change_pct ?? 0) - (b[1].change_pct ?? 0))
    .slice(0, 3)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Live Premiums</h1>
          <p className="text-xs text-slate-500 mt-0.5">Real-time NIFTY50 stock prices · Updates every 5 seconds</p>
        </div>
        <div className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full ${connected ? 'bg-emerald-950/50 text-emerald-400' : 'bg-slate-800/50 text-slate-400'}`}>
          <div className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400 animate-pulse' : 'bg-slate-400'}`} />
          {connected ? 'Live feed active' : 'Polling'}
        </div>
      </div>

      {/* Market Pulse */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-3 flex items-center gap-1.5">
            <TrendingUp size={11} className="text-emerald-400" /> Top Gainers
          </div>
          <div className="space-y-2">
            {topGainers.length === 0 ? <div className="text-xs text-slate-600">Loading...</div> :
              topGainers.map(([sym, q]) => (
                <div key={sym} className="flex items-center justify-between text-xs">
                  <span className="font-semibold text-white">{sym}</span>
                  <div className="flex items-center gap-2">
                    <span className="text-slate-300">₹{q.ltp?.toFixed(2)}</span>
                    <span className="text-emerald-400 font-medium">+{(q.change_pct ?? 0).toFixed(2)}%</span>
                  </div>
                </div>
              ))
            }
          </div>
        </div>
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-3 flex items-center gap-1.5">
            <TrendingDown size={11} className="text-red-400" /> Top Losers
          </div>
          <div className="space-y-2">
            {topLosers.length === 0 ? <div className="text-xs text-slate-600">Loading...</div> :
              topLosers.map(([sym, q]) => (
                <div key={sym} className="flex items-center justify-between text-xs">
                  <span className="font-semibold text-white">{sym}</span>
                  <div className="flex items-center gap-2">
                    <span className="text-slate-300">₹{q.ltp?.toFixed(2)}</span>
                    <span className="text-red-400 font-medium">{(q.change_pct ?? 0).toFixed(2)}%</span>
                  </div>
                </div>
              ))
            }
          </div>
        </div>
      </div>

      {/* Search */}
      <div className="relative">
        <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full bg-[#141b2d] border border-[#1e2d45] rounded-xl pl-9 pr-4 py-2.5 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50"
          placeholder="Search NIFTY50 stocks..."
        />
      </div>

      {/* Price Grid */}
      {loading ? (
        <div className="flex items-center justify-center gap-2 py-10 text-slate-500 text-sm">
          <RefreshCw size={15} className="animate-spin" /> Loading prices...
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2.5">
          {filtered.map(sym => {
            const q = merged[sym]
            const ltp = q?.ltp
            const pct = q?.change_pct ?? 0
            const vol = q?.volume
            const flash = flashMap[sym]
            const isUp = pct >= 0
            return (
              <div
                key={sym}
                className={`bg-[#141b2d] border rounded-xl p-3 transition-all duration-300 ${
                  flash === 'up' ? 'border-emerald-500/60 bg-emerald-950/20' :
                  flash === 'down' ? 'border-red-500/60 bg-red-950/20' :
                  'border-[#1e2d45] hover:border-[#243044]'
                }`}
              >
                <div className="flex items-start justify-between mb-1.5">
                  <span className="text-[11px] font-semibold text-white leading-tight">{sym}</span>
                  <span className={`text-[9px] font-medium px-1 py-0.5 rounded ${isUp ? 'bg-emerald-950/50 text-emerald-400' : 'bg-red-950/50 text-red-400'}`}>
                    {isUp ? '▲' : '▼'}
                  </span>
                </div>
                <div className={`text-sm font-bold ${ltp ? pnlColor(pct) : 'text-slate-600'}`}>
                  {ltp ? `₹${ltp.toFixed(2)}` : '—'}
                </div>
                <div className={`text-[10px] font-medium mt-0.5 ${pnlColor(pct)}`}>
                  {ltp ? `${isUp ? '+' : ''}${pct.toFixed(2)}%` : '—'}
                </div>
                {vol != null && (
                  <div className="text-[9px] text-slate-600 mt-1">{formatVolume(vol)}</div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
