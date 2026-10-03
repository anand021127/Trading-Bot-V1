/**
 * Pure helpers for the Copilot async chat-job contract (no React, no I/O).
 *
 *   POST /api/copilot/chat/submit          -> 202 {job_id, status}   (NEVER the answer)
 *   GET  /api/copilot/chat/status/{job_id} -> queued|thinking|cancelling|completed|failed|cancelled
 *   POST /api/copilot/chat/status/{job_id}/cancel
 *
 * A fast provider can already be `completed` when the POST envelope is built.
 * The envelope still carries no answer, so the UI must fetch the status once
 * IMMEDIATELY — without ever showing a fake "Thinking…" state.
 */

export type JobStatus = 'queued' | 'thinking' | 'cancelling' | 'completed' | 'failed' | 'cancelled'

export const IN_FLIGHT_STATUSES: readonly string[] = ['queued', 'thinking', 'cancelling']
export const TERMINAL_STATUSES: readonly string[] = ['completed', 'failed', 'cancelled']

export const isInFlight = (s: unknown): boolean => typeof s === 'string' && IN_FLIGHT_STATUSES.includes(s)
export const isTerminal = (s: unknown): boolean => typeof s === 'string' && TERMINAL_STATUSES.includes(s)

export type MsgState = 'sending' | 'thinking' | 'cancelling' | 'completed' | 'failed' | 'cancelled'

export interface SubmitEnvelope {
  job_id?: string | null
  status?: string
  error_code?: string
  error?: string
  session_id?: string
}

export interface JobStatusBody {
  job_id?: string
  status?: string
  answer?: string
  resolved_context?: Record<string, unknown>
  error_code?: string
  error?: string
}

export interface AssistantFields {
  state: MsgState
  text: string
  errorCode?: string
  errorMessage?: string
  contextKeys?: string[]
}

/** What the placeholder should look like right after the POST returned. */
export function stateFromSubmit(env: SubmitEnvelope): Pick<AssistantFields, 'state' | 'errorCode' | 'errorMessage'> | null {
  if (!env.job_id) {
    // Immediate typed failure (disabled / provider not configured): no job exists.
    return { state: 'failed', errorCode: env.error_code, errorMessage: env.error }
  }
  // queued / thinking / cancelling -> a real in-flight job.
  if (isInFlight(env.status)) return { state: env.status === 'cancelling' ? 'cancelling' : 'thinking' }
  // completed / failed / cancelled -> terminal already: DO NOT show "Thinking".
  // Return null so the caller fetches the authoritative status immediately.
  return null
}

/** Map a status-endpoint body onto the assistant message fields. */
export function fieldsFromStatus(d: JobStatusBody): AssistantFields {
  switch (d.status) {
    case 'completed':
      return {
        state: 'completed',
        text: d.answer ?? '',
        contextKeys: d.resolved_context
          ? Object.keys(d.resolved_context).filter(k => !k.startsWith('_'))
          : [],
      }
    case 'failed':
      return { state: 'failed', text: '', errorCode: d.error_code, errorMessage: d.error }
    case 'cancelled':
      return { state: 'cancelled', text: '', errorCode: 'REQUEST_CANCELLED' }
    case 'cancelling':
      return { state: 'cancelling', text: '' }
    default:
      return { state: 'thinking', text: '' }
  }
}

/** Typed backend error_code -> distinct, honest UI message. */
const COPILOT_NOTE = ' This is the Copilot chat model only — the AI Trading Decision gate is separate (see Live context).'

export function errorCodeToMessage(code?: string, fallback?: string): { label: string; text: string } {
  switch (code) {
    case 'PROVIDER_NOT_CONFIGURED':
      return { label: 'Copilot AI provider not configured', text: (fallback || 'No Copilot chat model is configured on the backend. Set COPILOT_LLM_BACKEND (local Ollama or openai + key) and retry. No fake answer was generated.') + COPILOT_NOTE }
    case 'COPILOT_DISABLED':
      return { label: 'Copilot disabled', text: (fallback || 'The Copilot is disabled in backend configuration (COPILOT_ENABLED=false).') + COPILOT_NOTE }
    case 'PROVIDER_UNAVAILABLE':
      return { label: 'Copilot AI provider unreachable', text: (fallback || 'Could not reach the Copilot chat model (connection refused / server down). This is NOT a frontend timeout.') + COPILOT_NOTE }
    case 'PROVIDER_TIMEOUT':
      return { label: 'Copilot AI provider timed out', text: (fallback || 'The Copilot chat model accepted the request but did not answer in time. You can retry, or ask a shorter question.') + COPILOT_NOTE }
    case 'PROVIDER_AUTH_FAILED':
      return { label: 'Copilot AI provider authentication failed', text: (fallback || 'The chat provider rejected the configured credentials. Check the API key configuration — keys are never displayed here.') + COPILOT_NOTE }
    case 'PROVIDER_RATE_LIMITED':
      return { label: 'Copilot AI provider rate limited', text: (fallback || 'The chat provider is rate limiting requests. Wait a moment and retry.') + COPILOT_NOTE }
    case 'MODEL_UNAVAILABLE':
      return { label: 'Copilot AI model unavailable', text: (fallback || 'The configured chat model is not available on the provider (not downloaded/loaded?).') + COPILOT_NOTE }
    case 'BACKEND_EXCEPTION':
      return { label: 'Copilot backend error', text: fallback || 'The Copilot backend hit an unexpected error. Check backend logs.' }
    case 'NETWORK':
      return { label: 'Network problem', text: fallback || 'Could not reach the backend. This is a connection problem, not an AI failure.' }
    case 'JOB_LOST':
      return { label: 'Request no longer on the server', text: fallback || 'The backend no longer knows this request (it may have restarted). Retry.' }
    case 'REQUEST_CANCELLED':
      return { label: 'Cancelled', text: 'Generation cancelled.' }
    default:
      return { label: 'Request failed', text: fallback || 'The request failed. Retry.' }
  }
}
