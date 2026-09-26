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
