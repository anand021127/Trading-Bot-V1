import { describe, expect, it } from 'vitest'
import {
  errorCodeToMessage, fieldsFromStatus, isInFlight, isTerminal, stateFromSubmit,
} from './copilotJobs'

describe('job status classification', () => {
  it('keeps polling for queued/thinking/cancelling and stops for terminal states', () => {
    for (const s of ['queued', 'thinking', 'cancelling']) {
      expect(isInFlight(s)).toBe(true)
      expect(isTerminal(s)).toBe(false)
    }
    for (const s of ['completed', 'failed', 'cancelled']) {
      expect(isInFlight(s)).toBe(false)
      expect(isTerminal(s)).toBe(true)
    }
    expect(isInFlight(undefined)).toBe(false)
    expect(isTerminal('bogus')).toBe(false)
  })
})

describe('stateFromSubmit', () => {
  it('shows Thinking only for a genuinely in-flight job', () => {
    expect(stateFromSubmit({ job_id: 'j', status: 'queued' })).toEqual({ state: 'thinking' })
    expect(stateFromSubmit({ job_id: 'j', status: 'thinking' })).toEqual({ state: 'thinking' })
    expect(stateFromSubmit({ job_id: 'j', status: 'cancelling' })).toEqual({ state: 'cancelling' })
  })
  it('returns null for an already-completed job so the caller fetches the answer immediately (no fake Thinking)', () => {
    expect(stateFromSubmit({ job_id: 'j', status: 'completed' })).toBeNull()
    expect(stateFromSubmit({ job_id: 'j', status: 'failed' })).toBeNull()
  })
  it('turns a job-less envelope into a typed failure', () => {
    expect(stateFromSubmit({ job_id: null, status: 'failed', error_code: 'PROVIDER_NOT_CONFIGURED', error: 'x' }))
      .toEqual({ state: 'failed', errorCode: 'PROVIDER_NOT_CONFIGURED', errorMessage: 'x' })
  })
})

describe('fieldsFromStatus', () => {
  it('maps completed with the answer and public context keys only', () => {
    const f = fieldsFromStatus({ status: 'completed', answer: 'hi', resolved_context: { bot_health: {}, _symbol: 'X' } })
    expect(f).toMatchObject({ state: 'completed', text: 'hi', contextKeys: ['bot_health'] })
  })
  it('maps failed / cancelled / cancelling / in-flight', () => {
    expect(fieldsFromStatus({ status: 'failed', error_code: 'PROVIDER_TIMEOUT', error: 'slow' }))
      .toMatchObject({ state: 'failed', errorCode: 'PROVIDER_TIMEOUT', errorMessage: 'slow' })
    expect(fieldsFromStatus({ status: 'cancelled' })).toMatchObject({ state: 'cancelled', errorCode: 'REQUEST_CANCELLED' })
    expect(fieldsFromStatus({ status: 'cancelling' }).state).toBe('cancelling')
    expect(fieldsFromStatus({ status: 'queued' }).state).toBe('thinking')
    expect(fieldsFromStatus({ status: 'thinking' }).state).toBe('thinking')
  })
})

describe('typed errors stay distinct', () => {
  const codes = ['PROVIDER_NOT_CONFIGURED', 'COPILOT_DISABLED', 'PROVIDER_UNAVAILABLE', 'PROVIDER_TIMEOUT',
    'PROVIDER_AUTH_FAILED', 'PROVIDER_RATE_LIMITED', 'MODEL_UNAVAILABLE', 'BACKEND_EXCEPTION', 'NETWORK', 'JOB_LOST',
    'REQUEST_CANCELLED']
  it('has a unique label per code and never leaks a key', () => {
    const labels = codes.map(c => errorCodeToMessage(c).label)
    expect(new Set(labels).size).toBe(codes.length)
    expect(errorCodeToMessage('SOMETHING_ELSE').label).toBe('Request failed')
  })
})
