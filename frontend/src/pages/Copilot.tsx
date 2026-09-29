import { useState, useEffect, useRef, useCallback } from 'react'
import axios from 'axios'
import {
  Bot, Send, Square, RotateCcw, Trash2, Loader2, AlertTriangle,
  CheckCircle2, Ban, Sparkles, Wrench, Cpu,
} from 'lucide-react'
import { api } from '../api/client'

/**
 * Copilot AI — restored page (originally removed from App.tsx/Layout in
 * commit b35dd60).
 *
 * Architecture (async job, no long-lived HTTP request):
 *   POST /api/copilot/chat/submit        -> 202 {job_id} immediately
 *   GET  /api/copilot/chat/status/{id}   -> queued|thinking|completed|failed|cancelled
 *   POST /api/copilot/chat/status/{id}/cancel -> cooperative cancel
 *
 * Poll failures are RETRIED (transient) — the UI distinguishes network
 * timeout / provider timeout / auth failure / rate limit / model
 * unavailable / backend exception / user cancellation / not-configured
 * instead of collapsing everything into "timeout of 30000ms exceeded".
 * This page is observation/explanation ONLY — it never places orders or
 * changes bot state.
 */

type ChatMsg = {
  id: string
  role: 'user' | 'assistant'
  text: string
  state?: 'sending' | 'thinking' | 'streaming' | 'completed' | 'failed' | 'cancelled'
  errorCode?: string
  errorMessage?: string
  contextKeys?: string[]
}

const QUICK_PROMPTS = [
  "Why didn't the bot trade?",
  'Explain today\'s bot status',
  'Show today\'s trades',
  'Explain the latest rejection',
  'Explain the latest backtest',
  'What is my current risk?',
  'Explain V8-D',
  'Is the bot using the capital I configured?',
  'What configuration mismatch exists?',
]

const POLL_INTERVAL_MS = 1000
const POLL_MAX_CONSECUTIVE_FAILURES = 5
const AI_DECISION_POLL_MS = 12000

type AILatency = {
  samples?: number
  success_count?: number
  failure_count?: number
  latency_ms_median?: number | null
  latency_ms_p95?: number | null
  error_counts?: Record<string, number>
}

type AIRecentDecision = {
  decision_id: string
  symbol: string
  decision: string
  confidence: number
  reason_codes: string[]
  model_provider: string
  model_name: string
  created_at: string
  latency_ms?: number | null
}

type AIDecisionStatus = {
  ai_decision_enabled: boolean
  worker_layer_state?: string | null
  provider: string
  model: string
  base_url: string
  timeout_seconds: number
  temperature: number
  architecture: string
  approval_semantics: string
  backtest_status: string
  latency: AILatency
  decision_counters?: Record<string, unknown>
  recent_decisions: AIRecentDecision[]
  note: string
}

type WhyNotTradedBreakdown = {
  stage: string
  v8_d?: string
  v8_d_rejection_reasons?: string[]
  ai_decision?: string | null
  ai_confidence?: number | null
  ai_reason_codes?: string[]
  ai_decision_id?: string | null
  ai_model?: string | null
  execution_reason?: string
}

type WhyNotTraded = {
  available: boolean
  reason?: string
  traded?: boolean
  signal?: string | null
  breakdown?: WhyNotTradedBreakdown
}

/**
 * AI Trading Decision panel (PHASE 5.1).
 *
 * Separate responsibility from the chat above: this is the AI DECISION
 * ENGINE that gates V8-D signals before the hard risk/execution pipeline.
 * "Execution Eligibility: YES" means the deterministic pipeline MAY
 * continue — it NEVER means the AI placed an order. Confidence shown here
 * is AI confidence in its own analysis, NOT a probability of profit.
 */
