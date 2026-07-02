import { useEffect, useMemo, useState } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { formatCurrency } from '../utils/formatters'

type PremiumQuote = {
  symbol: string
  premium: number
  change: number
  bid: number
  ask: number
}

const initialQuotes: PremiumQuote[] = [
  { symbol: 'NIFTY', premium: 185.5, change: 0.18, bid: 185.2, ask: 185.8 },
  { symbol: 'BANKNIFTY', premium: 312.8, change: 0.22, bid: 312.3, ask: 313.1 },
  { symbol: 'RELIANCE', premium: 142.3, change: -0.12, bid: 142.1, ask: 142.5 },
  { symbol: 'TCS', premium: 23.7, change: 0.04, bid: 23.6, ask: 23.8 },
]

export default function LivePremiums() {
  const { status, message, websocketUrl } = useWebSocket()
  const [quotes, setQuotes] = useState<PremiumQuote[]>(initialQuotes)

  useEffect(() => {
    const interval = window.setInterval(() => {
      setQuotes((current) =>
        current.map((item) => {
          const movement = (Math.random() - 0.5) * 0.3
          return {
            ...item,
            premium: Number((item.premium + movement).toFixed(2)),
            change: Number((movement * 0.6).toFixed(2)),
            bid: Number((item.bid + movement / 2).toFixed(2)),
            ask: Number((item.ask - movement / 2).toFixed(2)),
          }
        })
      )
    }, 4000)

    return () => window.clearInterval(interval)
  }, [])

  const statusLabel = useMemo(() => {
    return status === 'connected' ? 'Live feed active' : status === 'connecting' ? 'Connecting...' : 'Disconnected'
  }, [status])

  return (
    <div className="space-y-8">
      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">Live premiums</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">Options premium insight</h2>
          </div>
          <div className="rounded-3xl bg-white/5 px-4 py-3 text-sm text-white/80">{statusLabel}</div>
        </div>
        <div className="mt-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {quotes.map((quote) => (
            <div key={quote.symbol} className="rounded-3xl bg-white/5 p-5">
              <div className="text-sm uppercase tracking-[0.22em] text-white/50">{quote.symbol}</div>
              <div className="mt-3 flex items-end gap-2">
                <span className="text-3xl font-semibold text-white">{formatCurrency(quote.premium)}</span>
                <span className={`rounded-full px-3 py-1 text-xs ${quote.change >= 0 ? 'bg-emerald-500/15 text-emerald-300' : 'bg-rose-500/15 text-rose-300'}`}>
                  {quote.change >= 0 ? '+' : ''}{quote.change}%
                </span>
              </div>
              <div className="mt-4 grid gap-2 text-sm text-white/70">
                <div className="flex justify-between"><span>Bid</span><span>{formatCurrency(quote.bid)}</span></div>
                <div className="flex justify-between"><span>Ask</span><span>{formatCurrency(quote.ask)}</span></div>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm uppercase tracking-[0.18em] text-white/50">Market pulse</p>
            <h3 className="mt-2 text-2xl font-semibold text-white">Realtime statistics</h3>
          </div>
          <div className="rounded-3xl bg-white/5 px-4 py-3 text-sm text-white/80">WebSocket URL: {websocketUrl}</div>
        </div>
        <div className="mt-6 grid gap-4 sm:grid-cols-3">
          <div className="rounded-3xl bg-white/5 p-5 text-sm text-white/70">
            <div className="font-semibold text-white">Spread focus</div>
            <p className="mt-3">Capture premium moves within intraday opening range filters.</p>
          </div>
          <div className="rounded-3xl bg-white/5 p-5 text-sm text-white/70">
            <div className="font-semibold text-white">Volatility band</div>
            <p className="mt-3">Track the most active premiums and watch for breakout pressure.</p>
          </div>
          <div className="rounded-3xl bg-white/5 p-5 text-sm text-white/70">
            <div className="font-semibold text-white">Execution queue</div>
            <p className="mt-3">Prioritize the next launch when premium flows line up with trend bias.</p>
          </div>
        </div>
      </section>
    </div>
  )
}
