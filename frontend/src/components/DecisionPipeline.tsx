import type { PipelineView, SymbolCoverage, V8dCondition, V8dDiagnostics, V8dSide } from '../types/pipeline'

/**
 * Scanner → Market → Strategy → Latest Signal → AI Decision → Risk Check → Execution → Final,
 * for the latest scan, plus the V8-D per-condition detail and every scanned symbol.
 * Every "did not happen" case is spelled out (NOT EVALUATED / NOT ATTEMPTED / DISABLED / MARKET CLOSED …):
 * the panel never implies an evaluation that did not run, and NO SIGNAL is never shown as a rejection.
 */

type Tone = 'good' | 'bad' | 'warn' | 'info' | 'muted'

const TONE_CLASS: Record<Tone, string> = {
  good: 'text-emerald-300', bad: 'text-red-300', warn: 'text-amber-300', info: 'text-blue-300', muted: 'text-slate-400',
}

function toneOf(label: string, value: string): Tone {
  const v = value.toUpperCase()
  if (label === 'Scanner') {
    if (v.startsWith('RUNNING —') || v.includes('NOT RESPONDING')) return 'bad'
    return v === 'RUNNING' ? 'good' : v === 'STARTING' ? 'info' : 'muted'
  }
  if (label === 'Market') return v === 'LIVE' ? 'good' : v.includes('CLOSED') ? 'info' : 'muted'
  if (label === 'Latest Signal') {
    if (v.startsWith('BUY')) return 'good'
    if (v.startsWith('SIGNAL REJECTED')) return 'bad'
    return v.startsWith('NOT EVALUATED') ? 'warn' : 'muted'
  }
  if (label === 'AI Decision') {
    if (v.startsWith('APPROVED')) return 'good'
    if (v.startsWith('REJECTED') || v.startsWith('UNAVAILABLE')) return 'bad'
    return v.startsWith('WAIT') ? 'warn' : 'muted'
  }
  if (label === 'Risk Check') return v === 'PASS' ? 'good' : v === 'REJECTED' ? 'bad' : 'muted'
  if (label === 'Execution') return v.startsWith('FILLED') ? 'good' : v === 'REJECTED' || v === 'ERROR' ? 'bad' : 'muted'
  if (label === 'Final') return v.startsWith('FILLED') ? 'good' : 'muted'
  return 'muted'
}

function Row({ label, value, detail }: { label: string; value: string; detail?: string | null }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-0.5 sm:gap-3 py-1.5 border-b border-[#1e2d45]/60 last:border-0 min-w-0">
      <dt className="text-[11px] uppercase tracking-wider text-slate-500 shrink-0">{label}</dt>
      <dd className="min-w-0 sm:text-right [overflow-wrap:anywhere]">
        <span className={`text-sm font-semibold ${TONE_CLASS[toneOf(label, value)]}`} data-testid={`pipe-${label.toLowerCase().replace(/\s+/g, '-')}`}>{value}</span>
        {detail ? <span className="block text-[11px] text-slate-400 font-normal">{detail}</span> : null}
      </dd>
    </div>
  )
}

const pass = (c: V8dCondition | boolean | undefined): boolean | null =>
  c == null ? null : typeof c === 'boolean' ? c : c.pass
const detailOf = (c: V8dCondition | boolean | undefined): string | undefined =>
  c != null && typeof c !== 'boolean' ? c.detail : undefined

function Mark({ ok }: { ok: boolean | null }) {
  return ok == null
    ? <span className="text-slate-500">—</span>
    : ok ? <span className="text-emerald-300" aria-label="pass">✓</span> : <span className="text-red-300" aria-label="fail">✗</span>
}

const COND_KEYS = ['trend', 'pullback', 'rsi', 'reversal'] as const
const num = (v: number | null | undefined, d = 2) => (v == null ? '—' : v.toFixed(d))

