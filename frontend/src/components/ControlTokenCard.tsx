import { useState } from 'react'
import { KeyRound } from 'lucide-react'
import { getControlToken, setControlToken } from '../api/client'

/**
 * Operator token for the backend's CONTROL_TOKEN. When the backend has CONTROL_TOKEN set, every
 * state-changing action (Start / Stop / Kill, settings, universe, backtests, diagnostics …) needs
 * it. It is stored in THIS browser only (localStorage) and sent as the X-Control-Token header —
 * it is never put in the build, the URL or the page.
 */
export default function ControlTokenCard() {
  const [value, setValue] = useState('')
  const [saved, setSaved] = useState(() => getControlToken() !== '')

  const save = () => {
    setControlToken(value.trim())
    setSaved(value.trim() !== '')
    setValue('')
  }
  const clear = () => {
    setControlToken('')
    setSaved(false)
    setValue('')
  }

  return (
    <section aria-label="Control token" data-testid="control-token-card"
      className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4 sm:p-5">
      <h2 className="text-sm font-semibold text-white mb-1 flex items-center gap-2">
        <KeyRound size={14} className="text-slate-400" /> Control token
      </h2>
      <p className="text-xs text-slate-500 mb-3">
        Only needed if the backend has <code className="font-mono">CONTROL_TOKEN</code> set. Stored in this browser only.
        Status: <span data-testid="control-token-status" className={saved ? 'text-emerald-300' : 'text-slate-400'}>
          {saved ? 'token saved — sent with every change request' : 'no token saved'}
        </span>
      </p>
      <div className="flex flex-col sm:flex-row gap-2">
        <input type="password" autoComplete="off" value={value} onChange={e => setValue(e.target.value)}
          aria-label="Control token" placeholder={saved ? '•••••• (enter a new token to replace)' : 'Paste control token'}
          className="flex-1 min-w-0 bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 min-h-[44px] sm:min-h-0 text-base sm:text-sm text-slate-100 focus:outline-none focus:border-blue-500/60" />
        <button onClick={save} disabled={!value.trim()}
          className="min-h-[44px] sm:min-h-0 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white text-sm font-medium">Save</button>
        <button onClick={clear} disabled={!saved}
          className="min-h-[44px] sm:min-h-0 px-4 py-2 rounded-lg border border-[#1e2d45] text-slate-300 hover:bg-[#1a2235] disabled:opacity-40 text-sm">Clear</button>
      </div>
    </section>
  )
}
