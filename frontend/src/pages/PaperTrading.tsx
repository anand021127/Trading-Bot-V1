import { FormEvent, useEffect, useState } from 'react'
import { executeTrade, fetchOverview } from '../api/endpoints'
import type { OverviewData } from '../types'
import { formatCurrency } from '../utils/formatters'

const defaultForm = {
  symbol: 'NIFTY',
  side: 'buy',
  quantity: 1,
}

export default function PaperTrading() {
  const [overview, setOverview] = useState<OverviewData | null>(null)
  const [form, setForm] = useState(defaultForm)
  const [result, setResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  useEffect(() => {
    const loadOverview = async () => {
      try {
        const data = await fetchOverview()
        setOverview(data)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load overview')
      }
    }
    loadOverview()
  }, [])

  const submitTrade = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setIsSubmitting(true)
    setResult(null)
    setError(null)

    try {
      const data = await executeTrade({
        symbol: form.symbol,
        side: form.side,
        quantity: form.quantity,
      })
      setResult(`Order executed: ${data.order.symbol} ${data.order.side.toUpperCase()} ${data.order.quantity}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Trade submission failed')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="space-y-8">
      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">Paper trading</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">Trade simulation workspace</h2>
          </div>
          <p className="max-w-2xl text-sm text-white/70">Simulate order flow with a safe paper trading engine and monitor position state.</p>
        </div>
      </section>

      <div className="grid gap-8 xl:grid-cols-[1.3fr_0.7fr]">
        <section className="space-y-6 rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
          <div className="space-y-4">
            <h3 className="text-2xl font-semibold text-white">Submit paper order</h3>
            <p className="text-sm text-white/70">Enter a simulated market order and track the result in the backend engine.</p>
          </div>

          <form className="grid gap-4" onSubmit={submitTrade}>
            <div className="grid gap-4 sm:grid-cols-3">
              <label className="grid gap-2 text-sm text-white/70">
                Symbol
                <input
                  value={form.symbol}
                  onChange={(event) => setForm({ ...form, symbol: event.target.value.toUpperCase() })}
                  className="rounded-3xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none focus:border-accent"
                />
              </label>

              <label className="grid gap-2 text-sm text-white/70">
                Side
                <select
                  value={form.side}
                  onChange={(event) => setForm({ ...form, side: event.target.value })}
                  className="rounded-3xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none focus:border-accent"
                >
                  <option value="buy">Buy</option>
                  <option value="sell">Sell</option>
                </select>
              </label>

              <label className="grid gap-2 text-sm text-white/70">
                Quantity
                <input
                  type="number"
                  min={1}
                  value={form.quantity}
                  onChange={(event) => setForm({ ...form, quantity: Number(event.target.value) })}
                  className="rounded-3xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none focus:border-accent"
                />
              </label>
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="inline-flex items-center justify-center rounded-3xl bg-accent px-6 py-3 text-sm font-semibold text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isSubmitting ? 'Submitting...' : 'Place paper trade'}
            </button>
          </form>

          {result ? <div className="rounded-3xl bg-emerald-500/10 p-4 text-sm text-emerald-200">{result}</div> : null}
          {error ? <div className="rounded-3xl bg-rose-500/10 p-4 text-sm text-rose-200">{error}</div> : null}
        </section>

        <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
          <div className="space-y-4">
            <h3 className="text-2xl font-semibold text-white">Current paper portfolio</h3>
            <p className="text-sm text-white/70">See active positions and capital availability from the overview state.</p>
          </div>

          {overview ? (
            <div className="mt-6 space-y-4">
              <div className="rounded-3xl bg-white/5 p-5 text-sm text-white/70">
                <div className="flex justify-between">
                  <span>Total capital</span>
                  <span>{formatCurrency(overview.capital.total)}</span>
                </div>
                <div className="mt-3 flex justify-between">
                  <span>Available</span>
                  <span>{formatCurrency(overview.capital.available)}</span>
                </div>
              </div>
              <div className="rounded-3xl bg-white/5 p-5 text-sm text-white/70">
                <div className="flex justify-between">
                  <span>Open positions</span>
                  <span>{overview.open_positions.length}</span>
                </div>
                <div className="mt-3 text-sm text-white/70">Position bias: {overview.trend_bias}</div>
              </div>
            </div>
          ) : (
            <div className="rounded-3xl bg-white/5 p-5 text-sm text-white/70">Loading positions...</div>
          )}
        </section>
      </div>
    </div>
  )
}
