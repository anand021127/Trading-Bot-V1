import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import axios from 'axios'
import {
  Bot, Send, Square, RotateCcw, Trash2, AlertTriangle, Ban, Sparkles,
  Wrench, Cpu, ShieldCheck, PanelRight, X, WifiOff,
} from 'lucide-react'
import { api } from '../api/client'
import LiveContext from '../components/copilot/LiveContext'
import type { CopilotContext, CtxLoad } from '../components/copilot/contextTypes'
import {
  errorCodeToMessage, fieldsFromStatus, isTerminal, stateFromSubmit,
} from './copilotJobs'
import type { JobStatusBody, MsgState, SubmitEnvelope } from './copilotJobs'

/**
 * Copilot AI — chat-first workspace.
 *
 *   Desktop (>=1024px): chat ~70% | compact, independently scrolling Live Context ~30%
 *   Mobile/tablet:      chat is the full-width workspace; Live Context is a bottom-sheet
 *
 * Async job contract (no long-lived HTTP request; see ./copilotJobs.ts):
 *   POST /api/copilot/chat/submit               -> 202 {job_id, status}   (never the answer)
 *   GET  /api/copilot/chat/status/{job_id}      -> queued|thinking|cancelling|completed|failed|cancelled
 *   POST /api/copilot/chat/status/{job_id}/cancel
 * A job may already be `completed` in the POST envelope: the answer is then fetched
 * immediately from the status endpoint — no fake "Thinking…" state. Transient poll
 * failures are retried; typed provider errors stay distinct.
 *
 * This page is observation/explanation ONLY. It never places orders, changes
 * settings, or touches bot state; the "AI Trading Decision engine" shown in the
 * context panel is a different component from the Copilot provider.
 */

interface ChatMsg {
  id: string
  role: 'user' | 'assistant'
  text: string
  state?: MsgState
  errorCode?: string
  errorMessage?: string
  contextKeys?: string[]
}

interface ProviderInfo { configured: boolean; backend: string; model: string; enabled: boolean }

const QUICK_PROMPTS = [
  "Why didn't the bot trade?",
  "Explain today's bot status",
  "Show today's trades",
  'Explain the latest rejection',
  'Explain the latest backtest',
  'What is my current risk?',
] as const

const POLL_INTERVAL_MS = 1000
const POLL_MAX_CONSECUTIVE_FAILURES = 5
const CONTEXT_POLL_MS = 15000
const SESSION_KEY = 'copilot_session_id'

const newId = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `id-${Date.now()}-${Math.random().toString(16).slice(2)}`

