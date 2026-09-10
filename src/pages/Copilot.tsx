import { useEffect, useRef, useState } from 'react'
import { Bot, Send, Loader2, AlertTriangle, CheckCircle2, HelpCircle } from 'lucide-react'
import api from '../api/client'
import MetricCard from '../components/MetricCard'
import { usePolling } from '../hooks/usePolling'

interface CopilotStatus {
  enabled: boolean
  mode: string
  llm_backend: string
  min_risk_reward: number
  max_quote_age_seconds: number
}

interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  intent?: string
  usedLiveData?: boolean
  timestamp?: string
}

interface TradePlanResult {
  available: boolean
  decision?: string
  reason?: string
  analysis?: {
    direction: string
    confidence: number
    market_regime: string
    volatility: number | null
    momentum: number | null
    support: number | null
    resistance: number | null
    preferred_side: string | null
    setup_quality: number | null
    data_status: string
    data_age_seconds: number | null
    candle_timestamp: string | null
  } | null
  trade_plan?: {
    option_type: string
    strike: number | null
    expiry: string | null
    entry_price_low: number
    entry_price_high: number
    stop_loss: number
    target_1: number
    risk_reward: number | null
    open_interest: number | null
    spread_pct: number | null
    analysis_timestamp?: string
  } | null
  validation?: { approved: boolean; reasons_rejected: string[] } | null
}

const DECISION_STYLE: Record<string, string> = {
  TRADE: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  WAIT: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  SKIP: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
}

function DecisionBadge({ decision }: { decision?: string }) {
  const cls = DECISION_STYLE[decision || 'SKIP'] || DECISION_STYLE.SKIP
  return (
    <span className={`text-xs font-semibold px-2.5 py-1 rounded-full border ${cls}`}>
      {decision || 'SKIP'}
    </span>
  )
}

