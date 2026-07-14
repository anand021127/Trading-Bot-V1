import { useState } from 'react'
import { Play, CheckCircle, XCircle, RefreshCw, Clock, Loader } from 'lucide-react'
import { runAllTests, runSingleTest } from '../api/endpoints'

const ALL_TESTS = [
  { name: 'authentication', label: 'Authentication', desc: 'Verify Upstox API token is valid and not expired' },
  { name: 'historical_data', label: 'Historical Data', desc: 'Fetch 5 candles of RELIANCE 15-min from Upstox' },
  { name: 'live_quote', label: 'Live Quote', desc: 'Get current LTP for RELIANCE, TCS, HDFCBANK' },
  { name: 'websocket', label: 'WebSocket Feed', desc: 'Connect to Upstox WebSocket and receive a price tick' },
  { name: 'place_order_paper', label: 'Place Order (Paper)', desc: 'Simulate a paper order for 1 share — no real money' },
  { name: 'cancel_order_paper', label: 'Cancel Order (Paper)', desc: 'Cancel the above paper order' },
  { name: 'database', label: 'Database', desc: 'Write, read, and delete a test record from SQLite' },
  { name: 'indicators', label: 'Indicators', desc: 'Compute EMA, ATR, RSI, Choppiness on 100 real candles' },
  { name: 'risk_manager', label: 'Risk Manager', desc: 'Simulate daily loss limit and consecutive loss rules' },
  { name: 'telegram', label: 'Telegram Alert', desc: 'Send a test message to your Telegram bot' },
  { name: 'email', label: 'Email Alert', desc: 'Send a test email via Gmail SMTP' },
]

type TestStatus = 'PENDING' | 'RUNNING' | 'PASS' | 'FAIL'

interface TestState {
  status: TestStatus
  response_time_ms?: number | null
  details?: string
  error?: string | null
}

function StatusIcon({ status }: { status: TestStatus }) {
  if (status === 'PASS') return <CheckCircle size={16} className="text-emerald-400 flex-shrink-0" />
  if (status === 'FAIL') return <XCircle size={16} className="text-red-400 flex-shrink-0" />
  if (status === 'RUNNING') return <Loader size={16} className="text-blue-400 flex-shrink-0 animate-spin" />
  return <Clock size={16} className="text-slate-600 flex-shrink-0" />
}

function StatusBg(status: TestStatus) {
  if (status === 'PASS') return 'border-emerald-800/40 bg-emerald-950/10'
  if (status === 'FAIL') return 'border-red-800/40 bg-red-950/10'
  if (status === 'RUNNING') return 'border-blue-800/40 bg-blue-950/10'
  return 'border-[#1e2d45] bg-[#141b2d]'
}

