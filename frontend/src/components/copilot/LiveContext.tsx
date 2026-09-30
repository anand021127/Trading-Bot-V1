import type { ReactNode } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import type { CopilotContext, CtxLoad } from './contextTypes'

/**
 * Compact "Live Context" — renders the ONE authoritative backend context
 * (GET /api/copilot/context) that the Copilot itself is grounded in. Nothing
 * here is hard-coded or duplicated from other endpoints. Detailed sections are
 * collapsed by default; problems (mismatches / errors / scanner or data
 * failures) are surfaced prominently ONLY when present.
 */

const fmtINR = (n: unknown): string =>
  n == null || n === '' || Number.isNaN(Number(n))
    ? '—'
    : `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`

const pct = (v: number | null | undefined, digits = 2): string =>
  v == null ? '—' : `${(v * 100).toFixed(digits)}%`

const tone = (n: unknown): string => (Number(n) >= 0 ? 'text-emerald-300' : 'text-red-300')

function Section({ id, title, badge, badgeClass, defaultOpen = false, children }: {
  id: string
  title: string
  badge?: ReactNode
  badgeClass?: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  return (
    <details data-testid={`ctx-${id}`} open={defaultOpen}
      className="group rounded-lg border border-[#1e2d45] bg-[#0f1628] min-w-0">
      <summary
        className="cursor-pointer select-none list-none px-3 min-h-[44px] lg:min-h-[36px] flex items-center justify-between gap-2 text-xs font-semibold text-slate-200 hover:bg-[#141b2d] rounded-lg">
        <span className="flex items-center gap-1.5">
          <span aria-hidden className="text-slate-500 transition-transform group-open:rotate-90">›</span>
          {title}
        </span>
        {badge != null && badge !== '' && (
          <span className={`text-[10px] font-normal truncate max-w-[55%] text-right ${badgeClass ?? 'text-slate-400'}`}>{badge}</span>
        )}
      </summary>
      <div className="px-3 pb-2.5 pt-0.5 text-[11px] text-slate-300 space-y-1 [overflow-wrap:anywhere]">{children}</div>
    </details>
  )
}

function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex justify-between gap-3 min-w-0">
      <span className="text-slate-500 shrink-0">{k}</span>
      <span className="text-right min-w-0 [overflow-wrap:anywhere]">{v}</span>
    </div>
  )
}

const Muted = ({ children }: { children: ReactNode }) => <div className="text-slate-500">{children}</div>