function V8dDetail({ v8d }: { v8d: V8dDiagnostics }) {
  if (!v8d.evaluated) {
    return <p className="text-[11px] text-slate-400" data-testid="v8d-detail">V8-D not evaluated: {v8d.reason ?? 'insufficient data'}</p>
  }
  const sides: Array<['CE' | 'PE', V8dSide | undefined]> = [['CE', v8d.ce], ['PE', v8d.pe]]
  return (
    <div className="text-[11px] text-slate-300 space-y-1.5" data-testid="v8d-detail">
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-3 gap-y-0.5">
        <span>EMA20 <b className="text-slate-100" data-testid="v8d-ema20">{num(v8d.ema20)}</b></span>
        <span>EMA50 <b className="text-slate-100" data-testid="v8d-ema50">{num(v8d.ema50)}</b></span>
        <span>Separation <b className="text-slate-100" data-testid="v8d-sep">{v8d.ema_separation_pct != null ? `${v8d.ema_separation_pct >= 0 ? '+' : ''}${v8d.ema_separation_pct.toFixed(3)}%` : '—'}</b></span>
        <span>RSI <b className="text-slate-100" data-testid="v8d-rsi">{num(v8d.rsi)}</b></span>
        <span>Close <b className="text-slate-100">{num(v8d.price?.close)}</b></span>
        <span title="Informational — not an entry condition">ATR14 <b className="text-slate-100">{num(v8d.atr14_underlying)}</b></span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left" data-testid="v8d-conditions">
          <thead><tr className="text-slate-500"><th className="font-normal pr-3">Side</th>{COND_KEYS.map(k => <th key={k} className="font-normal pr-3 capitalize">{k}</th>)}<th className="font-normal">Pullback band</th></tr></thead>
          <tbody>
            {sides.map(([name, s]) => (
              <tr key={name} data-testid={`v8d-row-${name}`}>
                <td className="pr-3 font-semibold">{name}</td>
                {COND_KEYS.map(k => <td key={k} className="pr-3" title={detailOf(s?.[k])}><Mark ok={pass(s?.[k])} /></td>)}
                <td className="text-slate-400">{v8d.pullback_band?.[name === 'CE' ? 'ce' : 'pe']?.map(x => x.toFixed(1)).join(' – ') ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {v8d.decision == null && v8d.failed && v8d.failed.length > 0 && (
        <p data-testid="v8d-failed" className="text-slate-400">Closest side {v8d.closest_side}: failed <b className="text-amber-300">{v8d.failed.join(', ')}</b></p>
      )}
      {v8d.consistent === false && <p className="text-red-300" role="alert">Diagnostics disagree with the strategy's own verdict — treat the numbers with caution.</p>}
    </div>
  )
}

function Coverage({ rows, primary }: { rows: SymbolCoverage[]; primary?: string | null }) {
  return (
    <div className="overflow-x-auto" data-testid="symbol-coverage">
      <table className="w-full text-[11px] text-left">
        <thead><tr className="text-slate-500"><th className="font-normal pr-3">Symbol</th><th className="font-normal pr-3">Outcome</th><th className="font-normal pr-3">Failed</th><th className="font-normal pr-3">RSI</th><th className="font-normal">Sep %</th></tr></thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.symbol} data-testid={`cov-${r.symbol}`} className={r.symbol === primary ? 'text-slate-100' : 'text-slate-300'}>
              <td className="pr-3 font-semibold">{r.symbol}</td>
              <td className="pr-3">{r.outcome ?? '—'}{r.error ? ' ⚠' : ''}</td>
              <td className="pr-3">{r.binding ?? '—'}</td>
              <td className="pr-3">{num(r.rsi, 1)}</td>
              <td>{r.sep_pct != null ? r.sep_pct.toFixed(3) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function DecisionPipeline({ pipeline, compact = false }: { pipeline?: PipelineView | null; compact?: boolean }) {
  if (!pipeline) {
    return (
      <div data-testid="decision-pipeline" className="rounded-xl border border-[#1e2d45] bg-[#141b2d] p-3 text-xs text-slate-400">
        Decision pipeline unavailable — the backend did not report scanner state.
      </div>
    )
  }
  const aiDetail = [
    pipeline.ai_reason,
    pipeline.ai_confidence != null ? `confidence ${pipeline.ai_confidence}` : null,
    pipeline.ai_latency_ms != null ? `${Math.round(pipeline.ai_latency_ms)} ms` : null,
  ].filter(Boolean).join(' · ') || null
  const rows = pipeline.symbols ?? []

  return (
    <section aria-label="Decision pipeline" data-testid="decision-pipeline"
      className={`rounded-xl border border-[#1e2d45] bg-[#141b2d] min-w-0 ${compact ? 'p-2.5' : 'p-3 sm:p-4'}`}>
      {!compact && (
        <div className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
          <h2 className="text-sm font-semibold text-white">Decision pipeline</h2>
          <span className="text-[11px] text-slate-500">
            {pipeline.primary_symbol ? `${pipeline.primary_symbol} · ` : ''}
            {pipeline.scan_seq != null ? `scan #${pipeline.scan_seq}` : 'no scan yet'}
            {pipeline.scan_time_ist ? ` · ${pipeline.scan_time_ist}` : ''}
          </span>
        </div>
      )}
      <dl>
        <Row label="Scanner" value={pipeline.scanner} />
        <Row label="Market" value={pipeline.market} />
        <Row label="Strategy" value={pipeline.strategy} />
        <Row label="Latest Signal" value={pipeline.latest_signal} detail={pipeline.signal_detail} />
        <Row label="AI Decision" value={pipeline.ai_decision} detail={aiDetail} />
        <Row label="Risk Check" value={pipeline.risk_check} detail={pipeline.risk_detail} />
        <Row label="Execution" value={pipeline.execution} detail={pipeline.execution_detail} />
        {pipeline.final && <Row label="Final" value={pipeline.final} />}
      </dl>
      {pipeline.outcome && (
        <p className="mt-1 text-[10px] uppercase tracking-wider text-slate-500" data-testid="pipe-outcome">Outcome: {pipeline.outcome}</p>
      )}
      {pipeline.summary && (
        <p className="mt-2 text-xs text-slate-300 leading-relaxed [overflow-wrap:anywhere]" data-testid="pipe-summary">{pipeline.summary}</p>
      )}
      {pipeline.v8d && (
        <details className="mt-2 rounded-lg border border-[#1e2d45] bg-[#0f1628]" open={!compact && pipeline.outcome === 'NO_SIGNAL'}>
          <summary className="cursor-pointer select-none list-none px-3 min-h-[44px] lg:min-h-[32px] flex items-center text-xs font-semibold text-slate-200">
            V8-D conditions{pipeline.primary_symbol ? ` — ${pipeline.primary_symbol}` : ''}
          </summary>
          <div className="px-3 pb-2.5"><V8dDetail v8d={pipeline.v8d} /></div>
        </details>
      )}
      {rows.length > 0 && (
        <details className="mt-2 rounded-lg border border-[#1e2d45] bg-[#0f1628]" open={!compact && rows.length > 1}>
          <summary className="cursor-pointer select-none list-none px-3 min-h-[44px] lg:min-h-[32px] flex items-center text-xs font-semibold text-slate-200">
            Symbols scanned ({rows.length})
          </summary>
          <div className="px-3 pb-2.5"><Coverage rows={rows} primary={pipeline.primary_symbol} /></div>
        </details>
      )}
    </section>
  )
}
