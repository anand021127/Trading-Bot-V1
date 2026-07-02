import { useEffect, useState } from 'react'
import { fetchPerformance } from '../api/endpoints'
import type { PerformanceResponse } from '../types'

export default function Performance() {
  const [performance, setPerformance] = useState<PerformanceResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    const loadPerformance = async () => {
      try {
        const data = await fetchPerformance()
        setPerformance(data)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load performance snapshots')
      } finally {
        setIsLoading(false)
      }
    }

    loadPerformance()
  }, [])

  return (
    <div className="space-y-8">
      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">Performance analytics</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">Strategy snapshots</h2>
          </div>
          <p className="max-w-2xl text-sm text-white/70">Review stored performance snapshots and monitor long-term trends.</p>
        </div>
      </section>

      {isLoading ? (
        <div className="rounded-[2rem] border border-white/10 bg-card p-8 text-center text-white/70">Loading performance data...</div>
      ) : error ? (
        <div className="rounded-[2rem] border border-red-500/20 bg-red-950/40 p-8 text-red-200">{error}</div>
      ) : performance?.performance.length ? (
        <div className="overflow-hidden rounded-[2rem] border border-white/10 bg-card shadow-glow">
          <table className="min-w-full border-separate border-spacing-0 text-left">
            <thead className="bg-white/5 text-sm text-white/60">
              <tr>
                <th className="px-6 py-4">Date</th>
                <th className="px-6 py-4">Net P&L</th>
                <th className="px-6 py-4">Trades</th>
                <th className="px-6 py-4">Win rate</th>
                <th className="px-6 py-4">Equity</th>
              </tr>
            </thead>
            <tbody>
              {performance.performance.map((snapshot) => (
                <tr key={snapshot.date} className="border-t border-white/10 last:border-b-0">
                  <td className="px-6 py-4 text-sm text-white/80">{snapshot.date}</td>
                  <td className="px-6 py-4 text-sm text-white">{snapshot.net_pnl.toFixed(2)}</td>
                  <td className="px-6 py-4 text-sm text-white/70">{snapshot.trades_count}</td>
                  <td className="px-6 py-4 text-sm text-white/70">{snapshot.win_rate}%</td>
                  <td className="px-6 py-4 text-sm text-white/70">{snapshot.equity.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="rounded-[2rem] border border-white/10 bg-card p-8 text-center text-white/70">No performance snapshots available yet.</div>
      )}
    </div>
  )
}
