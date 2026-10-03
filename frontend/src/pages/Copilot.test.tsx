import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axios from 'axios'

/* ── mock the axios instance the page uses ── */
const get = vi.fn()
const post = vi.fn()
vi.mock('../api/client', () => ({ api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) } }))

import Copilot from './Copilot'

const CTX = {
  generated_at: '2026-09-30T10:00:00Z',
  bot: { running: true, mode: 'paper', strategy: 'V8_D_PULLBACK_ATM', runtime_label: 'Running — no signal',
    worker_alive: true, heartbeat_age_seconds: 3, kill_switch_active: false, market_open: true },
  configuration: { capital: { starting_capital: 20000, source: 'env' }, risk: { max_trades_per_day: 20 }, strategy: 'V8_D_PULLBACK_ATM' },
  websocket: { state: 'connected', streaming: true, last_tick_age_seconds: 2, market_data_status: 'LIVE' },
  scanner: { available: true, state_label: 'Running — no signal', scan_seq: 42, candle_count: 70, option_chain_count: 120, expiry: '2026-10-06' },
  latest_rejection: { gate_chain: { available: true, stage: 'NO_SIGNAL', human_summary: 'V8-D evaluated; no pullback.', traded: false, gates: { v8d_signal: { status: 'REJECTED' } } } },
  today: { available: true, trades_today: 0, configured_max_trades: 20, wins: 0, losses: 0, realized_pnl: 0 },
  risk: { available: true, trades_used: 0, max_trades: 20, daily_pnl: 0 },
  positions: { available: true, count: 0, positions: [] },
  recent_trades: { available: true, count: 0, trades: [] },
  backtest: { available: true, config: { start_date: '2025-09-28', end_date: '2026-09-29' },
    summary: { trades: 171, net_pnl: -45397.89, win_rate_pct: 26.9, profit_factor: 0.6, max_drawdown_pct: 50.77 } },
  ai: { available: true, enabled: true, provider: 'ollama', model: 'llama3', latest: { available: true, decision: 'WAIT', confidence: 61, reason_codes: ['LOW_VOL'], latency_ms: 812 } },
  pipeline: { scanner: 'RUNNING', market: 'LIVE', strategy: 'V8_D_PULLBACK_ATM', latest_signal: 'NO SIGNAL',
    ai_decision: 'NOT EVALUATED', ai_reason: 'Not consulted: no V8-D BUY signal.', risk_check: 'NOT EVALUATED', execution: 'NOT ATTEMPTED', final: 'NO TRADE', outcome: 'NO_SIGNAL' },
  configuration_mismatches: [] as { message: string }[],
}

const STATUS_OK = { provider_configured: true, llm_backend: 'local_openai_compatible', model: 'llama3.1:8b', enabled: true }

