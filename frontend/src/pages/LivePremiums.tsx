import { useState, useCallback } from 'react'
import { RefreshCw, TrendingUp, TrendingDown, Search, MoonStar, Wifi } from 'lucide-react'
import { useWebSocket } from '../hooks/useWebSocket'
import { usePolling } from '../hooks/usePolling'
import { formatVolume, pnlColor } from '../utils/formatters'
import api from '../api/client'

const NIFTY50 = [
  'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','KOTAKBANK','LT','SBIN','AXISBANK',
  'BHARTIARTL','ITC','ASIANPAINT','MARUTI','HCLTECH','SUNPHARMA','WIPRO','TITAN','ULTRACEMCO','BAJFINANCE',
  'NESTLEIND','TECHM','NTPC','POWERGRID','ONGC','JSWSTEEL','TATASTEEL','HINDALCO','TATAMOTORS','M&M',
  'BAJAJFINSV','DRREDDY','CIPLA','DIVISLAB','APOLLOHOSP','ADANIENT','ADANIPORTS','COALINDIA','BPCL','EICHERMOT',
  'HEROMOTOCO','INDUSINDBK','SBILIFE','HDFCLIFE','GRASIM','TATACONSUM','UPL','BRITANNIA','SHREECEM','BAJAJ-AUTO',
]

interface Quote {
  symbol: string
  ltp: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  change: number
  change_pct: number
  timestamp: string
  market_closed?: boolean
}

interface PriceResponse {
  prices: Record<string, Quote>
  market_open: boolean
  timestamp: string
  token_present: boolean
}

