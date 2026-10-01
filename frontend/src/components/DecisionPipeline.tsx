import type { PipelineView } from '../types/pipeline'

/**
 * Scanner → Market → Strategy → Latest Signal → AI Decision → AI Reason →
 * Risk Check → Execution, for the latest scan. Every "did not happen" case is
 * spelled out (NOT EVALUATED / DISABLED / MARKET CLOSED …): the panel never
 * implies an evaluation that did not run.
 */

type Tone = 'good' | 'bad' | 'warn' | 'info' | 'muted'

const TONE_CLASS: Record<Tone, string> = {
  good: 'text-emerald-300',
  bad: 'text-red-300',
  warn: 'text-amber-300',
  info: 'text-blue-300',
  muted: 'text-slate-400',
}

function toneOf(label: string, value: string): Tone {
  const v = value.toUpperCase()
  if (label === 'Scanner') {
    if (v.startsWith('RUNNING —') || v.includes('NOT RESPONDING')) return 'bad'
    return v === 'RUNNING' ? 'good' : v === 'STARTING' ? 'info' : 'muted'
  }
  if (label === 'Market') return v === 'LIVE' ? 'good' : v.includes('CLOSED') ? 'info' : 'muted'
  if (label === 'Latest Signal') return v.startsWith('BUY') ? 'good' : v.startsWith('NOT EVALUATED') ? 'warn' : 'muted'
  if (label === 'AI Decision') {
    if (v.startsWith('APPROVED')) return 'good'
    if (v.startsWith('REJECTED') || v.startsWith('UNAVAILABLE')) return 'bad'
    if (v.startsWith('WAIT')) return 'warn'
    return 'muted'
  }
  if (label === 'Risk Check') return v === 'PASS' ? 'good' : v === 'REJECTED' ? 'bad' : 'muted'
  if (label === 'Execution') return v.startsWith('FILLED') ? 'good' : v === 'REJECTED' || v === 'ERROR' ? 'bad' : 'muted'
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

  return (
    <section aria-label="Decision pipeline" data-testid="decision-pipeline"
      className={`rounded-xl border border-[#1e2d45] bg-[#141b2d] min-w-0 ${compact ? 'p-2.5' : 'p-3 sm:p-4'}`}>
      {!compact && (
        <div className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
          <h2 className="text-sm font-semibold text-white">Decision pipeline</h2>
          <span className="text-[11px] text-slate-500">
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
      </dl>
      {pipeline.summary && (
        <p className="mt-2 text-xs text-slate-300 leading-relaxed [overflow-wrap:anywhere]" data-testid="pipe-summary">{pipeline.summary}</p>
      )}
    </section>
  )
}
