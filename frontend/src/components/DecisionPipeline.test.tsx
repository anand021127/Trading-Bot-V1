import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import DecisionPipeline from './DecisionPipeline'
import type { PipelineView } from '../types/pipeline'

const base: PipelineView = {
  scanner: 'RUNNING', market: 'LIVE', strategy: 'V8_D_PULLBACK_ATM',
  latest_signal: 'NO SIGNAL', signal_detail: 'pullback/reversal criteria not met',
  ai_decision: 'NOT EVALUATED', ai_reason: 'Not consulted: no V8-D BUY signal.',
  risk_check: 'NOT EVALUATED', execution: 'NO TRADE', scan_seq: 7, scan_time_ist: '2026-10-01 10:23:15 IST',
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
    expect(val('execution')).toBe('NO TRADE')
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
    expect(val('execution')).toBe('NO TRADE')
  })

  it('AI disabled / unavailable are shown as such — never as an evaluation', () => {
    const { rerender } = render(<DecisionPipeline pipeline={{ ...base, ai_decision: 'DISABLED', ai_reason: 'AI Trading Decision engine is disabled — V8-D + risk controls only.' }} />)
    expect(val('ai-decision')).toBe('DISABLED')
    expect(screen.getByText(/engine is disabled/)).toBeInTheDocument()
    rerender(<DecisionPipeline pipeline={{ ...base, latest_signal: 'BUY CE', ai_decision: 'UNAVAILABLE — FAILED SAFE (NO TRADE)', ai_reason: 'AI_TIMEOUT' }} />)
    expect(val('ai-decision')).toBe('UNAVAILABLE — FAILED SAFE (NO TRADE)')
    expect(val('execution')).toBe('NO TRADE')
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
})