function AIDecisionPanel() {
  const [status, setStatus] = useState<AIDecisionStatus | null>(null)
  const [why, setWhy] = useState<WhyNotTraded | null>(null)
  const [loadError, setLoadError] = useState(false)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const [s, w] = await Promise.all([
          api.get('/api/ai-decision/status'),
          api.get('/api/ai-decision/why-not-traded'),
        ])
        if (!alive) return
        setLoadError(false)
        setStatus(s.data)
        setWhy(w.data)
      } catch {
        if (alive) setLoadError(true)
      }
    }
    void load()
    const id = setInterval(load, AI_DECISION_POLL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [])

  if (loadError && !status) {
    return (
      <div className="bg-[#141b2d] border border-red-900/40 rounded-xl p-4 text-xs text-red-300">
        AI Trading Decision status unavailable (backend unreachable).
      </div>
    )
  }
  if (!status) return null

  const stageBadge = (stage?: string) => {
    const cls = stage === 'TRADED'
      ? 'bg-emerald-950/40 border-emerald-700/50 text-emerald-300'
      : stage === 'AI_REJECTED' || stage === 'KILL_SWITCH'
        ? 'bg-red-950/40 border-red-800/50 text-red-300'
        : stage === 'AI_WAITING'
          ? 'bg-amber-950/40 border-amber-800/50 text-amber-300'
          : stage === 'RISK_REJECTED' || stage === 'EXECUTION_REJECTED' || stage === 'NO_VALID_LOT_SIZE' || stage === 'NO_VALID_CONTRACT'
            ? 'bg-orange-950/40 border-orange-800/50 text-orange-300'
            : 'bg-slate-800/70 border-slate-700 text-slate-300'
    const label = (stage || 'UNKNOWN').replace(/_/g, ' ')
    return <span className={`text-[11px] px-2 py-0.5 rounded-full border ${cls}`}>{label}</span>
  }

  const decisionChip = (d?: string | null) => {
    if (!d) return <span className="text-slate-500">—</span>
    const cls = d === 'APPROVE' ? 'text-emerald-300' : d === 'WAIT' ? 'text-amber-300' : 'text-red-300'
    return <span className={`font-semibold ${cls}`}>{d}</span>
  }

  const lat = status.latency || {}

  return (
    <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Cpu size={14} className="text-cyan-400" /> AI Trading Decision
            {status.ai_decision_enabled ? (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-950/40 border border-emerald-800/50 text-emerald-300">ENABLED</span>
            ) : (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-800/70 border border-slate-700 text-slate-400">DISABLED</span>
            )}
          </h2>
          <p className="text-[11px] text-slate-500 mt-0.5">
            V8-D signal → AI decision → hard risk → sizing → execution pipeline. {status.approval_semantics}.
          </p>
        </div>
        <div className="text-right text-[11px] text-slate-400">
          <div className="font-mono">{status.provider} · {status.model}</div>
          <div className="text-slate-600">temp {status.temperature} · timeout {status.timeout_seconds}s</div>
        </div>
      </div>

      {!status.ai_decision_enabled && (
        <div className="text-[11px] text-slate-400 bg-[#0f1628] border border-[#1e2d45] rounded-lg p-2.5">
          {status.note}
        </div>
      )}

      {/* Why didn't we trade? — gate-by-gate breakdown (§10) */}
      <div className="bg-[#0f1628] border border-[#1e2d45] rounded-lg p-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-[11px] uppercase tracking-wider text-slate-500">Why didn&apos;t we trade?</span>
          {stageBadge(why?.breakdown?.stage)}
        </div>
        {why?.available && why.breakdown ? (
          <div className="mt-2 space-y-1 text-[11px] text-slate-300">
            <div>V8-D: <span className={why.breakdown.v8_d === 'PASS' ? 'text-emerald-300' : 'text-slate-400'}>{why.breakdown.v8_d || '—'}</span>
              {why.breakdown.v8_d_rejection_reasons && why.breakdown.v8_d_rejection_reasons.length > 0 && (
                <span className="text-slate-500"> — {why.breakdown.v8_d_rejection_reasons.slice(0, 2).join('; ')}</span>
              )}
            </div>
            <div>AI: {decisionChip(why.breakdown.ai_decision)}
              {typeof why.breakdown.ai_confidence === 'number' && (
                <span className="text-slate-500"> · AI confidence {why.breakdown.ai_confidence}%</span>
              )}
              {why.breakdown.ai_reason_codes && why.breakdown.ai_reason_codes.length > 0 && (
                <span className="text-slate-500"> · {why.breakdown.ai_reason_codes.slice(0, 3).join(', ')}</span>
              )}
            </div>
            {why.breakdown.execution_reason && (
              <div className="text-slate-500">Pipeline: {why.breakdown.execution_reason}</div>
            )}
            <div className="text-slate-500">Final: {why.traded ? 'TRADED (paper)' : 'NO TRADE'}</div>
          </div>
        ) : (
          <div className="mt-2 text-[11px] text-slate-500">{why?.reason || 'No scan result recorded yet.'}</div>
        )}
      </div>

      {/* Latency telemetry (§23) */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-400">
        <span>Decisions measured: <span className="text-slate-200">{lat.samples ?? 0}</span></span>
        <span>Median: <span className="text-slate-200">{lat.latency_ms_median != null ? `${Math.round(lat.latency_ms_median)}ms` : '—'}</span></span>
        <span>p95: <span className="text-slate-200">{lat.latency_ms_p95 != null ? `${Math.round(lat.latency_ms_p95)}ms` : '—'}</span></span>
        <span>Failures: <span className="text-slate-200">{lat.failure_count ?? 0}</span></span>
        {lat.error_counts && Object.entries(lat.error_counts).map(([code, n]) => (
          <span key={code} className="text-amber-400">{code}: {n}</span>
        ))}
      </div>

      {/* Decision counters (§14) */}
      {status.decision_counters && Object.keys(status.decision_counters).length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-400">
          {Object.entries(status.decision_counters).map(([k, v]) =>
            k === 'rejection_breakdown' ? null : (
              <span key={k} className="capitalize">{k}: <span className="text-slate-200">{String(v)}</span></span>
            ))}
          {typeof status.decision_counters.rejection_breakdown === 'object' && status.decision_counters.rejection_breakdown !== null &&
            Object.entries(status.decision_counters.rejection_breakdown as Record<string, number>).map(([code, n]) => (
              <span key={code} className="text-orange-400">{code.replace(/_/g, ' ').toLowerCase()}: {n}</span>
            ))}
        </div>
      )}

      {/* Recent decisions — AI confidence, never probability of profit (§11) */}
      {status.recent_decisions.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-wider text-slate-500 mb-1">Recent AI decisions</div>
          <div className="space-y-1">
            {status.recent_decisions.slice(0, 5).map(d => (
              <div key={d.decision_id} className="flex items-center justify-between gap-2 text-[11px] bg-[#0f1628] border border-[#1e2d45] rounded px-2 py-1">
                <span className="font-mono text-slate-500">{d.created_at.slice(11, 19)}</span>
                <span className="text-slate-300">{d.symbol}</span>
                {decisionChip(d.decision)}
                <span className="text-slate-500" title="AI confidence in its own analysis — NOT a probability of profit">AI conf {d.confidence}%</span>
                <span className="text-slate-500 truncate max-w-[30%]">{(d.reason_codes || []).slice(0, 2).join(', ') || '—'}</span>
                <span className="text-slate-600">{d.latency_ms != null ? `${Math.round(d.latency_ms)}ms` : ''}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Backtest AI availability (§18) — never labeled AI-assisted silently */}
      <div className="text-[11px] text-slate-500">
        Backtest AI layer: <span className="text-amber-300">{status.backtest_status.replace(/_/g, ' ')}</span>
        {' '}(historical AI decisions are not reproducible — backtests are V8-D-only and labeled as such)
      </div>
    </div>
  )
}

/*
 * PHASE B — Operational context cards.
 * Render the ONE authoritative backend context (GET /api/copilot/context)
 * directly in the UI: bot status, configuration + source labels, market,
 * websocket, scanner, today, risk, latest signal/rejection with the full
 * gate chain, AI trading decision, positions, recent trades, backtest,
 * configuration mismatches and recent errors. Read-only observation —
 * mirrors exactly what the Copilot chat is grounded in.
 */
type CopilotContext = {
  generated_at?: string
  bot?: any
  configuration?: any
  market?: any
  data_health?: any
  websocket?: any
  scanner?: any
  latest_signal?: any
  latest_decision?: any
  latest_rejection?: any
  today?: any
  positions?: any
  recent_trades?: any
  risk?: any
  execution?: any
  reconciliation?: any
  broker?: any
  ai?: any
  backtest?: any
  configuration_mismatches?: any[]
  errors?: any
}

function CtxCard({ title, badge, children, defaultOpen = false }: {
  title: string; badge?: string | null; children: React.ReactNode; defaultOpen?: boolean
}) {
  return (
    <details className="bg-[#141b2d] border border-[#1e2d45] rounded-xl" open={defaultOpen}>
      <summary className="cursor-pointer select-none px-4 py-2.5 flex items-center justify-between gap-2 text-xs font-semibold text-white">
        <span>{title}</span>
        {badge != null && badge !== '' && (
          <span className="text-[10px] font-normal text-slate-400 truncate max-w-[60%]">{badge}</span>
        )}
      </summary>
      <div className="px-4 pb-3 text-[11px] text-slate-300 space-y-1">{children}</div>
    </details>
  )
}

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-slate-500">{k}</span>
      <span className="text-right truncate">{v}</span>
    </div>
  )
}

