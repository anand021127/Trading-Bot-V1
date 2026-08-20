import { useCallback, useState, useMemo } from 'react'
import {
  RefreshCw, TrendingUp, TrendingDown, Moon, AlertTriangle,
  KeyRound, ShieldCheck, Activity, BarChart2, Layers
} from 'lucide-react'
import { usePolling } from '../hooks/usePolling'
import { useConnection } from '../context/ConnectionContext'
import { useNavigate } from 'react-router-dom'
import { formatCurrency, formatVolume } from '../utils/formatters'
import api from '../api/client'

const UNDERLYINGS = [
  { symbol: 'NIFTY50', label: 'NIFTY 50', defaultStep: 50 },
  { symbol: 'BANKNIFTY', label: 'BANK NIFTY', defaultStep: 100 },
  { symbol: 'FINNIFTY', label: 'FIN NIFTY', defaultStep: 50 },
  { symbol: 'MIDCPNIFTY', label: 'MIDCAP NIFTY', defaultStep: 25 },
  { symbol: 'SENSEX', label: 'SENSEX', defaultStep: 100 },
  { symbol: 'BANKEX', label: 'BANKEX', defaultStep: 100 },
]

export type OptionContract = {
  strike: number
  option_type: 'CE' | 'PE'
  ltp?: number | null
  close_price?: number | null
  bid_price?: number | null
  ask_price?: number | null
  oi?: number | null
  oi_change?: number | null
  volume?: number | null
  iv?: number | null
  delta?: number | null
  gamma?: number | null
  theta?: number | null
  vega?: number | null
}

export type StrikeBuildup = {
  strike: number
  call_buildup?: string
  put_buildup?: string
}

export type ChainResponse = {
  underlying: string
  expiry?: string | null
  spot?: number | null
  contracts: OptionContract[]
  summary?: {
    pcr?: number | null
    max_pain?: number | null
    atm_strike?: number | null
    support?: number | null
    resistance?: number | null
    total_call_oi?: number | null
    total_put_oi?: number | null
    underlying_trend?: string | null
    strike_buildups?: StrikeBuildup[]
  }
  status?: string
  message?: string
}

interface IndexQuote {
  symbol: string
  ltp: number
  open?: number
  high?: number
  low?: number
  close?: number
  volume?: number
  change?: number
  change_pct?: number
  timestamp?: string
}

const BUILDUP_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  LONG_BUILDUP: { bg: 'bg-emerald-950/40 border-emerald-800/40', text: 'text-emerald-400', label: 'Long Buildup' },
  SHORT_COVERING: { bg: 'bg-teal-950/40 border-teal-800/40', text: 'text-teal-300', label: 'Short Covering' },
  SHORT_BUILDUP: { bg: 'bg-red-950/40 border-red-800/40', text: 'text-red-400', label: 'Short Buildup' },
  LONG_UNWINDING: { bg: 'bg-amber-950/40 border-amber-800/40', text: 'text-amber-300', label: 'Long Unwinding' },
  NEUTRAL: { bg: 'bg-slate-800/30 border-slate-700/30', text: 'text-slate-500', label: 'Neutral' },
}

