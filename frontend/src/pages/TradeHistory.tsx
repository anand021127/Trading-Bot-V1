import { useEffect, useState } from 'react'
import { fetchTrades } from '../api/endpoints'
import { formatCurrency } from '../utils/formatters'
import type { Trade } from '../types'

export default function TradeHistory() {
  const [trades, setTrades] = useState<Trade[]>([])
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    const loadTrades = async () => {
      try {
        const data = await fetchTrades()
        setTrades(data)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load trade history')
      } finally {
        setIsLoading(false)
      }
    }

    loadTrades()
  }, [])

  return (
    <div className="space-y-8">
      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">Trade history</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">Executed orders</h2>
          </div>
          <p className="max-w-2xl text-sm text-white/70">Review every executed trade with P&L detail and execution status.</p>
        </div>
      </section>

      {isLoading ? (
        <div className="rounded-[2rem] border border-white/10 bg-card p-8 text-center text-white/70">Loading trade history...</div>
      ) : error ? (
        <div className="rounded-[2rem] border border-red-500/20 bg-red-950/40 p-8 text-red-200">{error}</div>
      ) : trades.length === 0 ? (
        <div className="rounded-[2rem] border border-white/10 bg-card p-8 text-center text-white/70">No historical trades found. Execute a few orders to populate the log.</div>
      ) : (
        <div className="overflow-hidden rounded-[2rem] border border-white/10 bg-card shadow-glow">
          <table className="min-w-full border-separate border-spacing-0 text-left">
            <thead>
              <tr className="bg-white/5 text-sm text-white/60">
                <th className="px-6 py-4">Trade</th>
                <th className="px-6 py-4">Symbol</th>
                <th className="px-6 py-4">Side</th>
                <th className="px-6 py-4">Qty</th>
                <th className="px-6 py-4">Price</th>
                <th className="px-6 py-4">P&L</th>
                <th className="px-6 py-4">Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((trade) => (
                <tr key={trade.id} className="border-t border-white/10 last:border-b-0">
                  <td className="px-6 py-4 text-sm text-white/70">{trade.id.slice(0, 8)}</td>
                  <td className="px-6 py-4 text-sm text-white">{trade.symbol}</td>
                  <td className="px-6 py-4 text-sm text-white/70">{trade.side.toUpperCase()}</td>
                  <td className="px-6 py-4 text-sm text-white/70">{trade.quantity}</td>
                  <td className="px-6 py-4 text-sm text-white/70">{formatCurrency(trade.price)}</td>
                  <td className={`px-6 py-4 text-sm ${trade.pnl && trade.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {trade.pnl != null ? formatCurrency(trade.pnl) : '—'}
                  </td>
                  <td className="px-6 py-4 text-sm text-white/50">{new Date(trade.timestamp).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