export default function LiveContext({ ctx, load, onRefresh }: {
  ctx: CopilotContext | null
  load: CtxLoad
  onRefresh: () => void
}) {
  if (!ctx) {
    return (
      <div data-testid="live-context" className="space-y-2">
        <div className="rounded-lg border border-[#1e2d45] bg-[#0f1628] p-3 text-xs text-slate-400">
          {load === 'error'
            ? <>
                <div className="flex items-center gap-1.5 text-red-300 font-semibold"><AlertTriangle size={12} /> Live context unavailable</div>
                <div className="mt-1">The backend did not return <code className="font-mono">/api/copilot/context</code>.</div>
                <button onClick={onRefresh} className="mt-2 inline-flex items-center gap-1 min-h-[44px] lg:min-h-[32px] px-3 rounded border border-[#243044] text-slate-300 hover:bg-[#141b2d]"><RefreshCw size={12} /> Retry</button>
              </>
            : 'Loading live context…'}
        </div>
      </div>
    )
  }

  const bot = ctx.bot ?? {}
  const cfg = ctx.configuration ?? {}
  const ws = ctx.websocket ?? {}
  const scan = ctx.scanner ?? {}
  const chain = ctx.latest_rejection?.gate_chain ?? {}
  const today = ctx.today ?? {}
  const risk = ctx.risk ?? {}
  const pos = ctx.positions ?? {}
  const trades = ctx.recent_trades ?? {}
  const bt = ctx.backtest ?? {}
  const ai = ctx.ai ?? {}
  const mism = (ctx.configuration_mismatches ?? []).filter(m => m?.message)
  const errNote = ctx.errors?.available ? ctx.errors?.note : undefined
  const recentErrors = (ctx.errors?.recent ?? []).map(String)

  const scanBad = !!scan.error ||
    scan.state_label?.toLowerCase().includes('error') === true ||
    ['STARTED_WORKER_NOT_RESPONDING', 'RUNNING_SCANNER_ERROR', 'RUNNING_DATA_ERROR'].includes(scan.scanner_status ?? '')
  const problems: string[] = [
    ...mism.map(m => m.message as string),
    ...(scanBad ? [scan.error || scan.summary || 'Scanner needs attention'] : []),
    ...recentErrors.slice(0, 3),
  ]

  const runtimeLabel = bot.kill_switch_active
    ? 'KILLED'
    : bot.running ? (bot.runtime_label ?? 'RUNNING') : 'STOPPED'
  const runtimeBad = bot.running && bot.worker_alive === false
  const v8d = chain.gates?.v8d_signal
  const marketOpen = bot.market_open == null ? '—' : bot.market_open ? 'OPEN' : 'CLOSED'
  const aiLatest = ai.latest

  return (
    <div data-testid="live-context" className="space-y-2">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-slate-500">
        <span>{load === 'error' ? 'stale — refresh failed' : ctx.generated_at ? `updated ${new Date(ctx.generated_at).toLocaleTimeString()}` : 'live'}</span>
        <button onClick={onRefresh} aria-label="Refresh live context"
          className="inline-flex items-center justify-center min-h-[44px] min-w-[44px] lg:min-h-[28px] lg:min-w-[28px] rounded text-slate-400 hover:text-slate-200 hover:bg-[#141b2d]">
          <RefreshCw size={12} />
        </button>
      </div>

      {problems.length > 0 && (
        <div role="alert" data-testid="ctx-problems"
          className="rounded-lg border border-amber-800/60 bg-amber-950/30 p-2.5 text-[11px] text-amber-200 space-y-1">
          <div className="flex items-center gap-1.5 font-semibold"><AlertTriangle size={12} /> Needs attention ({problems.length})</div>
          {problems.slice(0, 5).map((p, i) => <div key={i} className="[overflow-wrap:anywhere]">• {p}</div>)}
          {errNote && <div className="text-amber-300/70">{errNote}</div>}
        </div>
      )}

      <Section id="runtime" title="Runtime" defaultOpen badge={runtimeLabel}
        badgeClass={runtimeBad ? 'text-red-300' : bot.kill_switch_active ? 'text-red-300' : 'text-slate-300'}>
        <KV k="Mode" v={(bot.mode ?? '—').toUpperCase()} />
        <KV k="Strategy" v={bot.strategy ?? '—'} />
        <KV k="Runtime state" v={bot.runtime_label ?? (bot.running ? 'RUNNING' : 'STOPPED')} />
        <KV k="Worker" v={bot.worker_alive == null ? '—' : bot.worker_alive
          ? `alive${bot.heartbeat_age_seconds != null ? ` · hb ${Math.round(bot.heartbeat_age_seconds)}s` : ''}`
          : <span className="text-red-300">NOT RUNNING</span>} />
        <KV k="Kill switch" v={bot.kill_switch_active ? <span className="text-red-300">ACTIVE</span> : 'clear'} />
        {bot.runtime_summary && bot.running && <Muted>{bot.runtime_summary}</Muted>}
      </Section>

      <Section id="market" title="Market" badge={`${marketOpen} · ${ws.state ?? 'unknown'}`}>
        <KV k="Session" v={ctx.market?.session_status ?? marketOpen} />
        <KV k="WebSocket" v={ws.state ?? '—'} />
        <KV k="Streaming ticks" v={ws.streaming ? 'YES (real ticks)' : 'NO'} />
        <KV k="Last tick" v={ws.last_tick_age_seconds != null ? `${Math.round(ws.last_tick_age_seconds)}s ago` : '—'} />
        <KV k="Market data" v={ws.market_data_status ?? '—'} />
        {ws.health_interpretation && <Muted>{ws.health_interpretation}</Muted>}
      </Section>

      <Section id="scanner" title="Scanner" badge={scan.available ? (scan.state_label ?? scan.scanner_status ?? 'running') : 'no data'}
        badgeClass={scanBad ? 'text-amber-300' : 'text-slate-400'}>
        {scan.available
          ? <>
              {scan.summary && <div className="text-slate-200">{scan.summary}</div>}
              <KV k="Scan #" v={scan.scan_seq != null ? `${scan.scan_seq}${scan.last_scan_ist ? ` · ${scan.last_scan_ist}` : ''}` : '—'} />
              <KV k="Last scan" v={scan.last_scan_seconds_ago != null ? `${Math.round(scan.last_scan_seconds_ago)}s ago` : '—'} />
              <KV k="Result" v={scan.worker_last_scan_reason ?? '—'} />
              <KV k="Candles" v={scan.candle_count != null ? `${scan.candle_count}${scan.candle_age_seconds != null ? ` · ${Math.round(scan.candle_age_seconds)}s old` : ''}` : '—'} />
              <KV k="Option chain" v={scan.expiry ? `${scan.expiry} · ${scan.option_chain_count ?? '—'} contracts` : '—'} />
              {scan.error && <div className="text-red-300">error: {scan.error}</div>}
            </>
          : <Muted>{scan.reason || 'No scanner state available.'}</Muted>}
      </Section>

      <Section id="why" title="Why didn't we trade?" defaultOpen
        badge={chain.available === false ? 'no scan' : (chain.stage ?? '—')}>
        {chain.available === false
          ? <Muted>{chain.reason || ctx.latest_rejection?.reason || 'No scan has been recorded since bot startup.'}</Muted>
          : <>
              <KV k="Stage" v={chain.stage ?? '—'} />
              <KV k="V8-D result" v={v8d?.status ?? chain.signal ?? '—'} />
              {chain.scan_reason && <KV k="Rejection" v={chain.scan_reason} />}
              <KV k="Outcome" v={chain.traded ? 'TRADED (paper)' : 'NO TRADE'} />
              {chain.human_summary && <div className="text-slate-200">{chain.human_summary}</div>}
              {chain.age_seconds != null && <Muted>recorded {Math.round(chain.age_seconds)}s ago</Muted>}
            </>}
      </Section>

      <Section id="risk" title="Risk" badge={risk.available ? `${risk.trades_used ?? 0}/${risk.max_trades ?? '—'} trades · ${fmtINR(risk.daily_pnl ?? today.realized_pnl)}` : '—'}>
        {risk.available
          ? <>
              <KV k="Trades used" v={`${risk.trades_used ?? 0} / ${risk.max_trades ?? '—'}`} />
              <KV k="Daily P&L" v={<span className={tone(risk.daily_pnl ?? 0)}>{fmtINR(risk.daily_pnl)}</span>} />
              {risk.daily_loss_used_pct != null && <KV k="Daily loss used" v={`${risk.daily_loss_used_pct}%`} />}
              {risk.consecutive_losses != null && <KV k="Consecutive losses" v={risk.consecutive_losses} />}
              <KV k="Risk state" v={risk.stop_reason ? <span className="text-amber-300">{risk.stop_reason}</span> : (risk.state ?? 'within limits')} />
              {risk.note && <Muted>{risk.note}</Muted>}
            </>
          : <Muted>{risk.reason || 'Risk state unavailable.'}</Muted>}
        {today.available && <KV k="Today" v={`${today.wins ?? 0}W · ${today.losses ?? 0}L`} />}
      </Section>

      <Section id="positions" title="Positions" badge={pos.available ? `${pos.count ?? 0} open` : '—'}>
        {pos.available
          ? ((pos.count ?? 0) > 0
              ? (pos.positions ?? []).map((p, i) => (
                  <div key={i}>{p.underlying ?? p.symbol} {p.strike ?? ''} {p.option_type ?? ''} · qty {p.quantity} @ {fmtINR(p.average_price)}</div>))
              : <Muted>No open positions.</Muted>)
          : <Muted>{pos.reason || 'Positions unavailable.'}</Muted>}
      </Section>

      <Section id="trades" title="Recent trades" badge={trades.available ? `last ${trades.count ?? 0}` : '—'}>
        {trades.available
          ? ((trades.count ?? 0) > 0
              ? (trades.trades ?? []).slice(0, 5).map((t, i) => (
                  <div key={t.trade_id ?? i} className="flex justify-between gap-2">
                    <span className="truncate">{t.underlying ?? t.symbol ?? '—'} {t.strike ?? ''} {t.option_type ?? ''}</span>
                    <span className={tone(t.net_pnl)}>{fmtINR(t.net_pnl)}</span>
                  </div>))
              : <Muted>No trades stored yet.</Muted>)
          : <Muted>{trades.reason || 'Trades unavailable.'}</Muted>}
      </Section>

      <Section id="backtest" title="Backtest" badge={bt.available ? `${bt.summary?.trades ?? '—'} trades · ${fmtINR(bt.summary?.net_pnl)}` : 'none stored'}>
        {bt.available
          ? <>
              <KV k="Period" v={`${bt.config?.start_date ?? '—'} → ${bt.config?.end_date ?? '—'}`} />
              <KV k="Trades" v={bt.summary?.trades ?? '—'} />
              <KV k="Net P&L" v={<span className={tone(bt.summary?.net_pnl)}>{fmtINR(bt.summary?.net_pnl)}</span>} />
              <KV k="Win rate" v={bt.summary?.win_rate_pct != null ? `${bt.summary.win_rate_pct}%` : '—'} />
              <KV k="Profit factor" v={bt.summary?.profit_factor ?? '—'} />
              <KV k="Max drawdown" v={bt.summary?.max_drawdown_pct != null ? `${bt.summary.max_drawdown_pct}%` : '—'} />
              <Muted>Latest stored result — never hard-coded.</Muted>
            </>
          : <Muted>{bt.reason || 'No completed backtest result is stored.'}</Muted>}
      </Section>

      <Section id="config" title="Configuration" badge={`${fmtINR(cfg.capital?.starting_capital)} · ${cfg.risk?.max_trades_per_day ?? '—'}/day`}>
        <KV k="Capital" v={<>{fmtINR(cfg.capital?.starting_capital)} <span className="text-slate-500">({cfg.capital?.source ?? '—'})</span></>} />
        <KV k="Equity" v={fmtINR(cfg.capital?.current_equity)} />
        <KV k="Risk / trade" v={pct(cfg.risk?.max_risk_per_trade_pct)} />
        <KV k="Daily loss limit" v={pct(cfg.risk?.max_daily_loss_pct)} />
        <KV k="Max trades / day" v={<>{cfg.risk?.max_trades_per_day ?? '—'} <span className="text-slate-500">({cfg.risk?.max_trades_source ?? '—'})</span></>} />
        <KV k="Strategy" v={cfg.strategy ?? bot.strategy ?? '—'} />
      </Section>

      <Section id="ai" title="AI Trading Decision engine"
        badge={ai.available ? (ai.enabled ? 'ENABLED' : 'DISABLED') : 'unknown'}>
        {ai.available
          ? <>
              <KV k="Engine" v={ai.enabled ? 'enabled' : 'disabled'} />
              <KV k="Model" v={`${ai.provider ?? '—'} / ${ai.model ?? '—'}`} />
              {aiLatest?.available
                ? <>
                    <KV k="Latest decision" v={`${aiLatest.decision ?? '—'}${aiLatest.symbol ? ` · ${aiLatest.symbol}` : ''}`} />
                    <KV k="Confidence" v={aiLatest.confidence != null ? `${aiLatest.confidence}% (its own analysis, not P(profit))` : '—'} />
                    <KV k="Reason codes" v={(aiLatest.reason_codes ?? []).join(', ') || '—'} />
                    <KV k="Latency" v={aiLatest.latency_ms != null ? `${Math.round(aiLatest.latency_ms)}ms` : '—'} />
                  </>
                : <Muted>{aiLatest?.reason ?? 'No AI decision persisted yet.'}</Muted>}
              <Muted>Gates V8-D BUYs before hard risk — separate from the Copilot provider answering chat.</Muted>
            </>
          : <Muted>{ai.reason || 'AI decision state unavailable.'}</Muted>}
      </Section>
    </div>
  )
}
