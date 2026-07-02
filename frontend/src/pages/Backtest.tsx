import { FormEvent, useState } from 'react'
import { runBacktest } from '../api/endpoints'
import type { BacktestRequest, BacktestResponse } from '../types'

export default function Backtest() {
  const [form, setForm] = useState<BacktestRequest>({
    start_date: '',
    end_date: '',
    commission_pct: 0.05,
    slippage_pct: 0.05,
  })
  const [result, setResult] = useState<BacktestResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isRunning, setIsRunning] = useState(false)

  const submitBacktest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setIsRunning(true)
    setError(null)
    setResult(null)

    try {
      const data = await runBacktest(form)
      setResult(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backtest failed')
    } finally {
      setIsRunning(false)
    }
  }

  return (
    <div className="space-y-8">
      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">Backtesting</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">Strategy performance review</h2>
          </div>
          <p className="max-w-2xl text-sm text-white/70">Run a historical simulation of your strategy with configurable slippage and commission assumptions.</p>
        </div>
      </section>

      <div className="grid gap-8 xl:grid-cols-[1.4fr_1fr]">
        <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
          <form className="space-y-6" onSubmit={submitBacktest}>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="grid gap-2 text-sm text-white/70">
                Start date
                <input
                  type="date"
                  value={form.start_date}
                  onChange={(event) => setForm({ ...form, start_date: event.target.value })}
                  className="rounded-3xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none focus:border-accent"
                />
              </label>
              <label className="grid gap-2 text-sm text-white/70">
                End date
                <input
                  type="date"
                  value={form.end_date}
                  onChange={(event) => setForm({ ...form, end_date: event.target.value })}
                  className="rounded-3xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none focus:border-accent"
                />
              </label>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="grid gap-2 text-sm text-white/70">
                Commission %
                <input
                  type="number"
                  min={0}
                  step={0.01}
                  value={form.commission_pct}
                  onChange={(event) => setForm({ ...form, commission_pct: Number(event.target.value) })}
                  className="rounded-3xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none focus:border-accent"
                />
              </label>
              <label className="grid gap-2 text-sm text-white/70">
                Slippage %
                <input
                  type="number"
                  min={0}
                  step={0.01}
                  value={form.slippage_pct}
                  onChange={(event) => setForm({ ...form, slippage_pct: Number(event.target.value) })}
                  className="rounded-3xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none focus:border-accent"
                />
              </label>
            </div>

            <button
              type="submit"
              disabled={isRunning}
              className="inline-flex items-center justify-center rounded-3xl bg-accent px-6 py-3 text-sm font-semibold text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isRunning ? 'Running backtest...' : 'Run backtest'}
            </button>
          </form>

          {error ? <div className="rounded-3xl bg-rose-500/10 p-4 text-sm text-rose-200">{error}</div> : null}
        </section>

        <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
          <div className="space-y-4">
            <h3 className="text-2xl font-semibold text-white">Backtest summary</h3>
            <p className="text-sm text-white/70">Review your latest historical simulation and tune the strategy assumptions.</p>
          </div>

          {result ? (
            <div className="mt-6 space-y-4 text-sm text-white/70">
              <div className="rounded-3xl bg-white/5 p-5">
                <div className="flex justify-between">
                  <span>Status</span>
                  <span className="text-white">{result.status}</span>
                </div>
                <div className="mt-3 flex justify-between">
                  <span>Total trades</span>
                  <span className="text-white">{result.summary.total_trades}</span>
                </div>
                <div className="mt-3 flex justify-between">
                  <span>Net P&L</span>
                  <span className="text-white">{result.summary.net_pnl.toFixed(2)}</span>
                </div>
                <div className="mt-3 flex justify-between">
                  <span>Win rate</span>
                  <span className="text-white">{result.summary.win_rate}%</span>
                </div>
                <div className="mt-3 flex justify-between">
                  <span>Range</span>
                  <span className="text-white">{result.summary.start_date} → {result.summary.end_date}</span>
                </div>
              </div>
            </div>
          ) : (
            <div className="mt-6 rounded-3xl bg-white/5 p-5 text-sm text-white/70">Run a backtest to see summary result details here.</div>
          )}
        </section>
      </div>
    </div>
  )
}