const fmtINR = (n: unknown) =>
  n == null ? '\u2014' : `\u20B9${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`

const stageBadgeCls = (stage?: string) =>
  stage === 'TRADED'
    ? 'text-emerald-300'
    : stage === 'UNKNOWN' || stage === 'NO_SIGNAL_OR_DATA'
      ? 'text-slate-400'
      : 'text-amber-300'

function BotContextCards() {
  const [ctx, setCtx] = useState<CopilotContext | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    const load = () => {
      api.get('/api/copilot/context')
        .then(r => { if (alive) { setCtx(r.data); setFailed(false) } })
        .catch(() => { if (alive) setFailed(true) })
    }
    void load()
    const id = setInterval(load, 15000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  if (failed && !ctx) return null
  if (!ctx) return null

  const bot = ctx.bot || {}
  const cfg = ctx.configuration || {}
  const ws = ctx.websocket || {}
  const scan = ctx.scanner || {}
  const today = ctx.today || {}
  const rej = ctx.latest_rejection || {}
  const chain = rej?.gate_chain || {}
  const sig = ctx.latest_signal || {}
  const ai = ctx.ai || {}
  const risk = ctx.risk || {}
  const pos = ctx.positions || {}
  const trades = ctx.recent_trades || {}
  const bt = ctx.backtest || {}
  const mism: any[] = ctx.configuration_mismatches || []

  return (
    <div className="space-y-2">
      <div className="text-[10px] uppercase tracking-wider text-slate-500 flex items-center justify-between">
        <span>Live operational context</span>
        {ctx.generated_at && <span>updated {new Date(ctx.generated_at).toLocaleTimeString()}</span>}
      </div>

      <CtxCard title="Bot Status" defaultOpen badge={bot.running ? 'RUNNING' : 'STOPPED'}>
        <KV k="Running" v={String(bot.running ?? '—')} />
        <KV k="Mode" v={(bot.mode ?? '—').toUpperCase()} />
        <KV k="Strategy" v={bot.strategy ?? '—'} />
        <KV k="Broker" v={bot.broker ?? '—'} />
        <KV k="Market" v={bot.market_open == null ? '—' : bot.market_open ? 'OPEN' : 'CLOSED'} />
        <KV k="Kill switch" v={bot.kill_switch_active ? 'ACTIVE' : 'clear'} />
      </CtxCard>

      <CtxCard title="Configuration (authoritative)" defaultOpen
        badge={`${fmtINR(cfg?.capital?.starting_capital)} · ${cfg?.risk?.max_trades_per_day ?? '\u2014'} trades/day`}>
        <KV k="Capital" v={<>{fmtINR(cfg?.capital?.starting_capital)} <span className="text-slate-500">({cfg?.capital?.source ?? '\u2014'})</span></>} />
        <KV k="Current equity" v={fmtINR(cfg?.capital?.current_equity)} />
        <KV k="Max trades/day" v={<>{cfg?.risk?.max_trades_per_day ?? '\u2014'} <span className="text-slate-500">({cfg?.risk?.max_trades_source ?? '\u2014'})</span></>} />
        <KV k="Risk per trade" v={cfg?.risk?.max_risk_per_trade_pct != null ? `${(cfg.risk.max_risk_per_trade_pct * 100).toFixed(2)}%` : '\u2014'} />
        <KV k="Daily loss limit" v={cfg?.risk?.max_daily_loss_pct != null ? `${(cfg.risk.max_daily_loss_pct * 100).toFixed(2)}%` : '\u2014'} />
        {mism.length > 0 && (
          <div className="mt-1 rounded border border-amber-800/50 bg-amber-950/30 p-2 text-amber-300">
            {mism.map((m, i) => <div key={i}>⚠ {m.message}</div>)}
          </div>
        )}
      </CtxCard>

      <CtxCard title="Market · Data · WebSocket"
        badge={`${ws?.state?.toUpperCase() ?? 'UNKNOWN'}${ws?.last_tick_age_seconds != null ? ` · ${Math.round(ws.last_tick_age_seconds)}s` : ''}`}>
        <KV k="Session" v={ctx?.market?.session_status ?? '\u2014'} />
        <KV k="WS state" v={ws?.state ?? '\u2014'} />
        <KV k="Streaming" v={ws?.streaming ? 'YES (real ticks)' : 'NO'} />
        <KV k="Last tick age" v={ws?.last_tick_age_seconds != null ? `${ws.last_tick_age_seconds}s` : '\u2014'} />
        <KV k="Market data" v={ws?.market_data_status ?? '\u2014'} />
        {ws?.health_interpretation && <div className="text-slate-500">{ws.health_interpretation}</div>}
      </CtxCard>

      <CtxCard title="Scanner"
        badge={scan?.available ? (scan?.scanner_status ?? scan?.worker_last_scan_reason ?? 'running') : 'no data'}>
        {scan?.available
          ? <>
              {scan?.last_scan_seconds_ago != null && <KV k="Last scan" v={`${Math.round(scan.last_scan_seconds_ago)}s ago`} />}
              {scan?.worker_last_scan_reason && <KV k="Worker last scan" v={scan.worker_last_scan_reason} />}
              {scan?.source_detail && <div className="text-slate-500">{scan.source_detail}</div>}
            </>
          : <div className="text-slate-500">{scan?.reason || 'No scanner state available.'}</div>}
      </CtxCard>

      <CtxCard title="Why didn't we trade?" defaultOpen
        badge={chain?.available === false ? 'no scan' : (chain?.stage ?? sig?.signal ?? '—')}>
        {chain?.available === false
          ? <div className="text-slate-500">{chain?.reason || rej?.reason || 'No scan has been recorded since bot startup.'}</div>
          : <>
              {chain?.human_summary && <div className="text-slate-200">{chain.human_summary}</div>}
              {chain?.signal === 'BUY' && <KV k="V8-D signal" v="BUY" />}
              {chain?.ai_decision && <KV k="AI decision" v={chain.ai_decision} />}
              {chain?.scan_reason && <div className="text-slate-500">reason: {chain.scan_reason}</div>}
              {chain?.age_seconds != null && <div className="text-slate-500">recorded {Math.round(chain.age_seconds)}s ago</div>}
            </>
        }
      </CtxCard>

      <CtxCard title="Latest signal"
        badge={sig?.available ? String(sig?.signal ?? 'NONE') : 'none recorded'}>
        {sig?.available
          ? <>
              <KV k="Signal" v={String(sig?.signal ?? 'NONE')} />
              {sig?.reason && <div className="text-slate-500">{sig.reason}</div>}
              {(sig?.rejection_reasons || []).slice(0, 2).map((r: string, i: number) => (
                <div key={i} className="text-slate-500">{r}</div>
              ))}
            </>
          : <div className="text-slate-500">{sig?.reason || 'No actionable V8-D signal has been recorded.'}</div>}
      </CtxCard>

      <CtxCard title="AI Trading Decision"
        badge={ai?.available ? (ai?.enabled ? 'ENABLED' : 'DISABLED') : 'unknown'}>
        {ai?.available
          ? <>
              <KV k="Enabled" v={String(ai?.enabled)} />
              <KV k="Model" v={`${ai?.provider ?? '\u2014'} / ${ai?.model ?? '\u2014'}`} />
              {ai?.latest?.available && <KV k="Latest" v={`${ai.latest.decision} (${Math.round(ai.latest.age_seconds ?? 0)}s ago)`} />}
              {ai?.latest && !ai.latest.available && <div className="text-slate-500">{ai.latest.reason}</div>}
              <div className="text-slate-500">Separate from the Copilot assistant — this gates V8-D BUYs before hard risk.</div>
            </>
          : <div className="text-slate-500">{ai?.reason || 'AI decision state unavailable.'}</div>}
      </CtxCard>

      <CtxCard title="Today"
        badge={today?.available ? `${today?.trades_today ?? 0}/${today?.configured_max_trades ?? '?'} trades · ${fmtINR(today?.realized_pnl)}` : '\u2014'}>
        {today?.available
          ? <>
              <KV k="Trades today" v={`${today?.trades_today ?? 0} / ${today?.configured_max_trades ?? '\u2014'}${today?.trades_remaining != null ? ` (${today.trades_remaining} left)` : ''}`} />
              <KV k="Wins / Losses" v={`${today?.wins ?? 0}W · ${today?.losses ?? 0}L`} />
              <KV k="Realized P&L" v={fmtINR(today?.realized_pnl)} />
            </>
          : <div className="text-slate-500">{today?.reason || 'No trades recorded today.'}</div>}
      </CtxCard>

      <CtxCard title="Risk"
        badge={risk?.available ? `${risk?.trades_used ?? 0}/${risk?.max_trades ?? '\u2014'}` : '\u2014'}>
        {risk?.available
          ? <>
              <KV k="Trades used" v={`${risk?.trades_used ?? 0}/${risk?.max_trades ?? '\u2014'}`} />
              {risk?.daily_loss_used_pct != null && <KV k="Daily loss used" v={`${risk.daily_loss_used_pct}%`} />}
              {risk?.daily_pnl != null && <KV k="Daily P&L" v={fmtINR(risk.daily_pnl)} />}
              {risk?.consecutive_losses != null && <KV k="Consecutive losses" v={risk.consecutive_losses} />}
              {risk?.stop_reason && <div className="text-amber-300">{risk.stop_reason}</div>}
              {risk?.note && <div className="text-slate-500">{risk.note}</div>}
            </>
          : <div className="text-slate-500">{risk?.reason || 'Risk state unavailable.'}</div>}
      </CtxCard>

      <CtxCard title="Positions"
        badge={pos?.available ? `${pos?.count ?? 0} open` : '\u2014'}>
        {pos?.available
          ? (pos?.count > 0
              ? (pos.positions || []).map((p: any, i: number) => (
                  <div key={i}>{p?.underlying ?? p?.symbol} {p?.strike ?? ''} {p?.option_type ?? ''} · qty {p?.quantity} @ {fmtINR(p?.average_price)}</div>
                ))
              : <div className="text-slate-500">No open positions.</div>)
          : <div className="text-slate-500">{pos?.reason || 'Positions unavailable.'}</div>}
      </CtxCard>

      <CtxCard title="Recent trades"
        badge={trades?.available ? `last ${trades?.count ?? 0}` : '\u2014'}>
        {trades?.available
          ? (trades?.count > 0
              ? (trades.trades || []).slice(0, 5).map((t: any) => (
                  <div key={t.trade_id} className="flex justify-between gap-2">
                    <span className="truncate">{t.underlying ?? t.symbol ?? '\u2014'} {t.strike ?? ''} {t.option_type ?? ''}</span>
                    <span className={Number(t.net_pnl) >= 0 ? 'text-emerald-300' : 'text-red-300'}>{fmtINR(t.net_pnl)}</span>
                  </div>
                ))
              : <div className="text-slate-500">No trades stored yet.</div>)
          : <div className="text-slate-500">{trades?.reason || 'Trades unavailable.'}</div>}
      </CtxCard>

      <CtxCard title="Backtest"
        badge={bt?.available ? `${bt?.summary?.trades ?? '\u2014'} trades · net ${fmtINR(bt?.summary?.net_pnl)}` : 'none stored'}>
        {bt?.available
          ? <>
              <KV k="Period" v={`${bt?.config?.start_date ?? '\u2014'} → ${bt?.config?.end_date ?? '\u2014'}`} />
              <KV k="Trades / Win rate" v={`${bt?.summary?.trades ?? '\u2014'} · ${bt?.summary?.win_rate_pct ?? '\u2014'}%`} />
              <KV k="Net P&L" v={fmtINR(bt?.summary?.net_pnl)} />
              <KV k="Profit factor / MaxDD" v={`${bt?.summary?.profit_factor ?? '\u2014'} · ${bt?.summary?.max_drawdown_pct ?? '\u2014'}%`} />
              <div className="text-slate-500">Read from the latest stored backtest result — never hardcoded.</div>
            </>
          : <div className="text-slate-500">{bt?.reason || 'No completed backtest result is stored.'}</div>}
      </CtxCard>

      {(mism.length > 0 || ctx?.errors?.available) && (
        <CtxCard title="Mismatches · Errors"
          badge={`${mism.length} mismatch${mism.length === 1 ? '' : 'es'}`}>
          {mism.length === 0 && <div className="text-slate-500">No configuration mismatches.</div>}
          {mism.map((m, i) => <div key={i} className="text-amber-300">⚠ {m.message}</div>)}
          {ctx?.errors?.note && <div className="text-slate-500">{ctx.errors.note}</div>}
        </CtxCard>
      )}
    </div>
  )
}

/** Map a typed backend error_code to a distinct, honest UI message. */
function errorCodeToMessage(code?: string, fallback?: string): { label: string; text: string } {
  switch (code) {
    case 'PROVIDER_NOT_CONFIGURED':
      return {
        label: 'AI provider not configured',
        text: fallback || 'No AI provider is configured on the backend. Set COPILOT_LLM_BACKEND (local Ollama or openai + key) and retry. No fake answer was generated.',
      }
    case 'COPILOT_DISABLED':
      return { label: 'Copilot disabled', text: fallback || 'The Copilot is disabled in backend configuration (COPILOT_ENABLED=false).' }
    case 'PROVIDER_UNAVAILABLE':
      return { label: 'AI provider unreachable', text: fallback || 'Could not reach the AI provider (connection refused / server down). This is NOT a frontend timeout.' }
    case 'PROVIDER_TIMEOUT':
      return { label: 'AI provider timed out', text: fallback || 'The AI provider accepted the request but did not answer in time. You can retry.' }
    case 'PROVIDER_AUTH_FAILED':
      return { label: 'AI provider authentication failed', text: fallback || 'The provider rejected the configured credentials. Check the API key configuration — keys are never displayed here.' }
    case 'PROVIDER_RATE_LIMITED':
      return { label: 'AI provider rate limited', text: fallback || 'The provider is rate limiting requests. Wait a moment and retry.' }
    case 'MODEL_UNAVAILABLE':
      return { label: 'AI model unavailable', text: fallback || 'The configured model is not available on the provider (not downloaded/loaded?).' }
    case 'BACKEND_EXCEPTION':
      return { label: 'Copilot backend error', text: fallback || 'The Copilot backend hit an unexpected error. Check backend logs.' }
    case 'REQUEST_CANCELLED':
      return { label: 'Cancelled', text: 'Generation cancelled.' }
    default:
      return { label: 'Request failed', text: fallback || 'The request failed. Retry.' }
  }
}

function classifyNetworkError(e: unknown): string {
  if (axios.isAxiosError(e)) {
    if (e.code === 'ECONNABORTED') return 'Network request timed out before the backend responded (this is a connection problem, not an AI failure).'
    if (!e.response) return 'Cannot reach the backend (network/backend offline).'
    const detail = (e.response.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string') return detail
    return `Backend error ${e.response.status}.`
  }
  return e instanceof Error ? e.message : 'Unknown error.'
}

export default function Copilot() {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [sessionId] = useState(() => {
    const existing = sessionStorage.getItem('copilot_session_id')
    if (existing) return existing
    const id = crypto.randomUUID()
    sessionStorage.setItem('copilot_session_id', id)
    return id
  })
  const [provider, setProvider] = useState<{ configured: boolean; backend: string; model: string; enabled: boolean } | null>(null)
  const [providerError, setProviderError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollFailuresRef = useRef(0)
  const activeJobRef = useRef<string | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)

  const generating = messages.some(
    m => m.role === 'assistant' && (m.state === 'sending' || m.state === 'thinking' || m.state === 'streaming'),
  )

  useEffect(() => {
    api.get('/api/copilot/status')
      .then(r => setProvider({
        configured: !!r.data.provider_configured,
        backend: r.data.llm_backend ?? 'none',
        model: r.data.model ?? '',
        enabled: !!r.data.enabled,
      }))
      .catch(() => setProviderError('Could not load Copilot status from the backend.'))
    return () => { if (pollRef.current) clearTimeout(pollRef.current) }
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages])

  const pollJob = useCallback((jobId: string, onDone: () => void) => {
    const tick = async () => {
      try {
        const r = await api.get(`/api/copilot/chat/status/${jobId}`)
        pollFailuresRef.current = 0
        const d = r.data
        setMessages(prev => prev.map(m => {
          if (m.id !== jobId) return m
          if (d.status === 'completed') {
            return { ...m, state: 'completed', text: d.answer ?? '', contextKeys: d.resolved_context ? Object.keys(d.resolved_context).filter((k: string) => !k.startsWith('_')) : [] }
          }
          if (d.status === 'failed') {
            return { ...m, state: 'failed', errorCode: d.error_code, errorMessage: d.error }
          }
          if (d.status === 'cancelled') {
            return { ...m, state: 'cancelled', errorCode: 'REQUEST_CANCELLED' }
          }
          if (d.status === 'cancelling') {
            return { ...m, state: 'thinking', text: 'Cancelling…' }
          }
          return { ...m, state: 'thinking' }
        }))
        if (d.status === 'completed' || d.status === 'failed' || d.status === 'cancelled') {
          activeJobRef.current = null
          onDone()
          return
        }
        pollRef.current = setTimeout(tick, POLL_INTERVAL_MS)
      } catch (e) {
        // Transient poll failures are retried — never instantly fatal.
        pollFailuresRef.current += 1
        if (pollFailuresRef.current >= POLL_MAX_CONSECUTIVE_FAILURES) {
          const message = classifyNetworkError(e)
          setMessages(prev => prev.map(m => m.id === jobId
            ? { ...m, state: 'failed', errorCode: 'NETWORK', errorMessage: `${message} The request may still be processing server-side.` }
            : m))
          activeJobRef.current = null
          onDone()
          return
        }
        pollRef.current = setTimeout(tick, POLL_INTERVAL_MS * (pollFailuresRef.current + 1))
      }
    }
    pollFailuresRef.current = 0
    activeJobRef.current = jobId
    tick()
  }, [])

  const send = useCallback(async (text: string) => {
    const q = text.trim()
    if (!q || generating) return
    setInput('')
    const userId = crypto.randomUUID()
    setMessages(prev => [...prev, { id: userId, role: 'user', text: q }])
    // Assistant placeholder while the submit request is in flight. Its id is
    // OPTIMISTIC — it is replaced by the SERVER's job_id below so that
    // pollJob (which maps by the backend's job_id) can match this message.
    // Using a fixed temp prefix (instead of crypto.randomUUID()) avoids any
    // collision with a real backend job id.
    const placeholderId = `temp-${crypto.randomUUID()}`
    setMessages(prev => [...prev, { id: placeholderId, role: 'assistant', text: '', state: 'thinking' }])
    let jobId = placeholderId
    try {
      const r = await api.post('/api/copilot/chat/submit', { question: q, session_id: sessionId })
      const d = r.data
      if (d.job_id) {
        jobId = d.job_id
        // Re-key the placeholder to the server's job id — pollJob matches
        // messages by this exact id, so the answer renders on completion.
        setMessages(prev => prev.map(m => m.id === placeholderId ? { ...m, id: jobId } : m))
        pollJob(d.job_id, () => {})
      } else {
        // Immediate typed failure (disabled / not configured).
        setMessages(prev => prev.map(m => m.id === jobId
          ? { ...m, state: 'failed', errorCode: d.error_code, errorMessage: d.error }
          : m))
      }
    } catch (e) {
      setMessages(prev => prev.map(m => m.id === jobId
        ? { ...m, state: 'failed', errorCode: 'NETWORK', errorMessage: classifyNetworkError(e) }
        : m))
    }
  }, [generating, pollJob, sessionId])

  const cancel = useCallback(async () => {
    if (!activeJobRef.current || cancelling) return
    setCancelling(true)
    try {
      await api.post(`/api/copilot/chat/status/${activeJobRef.current}/cancel`)
      // Polling observes the authoritative 'cancelled' status.
    } catch {
      // Polling will surface the real state; cancellation is best-effort.
    } finally {
      setCancelling(false)
    }
  }, [cancelling])

  const retry = useCallback((msg: ChatMsg) => {
    if (generating) return
    const idx = messages.findIndex(m => m.id === msg.id)
    const userMsg = [...messages.slice(0, idx)].reverse().find(m => m.role === 'user')
    setMessages(prev => prev.filter(m => m.id !== msg.id))
    if (userMsg) void send(userMsg.text)
  }, [generating, messages, send])

  const clear = useCallback(() => {
    if (pollRef.current) clearTimeout(pollRef.current)
    activeJobRef.current = null
    setMessages([])
    sessionStorage.removeItem('copilot_session_id')
  }, [])

  const statusChip = () => {
    if (providerError) return <span className="flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full bg-red-950/40 border border-red-800/50 text-red-300"><AlertTriangle size={11} /> backend offline</span>
    if (!provider) return <span className="text-[11px] text-slate-500">checking…</span>
    if (!provider.enabled) return <span className="flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full bg-slate-800/70 border border-slate-700 text-slate-300"><Ban size={11} /> copilot disabled</span>
    if (!provider.configured) return <span className="flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full bg-amber-950/40 border border-amber-800/50 text-amber-300"><Wrench size={11} /> no provider configured</span>
    return (
      <span className="flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full bg-emerald-950/40 border border-emerald-800/50 text-emerald-300">
        <Cpu size={11} /> {provider.backend} · {provider.model || 'default model'}
      </span>
    )
  }

  const renderMessage = (m: ChatMsg) => {
    if (m.role === 'user') {
      return (
        <div key={m.id} className="flex justify-end">
          <div className="max-w-[85%] bg-blue-600/20 border border-blue-600/40 rounded-xl px-3.5 py-2.5 text-sm text-blue-100 whitespace-pre-wrap">
            {m.text}
          </div>
        </div>
      )
    }
    const failed = m.state === 'failed'
    const cancelled = m.state === 'cancelled'
    const thinking = m.state === 'thinking' || m.state === 'streaming' || m.state === 'sending'
    const typed = m.errorCode ? errorCodeToMessage(m.errorCode, m.errorMessage) : null
    return (
      <div key={m.id} className="flex justify-start">
        <div className={`max-w-[85%] rounded-xl px-3.5 py-2.5 text-sm border ${
          failed ? 'bg-red-950/30 border-red-800/50 text-red-200'
          : cancelled ? 'bg-slate-900/50 border-slate-700 text-slate-300'
          : 'bg-[#141b2d] border-[#1e2d45] text-slate-200'
        }`}>
          {failed && typed && (
            <div className="font-semibold text-xs mb-1 flex items-center gap-1.5"><AlertTriangle size={12} /> {typed.label}</div>
          )}
          {failed && <div className="text-xs leading-relaxed">{typed?.text}</div>}
          {failed && (
            <button onClick={() => retry(m)}
              className="mt-2 flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-red-700/50 text-red-300 hover:bg-red-950/50">
              <RotateCcw size={11} /> Retry
            </button>
          )}
          {cancelled && (
            <div className="flex items-center gap-1.5 text-xs"><Ban size={12} /> Generation cancelled. <button onClick={() => retry(m)} className="underline hover:text-slate-100">Retry</button></div>
          )}
          {thinking && (
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <Loader2 size={13} className="animate-spin" /> Thinking…
              <button onClick={cancel} disabled={cancelling}
                className="ml-2 flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border border-slate-600 text-slate-300 hover:bg-[#1a2235] disabled:opacity-50">
                <Square size={10} /> Stop
              </button>
            </div>
          )}
          {!failed && !cancelled && !thinking && (
            <div className="whitespace-pre-wrap leading-relaxed">{m.text}</div>
          )}
          {m.contextKeys && m.contextKeys.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {m.contextKeys.map(k => (
                <span key={k} className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-[#0f1628] border border-[#1e2d45] text-slate-500">{k.replace(/_/g, ' ')}</span>
              ))}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4 h-full flex flex-col">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white flex items-center gap-2"><Bot size={18} className="text-cyan-400" /> Copilot AI</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Grounded in live bot state, trades, and backtests. Observation &amp; explanation only — it never places orders or changes settings.
          </p>
        </div>
        {statusChip()}
      </div>

      {provider && provider.enabled && !provider.configured && (
        <div className="bg-amber-950/20 border border-amber-800/40 rounded-lg p-3 text-xs text-amber-300">
          No AI provider is configured on the backend. Set <code className="font-mono">COPILOT_LLM_BACKEND</code>
          {' '}(<code className="font-mono">local_openai_compatible</code> for Ollama, or <code className="font-mono">openai</code> with its key env var)
          and restart the backend. Until then, the Copilot will show this configuration message instead of a fabricated answer.
        </div>
      )}

      <AIDecisionPanel />

      <BotContextCards />

      <div className="flex-1 min-h-0 bg-[#141b2d] border border-[#1e2d45] rounded-xl flex flex-col">
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {messages.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center text-center text-slate-500 gap-3 py-10">
              <Sparkles size={22} className="text-cyan-500/60" />
              <p className="text-sm max-w-md">Ask about the bot's status, today's paper trades, the latest rejection, or the latest backtest — answers are grounded in real project data.</p>
              <div className="flex flex-wrap justify-center gap-2 mt-1">
                {QUICK_PROMPTS.map(p => (
                  <button key={p} onClick={() => void send(p)} disabled={generating}
                    className="text-[11px] px-2.5 py-1.5 rounded-lg border border-[#243044] bg-[#0f1628] text-slate-400 hover:text-slate-200 hover:border-[#2a3a56] disabled:opacity-50">
                    {p}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map(renderMessage)}
          <div ref={bottomRef} />
        </div>

        <div className="border-t border-[#1e2d45] p-3 flex items-end gap-2">
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send(input)
              }
            }}
            placeholder={generating ? 'Waiting for the current answer…' : 'Ask about the bot, trades, rejections, backtests…'}
            rows={1}
            className="flex-1 resize-none bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50 max-h-32"
          />
          {generating ? (
            <button onClick={cancel} disabled={cancelling}
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-red-700/50 bg-red-950/30 text-red-300 text-sm font-medium hover:bg-red-950/60 disabled:opacity-50">
              <Square size={14} /> Stop
            </button>
          ) : (
            <button onClick={() => void send(input)} disabled={!input.trim()}
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium">
              <Send size={14} /> Send
            </button>
          )}
          <button onClick={clear} title="Clear conversation"
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-[#1e2d45] text-slate-400 text-sm hover:bg-[#1a2235]">
            <Trash2 size={14} />
          </button>
        </div>
      </div>

      <div className="flex items-center gap-1.5 text-[10px] text-slate-600">
        <CheckCircle2 size={10} /> Paper-mode observation assistant · no order placement · no credential access
      </div>
    </div>
  )
}
