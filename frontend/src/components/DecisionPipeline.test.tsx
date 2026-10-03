import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import DecisionPipeline from './DecisionPipeline'
import type { PipelineView } from '../types/pipeline'
import fixture from '../test/fixtures/pipeline_no_signal.json'

const base: PipelineView = {
  scanner: 'RUNNING', market: 'LIVE', strategy: 'V8_D_PULLBACK_ATM',
  latest_signal: 'NO SIGNAL', signal_detail: 'pullback/reversal criteria not met',
  ai_decision: 'NOT EVALUATED', ai_reason: 'Not consulted: no V8-D BUY signal.',
  risk_check: 'NOT EVALUATED', execution: 'NOT ATTEMPTED', final: 'NO TRADE', outcome: 'NO_SIGNAL', scan_seq: 7, scan_time_ist: '2026-10-01 10:23:15 IST',
  summary: 'Scanner ran at 10:23:15 IST. V8-D evaluated successfully. No signal because pullback/reversal criteria were not met.',
}
const val = (k: string) => screen.getByTestId(`pipe-${k}`).textContent

describe('DecisionPipeline', () => {
  it('shows every required row for a NO SIGNAL scan, with the actual reason', () => {
    render(<DecisionPipeline pipeline={base} />)
    expect(val('scanner')).toBe('RUNNING')
    expect(val('market')).toBe('LIVE')
    expect(val('strategy')).toBe('V8_D_PULLBACK_ATM')
    expect(val('latest-signal')).toBe('NO SIGNAL')
    expect(screen.getByText('pullback/reversal criteria not met')).toBeInTheDocument()
    expect(val('ai-decision')).toBe('NOT EVALUATED')
    expect(val('risk-check')).toBe('NOT EVALUATED')
    expect(val('execution')).toBe('NOT ATTEMPTED')
    expect(screen.getByTestId('pipe-summary')).toHaveTextContent('V8-D evaluated successfully')
  })

  it('MARKET CLOSED is stated, never disguised as a strategy result', () => {
    render(<DecisionPipeline pipeline={{ ...base, market: 'MARKET CLOSED', latest_signal: 'NOT EVALUATED — MARKET CLOSED',
      ai_reason: 'Not consulted: market closed.' }} />)
    expect(val('market')).toBe('MARKET CLOSED')
    expect(val('latest-signal')).toBe('NOT EVALUATED — MARKET CLOSED')
    expect(val('ai-decision')).toBe('NOT EVALUATED')
  })

  it('V8-D BUY rejected by the AI shows AI REJECTED + the reason, risk not evaluated, no trade', () => {
    render(<DecisionPipeline pipeline={{ ...base, latest_signal: 'BUY CE', signal_detail: 'V8-D produced a BUY signal.',
      ai_decision: 'REJECTED', ai_reason: 'POOR_RISK_REWARD — spread too wide', ai_confidence: 71, ai_latency_ms: 812,
      risk_check: 'NOT EVALUATED', risk_detail: 'Stopped earlier by the AI gate.' }} />)
    expect(val('latest-signal')).toBe('BUY CE')
    expect(val('ai-decision')).toBe('REJECTED')
    expect(screen.getByText(/POOR_RISK_REWARD — spread too wide · confidence 71 · 812 ms/)).toBeInTheDocument()
    expect(val('risk-check')).toBe('NOT EVALUATED')
    expect(val('execution')).toBe('NOT ATTEMPTED')
  })

  it('AI disabled / unavailable are shown as such — never as an evaluation', () => {
    const { rerender } = render(<DecisionPipeline pipeline={{ ...base, ai_decision: 'DISABLED', ai_reason: 'AI Trading Decision engine is disabled — V8-D + risk controls only.' }} />)
    expect(val('ai-decision')).toBe('DISABLED')
    expect(screen.getByText(/engine is disabled/)).toBeInTheDocument()
    rerender(<DecisionPipeline pipeline={{ ...base, latest_signal: 'BUY CE', ai_decision: 'UNAVAILABLE — FAILED SAFE (NO TRADE)', ai_reason: 'AI_TIMEOUT' }} />)
    expect(val('ai-decision')).toBe('UNAVAILABLE — FAILED SAFE (NO TRADE)')
    expect(val('execution')).toBe('NOT ATTEMPTED')
  })

  it('approved + pass + filled is shown as a paper fill', () => {
    render(<DecisionPipeline pipeline={{ ...base, latest_signal: 'BUY CE', ai_decision: 'APPROVED', risk_check: 'PASS', execution: 'FILLED (PAPER)' }} />)
    expect(val('ai-decision')).toBe('APPROVED')
    expect(val('risk-check')).toBe('PASS')
    expect(val('execution')).toBe('FILLED (PAPER)')
  })

  it('risk rejection after an AI approval is visible (AI cannot override risk)', () => {
    render(<DecisionPipeline pipeline={{ ...base, latest_signal: 'BUY CE', ai_decision: 'APPROVED', risk_check: 'REJECTED',
      risk_detail: 'kill_switch=STOP_NEW_ENTRIES', execution: 'REJECTED', execution_detail: 'kill_switch=STOP_NEW_ENTRIES' }} />)
    expect(val('ai-decision')).toBe('APPROVED')
    expect(val('risk-check')).toBe('REJECTED')
    expect(val('execution')).toBe('REJECTED')
  })

  it('degrades honestly when the backend reports nothing', () => {
    render(<DecisionPipeline pipeline={null} />)
    expect(screen.getByTestId('decision-pipeline')).toHaveTextContent('unavailable')
  })

  it('shows the final state NO TRADE and the outcome', () => {
    render(<DecisionPipeline pipeline={base} />)
    expect(val('final')).toBe('NO TRADE')
    expect(screen.getByTestId('pipe-outcome')).toHaveTextContent('NO_SIGNAL')
  })

  it('SIGNAL_REJECTED is a different state from NO_SIGNAL', () => {
    render(<DecisionPipeline pipeline={{ ...base, outcome: 'SIGNAL_REJECTED', latest_signal: 'SIGNAL REJECTED (BY V8-D)',
      signal_detail: 'No ATM CE contract', ai_reason: 'Not consulted: V8-D rejected the signal first.', risk_detail: 'Stopped earlier by V8-D.' }} />)
    expect(val('latest-signal')).toBe('SIGNAL REJECTED (BY V8-D)')
    expect(screen.getByTestId('pipe-outcome')).toHaveTextContent('SIGNAL_REJECTED')
    expect(val('ai-decision')).toBe('NOT EVALUATED')
    expect(val('execution')).toBe('NOT ATTEMPTED')
  })

  it('renders the backend-generated NO_SIGNAL fixture value for value (backend == frontend)', () => {
    const p = fixture as unknown as PipelineView
    render(<DecisionPipeline pipeline={p} />)
    expect(val('scanner')).toBe(p.scanner)
    expect(val('latest-signal')).toBe('NO SIGNAL')
    expect(val('ai-decision')).toBe('NOT EVALUATED')
    expect(val('risk-check')).toBe('NOT EVALUATED')
    expect(val('execution')).toBe('NOT ATTEMPTED')
    expect(val('final')).toBe('NO TRADE')
    expect(screen.getByTestId('pipe-outcome')).toHaveTextContent('NO_SIGNAL')
    const v = p.v8d!
    expect(screen.getByTestId('v8d-ema20')).toHaveTextContent(v.ema20!.toFixed(2))
    expect(screen.getByTestId('v8d-ema50')).toHaveTextContent(v.ema50!.toFixed(2))
    expect(screen.getByTestId('v8d-rsi')).toHaveTextContent(v.rsi!.toFixed(2))
    expect(screen.getByTestId('v8d-sep')).toHaveTextContent(v.ema_separation_pct!.toFixed(3))
    expect(screen.getByTestId('v8d-failed')).toHaveTextContent(v.failed!.join(', '))
    expect(screen.getByTestId('v8d-row-CE')).toBeInTheDocument()
    expect(screen.getByTestId('v8d-row-PE')).toBeInTheDocument()
    expect(screen.getByTestId('pipe-summary')).toHaveTextContent('V8-D evaluated successfully')
    // the pass/fail marks equal the backend's per-condition booleans
    const rowPE = screen.getByTestId('v8d-row-PE')
    const marks = Array.from(rowPE.querySelectorAll('[aria-label]')).map(n => n.getAttribute('aria-label'))
    const expected = (['trend', 'pullback', 'rsi', 'reversal'] as const).map(k => {
      const c = v.pe![k]; const ok = typeof c === 'boolean' ? c : c.pass
      return ok ? 'pass' : 'fail'
    })
    expect(marks).toEqual(expected)
    // coverage table lists the scanned symbol with the same binding condition
    expect(screen.getByTestId('cov-NIFTY50')).toHaveTextContent('NO_SIGNAL')
    expect(screen.getByTestId('cov-NIFTY50')).toHaveTextContent(p.symbols![0].binding!)
  })

  it('lists every scanned symbol, including one with a data error', () => {
    render(<DecisionPipeline pipeline={{ ...base, primary_symbol: 'NIFTY50', symbols: [
      { symbol: 'NIFTY50', outcome: 'NO_SIGNAL', binding: 'trend', rsi: 44.12, sep_pct: -0.1 },
      { symbol: 'SENSEX', outcome: 'DATA_ERROR', error: 'candle_fetch_error' },
      { symbol: 'BANKNIFTY', outcome: 'FILLED' },
    ] }} />)
    expect(screen.getByTestId('cov-SENSEX')).toHaveTextContent('DATA_ERROR')
    expect(screen.getByTestId('cov-BANKNIFTY')).toHaveTextContent('FILLED')
    expect(screen.getByText('Symbols scanned (3)')).toBeInTheDocument()
  })

  it('says so when V8-D was not evaluated instead of inventing values', () => {
    render(<DecisionPipeline pipeline={{ ...base, v8d: { evaluated: false, reason: 'insufficient_candles:12<50' } }} />)
    expect(screen.getByTestId('v8d-detail')).toHaveTextContent('V8-D not evaluated: insufficient_candles:12<50')
  })
})