type Handler = (url: string) => unknown
function wireGet(over: Partial<Record<string, Handler>> = {}) {
  get.mockImplementation((url: string) => {
    for (const [prefix, h] of Object.entries(over)) {
      if (h && url.startsWith(prefix)) return Promise.resolve({ data: h(url) })
    }
    if (url === '/api/copilot/status') return Promise.resolve({ data: STATUS_OK })
    if (url === '/api/copilot/context') return Promise.resolve({ data: CTX })
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
}

async function ask(text: string) {
  const user = userEvent.setup()
  const box = screen.getByLabelText('Message Copilot')
  await user.type(box, text)
  await user.click(screen.getByLabelText('Send message'))
}

beforeEach(() => {
  get.mockReset(); post.mockReset()
  wireGet()
})

describe('layout & header', () => {
  it('is chat-first: chat panel + composer visible immediately, context in a separate aside', async () => {
    render(<Copilot />)
    const chat = screen.getByTestId('chat-panel')
    expect(within(chat).getByTestId('chat-log')).toBeInTheDocument()
    expect(within(chat).getByLabelText('Message Copilot')).toBeInTheDocument()   // composer inside the chat panel
    expect(screen.getByTestId('context-aside')).toHaveClass('hidden', 'lg:flex') // desktop-only aside, collapses < lg
    expect(chat.className).toMatch(/lg:basis-\[70%\]/)
    expect(screen.getByTestId('context-aside').className).toMatch(/lg:basis-\[30%\]/)
    // Not 10+ context cards before the chat: the chat section precedes the aside in the DOM
    const page = screen.getByTestId('copilot-page')
    const order = Array.from(page.querySelectorAll('[data-testid="chat-panel"],[data-testid="context-aside"]')).map(n => n.getAttribute('data-testid'))
    expect(order).toEqual(['chat-panel', 'context-aside'])
  })

  it('shows the required header text and distinguishes provider vs AI Trading Decision', async () => {
    render(<Copilot />)
    expect(screen.getByText('Copilot AI')).toBeInTheDocument()
    expect(screen.getByText('Grounded in live bot state, trades and backtests')).toBeInTheDocument()
    expect(screen.getAllByText(/Observation/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/No order placement/i).length).toBeGreaterThan(0)
    await waitFor(() => expect(screen.getByTestId('provider-chip')).toHaveTextContent('local_openai_compatible'))
    expect(screen.getByTestId('provider-chip')).toHaveAttribute('title', expect.stringContaining('Separate from the AI Trading Decision engine'))
    await waitFor(() => expect(screen.getByTestId('ctx-ai')).toHaveTextContent('AI Trading Decision engine'))
    expect(screen.getByTestId('ctx-ai')).toHaveTextContent(/separate from the Copilot provider/i)
  })

  it.each([
    ['configured', STATUS_OK, /local_openai_compatible/],
    ['not configured', { ...STATUS_OK, provider_configured: false }, /provider not configured/],
    ['disabled', { ...STATUS_OK, enabled: false }, /copilot disabled/],
  ])('provider chip: %s', async (_n, status, re) => {
    wireGet({ '/api/copilot/status': () => status })
    render(<Copilot />)
    await waitFor(() => expect(screen.getByTestId('provider-chip')).toHaveTextContent(re))
  })

  it('provider chip: backend offline', async () => {
    get.mockImplementation(() => Promise.reject(new Error('down')))
    render(<Copilot />)
    await waitFor(() => expect(screen.getByTestId('provider-chip')).toHaveTextContent(/backend offline/))
    await waitFor(() => expect(screen.getByText(/Live context unavailable/)).toBeInTheDocument())
  })
})

describe('empty state / quick prompts', () => {
  it('offers the six required prompts and sends one on click', async () => {
    post.mockResolvedValue({ data: { job_id: 'j1', status: 'completed', session_id: 's' } })
    wireGet({ '/api/copilot/chat/status/': () => ({ job_id: 'j1', status: 'completed', answer: 'answer 1' }) })
    render(<Copilot />)
    const prompts = within(screen.getByTestId('quick-prompts')).getAllByRole('button').map(b => b.textContent)
    expect(prompts).toEqual([
      "Why didn't the bot trade?", "Explain today's bot status", "Show today's trades",
      'Explain the latest rejection', 'Explain the latest backtest', 'What is my current risk?',
    ])
    await userEvent.setup().click(screen.getByText('What is my current risk?'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/copilot/chat/submit',
      expect.objectContaining({ question: 'What is my current risk?' }), expect.anything()))
    expect(await screen.findByText('answer 1')).toBeInTheDocument()
    expect(screen.queryByTestId('chat-empty')).not.toBeInTheDocument()
  })
})

describe('composer', () => {
  it('Send is disabled when empty; Enter sends; Shift+Enter does not', async () => {
    post.mockResolvedValue({ data: { job_id: 'j', status: 'completed' } })
    wireGet({ '/api/copilot/chat/status/': () => ({ job_id: 'j', status: 'completed', answer: 'ok' }) })
    render(<Copilot />)
    const box = screen.getByLabelText('Message Copilot')
    expect(screen.getByLabelText('Send message')).toBeDisabled()
    fireEvent.change(box, { target: { value: 'line one' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(post).not.toHaveBeenCalled()
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('ok')).toBeInTheDocument()
    expect((box as HTMLTextAreaElement).value).toBe('')
  })

  it('every icon-only control has an accessible name', async () => {
    render(<Copilot />)
    expect(screen.getByLabelText('Clear conversation')).toBeInTheDocument()
    expect(screen.getByLabelText('Send message')).toBeInTheDocument()
    expect(screen.getByLabelText('Message Copilot')).toBeInTheDocument()
    expect(screen.getByRole('log')).toHaveAttribute('aria-live', 'polite')
  })
})

describe('async job contract', () => {
  it('CASE B — POST says completed (no answer): fetches the answer immediately, never shows Thinking', async () => {
    post.mockResolvedValue({ data: { job_id: 'fast', status: 'completed', session_id: 's' } })   // NO answer in POST
    // Hold the status response open so we can inspect the UI DURING the fetch.
    let release: (v: unknown) => void = () => {}
    const gate = new Promise(res => { release = res })
    get.mockImplementation((url: string) => {
      if (url === '/api/copilot/status') return Promise.resolve({ data: STATUS_OK })
      if (url === '/api/copilot/context') return Promise.resolve({ data: CTX })
      if (url === '/api/copilot/chat/status/fast') return gate.then(() => ({ data: { job_id: 'fast', status: 'completed', answer: 'Instant grounded answer.', resolved_context: { bot_health: {} } } }))
      return Promise.reject(new Error(`unexpected GET ${url}`))
    })
    render(<Copilot />)
    await ask('hello')
    // status fetched IMMEDIATELY after the POST (no timer needed)...
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/copilot/chat/status/fast'))
    // ...and while it is in flight the UI must NOT pretend the model is thinking.
    expect(screen.queryByText('Thinking…')).not.toBeInTheDocument()
    expect(screen.getByTestId('thinking-indicator')).toHaveTextContent('Sending…')
    await act(async () => { release(null) })
    expect(await screen.findByText('Instant grounded answer.')).toBeInTheDocument()
    expect(screen.queryByTestId('thinking-indicator')).not.toBeInTheDocument()
  })

  it('CASE A — slow provider: Thinking while queued/thinking, keeps polling, then renders', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let n = 0
      post.mockResolvedValue({ data: { job_id: 'slow', status: 'queued' } })
      wireGet({ '/api/copilot/chat/status/': () => (++n < 3 ? { job_id: 'slow', status: n === 1 ? 'queued' : 'thinking' } : { job_id: 'slow', status: 'completed', answer: 'Finally.' }) })
      render(<Copilot />)
      await ask('slow one')
      await waitFor(() => expect(screen.getByText('Thinking…')).toBeInTheDocument())
      expect(screen.getByLabelText('Stop generating')).toBeInTheDocument()
      await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
      expect(await screen.findByText('Finally.')).toBeInTheDocument()
      expect(n).toBeGreaterThanOrEqual(3)
      const calls = get.mock.calls.filter(c => String(c[0]).startsWith('/api/copilot/chat/status/')).length
      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(get.mock.calls.filter(c => String(c[0]).startsWith('/api/copilot/chat/status/')).length).toBe(calls)   // stopped polling
    } finally { vi.useRealTimers() }
  })

  it('CASE C — provider failure: typed error, distinct label, Retry works', async () => {
    post.mockResolvedValue({ data: { job_id: 'bad', status: 'thinking' } })
    let fail = true
    wireGet({ '/api/copilot/chat/status/': () => (fail
      ? { job_id: 'bad', status: 'failed', error_code: 'PROVIDER_TIMEOUT', error: 'The provider took too long.' }
      : { job_id: 'bad2', status: 'completed', answer: 'recovered' }) })
    render(<Copilot />)
    await ask('will fail')
    const err = await screen.findByTestId('msg-error')
    expect(err).toHaveAttribute('data-error-code', 'PROVIDER_TIMEOUT')
    expect(err).toHaveTextContent('Copilot AI provider timed out')
    fail = false
    post.mockResolvedValue({ data: { job_id: 'bad2', status: 'completed' } })
    await userEvent.setup().click(screen.getByLabelText('Retry this question'))
    expect(await screen.findByText('recovered')).toBeInTheDocument()
    expect(screen.getAllByTestId('msg-user')).toHaveLength(1)    // Retry re-uses the bubble, no duplicate
  })

  it('immediate typed failure (no job): PROVIDER_NOT_CONFIGURED is shown, never a fake answer', async () => {
    post.mockResolvedValue({ data: { job_id: null, status: 'failed', error_code: 'PROVIDER_NOT_CONFIGURED', error: 'No AI provider is configured.' } })
    render(<Copilot />)
    await ask('anything')
    const err = await screen.findByTestId('msg-error')
    expect(err).toHaveAttribute('data-error-code', 'PROVIDER_NOT_CONFIGURED')
    expect(err).toHaveTextContent('Copilot AI provider not configured')
    expect(get.mock.calls.some(c => String(c[0]).startsWith('/api/copilot/chat/status/'))).toBe(false)
  })

  it('CASE D — cancellation: Stop posts cancel and the authoritative cancelled state is shown', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      post.mockImplementation((url: string) =>
        Promise.resolve({ data: url.endsWith('/cancel') ? { cancelled: true } : { job_id: 'c1', status: 'thinking' } }))
      let cancelled = false
      wireGet({ '/api/copilot/chat/status/': () => (cancelled ? { job_id: 'c1', status: 'cancelled' } : { job_id: 'c1', status: 'thinking' }) })
      render(<Copilot />)
      await ask('cancel me')
      await screen.findByText('Thinking…')
      cancelled = true
      await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(screen.getByLabelText('Stop generating'))
      expect(post).toHaveBeenCalledWith('/api/copilot/chat/status/c1/cancel')
      await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
      expect(await screen.findByText('Generation cancelled.')).toBeInTheDocument()
      expect(screen.getByLabelText('Send message')).toBeInTheDocument()   // back to idle
    } finally { vi.useRealTimers() }
  })

  it('transient poll failures are retried, not instantly fatal', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      post.mockResolvedValue({ data: { job_id: 'flaky', status: 'thinking' } })
      let n = 0
      get.mockImplementation((url: string) => {
        if (url === '/api/copilot/status') return Promise.resolve({ data: STATUS_OK })
        if (url === '/api/copilot/context') return Promise.resolve({ data: CTX })
        if (++n <= 2) return Promise.reject(new axios.AxiosError('blip', 'ERR_NETWORK'))
        return Promise.resolve({ data: { job_id: 'flaky', status: 'completed', answer: 'survived' } })
      })
      render(<Copilot />)
      await ask('flaky')
      await act(async () => { await vi.advanceTimersByTimeAsync(8000) })
      expect(await screen.findByText('survived')).toBeInTheDocument()
    } finally { vi.useRealTimers() }
  })

  it('a POST network failure is a typed NETWORK error', async () => {
    post.mockRejectedValue(new axios.AxiosError('offline', 'ERR_NETWORK'))
    render(<Copilot />)
    await ask('x')
    expect(await screen.findByTestId('msg-error')).toHaveAttribute('data-error-code', 'NETWORK')
  })
})

