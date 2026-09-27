import { useCallback, useEffect, useState } from 'react'
import api from '../api/client'
import type { Operations } from '../types/operations'

/**
 * PHASE 5.3 §30 — ONE clear operations dashboard (TRADING CONTROL).
 * Shows the real backend state (mode, AI, broker, market, health,
 * reconciliation, kill switch) and the operator controls:
 *   [ ENABLE AI / DISABLE AI ]  [ PAPER MODE ]  [ LIVE MODE ]  [ KILL SWITCH ]
 * Dangerous actions confirm first; every control is server-enforced —
 * the UI can only request, never bypass.
 */

function Dot({ ok }: { ok: boolean }) {
  return <span className={ok ? 'text-emerald-400' : 'text-rose-400'}>{ok ? '\u25CF' : '\u25CB'}</span>
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2 border-b border-[#1e2d45] last:border-0">
      <span className="text-slate-400 text-sm">{label}</span>
      <span className="text-sm text-white font-medium">{children}</span>
    </div>
  )
}

export default function Operations() {
  const [ops, setOps] = useState<Operations | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await api.get('/bot/operations')
      setOps(r.data)
    } catch {
      /* keep last snapshot */
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  const post = async (name: string, path: string, body?: unknown) => {
    setBusy(name)
    setMessage(null)
    try {
      const r = await api.post(path, body ?? {})
      setMessage({ kind: r.data?.success === false ? 'err' : 'ok', text: String(r.data?.message ?? 'done') })
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { message?: string; detail?: string } } })?.response?.data
      setMessage({ kind: 'err', text: String(msg?.message ?? msg?.detail ?? 'request failed') })
    } finally {
      setBusy(null)
      load()
    }
  }

  const enableAI = () => post('ai', '/bot/ai-toggle', { enabled: true })
  const disableAI = () => post('ai', '/bot/ai-toggle', { enabled: false })
  const paperMode = () => post('paper', '/bot/mode', { mode: 'paper' })
  const liveMode = () => {
    if (window.confirm(
      'Arm LIVE trading?\n\nThis runs the full backend readiness gate (auth, funds, data, reconciliation, risk, kill switch, strategy, instruments).\n' +
      'Even when all gates pass, live execution only arms after a worker restart with TRADING_MODE=live. Continue?')) {
      post('live', '/bot/mode', { mode: 'live' })
    }
  }
  const kill = () => {
    if (window.confirm('EMERGENCY KILL — stop all trading immediately?\n\nNew orders are blocked backend-side until the switch is reset.')) {
      post('kill', '/bot/kill')
    }
  }

  const ai = ops?.ai
  const live = ops?.live_readiness
  const recon = ops?.reconciliation

  return (
    <div className="p-4 lg:p-6 space-y-4 max-w-3xl mx-auto">
      <div>
        <h1 className="text-xl font-bold text-white">TRADING CONTROL</h1>
        <p className="text-slate-400 text-sm">Live operational state — backend-enforced, refreshed every 5s</p>
      </div>

      {message && (
        <div className={`rounded-lg border px-3 py-2 text-sm ${message.kind === 'ok' ? 'border-emerald-700 bg-emerald-950/40 text-emerald-300' : 'border-rose-700 bg-rose-950/40 text-rose-300'}`}>
          {message.text}
        </div>
      )}

      <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-4">
        <Row label="Mode">{(ops?.mode ?? '—').toUpperCase()}</Row>
        <Row label="Strategy">{ops?.strategy ?? '—'}</Row>
        <Row label="AI">
          <span className={ai?.enabled ? 'text-emerald-400' : 'text-slate-400'}>
            {ai?.enabled ? 'ON' : 'OFF'}
          </span>
          <span className="text-slate-500 text-xs ml-2">{ai?.provider ?? ''}/{ai?.model ?? ''}</span>
        </Row>
        <Row label="Broker">{ops?.broker ?? '—'}</Row>
        <Row label="Market">
          <span className={(ops?.market?.status === 'OPEN') ? 'text-emerald-400' : 'text-slate-400'}>
            {ops?.market?.status ?? 'UNKNOWN'}
          </span>
        </Row>
        <Row label="API">{ops?.api_health && Object.keys(ops.api_health).length ? 'HEALTHY' : 'UNKNOWN'}</Row>
        <Row label="Data">
          {ops?.data_health && !ops.data_health.is_stale && ops.data_health.symbols_loaded ? 'HEALTHY' : 'STALE/UNKNOWN'}
        </Row>
        <Row label="Reconcile">
          <span className={recon?.state === 'OK' ? 'text-emerald-400' : recon?.state === 'FAILED' ? 'text-rose-400' : 'text-amber-400'}>
            {recon?.state ?? 'UNKNOWN'}
          </span>
          {recon?.age_seconds != null && (
            <span className="text-slate-500 text-xs ml-2">{Math.round(recon.age_seconds)}s ago</span>
          )}
        </Row>
        <Row label="Kill Switch">
          {ops?.kill_switch?.triggered
            ? <span className="text-rose-400 font-bold">🔴 TRIGGERED</span>
            : <span className="text-emerald-400">🟢 ARMED-CLEAR</span>}
        </Row>
        <Row label="Live Readiness">
          {live?.ready
            ? <span className="text-emerald-400 font-bold">LIVE READY</span>
            : <span className="text-amber-400">LIVE BLOCKED</span>}
        </Row>
      </div>

      {live && !live.ready && (
        <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-4">
          <p className="text-slate-400 text-sm font-semibold mb-2">Blocked reasons</p>
          <ul className="list-disc list-inside text-amber-300 text-sm space-y-1">
            {(live.blocked_reasons ?? []).map((r) => <li key={r}>{r}</li>)}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {ai?.enabled
          ? <button disabled={busy === 'ai'} onClick={disableAI}
              className="rounded-lg bg-[#141b2d] border border-[#1e2d45] px-3 py-2.5 text-sm text-white hover:border-emerald-600 disabled:opacity-50">
              {busy === 'ai' ? '…' : 'DISABLE AI'}
            </button>
          : <button disabled={busy === 'ai'} onClick={enableAI}
              className="rounded-lg bg-emerald-900/40 border border-emerald-700 px-3 py-2.5 text-sm text-emerald-200 hover:bg-emerald-900/70 disabled:opacity-50">
              {busy === 'ai' ? '…' : 'ENABLE AI'}
            </button>}
        <button disabled={busy === 'paper'} onClick={paperMode}
          className="rounded-lg bg-[#141b2d] border border-[#1e2d45] px-3 py-2.5 text-sm text-white hover:border-sky-600 disabled:opacity-50">
          {busy === 'paper' ? '…' : 'PAPER MODE'}
        </button>
        <button disabled={busy === 'live'} onClick={liveMode}
          className="rounded-lg bg-[#141b2d] border border-amber-700/60 px-3 py-2.5 text-sm text-amber-200 hover:border-amber-500 disabled:opacity-50">
          {busy === 'live' ? '…' : 'LIVE MODE'}
        </button>
        <button disabled={busy === 'kill'} onClick={kill}
          className="rounded-lg bg-rose-950/60 border border-rose-800 px-3 py-2.5 text-sm text-rose-200 font-semibold hover:bg-rose-900/70 disabled:opacity-50">
          {busy === 'kill' ? '…' : '🔴 KILL SWITCH'}
        </button>
      </div>

      <div className="flex items-center gap-2 text-xs text-slate-500">
        <Dot ok={!!ops} />
        <span>
          {ops?.generated_at ? `backend snapshot ${new Date(ops.generated_at).toLocaleTimeString()}` : 'waiting for backend…'}
        </span>
      </div>
    </div>
  )
}