const formatNumber = (val?: number | null, decimals = 2) => {
  if (val == null || isNaN(val)) return '—'
  return val.toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

export default function OptionChain() {
  const [underlying, setUnderlying] = useState('NIFTY50')
  const [selectedExpiry, setSelectedExpiry] = useState<string>('')
  const [chainData, setChainData] = useState<ChainResponse | null>(null)
  const [indexQuotes, setIndexQuotes] = useState<Record<string, IndexQuote>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [flashMap, setFlashMap] = useState<Record<string, 'up' | 'down'>>({})

  const { status, isMarketOpen, isAuthExpired, prices: wsPrices, reportApiSuccess, reportApiFailure } = useConnection()
  const navigate = useNavigate()

  // Load index quotes for all underlyings
  const loadIndexQuotes = useCallback(async () => {
    try {
      const res = await api.get<{ prices: Record<string, IndexQuote> }>('/api/prices/underlyings')
      if (res.data?.prices) {
        setIndexQuotes(prev => {
          const flashes: Record<string, 'up' | 'down'> = {}
          Object.entries(res.data.prices).forEach(([sym, q]) => {
            if (prev[sym] && q.ltp !== prev[sym].ltp && q.ltp > 0) {
              flashes[sym] = q.ltp > prev[sym].ltp ? 'up' : 'down'
            }
          })
          if (Object.keys(flashes).length > 0) {
            setFlashMap(flashes)
            setTimeout(() => setFlashMap({}), 600)
          }
          return res.data.prices
        })
      }
    } catch {
      // Ignore underlying prices failure gracefully
    }
  }, [])

  // Load option chain data
  const loadChain = useCallback(async () => {
    try {
      const params: Record<string, string> = { underlying }
      if (selectedExpiry) params.expiry = selectedExpiry

      const response = await api.get<ChainResponse>('/api/options/chain', { params })
      setChainData(response.data)
      setError(null)
      reportApiSuccess()
    } catch (err: any) {
      reportApiFailure(err)
      if (err?.response?.status === 401 || err?.response?.data?.status === 'AUTH_EXPIRED') {
        setError('Upstox Access Token is expired or invalid.')
      } else {
        setError(err instanceof Error ? err.message : 'Unable to load option chain data')
      }
    } finally {
      setLoading(false)
    }
  }, [underlying, selectedExpiry, reportApiSuccess, reportApiFailure])

  const refreshAll = useCallback(() => {
    loadIndexQuotes()
    loadChain()
  }, [loadIndexQuotes, loadChain])

  usePolling(refreshAll, 5000)

  // Merge websocket prices with active quotes
  const currentSpot = useMemo(() => {
    const ws = wsPrices[underlying]?.ltp
    if (ws) return ws
    const quote = indexQuotes[underlying]?.ltp
    if (quote) return quote
    return chainData?.spot ?? null
  }, [wsPrices, indexQuotes, chainData, underlying])

  // Extract strikes & contracts
  const contracts = chainData?.contracts ?? []
  const callsByStrike = useMemo(() => {
    const map = new Map<number, OptionContract>()
    contracts.filter(c => c.option_type === 'CE').forEach(c => map.set(c.strike, c))
    return map
  }, [contracts])

  const putsByStrike = useMemo(() => {
    const map = new Map<number, OptionContract>()
    contracts.filter(c => c.option_type === 'PE').forEach(c => map.set(c.strike, c))
    return map
  }, [contracts])

  const allStrikes = useMemo(() => {
    const set = new Set<number>()
    contracts.forEach(c => set.add(c.strike))
    return Array.from(set).sort((a, b) => a - b)
  }, [contracts])

  const summary = chainData?.summary ?? {}
  const buildups = useMemo(() => {
    const map = new Map<number, StrikeBuildup>()
    summary.strike_buildups?.forEach(b => map.set(b.strike, b))
    return map
  }, [summary.strike_buildups])

  // Find ATM Strike
  const step = UNDERLYINGS.find(u => u.symbol === underlying)?.defaultStep ?? 50
  const atmStrike = currentSpot ? Math.round(currentSpot / step) * step : summary.atm_strike ?? null

  const pcr = summary.pcr
  const pcrSentiment = pcr != null ? (pcr >= 1.2 ? 'Bullish' : pcr <= 0.8 ? 'Bearish' : 'Neutral') : null

  return (
    <div className="space-y-5">
      {/* ─── Top Underlying Index Ribbon ─── */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
            <Layers size={13} className="text-blue-400" />
            Market Underlyings & Spot Quotes
          </div>
          <span className="text-[11px] text-slate-500">Click any index to inspect chain</span>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
          {UNDERLYINGS.map((idx) => {
            const quote = indexQuotes[idx.symbol]
            const ws = wsPrices[idx.symbol]
            const ltp = ws?.ltp ?? quote?.ltp ?? null
            const chgPct = ws?.change_pct ?? quote?.change_pct ?? 0
            const isSelected = underlying === idx.symbol
            const isUp = chgPct >= 0
            const flash = flashMap[idx.symbol]

            return (
              <button
                key={idx.symbol}
                onClick={() => {
                  setUnderlying(idx.symbol)
                  setSelectedExpiry('')
                }}
                className={`p-3 rounded-xl border text-left transition-all relative overflow-hidden ${
                  isSelected
                    ? 'bg-blue-950/40 border-blue-500/60 shadow-lg shadow-blue-950/30'
                    : 'bg-[#0d1424] border-[#1e2d45] hover:bg-[#141b2d] hover:border-slate-600/50'
                } ${
                  flash === 'up' ? 'ring-1 ring-emerald-400/60' : flash === 'down' ? 'ring-1 ring-red-400/60' : ''
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-semibold text-white truncate">{idx.label}</span>
                  {isSelected && (
                    <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" />
                  )}
                </div>

                <div className="flex items-baseline justify-between mt-1">
                  <span className="text-sm font-bold text-slate-100 tabular-nums">
                    {ltp ? formatCurrency(ltp) : '—'}
                  </span>
                </div>

                <div className="flex items-center gap-1 mt-1 text-[11px]">
                  {isUp ? (
                    <TrendingUp size={12} className="text-emerald-400" />
                  ) : (
                    <TrendingDown size={12} className="text-red-400" />
                  )}
                  <span className={`font-medium tabular-nums ${isUp ? 'text-emerald-400' : 'text-red-400'}`}>
                    {chgPct >= 0 ? `+${chgPct.toFixed(2)}%` : `${chgPct.toFixed(2)}%`}
                  </span>
                </div>
              </button>
            )
          })}
        </div>
      </div>

      {/* ─── State Alerts & Banners ─── */}
      {isAuthExpired && (
        <div className="bg-amber-950/40 border border-amber-600/50 rounded-xl p-4 flex items-start justify-between gap-3 text-amber-200 text-xs">
          <div className="flex items-start gap-2.5">
            <KeyRound size={16} className="text-amber-400 flex-shrink-0 mt-0.5" />
            <div>
              <div className="font-semibold text-amber-300">Upstox Access Token Expired (HTTP 401 / UDAPI100050)</div>
              <div className="text-amber-300/70 text-[11px] mt-0.5">
                Upstox access tokens expire daily at 3:30 AM IST. Re-authenticate to access real-time ticks.
              </div>
            </div>
          </div>
          <button
            onClick={() => navigate('/settings')}
            className="px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-slate-950 font-medium rounded-lg transition-colors flex-shrink-0"
          >
            Update Token
          </button>
        </div>
      )}

      {!isMarketOpen && (
        <div className="bg-slate-900/60 border border-slate-700/50 rounded-xl px-4 py-2.5 flex items-center justify-between text-xs text-slate-300">
          <div className="flex items-center gap-2">
            <Moon size={14} className="text-blue-400" />
            <span>
              <strong>NSE Market Closed</strong> — Market hours: Monday–Friday 9:15 AM to 3:30 PM IST. Displaying last settlement prices.
            </span>
          </div>
          <span className="text-[10px] text-slate-500 hidden sm:inline">Offline Settlement Mode</span>
        </div>
      )}

      {error && !isAuthExpired && (
        <div className="bg-red-950/30 border border-red-800/50 rounded-xl p-4 flex items-center justify-between text-xs text-red-300">
          <div className="flex items-center gap-2">
            <AlertTriangle size={14} className="text-red-400" />
            <span>{error}</span>
          </div>
          <button onClick={loadChain} className="underline text-red-300 hover:text-white">
            Retry
          </button>
        </div>
      )}

      {/* ─── Control Bar & Metric Summary Cards ─── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2.5">
        <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">Spot Price</div>
          <div className="text-base font-bold text-white mt-1 tabular-nums">
            {currentSpot ? formatCurrency(currentSpot) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">{underlying} Spot Index</div>
        </div>

        <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">ATM Strike</div>
          <div className="text-base font-bold text-blue-400 mt-1 tabular-nums">
            {atmStrike ? formatNumber(atmStrike, 0) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">At-The-Money</div>
        </div>

        <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">PCR (Put-Call)</div>
          <div className="flex items-baseline gap-1.5 mt-1">
            <span className="text-base font-bold text-white tabular-nums">{pcr != null ? pcr.toFixed(2) : '—'}</span>
            {pcrSentiment && (
              <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${
                pcrSentiment === 'Bullish' ? 'bg-emerald-950 text-emerald-400 border border-emerald-800/40' :
                pcrSentiment === 'Bearish' ? 'bg-red-950 text-red-400 border border-red-800/40' :
                'bg-slate-800 text-slate-400'
              }`}>
                {pcrSentiment}
              </span>
            )}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Total OI Ratio</div>
        </div>

        <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">Max Pain</div>
          <div className="text-base font-bold text-amber-400 mt-1 tabular-nums">
            {summary.max_pain ? formatNumber(summary.max_pain, 0) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Expiry Target</div>
        </div>

        <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">Support (Put OI)</div>
          <div className="text-base font-bold text-emerald-400 mt-1 tabular-nums">
            {summary.support ? formatNumber(summary.support, 0) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Peak Put Concentration</div>
        </div>

        <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">Resistance (Call OI)</div>
          <div className="text-base font-bold text-red-400 mt-1 tabular-nums">
            {summary.resistance ? formatNumber(summary.resistance, 0) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Peak Call Concentration</div>
        </div>
      </div>

      {/* ─── Main Option Chain Table ─── */}
      <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl overflow-hidden shadow-xl">
        <div className="p-4 border-b border-[#1e2d45] flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <BarChart2 size={16} className="text-blue-400" />
            <h2 className="text-sm font-bold text-white">
              {underlying} Option Strikes & Liquidity
            </h2>
            {chainData?.expiry && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-blue-950 text-blue-300 border border-blue-800/40">
                Expiry: {chainData.expiry}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={refreshAll}
              disabled={loading}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-[#141b2d] hover:bg-[#1a233b] border border-[#1e2d45] text-slate-300 text-xs font-medium rounded-lg transition-colors"
            >
              <RefreshCw size={12} className={loading ? 'animate-spin text-blue-400' : ''} />
              Refresh
            </button>
          </div>
        </div>

        {loading && !chainData ? (
          <div className="flex flex-col items-center justify-center py-20 text-slate-500 space-y-2">
            <RefreshCw size={24} className="animate-spin text-blue-400" />
            <span className="text-xs">Resolving option contracts & Greeks...</span>
          </div>
        ) : allStrikes.length === 0 ? (
          <div className="p-12 text-center text-slate-500 text-xs">
            No option contracts resolved for {underlying}. Ensure market hours or valid Upstox credentials.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-[#1e2d45] bg-[#0a0e1a]">
                  <th colSpan={7} className="px-3 py-2.5 text-center text-emerald-400 font-bold border-r border-[#1e2d45] bg-emerald-950/20">
                    CALL OPTIONS (CE)
                  </th>
                  <th className="px-4 py-2.5 text-center text-white font-bold bg-[#141b2d] border-r border-l border-[#1e2d45]">
                    STRIKE
                  </th>
                  <th colSpan={7} className="px-3 py-2.5 text-center text-red-400 font-bold border-l border-[#1e2d45] bg-red-950/20">
                    PUT OPTIONS (PE)
                  </th>
                </tr>
                <tr className="border-b border-[#1e2d45] bg-[#0a0e1a]/80 text-[10px] text-slate-400">
                  {/* CALLS */}
                  <th className="px-2.5 py-2 text-left">Buildup</th>
                  <th className="px-2.5 py-2 text-right">Delta</th>
                  <th className="px-2.5 py-2 text-right">IV %</th>
                  <th className="px-2.5 py-2 text-right">OI Chg</th>
                  <th className="px-2.5 py-2 text-right">OI (Qty)</th>
                  <th className="px-2.5 py-2 text-right">Volume</th>
                  <th className="px-2.5 py-2 text-right font-semibold text-emerald-400 border-r border-[#1e2d45]">LTP (₹)</th>

                  {/* STRIKE */}
                  <th className="px-4 py-2 text-center text-slate-200 font-bold bg-[#141b2d]">Price</th>

                  {/* PUTS */}
                  <th className="px-2.5 py-2 text-left font-semibold text-red-400 border-l border-[#1e2d45]">LTP (₹)</th>
                  <th className="px-2.5 py-2 text-right">Volume</th>
                  <th className="px-2.5 py-2 text-right">OI (Qty)</th>
                  <th className="px-2.5 py-2 text-right">OI Chg</th>
                  <th className="px-2.5 py-2 text-right">IV %</th>
                  <th className="px-2.5 py-2 text-right">Delta</th>
                  <th className="px-2.5 py-2 text-right">Buildup</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1e2d45]/60 font-mono">
                {allStrikes.map((strike) => {
                  const call = callsByStrike.get(strike)
                  const put = putsByStrike.get(strike)
                  const isAtm = atmStrike === strike
                  const b = buildups.get(strike)
                  const callBuildup = b?.call_buildup ?? 'NEUTRAL'
                  const putBuildup = b?.put_buildup ?? 'NEUTRAL'

                  const callStyle = BUILDUP_STYLES[callBuildup] || BUILDUP_STYLES.NEUTRAL
                  const putStyle = BUILDUP_STYLES[putBuildup] || BUILDUP_STYLES.NEUTRAL

                  const isItmCall = currentSpot ? strike < currentSpot : false
                  const isItmPut = currentSpot ? strike > currentSpot : false

                  return (
                    <tr
                      key={strike}
                      className={`hover:bg-[#141b2d]/70 transition-colors ${
                        isAtm ? 'bg-blue-950/30 ring-1 ring-blue-500/40 font-semibold' : ''
                      }`}
                    >
                      {/* CALL: Buildup */}
                      <td className={`px-2.5 py-2 text-left ${isItmCall ? 'bg-emerald-950/10' : ''}`}>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border ${callStyle.bg} ${callStyle.text}`}>
                          {callStyle.label}
                        </span>
                      </td>

                      {/* CALL: Delta */}
                      <td className={`px-2.5 py-2 text-right text-slate-300 tabular-nums ${isItmCall ? 'bg-emerald-950/10' : ''}`}>
                        {call?.delta != null ? call.delta.toFixed(2) : '—'}
                      </td>

                      {/* CALL: IV */}
                      <td className={`px-2.5 py-2 text-right text-slate-400 tabular-nums ${isItmCall ? 'bg-emerald-950/10' : ''}`}>
                        {call?.iv != null ? `${call.iv.toFixed(1)}%` : '—'}
                      </td>

                      {/* CALL: OI Change */}
                      <td className={`px-2.5 py-2 text-right tabular-nums ${isItmCall ? 'bg-emerald-950/10' : ''} ${
                        (call?.oi_change ?? 0) > 0 ? 'text-emerald-400' : (call?.oi_change ?? 0) < 0 ? 'text-red-400' : 'text-slate-500'
                      }`}>
                        {call?.oi_change != null ? (call.oi_change > 0 ? `+${call.oi_change.toLocaleString()}` : call.oi_change.toLocaleString()) : '—'}
                      </td>

                      {/* CALL: OI */}
                      <td className={`px-2.5 py-2 text-right text-slate-200 tabular-nums ${isItmCall ? 'bg-emerald-950/10' : ''}`}>
                        {call?.oi != null ? call.oi.toLocaleString() : '—'}
                      </td>

                      {/* CALL: Volume */}
                      <td className={`px-2.5 py-2 text-right text-slate-400 tabular-nums ${isItmCall ? 'bg-emerald-950/10' : ''}`}>
                        {call?.volume != null ? formatVolume(call.volume) : '—'}
                      </td>

                      {/* CALL: LTP */}
                      <td className={`px-2.5 py-2 text-right font-bold text-emerald-400 border-r border-[#1e2d45] tabular-nums ${
                        isItmCall ? 'bg-emerald-950/20' : ''
                      }`}>
                        {call?.ltp != null ? formatNumber(call.ltp) : '—'}
                      </td>

                      {/* CENTER: Strike Price */}
                      <td className={`px-4 py-2 text-center font-bold text-slate-100 bg-[#141b2d] border-r border-l border-[#1e2d45] ${
                        isAtm ? 'text-blue-400 bg-blue-950/50' : ''
                      }`}>
                        <div className="flex items-center justify-center gap-1.5">
                          <span>{strike}</span>
                          {isAtm && (
                            <span className="text-[9px] px-1 py-0.2 rounded bg-blue-500 text-white font-sans uppercase">
                              ATM
                            </span>
                          )}
                        </div>
                      </td>

                      {/* PUT: LTP */}
                      <td className={`px-2.5 py-2 text-left font-bold text-red-400 border-l border-[#1e2d45] tabular-nums ${
                        isItmPut ? 'bg-red-950/20' : ''
                      }`}>
                        {put?.ltp != null ? formatNumber(put.ltp) : '—'}
                      </td>

                      {/* PUT: Volume */}
                      <td className={`px-2.5 py-2 text-right text-slate-400 tabular-nums ${isItmPut ? 'bg-red-950/10' : ''}`}>
                        {put?.volume != null ? formatVolume(put.volume) : '—'}
                      </td>

                      {/* PUT: OI */}
                      <td className={`px-2.5 py-2 text-right text-slate-200 tabular-nums ${isItmPut ? 'bg-red-950/10' : ''}`}>
                        {put?.oi != null ? put.oi.toLocaleString() : '—'}
                      </td>

                      {/* PUT: OI Change */}
                      <td className={`px-2.5 py-2 text-right tabular-nums ${isItmPut ? 'bg-red-950/10' : ''} ${
                        (put?.oi_change ?? 0) > 0 ? 'text-emerald-400' : (put?.oi_change ?? 0) < 0 ? 'text-red-400' : 'text-slate-500'
                      }`}>
                        {put?.oi_change != null ? (put.oi_change > 0 ? `+${put.oi_change.toLocaleString()}` : put.oi_change.toLocaleString()) : '—'}
                      </td>

                      {/* PUT: IV */}
                      <td className={`px-2.5 py-2 text-right text-slate-400 tabular-nums ${isItmPut ? 'bg-red-950/10' : ''}`}>
                        {put?.iv != null ? `${put.iv.toFixed(1)}%` : '—'}
                      </td>

                      {/* PUT: Delta */}
                      <td className={`px-2.5 py-2 text-right text-slate-300 tabular-nums ${isItmPut ? 'bg-red-950/10' : ''}`}>
                        {put?.delta != null ? put.delta.toFixed(2) : '—'}
                      </td>

                      {/* PUT: Buildup */}
                      <td className={`px-2.5 py-2 text-right ${isItmPut ? 'bg-red-950/10' : ''}`}>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border ${putStyle.bg} ${putStyle.text}`}>
                          {putStyle.label}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
