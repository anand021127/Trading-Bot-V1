import { useState, useCallback, useEffect } from 'react'
import { Save, RefreshCw, ExternalLink, CheckCircle, XCircle, AlertTriangle, Wifi, WifiOff } from 'lucide-react'
import { fetchSettings, updateSettings, fetchEnvStatus, regenerateToken, fetchUniverse, updateUniverse } from '../api/endpoints'
import type { Settings, UniverseConfigResponse } from '../types'
import toast from 'react-hot-toast'
import api from '../api/client'

interface BrokerStatus {
  overall: 'CONNECTED' | 'DISCONNECTED'
  token_present: boolean
  token_valid: boolean
  api_reachable: boolean
  reason: string
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-5">
      <h2 className="text-sm font-semibold text-white mb-4 pb-3 border-b border-[#1e2d45]">{title}</h2>
      <div className="space-y-4">{children}</div>
    </div>
  )
}

function Field({ label, desc, children }: { label: string; desc?: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-2 md:gap-4 items-start">
      <div>
        <div className="text-xs font-medium text-slate-300">{label}</div>
        {desc && <div className="text-[10px] text-slate-600 mt-0.5">{desc}</div>}
      </div>
      <div className="md:col-span-2">{children}</div>
    </div>
  )
}

function NumInput({ value, onChange, min, max, step }: {
  value: number; onChange: (v: number) => void
  min?: number; max?: number; step?: number
}) {
  return (
    <input type="number" value={value}
      onChange={e => onChange(Number(e.target.value))}
      min={min} max={max} step={step ?? 1}
      className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
  )
}