export default function ApiTest() {
  const [testMap, setTestMap] = useState<Record<string, TestState>>({})
  const [runningAll, setRunningAll] = useState(false)
  const [summary, setSummary] = useState<{ passed: number; failed: number } | null>(null)

  const setTest = (name: string, state: Partial<TestState>) =>
    setTestMap(prev => ({ ...prev, [name]: { ...(prev[name] ?? { status: 'PENDING' }), ...state } }))

  const runOne = async (testName: string) => {
    setTest(testName, { status: 'RUNNING', details: undefined, error: undefined })
    try {
      const res = await runSingleTest(testName)
      setTest(testName, {
        status: (res.status as TestStatus) === 'PASS' ? 'PASS' : 'FAIL',
        response_time_ms: res.response_time_ms,
        details: res.details,
        error: res.error,
      })
    } catch (e) {
      setTest(testName, {
        status: 'FAIL',
        error: e instanceof Error ? e.message : 'Network error — is backend running?',
      })
    }
  }

  const runAll = async () => {
    setRunningAll(true)
    setSummary(null)
    const initial: Record<string, TestState> = {}
    ALL_TESTS.forEach(t => { initial[t.name] = { status: 'PENDING' } })
    setTestMap(initial)

    try {
      // Mark all as running first for visual effect
      for (const t of ALL_TESTS) {
        setTest(t.name, { status: 'RUNNING' })
        await new Promise(r => setTimeout(r, 100))
      }
      const res = await runAllTests()
      const results = res.results ?? []
      results.forEach(r => {
        setTest(r.test_name, {
          status: (r.status as TestStatus),
          response_time_ms: r.response_time_ms,
          details: r.details,
          error: r.error,
        })
      })
      setSummary({ passed: res.passed ?? 0, failed: res.failed ?? 0 })
    } catch {
      // If backend is down, mark all as failed
      ALL_TESTS.forEach(t => setTest(t.name, { status: 'FAIL', error: 'Backend unreachable' }))
      setSummary({ passed: 0, failed: ALL_TESTS.length })
    } finally {
      setRunningAll(false)
    }
  }

  const totalTests = ALL_TESTS.length
  const passedCount = Object.values(testMap).filter(t => t.status === 'PASS').length
  const failedCount = Object.values(testMap).filter(t => t.status === 'FAIL').length

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold text-white">API Test & Diagnostics</h1>
          <p className="text-xs text-slate-500 mt-0.5">Verify every system component before going live</p>
        </div>
        <button
          onClick={runAll}
          disabled={runningAll}
          className="flex items-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors min-w-[140px] justify-center"
        >
          {runningAll ? (
            <><RefreshCw size={14} className="animate-spin" /> Running...</>
          ) : (
            <><Play size={14} /> Run All Tests</>
          )}
        </button>
      </div>

      {/* Score card */}
      {summary && (
        <div className={`rounded-xl border p-4 flex items-center gap-4 ${summary.failed === 0 ? 'bg-emerald-950/20 border-emerald-800/50' : 'bg-amber-950/20 border-amber-800/50'}`}>
          <div className={`text-3xl font-bold ${summary.failed === 0 ? 'text-emerald-400' : 'text-amber-400'}`}>
            {summary.passed}/{totalTests}
          </div>
          <div>
            <div className={`text-sm font-semibold ${summary.failed === 0 ? 'text-emerald-300' : 'text-amber-300'}`}>
              {summary.failed === 0 ? '🎉 All tests passed! Bot is ready.' : `${summary.failed} test${summary.failed > 1 ? 's' : ''} failed — fix before going live`}
            </div>
            <div className="text-xs text-slate-500 mt-0.5">{summary.passed} passed · {summary.failed} failed · {totalTests} total</div>
          </div>
        </div>
      )}

      {/* Progress bar when running */}
      {runningAll && (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4">
          <div className="flex justify-between text-xs text-slate-400 mb-2">
            <span>Running tests...</span>
            <span>{passedCount + failedCount}/{totalTests}</span>
          </div>
          <div className="h-1.5 bg-[#1e2d45] rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 rounded-full transition-all duration-300"
              style={{ width: `${((passedCount + failedCount) / totalTests) * 100}%` }}
            />
          </div>
        </div>
      )}

      {/* Test cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {ALL_TESTS.map(test => {
          const state = testMap[test.name] ?? { status: 'PENDING' as TestStatus }
          return (
            <div key={test.name} className={`rounded-xl border p-4 transition-all ${StatusBg(state.status)}`}>
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-2.5 flex-1 min-w-0">
                  <StatusIcon status={state.status} />
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-white">{test.label}</div>
                    <div className="text-[11px] text-slate-500 mt-0.5">{test.desc}</div>
                  </div>
                </div>
                <button
                  onClick={() => runOne(test.name)}
                  disabled={state.status === 'RUNNING' || runningAll}
                  className="flex-shrink-0 flex items-center gap-1 px-2.5 py-1.5 bg-[#0f1628] border border-[#1e2d45] rounded-lg text-[11px] text-slate-300 hover:text-white hover:border-[#243044] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  {state.status === 'RUNNING' ? <><RefreshCw size={10} className="animate-spin" /> Running</> : <><Play size={10} /> Run</>}
                </button>
              </div>

              {/* Result details */}
              {state.status !== 'PENDING' && state.status !== 'RUNNING' && (
                <div className="mt-3 pt-3 border-t border-[#1e2d45]/50 space-y-1">
                  {state.response_time_ms != null && (
                    <div className="text-[10px] text-slate-600">{state.response_time_ms.toFixed(0)}ms response time</div>
                  )}
                  {state.details && (
                    <div className="text-[11px] text-slate-400">{state.details}</div>
                  )}
                  {state.error && (
                    <div className="text-[11px] text-red-400 bg-red-950/20 rounded px-2 py-1 mt-1">{state.error}</div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Tips */}
      <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
        <h2 className="text-sm font-semibold text-white mb-3">Common Fixes</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs text-slate-500">
          {[
            { title: 'Authentication fails', fix: 'Regenerate token from Settings page. Upstox tokens expire daily.' },
            { title: 'Historical Data fails', fix: 'Market may be closed. Data still available but check API status at status.upstox.com' },
            { title: 'WebSocket fails', fix: 'WebSocket URL must use wss:// in production. Check VITE_WS_URL in Vercel env vars.' },
            { title: 'Telegram fails', fix: 'Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in backend environment variables.' },
            { title: 'Email fails', fix: 'Use Gmail App Password (not your login password). Ensure 2FA is enabled on Gmail.' },
            { title: 'Database fails', fix: 'On Render: ensure /data persistent disk is attached. Run setup_db.py script.' },
          ].map(item => (
            <div key={item.title} className="rounded-lg bg-[#0f1628] border border-[#1e2d45] p-3">
              <div className="text-slate-300 font-medium mb-0.5">{item.title}</div>
              <div>{item.fix}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