function classifyNetworkError(e: unknown): string {
  if (axios.isAxiosError(e)) {
    if (e.code === 'ECONNABORTED') return 'Network request timed out before the backend responded (a connection problem, not an AI failure).'
    if (!e.response) return 'Cannot reach the backend (network/backend offline).'
    const detail = (e.response.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string') return detail
    return `Backend error ${e.response.status}.`
  }
  return e instanceof Error ? e.message : 'Unknown error.'
}

function readSession(): string {
  try {
    const existing = sessionStorage.getItem(SESSION_KEY)
    if (existing) return existing
    const id = newId()
    sessionStorage.setItem(SESSION_KEY, id)
    return id
  } catch {
    return newId()
  }
}

/* ─────────────── header pieces ─────────────── */

function ProviderChip({ provider, offline }: { provider: ProviderInfo | null; offline: boolean }) {
  const base = 'inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-full border max-w-full'
  if (offline) {
    return <span data-testid="provider-chip" className={`${base} bg-red-950/40 border-red-800/50 text-red-300`}><WifiOff size={11} /> backend offline</span>
  }
  if (!provider) return <span data-testid="provider-chip" className="text-[11px] text-slate-500">checking provider…</span>
  if (!provider.enabled) {
    return <span data-testid="provider-chip" className={`${base} bg-slate-800/70 border-slate-700 text-slate-300`}><Ban size={11} /> copilot disabled</span>
  }
  if (!provider.configured) {
    return <span data-testid="provider-chip" className={`${base} bg-amber-950/40 border-amber-800/50 text-amber-300`}><Wrench size={11} /> provider not configured</span>
  }
  return (
    <span data-testid="provider-chip" className={`${base} bg-emerald-950/40 border-emerald-800/50 text-emerald-300`}
      title="The LLM provider that answers chat. Separate from the AI Trading Decision engine.">
      <Cpu size={11} /> <span className="truncate">{provider.backend} · {provider.model || 'default model'}</span>
    </span>
  )
}

/* ─────────────── page ─────────────── */

export default function Copilot() {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [sessionId, setSessionId] = useState<string>(readSession)
  const [provider, setProvider] = useState<ProviderInfo | null>(null)
  const [providerOffline, setProviderOffline] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [ctx, setCtx] = useState<CopilotContext | null>(null)
  const [ctxLoad, setCtxLoad] = useState<CtxLoad>('loading')
  const [drawerOpen, setDrawerOpen] = useState(false)

  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollFailuresRef = useRef(0)
  const activeJobRef = useRef<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const runTokenRef = useRef(0)            // invalidates in-flight work after Clear / unmount
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const closeBtnRef = useRef<HTMLButtonElement | null>(null)

  const generating = useMemo(
    () => messages.some(m => m.role === 'assistant' &&
      (m.state === 'sending' || m.state === 'thinking' || m.state === 'cancelling')),
    [messages],
  )

  /* provider status */
  useEffect(() => {
    let alive = true
    api.get('/api/copilot/status')
      .then(r => {
        if (!alive) return
        setProviderOffline(false)
        setProvider({
          configured: !!r.data.provider_configured,
          backend: r.data.llm_backend ?? 'none',
          model: r.data.model ?? '',
          enabled: !!r.data.enabled,
        })
      })
      .catch(() => { if (alive) setProviderOffline(true) })
    return () => { alive = false }
  }, [])

  /* live context — ONE fetch loop shared by the desktop panel and the mobile sheet */
  const loadContext = useCallback(() => {
    api.get('/api/copilot/context')
      .then(r => { setCtx(r.data as CopilotContext); setCtxLoad('ok') })
      .catch(() => setCtxLoad('error'))
  }, [])
  useEffect(() => {
    loadContext()
    const id = setInterval(loadContext, CONTEXT_POLL_MS)
    return () => clearInterval(id)
  }, [loadContext])

  /* cleanup */
  useEffect(() => () => {
    runTokenRef.current += 1
    if (pollRef.current) clearTimeout(pollRef.current)
    abortRef.current?.abort()
  }, [])

  /* keep the newest message in view */
  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' })
  }, [messages])

  /* auto-grow composer (1..6 lines) */
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 144)}px`
  }, [input])

  /* mobile sheet: Esc closes, focus moves in */
  useEffect(() => {
    if (!drawerOpen) return
    closeBtnRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setDrawerOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  const patchMessage = useCallback((id: string, patch: Partial<ChatMsg>) => {
    setMessages(prev => prev.map(m => (m.id === id ? { ...m, ...patch } : m)))
  }, [])

  /* poll the status endpoint until terminal; first tick is IMMEDIATE */
  const pollJob = useCallback((jobId: string, token: number) => {
    const tick = async () => {
      if (token !== runTokenRef.current) return
      try {
        const r = await api.get(`/api/copilot/chat/status/${jobId}`)
        if (token !== runTokenRef.current) return
        pollFailuresRef.current = 0
        const d = r.data as JobStatusBody
        const f = fieldsFromStatus(d)
        patchMessage(jobId, { state: f.state, text: f.text, errorCode: f.errorCode, errorMessage: f.errorMessage, contextKeys: f.contextKeys })
        if (isTerminal(d.status)) { activeJobRef.current = null; return }
        pollRef.current = setTimeout(tick, POLL_INTERVAL_MS)     // queued | thinking | cancelling
      } catch (e) {
        if (token !== runTokenRef.current) return
        if (axios.isAxiosError(e) && e.response?.status === 404) {
          patchMessage(jobId, { state: 'failed', errorCode: 'JOB_LOST' })
          activeJobRef.current = null
          return
        }
        // Transient poll failures are retried with backoff — never instantly fatal.
        pollFailuresRef.current += 1
        if (pollFailuresRef.current >= POLL_MAX_CONSECUTIVE_FAILURES) {
          patchMessage(jobId, {
            state: 'failed', errorCode: 'NETWORK',
            errorMessage: `${classifyNetworkError(e)} The request may still be processing server-side.`,
          })
          activeJobRef.current = null
          return
        }
        pollRef.current = setTimeout(tick, POLL_INTERVAL_MS * (pollFailuresRef.current + 1))
      }
    }
    pollFailuresRef.current = 0
    activeJobRef.current = jobId
    void tick()
  }, [patchMessage])

  /* submit one question; `reuseUserMsg` keeps the existing bubble (Retry) */
  const submit = useCallback(async (question: string, reuseUserMsg = false) => {
    const q = question.trim()
    if (!q) return
    const token = ++runTokenRef.current
    if (!reuseUserMsg) setMessages(prev => [...prev, { id: newId(), role: 'user', text: q }])
    // Placeholder id is optimistic; it is re-keyed to the server's job_id.
    const placeholderId = `temp-${newId()}`
    setMessages(prev => [...prev, { id: placeholderId, role: 'assistant', text: '', state: 'sending' }])
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const r = await api.post('/api/copilot/chat/submit', { question: q, session_id: sessionId }, { signal: controller.signal })
      if (token !== runTokenRef.current) return
      const env = r.data as SubmitEnvelope
      const next = stateFromSubmit(env)
      if (!env.job_id) {
        patchMessage(placeholderId, next ?? { state: 'failed' })
        return
      }
      const jobId = env.job_id
      // Re-key so pollJob (which matches by the backend's job_id) can find it.
      setMessages(prev => prev.map(m => (m.id === placeholderId
        ? { ...m, id: jobId, ...(next ?? {}) }        // next===null (already terminal): keep 'sending', NO fake Thinking
        : m)))
      pollJob(jobId, token)
    } catch (e) {
      if (token !== runTokenRef.current) return
      if (axios.isCancel(e) || (e instanceof Error && e.name === 'CanceledError')) {
        patchMessage(placeholderId, { state: 'cancelled', errorCode: 'REQUEST_CANCELLED' })
        return
      }
      patchMessage(placeholderId, { state: 'failed', errorCode: 'NETWORK', errorMessage: classifyNetworkError(e) })
    } finally {
      if (abortRef.current === controller) abortRef.current = null
    }
  }, [patchMessage, pollJob, sessionId])

  const send = useCallback((text: string) => {
    if (generating || !text.trim()) return
    setInput('')
    void submit(text)
  }, [generating, submit])

  const stop = useCallback(async () => {
    if (cancelling) return
    const jobId = activeJobRef.current
    if (!jobId) {
      // POST still in flight (no job id yet): abort the request itself.
      abortRef.current?.abort()
      return
    }
    setCancelling(true)
    patchMessage(jobId, { state: 'cancelling' })
    try {
      await api.post(`/api/copilot/chat/status/${jobId}/cancel`)   // polling reports the authoritative state
    } catch {
      /* best-effort: polling surfaces the real state */
    } finally {
      setCancelling(false)
    }
  }, [cancelling, patchMessage])

  const retry = useCallback((msg: ChatMsg) => {
    if (generating) return
    const idx = messages.findIndex(m => m.id === msg.id)
    const userMsg = [...messages.slice(0, Math.max(idx, 0))].reverse().find(m => m.role === 'user')
    if (!userMsg) return
    setMessages(prev => prev.filter(m => m.id !== msg.id))
    void submit(userMsg.text, true)
  }, [generating, messages, submit])

  const clear = useCallback(() => {
    runTokenRef.current += 1                       // drop any in-flight poll / POST result
    if (pollRef.current) clearTimeout(pollRef.current)
    abortRef.current?.abort()
    activeJobRef.current = null
    setCancelling(false)
    setMessages([])
    setInput('')
    // A NEW server-side conversation: the old session id must not be reused.
    const fresh = newId()
    try { sessionStorage.setItem(SESSION_KEY, fresh) } catch { /* private mode */ }
    setSessionId(fresh)
    textareaRef.current?.focus()
  }, [])

  /* ─────────────── message rendering ─────────────── */

  const renderMessage = (m: ChatMsg) => {
    if (m.role === 'user') {
      return (
        <div key={m.id} className="flex justify-end" data-testid="msg-user">
          <div className="max-w-[88%] sm:max-w-[75%] bg-blue-600/20 border border-blue-600/40 rounded-2xl rounded-br-md px-3.5 py-2 text-sm text-blue-50 whitespace-pre-wrap [overflow-wrap:anywhere] leading-relaxed">
            {m.text}
          </div>
        </div>
      )
    }
    const failed = m.state === 'failed'
    const cancelled = m.state === 'cancelled'
    const pending = m.state === 'sending' || m.state === 'thinking' || m.state === 'cancelling'
    const typed = failed ? errorCodeToMessage(m.errorCode, m.errorMessage) : null
    return (
      <div key={m.id} className="flex justify-start gap-2" data-testid="msg-assistant" data-state={m.state}>
        <div aria-hidden className="hidden sm:flex shrink-0 w-7 h-7 mt-0.5 rounded-full bg-cyan-500/10 border border-cyan-500/30 items-center justify-center">
          <Bot size={14} className="text-cyan-400" />
        </div>
        <div className={`max-w-[92%] sm:max-w-[80%] min-w-0 [overflow-wrap:anywhere] rounded-2xl rounded-bl-md px-3.5 py-2 text-sm border ${
          failed ? 'bg-red-950/30 border-red-800/50 text-red-100'
          : cancelled ? 'bg-slate-900/50 border-slate-700 text-slate-300'
          : 'bg-[#141b2d] border-[#1e2d45] text-slate-200'}`}>
          {pending && (
            <div role="status" data-testid="thinking-indicator" className="flex items-center gap-2 text-xs text-slate-400 py-0.5">
              <span aria-hidden className="inline-flex gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400/70 animate-bounce [animation-delay:-0.3s]" />
                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400/70 animate-bounce [animation-delay:-0.15s]" />
                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400/70 animate-bounce" />
              </span>
              {m.state === 'sending' ? 'Sending…' : m.state === 'cancelling' ? 'Cancelling…' : 'Thinking…'}
            </div>
          )}
          {failed && typed && (
            <div role="alert" data-testid="msg-error" data-error-code={m.errorCode ?? ''}>
              <div className="font-semibold text-xs mb-1 flex items-center gap-1.5"><AlertTriangle size={12} /> {typed.label}</div>
              <div className="text-xs leading-relaxed">{typed.text}</div>
            </div>
          )}
          {cancelled && <div className="flex items-center gap-1.5 text-xs"><Ban size={12} /> Generation cancelled.</div>}
          {(failed || cancelled) && (
            <button onClick={() => retry(m)} aria-label="Retry this question" disabled={generating}
              className="mt-2 inline-flex items-center gap-1.5 min-h-[44px] lg:min-h-[32px] px-3 text-xs rounded-lg border border-slate-600 text-slate-200 hover:bg-[#1a2235] disabled:opacity-50">
              <RotateCcw size={12} /> Retry
            </button>
          )}
          {m.state === 'completed' && <div className="whitespace-pre-wrap leading-relaxed">{m.text}</div>}
          {m.state === 'completed' && m.contextKeys && m.contextKeys.length > 0 && (
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

  const problemCount = (ctx?.configuration_mismatches?.length ?? 0) + (ctx?.scanner?.error ? 1 : 0)

  return (
    <div data-testid="copilot-page"
      className="flex flex-col gap-2.5 min-w-0 max-w-full h-[calc(100dvh-11rem)] lg:h-[calc(100dvh-7rem)] min-h-[26rem]">

      {/* ── compact header ── */}
      <header className="shrink-0 flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
        <div className="min-w-0">
          <h1 className="text-base sm:text-lg font-bold text-white flex items-center gap-2 leading-tight">
            <Bot size={18} className="text-cyan-400 shrink-0" /> Copilot AI
          </h1>
          <p className="text-[11px] sm:text-xs text-slate-500 leading-snug">Grounded in live bot state, trades and backtests</p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 min-w-0">
          <ProviderChip provider={provider} offline={providerOffline} />
          <span className="hidden sm:inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-full border border-[#243044] text-slate-400">
            <ShieldCheck size={10} /> Observation &amp; explanation only
          </span>
          <span className="hidden sm:inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-full border border-[#243044] text-slate-400">
            <Ban size={10} /> No order placement
          </span>
          <button onClick={() => setDrawerOpen(true)} aria-haspopup="dialog" aria-expanded={drawerOpen}
            aria-controls="live-context-sheet"
            className="lg:hidden inline-flex items-center gap-1.5 min-h-[44px] px-3 rounded-lg border border-[#243044] bg-[#0f1628] text-xs text-slate-200 hover:bg-[#141b2d]">
            <PanelRight size={14} /> Context
            {problemCount > 0 && <span className="ml-0.5 min-w-[16px] h-4 px-1 rounded-full bg-amber-500 text-[10px] text-black font-bold flex items-center justify-center">{problemCount}</span>}
          </button>
        </div>
      </header>
      <p className="sm:hidden shrink-0 -mt-1 text-[10px] text-slate-500 flex items-center gap-1">
        <ShieldCheck size={10} /> Observation only · no order placement
      </p>

      {provider && provider.enabled && !provider.configured && (
        <div role="note" className="shrink-0 bg-amber-950/20 border border-amber-800/40 rounded-lg px-3 py-2 text-[11px] text-amber-300">
          No AI provider is configured. Set <code className="font-mono">COPILOT_LLM_BACKEND</code> (<code className="font-mono">local_openai_compatible</code> for Ollama, or <code className="font-mono">openai</code> with its key) and restart the backend. Until then the Copilot returns this message instead of a fabricated answer.
        </div>
      )}

      {/* ── body: chat | live context ── */}
      <div className="flex-1 min-h-0 flex gap-3">

        {/* chat workspace */}
        <section aria-label="Copilot chat" data-testid="chat-panel"
          className="flex-1 min-w-0 min-h-0 flex flex-col bg-[#141b2d] border border-[#1e2d45] rounded-xl lg:basis-[70%]">
          <div role="log" aria-live="polite" aria-relevant="additions text" aria-label="Conversation" data-testid="chat-log"
            className="flex-1 min-h-0 overflow-y-auto overscroll-contain px-3 sm:px-5 py-3 space-y-3">
            {messages.length === 0 ? (
              <div data-testid="chat-empty" className="min-h-full flex flex-col items-center justify-center text-center gap-3 py-4">
                <div className="w-10 h-10 rounded-full bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center">
                  <Sparkles size={18} className="text-cyan-400" />
                </div>
                <div>
                  <h2 className="text-sm font-semibold text-white">How can I help?</h2>
                  <p className="text-xs text-slate-500 mt-1 max-w-sm">Ask about the bot, today's paper trades, the latest rejection or backtest. Answers are grounded in real project data.</p>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-xl" data-testid="quick-prompts">
                  {QUICK_PROMPTS.map(p => (
                    <button key={p} onClick={() => send(p)} disabled={generating}
                      className="min-h-[44px] lg:min-h-[36px] px-3 py-2 text-xs text-left rounded-lg border border-[#243044] bg-[#0f1628] text-slate-300 hover:text-white hover:border-cyan-700/60 disabled:opacity-50">
                      {p}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="w-full max-w-3xl mx-auto space-y-3">{messages.map(renderMessage)}</div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* composer — pinned to the bottom of the chat panel */}
          <div className="shrink-0 border-t border-[#1e2d45] p-2 sm:p-3">
            <div className="w-full max-w-3xl mx-auto flex items-end gap-2">
              <textarea ref={textareaRef} value={input} rows={1}
                aria-label="Message Copilot" data-testid="composer-input"
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault()
                    send(input)
                  } else if (e.key === 'Escape' && generating) {
                    void stop()
                  }
                }}
                placeholder={generating ? 'Waiting for the current answer…' : 'Ask about the bot, trades, rejections, backtests…  (Enter to send, Shift+Enter for a new line)'}
                className="flex-1 min-w-0 resize-none bg-[#0f1628] border border-[#1e2d45] rounded-xl px-3 py-2.5 text-base sm:text-sm text-slate-100 placeholder:text-slate-600 focus:outline-none focus:border-blue-500/60 focus-visible:ring-1 focus-visible:ring-blue-500/50 max-h-36 leading-snug" />
              {generating ? (
                <button onClick={() => void stop()} disabled={cancelling} aria-label="Stop generating" data-testid="stop-btn"
                  className="shrink-0 inline-flex items-center justify-center gap-1.5 px-3 min-h-[44px] rounded-xl border border-red-700/50 bg-red-950/30 text-red-200 text-sm font-medium hover:bg-red-950/60 disabled:opacity-50">
                  <Square size={14} /> <span className="hidden sm:inline">Stop</span>
                </button>
              ) : (
                <button onClick={() => send(input)} disabled={!input.trim()} aria-label="Send message" data-testid="send-btn"
                  className="shrink-0 inline-flex items-center justify-center gap-1.5 px-3.5 min-h-[44px] rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:hover:bg-blue-600 text-white text-sm font-medium">
                  <Send size={14} /> <span className="hidden sm:inline">Send</span>
                </button>
              )}
              <button onClick={clear} aria-label="Clear conversation" title="Clear conversation" data-testid="clear-btn"
                disabled={messages.length === 0 && !input}
                className="shrink-0 inline-flex items-center justify-center min-h-[44px] min-w-[44px] rounded-xl border border-[#1e2d45] text-slate-400 hover:text-slate-200 hover:bg-[#1a2235] disabled:opacity-40">
                <Trash2 size={15} />
              </button>
            </div>
          </div>
        </section>

        {/* desktop: compact, independently scrolling Live Context */}
        <aside aria-label="Live context" data-testid="context-aside"
          className="hidden lg:flex lg:basis-[30%] lg:max-w-[26rem] lg:min-w-[19rem] min-h-0 flex-col">
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-1.5 px-0.5">Live context</div>
          <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain pr-1">
            <LiveContext ctx={ctx} load={ctxLoad} onRefresh={loadContext} />
          </div>
        </aside>
      </div>

      {/* mobile / tablet: collapsible bottom sheet */}
      {drawerOpen && (
        <div className="lg:hidden fixed inset-0 z-50" data-testid="context-sheet">
          <button aria-label="Close live context" tabIndex={-1} onClick={() => setDrawerOpen(false)}
            className="absolute inset-0 bg-black/60 cursor-default" />
          <div id="live-context-sheet" role="dialog" aria-modal="true" aria-label="Live context"
            className="absolute inset-x-0 bottom-0 max-h-[85dvh] flex flex-col bg-[#0d1424] border-t border-[#1e2d45] rounded-t-2xl pb-[env(safe-area-inset-bottom)]">
            <div className="shrink-0 flex items-center justify-between px-4 py-2 border-b border-[#1e2d45]">
              <span className="text-sm font-semibold text-white">Live context</span>
              <button ref={closeBtnRef} onClick={() => setDrawerOpen(false)} aria-label="Close live context"
                className="inline-flex items-center justify-center min-h-[44px] min-w-[44px] rounded-lg text-slate-300 hover:bg-[#141b2d]">
                <X size={16} />
              </button>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain p-3">
              <LiveContext ctx={ctx} load={ctxLoad} onRefresh={loadContext} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