function Toggle({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <button onClick={() => onChange(!value)}
      className={`relative w-11 h-6 rounded-full transition-colors ${value ? 'bg-blue-600' : 'bg-[#1e2d45]'}`}>
      <div className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${value ? 'translate-x-5' : 'translate-x-0'}`} />
    </button>
  )
}

function UniverseSection() {
  const [universe, setUniverse] = useState<UniverseConfigResponse | null>(null)
  const [customText, setCustomText] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    fetchUniverse().then(u => {
      setUniverse(u)
      setCustomText(u.custom_symbols.join(', '))
    }).catch(() => {})
  }, [])

  const save = async (patch: Partial<UniverseConfigResponse>) => {
    if (!universe) return
    setSaving(true)
    try {
      const updated = await updateUniverse({ ...universe, ...patch })
      setUniverse(updated)
      toast.success('Universe updated — bot will only scan these instruments')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to update universe')
    } finally {
      setSaving(false)
    }
  }

  if (!universe) {
    return (
      <Section title="Trading Universe">
        <div className="text-xs text-slate-600">Loading...</div>
      </Section>
    )
  }

  const modes = universe.valid_modes

  return (
    <Section title="Trading Universe">
      <Field label="Mode" desc="What the bot scans and trades">
        <div className="flex gap-2 flex-wrap">
          {modes.map(m => (
            <button key={m} disabled={saving} onClick={() => save({ mode: m })}
              className={`px-3 py-2 rounded-lg text-xs font-medium border transition-colors ${
                universe.mode === m
                  ? 'bg-blue-600/20 text-blue-400 border-blue-600/50'
                  : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044]'
              }`}>
              {m.replace('_', ' ')}
            </button>
          ))}
        </div>
      </Field>

      {universe.mode === 'STOCKS' && (
        <>
          <Field label="Index" desc="NIFTY50 = all 50 constituents. Custom = your own list.">
            <div className="flex gap-2">
              {['NIFTY50', 'CUSTOM'].map(idx => (
                <button key={idx} disabled={saving} onClick={() => save({ index: idx })}
                  className={`px-3 py-2 rounded-lg text-xs font-medium border transition-colors ${
                    universe.index === idx
                      ? 'bg-blue-600/20 text-blue-400 border-blue-600/50'
                      : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044]'
                  }`}>
                  {idx}
                </button>
              ))}
            </div>
          </Field>

          {universe.index === 'CUSTOM' && (
            <Field label="Custom Symbols" desc="Comma-separated, e.g. RELIANCE, TCS, HDFCBANK">
              <div className="flex gap-2">
                <input value={customText} onChange={e => setCustomText(e.target.value)}
                  className="flex-1 bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
                <button disabled={saving}
                  onClick={() => save({ custom_symbols: customText.split(',').map(s => s.trim().toUpperCase()).filter(Boolean) })}
                  className="px-3 py-2 rounded-lg text-xs font-medium bg-blue-600/20 text-blue-400 border border-blue-600/50">
                  Apply
                </button>
              </div>
            </Field>
          )}

          <Field label="Max Symbols" desc="Cap how many the scanner watches at once">
            <NumInput value={universe.max_symbols} onChange={v => save({ max_symbols: v })} min={1} max={50} />
          </Field>
        </>
      )}

      <Field label="Currently Watching" desc="Exactly what the bot/scanner will look at">
        <div className="flex flex-wrap gap-1.5">
          {universe.resolved_symbols.map(s => (
            <span key={s} className="px-2 py-1 rounded bg-[#0f1628] border border-[#1e2d45] text-[11px] text-slate-300">{s}</span>
          ))}
        </div>
      </Field>
    </Section>
  )
}

export default function Settings() {
  const [settings, setSettings]       = useState<Settings | null>(null)
  const [envStatus, setEnvStatus]     = useState<Record<string, boolean>>({})
  const [brokerStatus, setBrokerStatus] = useState<BrokerStatus | null>(null)
  const [loading, setLoading]         = useState(true)
  const [saving, setSaving]           = useState(false)
  const [tokenLoading, setTokenLoading] = useState(false)
  const [brokerLoading, setBrokerLoading] = useState(false)
  const [saved, setSaved]             = useState(false)
  const [error, setError]             = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [s, env] = await Promise.all([fetchSettings(), fetchEnvStatus()])
      setSettings(s)
      setEnvStatus(env)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load settings')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const checkBroker = async () => {
    setBrokerLoading(true)
    try {
      const res = await api.get<BrokerStatus>('/api/settings/broker-status')
      setBrokerStatus(res.data)
    } catch {
      setBrokerStatus({ overall: 'DISCONNECTED', token_present: false, token_valid: false, api_reachable: false, reason: 'Backend unreachable' })
    } finally {
      setBrokerLoading(false)
    }
  }

  const update = (path: string[], value: unknown) => {
    setSettings(prev => {
      if (!prev) return prev
      const next = structuredClone(prev) as Record<string, unknown>
      let obj = next
      for (let i = 0; i < path.length - 1; i++) {
        obj = obj[path[i]] as Record<string, unknown>
      }
      obj[path[path.length - 1]] = value
      return next as Settings
    })
    setSaved(false)
  }

  const handleSave = async () => {
    if (!settings) return
    setSaving(true)
    try {
      const res = await updateSettings(settings)
      setSaved(true)
      toast.success('Settings saved to database — persists across restarts!')
      setTimeout(() => setSaved(false), 4000)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  const handleRegenToken = async () => {
    setTokenLoading(true)
    try {
      const res = await regenerateToken()
      if (res.auth_url) {
        window.open(res.auth_url, '_blank', 'noopener,noreferrer')
        toast.success('Auth page opened in new tab. After approving, token is saved automatically.')
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to generate token URL')
    } finally {
      setTokenLoading(false)
    }
  }

  const envVars = [
    { key: 'UPSTOX_CLIENT_ID',     label: 'Upstox Client ID' },
    { key: 'UPSTOX_CLIENT_SECRET', label: 'Upstox Client Secret' },
    { key: 'UPSTOX_ACCESS_TOKEN',  label: 'Access Token (auto-refreshed)' },
    { key: 'EMAIL_PASSWORD',        label: 'Gmail App Password' },
    { key: 'SENDER_EMAIL',          label: 'Sender Email' },
    { key: 'RECIPIENT_EMAIL',       label: 'Recipient Email' },
    { key: 'TELEGRAM_BOT_TOKEN',    label: 'Telegram Bot Token' },
    { key: 'TELEGRAM_CHAT_ID',      label: 'Telegram Chat ID' },
  ]

  if (loading) return (
    <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-10 text-center text-slate-500 text-sm">
      <RefreshCw size={16} className="animate-spin mx-auto mb-2" />Loading settings from database...
    </div>
  )

  if (error) return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold text-white">Settings</h1>
      <div className="bg-red-950/30 border border-red-800/50 rounded-xl p-5 text-red-300 text-sm">
        <div className="font-medium mb-1">Could not load settings</div>
        <div className="text-xs text-red-400/70">{error}</div>
        <button onClick={load} className="mt-3 text-xs text-red-300 hover:text-red-200 underline">Retry</button>
      </div>
    </div>
  )

  const cap   = settings?.capital
  const risk  = settings?.risk
  const strat = settings?.strategy
  const ind   = settings?.indicators
  const notif = settings?.notifications

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Settings</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Settings are saved to the database — persist across page refreshes and server restarts
          </p>
        </div>
        <button onClick={handleSave} disabled={saving}
          className={`flex items-center gap-2 px-4 py-2.5 text-white text-sm font-medium rounded-lg transition-colors ${
            saved ? 'bg-emerald-600' : 'bg-blue-600 hover:bg-blue-700'
          } disabled:opacity-60`}>
          {saving ? <><RefreshCw size={13} className="animate-spin" /> Saving...</>
           : saved ? <><CheckCircle size={13} /> Saved!</>
           : <><Save size={13} /> Save Settings</>}
        </button>
      </div>

      {/* Broker Connection Status */}
      <Section title="Broker Connection Status">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1">
            {brokerStatus ? (
              <div className={`flex items-start gap-3 p-3 rounded-lg border ${
                brokerStatus.overall === 'CONNECTED'
                  ? 'bg-emerald-950/20 border-emerald-800/40'
                  : 'bg-red-950/20 border-red-800/40'
              }`}>
                {brokerStatus.overall === 'CONNECTED'
                  ? <Wifi size={15} className="text-emerald-400 flex-shrink-0 mt-0.5" />
                  : <WifiOff size={15} className="text-red-400 flex-shrink-0 mt-0.5" />}
                <div>
                  <div className={`text-sm font-semibold ${brokerStatus.overall === 'CONNECTED' ? 'text-emerald-400' : 'text-red-400'}`}>
                    Broker: {brokerStatus.overall}
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">{brokerStatus.reason}</div>
                  <div className="flex gap-3 mt-2 text-[10px] text-slate-500">
                    <span>Token: {brokerStatus.token_present ? '✓ Present' : '✗ Missing'}</span>
                    <span>Valid: {brokerStatus.token_valid ? '✓ Yes' : '✗ No'}</span>
                    <span>API: {brokerStatus.api_reachable ? '✓ Reachable' : '✗ Down'}</span>
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-xs text-slate-500 p-3 bg-[#0f1628] border border-[#1e2d45] rounded-lg">
                Click "Check Connection" to verify broker status. This actually calls Upstox API — not just checking if token exists.
              </div>
            )}
          </div>
          <button onClick={checkBroker} disabled={brokerLoading}
            className="flex items-center gap-2 px-3 py-2 bg-[#0f1628] border border-[#1e2d45] hover:border-[#243044] text-slate-300 hover:text-white text-xs rounded-lg transition-colors whitespace-nowrap disabled:opacity-50">
            {brokerLoading ? <RefreshCw size={12} className="animate-spin" /> : <Wifi size={12} />}
            Check Connection
          </button>
        </div>
      </Section>

      {/* Token Management */}
      <Section title="Upstox Access Token">
        <div className="space-y-3">
          <div className="text-xs text-slate-400">
            Upstox tokens expire every 24 hours. Click "Generate Token" — a browser tab opens for you to log in.
            After approval, the token is automatically saved to the database and survives server restarts.
            You do NOT need to edit Render environment variables daily.
          </div>

          <div className="flex items-center gap-3 flex-wrap">
            <div className={`flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg border ${
              envStatus['UPSTOX_ACCESS_TOKEN']
                ? 'bg-emerald-950/30 border-emerald-800/40 text-emerald-400'
                : 'bg-red-950/30 border-red-800/40 text-red-400'
            }`}>
              {envStatus['UPSTOX_ACCESS_TOKEN'] ? <CheckCircle size={12} /> : <XCircle size={12} />}
              {envStatus['UPSTOX_ACCESS_TOKEN'] ? 'Token is set' : 'Token missing — bot cannot trade'}
            </div>

            <button onClick={handleRegenToken} disabled={tokenLoading}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-xs font-medium rounded-lg transition-colors disabled:opacity-60">
              {tokenLoading ? <RefreshCw size={12} className="animate-spin" /> : <ExternalLink size={12} />}
              Generate / Reconnect Token
            </button>
          </div>

          <div className="text-[10px] text-slate-600 bg-[#0f1628] border border-[#1e2d45] rounded-lg p-2.5">
            <strong className="text-slate-500">How it works:</strong> Click "Generate Token" → approve in Upstox → 
            token auto-saved to SQLite database → survives Render restarts (on persistent disk /data/).
            No Render dashboard edits needed.
          </div>
        </div>
      </Section>

      {/* Environment Variables Status */}
      <Section title="Environment Variables Status">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {envVars.map(v => (
            <div key={v.key} className="flex items-center justify-between p-2.5 rounded-lg bg-[#0f1628] border border-[#1e2d45]">
              <div>
                <div className="text-xs font-medium text-slate-300">{v.label}</div>
                <div className="text-[10px] font-mono text-slate-600 mt-0.5">{v.key}</div>
              </div>
              {envStatus[v.key]
                ? <div className="flex items-center gap-1 text-emerald-400 text-xs"><CheckCircle size={12} /> Set</div>
                : <div className="flex items-center gap-1 text-red-400 text-xs"><XCircle size={12} /> Not set</div>}
            </div>
          ))}
        </div>
        <div className="text-[10px] text-slate-600 pt-1">
          Set these in Render Dashboard → Environment. Never commit to git.
          SENDER_EMAIL and RECIPIENT_EMAIL are needed for email alerts.
        </div>
      </Section>

      {/* Trading Mode */}
      <Section title="Trading Mode">
        <Field label="Current Mode" desc="Paper = simulated orders. Live = real money.">
          <div className="flex gap-2">
            {['paper', 'live', 'backtest'].map(m => (
              <button key={m} onClick={() => update(['mode'], m)}
                className={`px-4 py-2 rounded-lg text-xs font-medium border uppercase tracking-wide transition-colors ${
                  settings?.mode === m
                    ? m === 'live'
                      ? 'bg-red-600/20 text-red-400 border-red-600/50'
                      : 'bg-blue-600/20 text-blue-400 border-blue-600/50'
                    : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044]'
                }`}>
                {m}
              </button>
            ))}
          </div>
          {settings?.mode === 'live' && (
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-red-400">
              <AlertTriangle size={11} /> Live mode uses real money. Complete 20+ days of paper trading first.
            </div>
          )}
        </Field>
      </Section>

      <UniverseSection />

      {/* Capital */}
      {cap && (
        <Section title="Capital Settings">
          <Field label="Total Capital (₹)" desc="Your full trading capital">
            <NumInput value={cap.total} onChange={v => update(['capital', 'total'], v)} min={10000} step={10000} />
          </Field>
          <Field label="Max Per Trade (%)" desc="Max % of capital per trade (default 20%)">
            <NumInput value={cap.max_allocation_per_trade * 100}
              onChange={v => update(['capital', 'max_allocation_per_trade'], v / 100)} min={5} max={50} step={5} />
          </Field>
          <Field label="Cash Buffer (%)" desc="Always keep this % as reserve (default 40%)">
            <NumInput value={cap.cash_buffer * 100}
              onChange={v => update(['capital', 'cash_buffer'], v / 100)} min={20} max={80} step={5} />
          </Field>
        </Section>
      )}

      {/* Risk */}
      {risk && (
        <Section title="Risk Management">
          <Field label="Risk Per Trade (%)" desc="Max % of capital to risk per trade (1% recommended)">
            <NumInput value={risk.max_risk_per_trade_pct * 100}
              onChange={v => update(['risk', 'max_risk_per_trade_pct'], v / 100)} min={0.5} max={3} step={0.25} />
          </Field>
          <Field label="Daily Loss Limit (%)" desc="Stop trading when daily loss hits this % (2% recommended)">
            <NumInput value={risk.max_daily_loss_pct * 100}
              onChange={v => update(['risk', 'max_daily_loss_pct'], v / 100)} min={1} max={5} step={0.25} />
          </Field>
          <Field label="Max Trades Per Day">
            <NumInput value={risk.max_trades_per_day}
              onChange={v => update(['risk', 'max_trades_per_day'], v)} min={1} max={10} />
          </Field>
          <Field label="Max Concurrent Positions">
            <NumInput value={risk.max_concurrent_positions}
              onChange={v => update(['risk', 'max_concurrent_positions'], v)} min={1} max={5} />
          </Field>
          <Field label="Max Consecutive Losses" desc="Pause after this many losses in a row">
            <NumInput value={risk.max_consecutive_losses}
              onChange={v => update(['risk', 'max_consecutive_losses'], v)} min={2} max={6} />
          </Field>
        </Section>
      )}

      {/* Strategy Timings */}
      {strat && (
        <Section title="Strategy Time Windows">
          {[
            { label: 'ORB Window Start',  key: 'orb_window_start',   val: strat.orb_window_start   ?? '09:15', desc: 'Opening range recording begins' },
            { label: 'ORB Window End',    key: 'orb_window_end',     val: strat.orb_window_end     ?? '09:30', desc: 'Opening range recording ends' },
            { label: 'Entry Window End',  key: 'entry_window_end',   val: strat.entry_window_end   ?? '12:30', desc: 'No new entries after this time' },
            { label: 'Square Off By',     key: 'exit_all_by',        val: strat.exit_all_by        ?? '14:45', desc: 'Exit all positions before this time' },
          ].map(item => (
            <Field key={item.key} label={item.label} desc={item.desc}>
              <input type="time" value={item.val}
                onChange={e => update(['strategy', item.key], e.target.value)}
                className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
            </Field>
          ))}
        </Section>
      )}

      {/* Indicators */}
      {ind && (
        <Section title="Indicator Parameters">
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            {[
              { label: 'EMA Fast',         key: 'ema_fast',          val: ind.ema_fast          ?? 20,   min: 5,   max: 50,  step: 1 },
              { label: 'EMA Slow',         key: 'ema_slow',          val: ind.ema_slow          ?? 50,   min: 20,  max: 100, step: 1 },
              { label: 'EMA Trend',        key: 'ema_trend',         val: ind.ema_trend         ?? 200,  min: 100, max: 300, step: 10 },
              { label: 'RSI Period',       key: 'rsi_period',        val: ind.rsi_period        ?? 14,   min: 5,   max: 21,  step: 1 },
              { label: 'RSI Min Entry',    key: 'rsi_min',           val: ind.rsi_min           ?? 55,   min: 40,  max: 70,  step: 1 },
              { label: 'RSI Max Entry',    key: 'rsi_max',           val: ind.rsi_max           ?? 75,   min: 60,  max: 90,  step: 1 },
              { label: 'ATR Period',       key: 'atr_period',        val: ind.atr_period        ?? 14,   min: 7,   max: 21,  step: 1 },
              { label: 'Choppiness Max',   key: 'choppiness_max',    val: ind.choppiness_max    ?? 61.8, min: 50,  max: 70,  step: 0.1 },
              { label: 'Volume Mult.',     key: 'volume_multiplier', val: ind.volume_multiplier ?? 1.5,  min: 1,   max: 3,   step: 0.1 },
            ].map(item => (
              <div key={item.key}>
                <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">{item.label}</label>
                <NumInput value={item.val} onChange={v => update(['indicators', item.key], v)}
                  min={item.min} max={item.max} step={item.step} />
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Notifications */}
      {notif && (
        <Section title="Notifications">
          <Field label="Email Alerts" desc="Trade entry/exit alerts and daily summary by email">
            <Toggle value={!!notif.email_enabled} onChange={v => update(['notifications', 'email_enabled'], v)} />
          </Field>
          <Field label="Telegram Alerts" desc="Instant trade alerts on your phone via Telegram">
            <Toggle value={!!notif.telegram_enabled} onChange={v => update(['notifications', 'telegram_enabled'], v)} />
          </Field>
          <div className="text-[10px] text-slate-600 p-2.5 bg-[#0f1628] border border-[#1e2d45] rounded-lg">
            Add SENDER_EMAIL, RECIPIENT_EMAIL, EMAIL_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
            as Render environment variables to enable alerts.
          </div>
        </Section>
      )}
    </div>
  )
}
