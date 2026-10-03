import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ScannerEntry } from '../types'

vi.mock('../hooks/usePolling', () => ({ usePolling: () => ({ data: null, loading: false, error: null, refresh: () => {} }) }))
vi.mock('../api/client', () => ({ default: { get: vi.fn() }, api: { get: vi.fn() } }))

import { ScannerRow } from './Scanner'

const entry: ScannerEntry = {
  symbol: 'BANKNIFTY', ltp: 52000, scanned_at: '', ema_status: 'FAILED', rsi_value: 43.0394, rsi_status: 'FAILED',
  atr: null, volume_status: 'NOT_USED', trend: '', decision: 'NO TRADE', signal: 'NONE', confidence: 0,
  rejected_reasons: [], strategy_breakdown: [], error: null,
  ema20: 21624.69, ema50: 21645.9, ema_separation_pct: -0.098, candle_count: 120, v8d_failed: ['trend'],
  indicator_note: 'volume is not part of V8-D',
}

describe('Option Scanner row', () => {
  it('shows the real EMA/RSI values and an honest "Not used" for volume (never N/A everywhere)', async () => {
    render(<ScannerRow entry={entry} />)
    expect(screen.getByTestId('scanner-ema-values')).toHaveTextContent('21625/21646')
    expect(screen.getByText(/RSI 43\.0/)).toBeInTheDocument()
    expect(screen.getByText('Not used')).toBeInTheDocument()
    expect(screen.queryAllByText('N/A')).toHaveLength(0)
    await userEvent.setup().click(screen.getByRole('button'))
    expect(screen.getByTestId('scanner-v8d-detail')).toHaveTextContent('-0.098%')
    expect(screen.getByTestId('scanner-v8d-detail')).toHaveTextContent('failed: trend')
  })

  it('stays N/A (honestly) when the backend has no indicator values', () => {
    render(<ScannerRow entry={{ ...entry, ema_status: 'N/A', rsi_status: 'N/A', rsi_value: null, ema20: null, ema50: null,
      ema_separation_pct: null, v8d_failed: [], indicator_note: 'V8-D indicators unavailable for this scan' }} />)
    expect(screen.getAllByText('N/A').length).toBe(2)
    expect(screen.getByTestId('scanner-ema-values')).toHaveTextContent('EMA —')
  })
})