describe('clear conversation', () => {
  it('empties the chat and starts a NEW server session', async () => {
    post.mockResolvedValue({ data: { job_id: 'j', status: 'completed' } })
    wireGet({ '/api/copilot/chat/status/': () => ({ job_id: 'j', status: 'completed', answer: 'a1' }) })
    render(<Copilot />)
    await ask('first')
    await screen.findByText('a1')
    const s1 = (post.mock.calls[0][1] as { session_id: string }).session_id
    await userEvent.setup().click(screen.getByLabelText('Clear conversation'))
    expect(screen.getByTestId('chat-empty')).toBeInTheDocument()
    await ask('second')
    await screen.findByText('a1')
    const s2 = (post.mock.calls[1][1] as { session_id: string }).session_id
    expect(s2).not.toBe(s1)
  })
})

describe('Live Context', () => {
  it('renders the authoritative context sections compactly, detail sections collapsed', async () => {
    render(<Copilot />)
    const aside = screen.getByTestId('context-aside')
    await within(aside).findByTestId('ctx-runtime')
    for (const id of ['runtime', 'market', 'scanner', 'why', 'risk', 'positions', 'trades', 'backtest', 'config', 'ai']) {
      expect(within(aside).getByTestId(`ctx-${id}`)).toBeInTheDocument()
    }
    expect(within(aside).getByTestId('ctx-runtime')).toHaveAttribute('open')
    expect(within(aside).getByTestId('ctx-why')).toHaveAttribute('open')
    expect(within(aside).getByTestId('ctx-scanner')).not.toHaveAttribute('open')
    expect(within(aside).getByTestId('ctx-backtest')).not.toHaveAttribute('open')
    // values come from the API payload, not hard-coded
    expect(within(aside).getByTestId('ctx-backtest')).toHaveTextContent('171 trades')
    expect(within(aside).getByTestId('ctx-scanner')).toHaveTextContent('Running — no signal')
    expect(within(aside).getByTestId('ctx-ai')).toHaveTextContent('61%')
    expect(within(aside).queryByTestId('ctx-problems')).not.toBeInTheDocument()   // errors only when present
    // the decision pipeline (scanner → signal → AI → risk → execution) is shown compactly at the top
    expect(within(aside).getByTestId('decision-pipeline')).toHaveTextContent('NO SIGNAL')
    expect(within(aside).getByTestId('pipe-ai-decision')).toHaveTextContent('NOT EVALUATED')
  })

  it('surfaces mismatches/errors prominently ONLY when present', async () => {
    wireGet({ '/api/copilot/context': () => ({ ...CTX, configuration_mismatches: [{ message: 'Capital differs between UI and env' }],
      scanner: { ...CTX.scanner, error: 'candle_fetch_error' } }) })
    render(<Copilot />)
    const box = await screen.findByTestId('ctx-problems')
    expect(box).toHaveTextContent('Capital differs between UI and env')
    expect(box).toHaveTextContent('candle_fetch_error')
  })

  it('mobile: context is a collapsible bottom sheet, closed by default, Esc closes', async () => {
    render(<Copilot />)
    expect(screen.queryByTestId('context-sheet')).not.toBeInTheDocument()
    const btn = screen.getByRole('button', { name: /Context/ })
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    await userEvent.setup().click(btn)
    const sheet = await screen.findByRole('dialog', { name: 'Live context' })
    expect(within(sheet).getByTestId('live-context')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('context-sheet')).not.toBeInTheDocument())
  })
})

describe('safety: the Copilot page never touches trading endpoints', () => {
  it('only calls /api/copilot/* endpoints', async () => {
    post.mockResolvedValue({ data: { job_id: 'j', status: 'completed' } })
    wireGet({ '/api/copilot/chat/status/': () => ({ job_id: 'j', status: 'completed', answer: 'a' }) })
    render(<Copilot />)
    await ask('hi')
    await screen.findByText('a')
    const urls = [...get.mock.calls, ...post.mock.calls].map(c => String(c[0]))
    expect(urls.length).toBeGreaterThan(0)
    for (const u of urls) expect(u.startsWith('/api/copilot/')).toBe(true)
    for (const u of urls) expect(u).not.toMatch(/order|bot\/(start|stop|kill)|broker|execute/i)
  })
})