export default function Copilot() {
  const [status, setStatus] = useState<CopilotStatus | null>(null)
  const [symbol, setSymbol] = useState('NIFTY50')
  const [plan, setPlan] = useState<TradePlanResult | null>(null)
  const [planLoading, setPlanLoading] = useState(false)
  const [positions, setPositions] = useState<any>(null)
  const [health, setHealth] = useState<any>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  // Conversation memory session id — persisted across chat turns (and
  // across page remounts, within the same tab) so follow-ups like
  // "Which market are you analyzing?" resolve correctly. A fresh one is
  // requested from the backend on first use if none is stored yet.
  const sessionIdRef = useRef<string | null>(sessionStorage.getItem('copilot_session_id'))

  // ISSUE 4 fix: both the manual "Analyze" button and the 10s poll call
  // the same trade-plan endpoint independently. Without sequencing, a
  // slower OLDER request can resolve AFTER a newer one and overwrite
  // fresher state with stale data (the reported "confidence keeps
  // flipping" symptom). requestIdRef guarantees only the response to the
  // most-recently-ISSUED request is ever applied, regardless of which
  // network call happens to complete first.
  const requestIdRef = useRef(0)

  useEffect(() => {
    api.get<CopilotStatus>('/api/copilot/status').then(r => setStatus(r.data)).catch(() => setStatus(null))
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  async function fetchPlanFor(sym: string) {
    const myId = ++requestIdRef.current
    try {
      const r = await api.post<TradePlanResult>('/api/copilot/trade-plan', { symbol: sym })
      if (myId === requestIdRef.current) {
        setPlan(r.data)
        setLastUpdated(new Date())
      }
      // else: a newer request has since been issued — this response is
      // stale and is deliberately discarded, not applied.
    } catch {
      if (myId === requestIdRef.current) {
        setPlan({ available: false, reason: 'Request failed — is the Copilot enabled and the backend reachable?' })
      }
    }
  }

  async function refreshPlan() {
    setPlanLoading(true)
    await fetchPlanFor(symbol)
    setPlanLoading(false)
  }

  // Sensible, non-excessive auto-refresh — same 10s cadence as the
  // Overview page (a state-summary page, not a fast tape like Scanner's
  // 4s). Pulls the current TradePlan, open positions, and bot health
  // together so "last update time" reflects one consistent snapshot.
  // Uses the SAME sequencing guard as the manual Analyze button, since
  // both write to the same `plan` state.
  usePolling(async () => {
    try {
      const myId = ++requestIdRef.current
      const [planRes, posRes, diagRes] = await Promise.all([
        api.post<TradePlanResult>('/api/copilot/trade-plan', { symbol }).catch(() => null),
        api.get('/api/copilot/positions').catch(() => null),
        api.get('/api/copilot/diagnostics').catch(() => null),
      ])
      if (myId !== requestIdRef.current) return  // superseded by a newer request — discard
      if (planRes) setPlan(planRes.data)
      if (posRes) setPositions(posRes.data)
      if (diagRes) setHealth(diagRes.data)
      setLastUpdated(new Date())
    } catch {
      // a failed poll just doesn't update — the UI keeps showing the last good snapshot
    }
  }, 10000, !!status?.enabled)


  async function sendChat() {
    const question = input.trim()
    if (!question || chatLoading) return
    setMessages(m => [...m, { role: 'user', text: question }])
    setInput('')
    setChatLoading(true)
    try {
      const r = await api.post('/api/copilot/chat', { question, session_id: sessionIdRef.current })
      if (r.data.session_id) {
        sessionIdRef.current = r.data.session_id
        sessionStorage.setItem('copilot_session_id', r.data.session_id)
      }
      const ctx = r.data.resolved_context || {}
      const usedLiveData = !!(ctx.market_status || ctx.indicators || ctx.trade_plan || ctx.analysis || ctx.gap_analysis)
      setMessages(m => [...m, {
        role: 'assistant', text: r.data.answer, usedLiveData,
        timestamp: new Date().toLocaleTimeString(),
      }])
    } catch {
      setMessages(m => [...m, { role: 'assistant', text: 'Could not reach the Copilot right now.' }])
    } finally {
      setChatLoading(false)
    }
  }

  const analysis = plan?.analysis
  const tp = plan?.trade_plan

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bot size={20} className="text-indigo-400" />
          <h1 className="text-lg font-bold text-white">AI Trading Copilot</h1>
        </div>
        {status && (
          <div className="flex items-center gap-2 text-xs">
            <span className={`px-2 py-1 rounded-full border ${status.enabled
              ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
              : 'bg-slate-500/15 text-slate-400 border-slate-500/30'}`}>
              {status.enabled ? 'ENABLED' : 'DISABLED'}
            </span>
            <span className="px-2 py-1 rounded-full border bg-indigo-500/15 text-indigo-300 border-indigo-500/30 uppercase">
              {status.mode}
            </span>
            {lastUpdated && (
              <span className="text-slate-600">Updated {lastUpdated.toLocaleTimeString()}</span>
            )}
          </div>
        )}
      </div>

      {status && !status.enabled && (
        <div className="flex items-center gap-2 text-xs text-amber-400 bg-amber-500/10 border border-amber-500/25 rounded-lg px-3 py-2">
          <AlertTriangle size={14} />
          Copilot is disabled (COPILOT_ENABLED=false). Diagnostics and status still work; trade-plan/chat will say so.
        </div>
      )}

      {/* Market + opportunity summary */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <select
              value={symbol}
              onChange={e => setSymbol(e.target.value)}
              className="bg-[#0b0f1a] border border-[#1e2d45] rounded-lg text-sm text-white px-2 py-1"
            >
              {['NIFTY50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'MIDCPNIFTY', 'BANKEX'].map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <button
              onClick={refreshPlan}
              disabled={planLoading}
              className="text-xs bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 rounded-lg px-3 py-1.5 hover:bg-indigo-500/30 transition-colors disabled:opacity-50"
            >
              {planLoading ? <Loader2 size={12} className="animate-spin inline" /> : 'Analyze'}
            </button>
          </div>
          {plan && <DecisionBadge decision={plan.decision} />}
        </div>

        {(analysis?.candle_timestamp || tp?.analysis_timestamp) && (
          <div className="text-[10px] text-slate-600 flex gap-3">
            {tp?.analysis_timestamp && <span>Analysis: {new Date(tp.analysis_timestamp).toLocaleTimeString()}</span>}
            {analysis?.candle_timestamp && <span>Data: {new Date(analysis.candle_timestamp).toLocaleTimeString()}</span>}
          </div>
        )}

        {plan && !plan.available && (
          <div className="text-xs text-slate-500">{plan.reason || 'Not available.'}</div>
        )}

        {analysis?.data_status === 'STALE' && (
          <div className="flex items-center gap-2 text-xs text-red-400 bg-red-500/10 border border-red-500/25 rounded-lg px-3 py-2">
            <AlertTriangle size={14} />
            STALE DATA — indicator snapshot is {analysis.data_age_seconds != null ? `${Math.round(analysis.data_age_seconds)}s` : ''} old
            (as of {analysis.candle_timestamp || 'unknown'}). Trade decisions are blocked until fresh data is available.
          </div>
        )}

        {analysis && (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <MetricCard title="Direction" value={analysis.direction} sub={`${analysis.confidence}% confidence`} />
            <MetricCard title="Regime" value={analysis.market_regime} />
            <MetricCard title="Support" value={analysis.support != null ? analysis.support.toFixed(2) : '—'} />
            <MetricCard title="Resistance" value={analysis.resistance != null ? analysis.resistance.toFixed(2) : '—'} />
          </div>
        )}

        {tp && (
          <div className="border-t border-[#1e2d45] pt-3">
            <div className="text-[10px] text-slate-500 uppercase tracking-widest font-medium mb-2">Current Opportunity</div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <MetricCard title="CE / PE" value={tp.option_type || '—'} />
              <MetricCard title="Strike" value={tp.strike != null ? String(tp.strike) : '—'} sub={tp.expiry || undefined} />
              <MetricCard title="Entry" value={`${tp.entry_price_low.toFixed(2)}–${tp.entry_price_high.toFixed(2)}`} />
              <MetricCard title="R:R" value={tp.risk_reward != null ? `${tp.risk_reward}:1` : '—'} />
              <MetricCard title="Stop Loss" value={tp.stop_loss.toFixed(2)} valueColor="text-red-400" />
              <MetricCard title="Target" value={tp.target_1.toFixed(2)} valueColor="text-emerald-400" />
              <MetricCard title="Open Interest" value={tp.open_interest != null ? String(tp.open_interest) : '—'} />
              <MetricCard title="Spread" value={tp.spread_pct != null ? `${tp.spread_pct}%` : '—'} />
            </div>
          </div>
        )}

        {plan?.validation && (
          <div className="flex items-start gap-2 text-xs pt-2 border-t border-[#1e2d45]">
            {plan.validation.approved ? (
              <CheckCircle2 size={14} className="text-emerald-400 mt-0.5 shrink-0" />
            ) : (
              <HelpCircle size={14} className="text-slate-500 mt-0.5 shrink-0" />
            )}
            <span className="text-slate-400">
              {plan.validation.approved
                ? 'Passed deterministic validation (RiskManager, R:R, freshness, spread).'
                : `Not approved: ${plan.validation.reasons_rejected.join('; ') || plan.reason}`}
            </span>
          </div>
        )}
      </div>

      {/* Position + bot health strip */}
      <div className="grid grid-cols-2 gap-3">
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest font-medium mb-2">Position</div>
          {positions?.open_positions?.available && positions.open_positions.positions?.length > 0 ? (
            <div className="text-sm text-white">
              {positions.open_positions.positions.length} open · {positions?.account_risk?.available
                ? `risk ${JSON.stringify(positions.account_risk.daily_pnl ?? '')}` : ''}
            </div>
          ) : (
            <div className="text-xs text-slate-500">
              {positions?.open_positions?.available === false ? positions.open_positions.reason : 'No open position.'}
            </div>
          )}
        </div>
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-3">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest font-medium mb-2">Bot Health</div>
          {health ? (
            <div className={`text-sm font-medium ${
              health.overall_status === 'OK' ? 'text-emerald-400'
              : health.overall_status === 'ERROR' ? 'text-red-400' : 'text-amber-400'
            }`}>
              {health.overall_status} · {health.checked_components} components checked
            </div>
          ) : (
            <div className="text-xs text-slate-500">Not checked yet.</div>
          )}
        </div>
      </div>

      {/* Chat */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl flex flex-col h-[420px]">
        <div className="px-4 py-2.5 border-b border-[#1e2d45] text-[10px] text-slate-500 uppercase tracking-widest font-medium">
          Ask the trading assistant
        </div>
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
          {messages.length === 0 && (
            <div className="text-xs text-slate-600">
              Try: "How is the market?", "Is NIFTY bullish?", "Any trade opportunity?", "Check the complete bot."
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`flex flex-col ${m.role === 'user' ? 'items-end' : 'items-start'}`}>
              <div className={`max-w-[85%] text-sm rounded-xl px-3 py-2 whitespace-pre-wrap ${
                m.role === 'user'
                  ? 'bg-indigo-500/20 text-indigo-100 border border-indigo-500/30'
                  : 'bg-[#0b0f1a] text-slate-300 border border-[#1e2d45]'
              }`}>
                {m.text}
              </div>
              {m.role === 'assistant' && m.timestamp && (
                <div className="text-[10px] text-slate-600 mt-1 px-1">
                  {m.usedLiveData ? `Live data · ${m.timestamp}` : m.timestamp}
                </div>
              )}
            </div>
          ))}
          {chatLoading && <div className="text-xs text-slate-500 flex items-center gap-1"><Loader2 size={12} className="animate-spin" /> thinking…</div>}
        </div>
        <div className="border-t border-[#1e2d45] p-3 flex items-center gap-2">
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && sendChat()}
            placeholder="Ask the trading assistant..."
            className="flex-1 bg-[#0b0f1a] border border-[#1e2d45] rounded-lg text-sm text-white px-3 py-2 outline-none focus:border-indigo-500/50"
          />
          <button
            onClick={sendChat}
            disabled={chatLoading || !input.trim()}
            className="bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 rounded-lg p-2 hover:bg-indigo-500/30 transition-colors disabled:opacity-50"
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  )
}