export default function LivePremiums() {
  const [quotes, setQuotes]     = useState<Record<string, Quote>>({})
  const [marketOpen, setMarketOpen] = useState<boolean | null>(null)
  const [tokenPresent, setTokenPresent] = useState(true)
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState<string | null>(null)
  const [search, setSearch]     = useState('')
  const [flashMap, setFlashMap] = useState<Record<string, 'up' | 'down'>>({})
  const { prices: wsQuotes, status } = useWebSocket()

  const load = useCallback(async () => {
    try {
      const res = await api.get<PriceResponse>('/api/prices/nifty50')
      const data = res.data
      // Handle both shapes: {prices: {...}} and flat {SYMBOL: {...}}
      const priceMap: Record<string, Quote> =
        data.prices && typeof data.prices === 'object' && !Array.isArray(data.prices)
          ? data.prices as Record<string, Quote>
          : data as unknown as Record<string, Quote>

      setMarketOpen(data.market_open ?? null)
      setTokenPresent(data.token_present ?? true)

      setQuotes(prev => {
        const flashes: Record<string, 'up' | 'down'> = {}
        Object.entries(priceMap).forEach(([sym, q]) => {
          if (prev[sym] && q.ltp !== prev[sym].ltp && q.ltp > 0) {
            flashes[sym] = q.ltp > prev[sym].ltp ? 'up' : 'down'
          }
        })
        if (Object.keys(flashes).length) {
          setFlashMap(flashes)
          setTimeout(() => setFlashMap({}), 600)
        }
        return priceMap
      })
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load prices')
    } finally {
      setLoading(false)
    }
  }, [])

  usePolling(load, 6000)

  // Merge with WebSocket prices
  const merged: Record<string, Partial<Quote>> = { ...quotes }
  Object.entries(wsQuotes).forEach(([sym, q]) => {
    if (merged[sym]) {
      merged[sym] = { ...merged[sym], ltp: q.ltp, change_pct: q.change_pct, volume: q.volume }
    }
  })

  const filtered = NIFTY50.filter(s => !search || s.includes(search.toUpperCase()))
  const connected = status === 'connected'

  // Only show gainers/losers when market is open and we have real prices
  const hasRealPrices = Object.values(merged).some(q => (q.ltp ?? 0) > 0)

  const sortedByChange = Object.entries(merged)
    .filter(([, q]) => (q.ltp ?? 0) > 0 && q.change_pct != null)
    .sort((a, b) => (b[1].change_pct ?? 0) - (a[1].change_pct ?? 0))

  const topGainers = sortedByChange.slice(0, 3)
  const topLosers  = sortedByChange.slice(-3).reverse()

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Live Premiums</h1>
          <p className="text-xs text-slate-500 mt-0.5">NIFTY50 stock prices · Updates every 6 seconds</p>
        </div>
        <div className={`flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-full border ${
          connected ? 'bg-emerald-950/40 text-emerald-400 border-emerald-800/40' : 'bg-slate-800/40 text-slate-400 border-slate-700/40'
        }`}>
          <Wifi size={11} />
          {connected ? 'Live feed active' : 'Polling mode'}
        </div>
      </div>

      {/* Market closed banner */}
      {marketOpen === false && (
        <div className="flex items-center gap-3 bg-slate-800/40 border border-slate-700/50 rounded-xl px-4 py-3">
          <MoonStar size={16} className="text-slate-400 flex-shrink-0" />
          <div>
            <div className="text-sm font-medium text-slate-300">NSE Market is Closed</div>
            <div className="text-xs text-slate-500 mt-0.5">
              Trading hours: Monday–Friday, 9:15 AM – 3:30 PM IST.
              {' '}Prices shown are last available values (LTP may be 0 or previous close).
            </div>
          </div>
        </div>
      )}

      {/* No token warning */}
      {!tokenPresent && (
        <div className="flex items-center gap-3 bg-amber-950/30 border border-amber-800/50 rounded-xl px-4 py-3 text-xs text-amber-300">
          ⚠️ Upstox token not set. Go to <strong>Settings → Generate Token</strong> to see live prices.
        </div>
      )}

      {/* Error */}
      {error && !loading && (
        <div className="bg-red-950/30 border border-red-800/50 rounded-xl px-4 py-3 text-xs text-red-300">
          Failed to load prices: {error}
        </div>
      )}

      {/* Top Gainers / Losers — only when real data exists */}
      {hasRealPrices && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
            <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-3 flex items-center gap-1.5">
              <TrendingUp size={11} className="text-emerald-400" /> Top Gainers
            </div>
            <div className="space-y-2">
              {topGainers.length === 0
                ? <div className="text-xs text-slate-600">No data yet</div>
                : topGainers.map(([sym, q]) => (
                  <div key={sym} className="flex items-center justify-between text-xs">
                    <span className="font-semibold text-white">{sym}</span>
                    <div className="flex items-center gap-2">
                      <span className="text-slate-300">₹{(q.ltp ?? 0).toFixed(2)}</span>
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
              {topLosers.length === 0
                ? <div className="text-xs text-slate-600">No data yet</div>
                : topLosers.map(([sym, q]) => (
                  <div key={sym} className="flex items-center justify-between text-xs">
                    <span className="font-semibold text-white">{sym}</span>
                    <div className="flex items-center gap-2">
                      <span className="text-slate-300">₹{(q.ltp ?? 0).toFixed(2)}</span>
                      <span className="text-red-400 font-medium">{(q.change_pct ?? 0).toFixed(2)}%</span>
                    </div>
                  </div>
                ))
              }
            </div>
          </div>
        </div>
      )}

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
            const q    = merged[sym]
            const ltp  = q?.ltp ?? 0
            const pct  = q?.change_pct ?? 0
            const vol  = q?.volume
            const flash = flashMap[sym]
            const isUp  = pct >= 0
            const hasData = ltp > 0

            return (
              <div
                key={sym}
                className={`bg-[#141b2d] border rounded-xl p-3 transition-all duration-300 ${
                  flash === 'up'   ? 'border-emerald-500/60 bg-emerald-950/20' :
                  flash === 'down' ? 'border-red-500/60 bg-red-950/20' :
                  'border-[#1e2d45] hover:border-[#243044]'
                }`}
              >
                <div className="flex items-start justify-between mb-1.5">
                  <span className="text-[11px] font-semibold text-white leading-tight">{sym}</span>
                  {hasData && (
                    <span className={`text-[9px] font-medium px-1 py-0.5 rounded ${isUp ? 'bg-emerald-950/50 text-emerald-400' : 'bg-red-950/50 text-red-400'}`}>
                      {isUp ? '▲' : '▼'}
                    </span>
                  )}
                </div>
                <div className={`text-sm font-bold ${hasData ? pnlColor(pct) : 'text-slate-600'}`}>
                  {hasData ? `₹${ltp.toFixed(2)}` : marketOpen === false ? 'Closed' : '—'}
                </div>
                <div className={`text-[10px] font-medium mt-0.5 ${hasData ? pnlColor(pct) : 'text-slate-600'}`}>
                  {hasData ? `${isUp ? '+' : ''}${pct.toFixed(2)}%` : '—'}
                </div>
                {vol != null && vol > 0 && (
                  <div className="text-[9px] text-slate-600 mt-1">{formatVolume(vol)}</div>
                )}
                {!hasData && marketOpen === false && (
                  <div className="text-[9px] text-slate-700 mt-1">Market closed</div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
