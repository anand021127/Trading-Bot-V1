import { useState, useCallback, useEffect } from 'react'
import { Save, RefreshCw, ExternalLink, CheckCircle, XCircle, AlertTriangle } from 'lucide-react'
import { fetchSettings, updateSettings, fetchEnvStatus, regenerateToken } from '../api/endpoints'
import type { Settings } from '../types'
import toast from 'react-hot-toast'

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

function NumberInput({ value, onChange, min, max, step }: { value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number }) {
  return (
    <input
      type="number" value={value}
      onChange={e => onChange(Number(e.target.value))}
      min={min} max={max} step={step ?? 1}
      className="w-full bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50"
    />
  )
}

export default function Settings() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [envStatus, setEnvStatus] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [tokenLoading, setTokenLoading] = useState(false)
  const [restartRequired, setRestartRequired] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
  }

  const handleSave = async () => {
    if (!settings) return
    setSaving(true)
    try {
      const res = await updateSettings(settings)
      setRestartRequired(res.restart_required ?? false)
      toast.success('Settings saved successfully')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to save settings')
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
        toast.success('Auth page opened. Approve access and the token will be saved automatically.')
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Failed to generate token URL')
    } finally {
      setTokenLoading(false)
    }
  }

  const envVars = [
    { key: 'UPSTOX_CLIENT_ID', label: 'Upstox Client ID' },
    { key: 'UPSTOX_CLIENT_SECRET', label: 'Upstox Client Secret' },
    { key: 'UPSTOX_ACCESS_TOKEN', label: 'Access Token (daily)' },
    { key: 'EMAIL_PASSWORD', label: 'Gmail App Password' },
    { key: 'TELEGRAM_BOT_TOKEN', label: 'Telegram Bot Token' },
    { key: 'TELEGRAM_CHAT_ID', label: 'Telegram Chat ID' },
  ]

  if (loading) return (
    <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-10 text-center text-slate-500 text-sm">Loading settings...</div>
  )

  if (error) return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold text-white">Settings</h1>
      <div className="bg-red-950/30 border border-red-800/50 rounded-xl p-5 text-red-300 text-sm">
        <div className="font-medium mb-1">Could not load settings</div>
        <div className="text-red-400/70 text-xs">{error}</div>
        <button onClick={load} className="mt-3 text-xs text-red-300 hover:text-red-200 underline">Retry</button>
      </div>
    </div>
  )

  const cap = settings?.capital
  const risk = settings?.risk
  const strat = settings?.strategy
  const ind = settings?.indicators
  const notif = settings?.notifications

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Settings</h1>
          <p className="text-xs text-slate-500 mt-0.5">Configure bot parameters — changes require restart</p>
        </div>
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-medium rounded-lg transition-colors"
        >
          {saving ? <><RefreshCw size={13} className="animate-spin" /> Saving...</> : <><Save size={13} /> Save Settings</>}
        </button>
      </div>

      {restartRequired && (
        <div className="flex items-center gap-2 bg-amber-950/30 border border-amber-800/50 rounded-xl px-4 py-3 text-amber-300 text-xs">
          <AlertTriangle size={13} />
          Settings saved. Restart the backend worker for changes to take effect.
        </div>
      )}

      {/* Environment Variables */}
      <Section title="Environment Variables">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {envVars.map(v => (
            <div key={v.key} className="flex items-center justify-between p-2.5 rounded-lg bg-[#0f1628] border border-[#1e2d45]">
              <div>
                <div className="text-xs font-medium text-slate-300">{v.label}</div>
                <div className="text-[10px] font-mono text-slate-600 mt-0.5">{v.key}</div>
              </div>
              {envStatus[v.key] ? (
                <div className="flex items-center gap-1.5 text-emerald-400 text-xs"><CheckCircle size={13} /> Set</div>
              ) : (
                <div className="flex items-center gap-1.5 text-red-400 text-xs"><XCircle size={13} /> Not set</div>
              )}
            </div>
          ))}
        </div>
        <div className="text-[10px] text-slate-600 pt-1">
          Environment variables are set in your .env file (local) or Render / Vercel dashboard (production). Never stored in settings.yaml.
        </div>
      </Section>

      {/* Token */}
      <Section title="Upstox Access Token">
        <div className="flex items-start gap-4">
          <div className="flex-1">
            <div className="text-xs text-slate-400 mb-3">
              Upstox access tokens expire every 24 hours. Regenerate every morning before market opens (before 9:00 AM IST). The token is automatically saved after you authorize.
            </div>
            <div className="flex items-center gap-2">
              {envStatus['UPSTOX_ACCESS_TOKEN'] ? (
                <div className="flex items-center gap-1.5 text-emerald-400 text-xs"><CheckCircle size={13} /> Token is set</div>
              ) : (
                <div className="flex items-center gap-1.5 text-red-400 text-xs"><XCircle size={13} /> Token missing — bot cannot trade</div>
              )}
            </div>
          </div>
          <button
            onClick={handleRegenToken}
            disabled={tokenLoading}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0f1628] border border-[#1e2d45] rounded-lg text-xs text-slate-300 hover:text-white hover:border-blue-600/50 transition-colors whitespace-nowrap"
          >
            {tokenLoading ? <RefreshCw size={12} className="animate-spin" /> : <ExternalLink size={12} />}
            Generate Token
          </button>
        </div>
      </Section>

      {/* Trading Mode */}
      <Section title="Trading Mode">
        <Field label="Current Mode" desc="Switch between paper trading and live trading">
          <div className="flex gap-2">
            {['paper', 'live', 'backtest'].map(m => (
              <button
                key={m}
                onClick={() => update(['mode'], m)}
                className={`px-4 py-2 rounded-lg text-xs font-medium border transition-colors uppercase tracking-wide ${
                  settings?.mode === m
                    ? m === 'live' ? 'bg-red-600/20 text-red-400 border-red-600/50' : 'bg-blue-600/20 text-blue-400 border-blue-600/50'
                    : 'bg-[#0f1628] text-slate-500 border-[#1e2d45] hover:border-[#243044]'
                }`}
              >
                {m}
              </button>
            ))}
          </div>
          {settings?.mode === 'live' && (
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-red-400">
              <AlertTriangle size={11} /> Live mode uses real money. Only switch after completing 20+ days of paper trading.
            </div>
          )}
        </Field>
      </Section>

      {/* Capital */}
      {cap && (
        <Section title="Capital Settings">
          <Field label="Total Capital (₹)" desc="Your full trading capital">
            <NumberInput value={cap.total} onChange={v => update(['capital', 'total'], v)} min={10000} step={10000} />
          </Field>
          <Field label="Max Per Trade (%)" desc="Maximum % of capital allocated to a single trade (default 20%)">
            <NumberInput value={cap.max_allocation_per_trade * 100} onChange={v => update(['capital', 'max_allocation_per_trade'], v / 100)} min={5} max={50} step={5} />
          </Field>
          <Field label="Cash Buffer (%)" desc="Always keep this % as cash reserve (default 40%)">
            <NumberInput value={cap.cash_buffer * 100} onChange={v => update(['capital', 'cash_buffer'], v / 100)} min={20} max={80} step={5} />
          </Field>
        </Section>
      )}

      {/* Risk */}
      {risk && (
        <Section title="Risk Management">
          <Field label="Risk Per Trade (%)" desc="Max % of capital to risk per trade (1% recommended)">
            <NumberInput value={risk.max_risk_per_trade_pct * 100} onChange={v => update(['risk', 'max_risk_per_trade_pct'], v / 100)} min={0.5} max={3} step={0.25} />
          </Field>
          <Field label="Daily Loss Limit (%)" desc="Stop trading when daily loss hits this % (2% recommended)">
            <NumberInput value={risk.max_daily_loss_pct * 100} onChange={v => update(['risk', 'max_daily_loss_pct'], v / 100)} min={1} max={5} step={0.25} />
          </Field>
          <Field label="Max Trades Per Day" desc="Maximum number of new trades per day">
            <NumberInput value={risk.max_trades_per_day} onChange={v => update(['risk', 'max_trades_per_day'], v)} min={1} max={10} />
          </Field>
          <Field label="Max Concurrent Positions" desc="Maximum simultaneously open positions">
            <NumberInput value={risk.max_concurrent_positions} onChange={v => update(['risk', 'max_concurrent_positions'], v)} min={1} max={5} />
          </Field>
          <Field label="Max Consecutive Losses" desc="Pause trading after this many losses in a row">
            <NumberInput value={risk.max_consecutive_losses} onChange={v => update(['risk', 'max_consecutive_losses'], v)} min={2} max={6} />
          </Field>
        </Section>
      )}

      {/* Strategy Timings */}
      {strat && (
        <Section title="Strategy Time Windows">
          <Field label="ORB Window Start" desc="Opening range recording begins (default 09:15)">
            <input type="time" value={strat.orb_window_start ?? '09:15'} onChange={e => update(['strategy', 'orb_window_start'], e.target.value)}
              className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
          </Field>
          <Field label="ORB Window End" desc="Opening range recording ends (default 09:30)">
            <input type="time" value={strat.orb_window_end ?? '09:30'} onChange={e => update(['strategy', 'orb_window_end'], e.target.value)}
              className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
          </Field>
          <Field label="Entry Window End" desc="No new entries after this time (default 12:30)">
            <input type="time" value={strat.entry_window_end ?? '12:30'} onChange={e => update(['strategy', 'entry_window_end'], e.target.value)}
              className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
          </Field>
          <Field label="Square Off By" desc="Exit all positions before this time (default 14:45)">
            <input type="time" value={strat.exit_all_by ?? '14:45'} onChange={e => update(['strategy', 'exit_all_by'], e.target.value)}
              className="bg-[#0f1628] border border-[#1e2d45] rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-600/50" />
          </Field>
        </Section>
      )}

      {/* Indicators */}
      {ind && (
        <Section title="Indicator Parameters">
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            {[
              { label: 'EMA Fast Period', key: 'ema_fast', val: ind.ema_fast ?? 20, min: 5, max: 50 },
              { label: 'EMA Slow Period', key: 'ema_slow', val: ind.ema_slow ?? 50, min: 20, max: 100 },
              { label: 'EMA Trend Period', key: 'ema_trend', val: ind.ema_trend ?? 200, min: 100, max: 300 },
              { label: 'RSI Period', key: 'rsi_period', val: ind.rsi_period ?? 14, min: 5, max: 21 },
              { label: 'RSI Min Entry', key: 'rsi_min', val: ind.rsi_min ?? 55, min: 40, max: 70 },
              { label: 'RSI Max Entry', key: 'rsi_max', val: ind.rsi_max ?? 75, min: 60, max: 90 },
              { label: 'ATR Period', key: 'atr_period', val: ind.atr_period ?? 14, min: 7, max: 21 },
              { label: 'Choppiness Max', key: 'choppiness_max', val: ind.choppiness_max ?? 61.8, min: 50, max: 70, step: 0.1 },
              { label: 'Volume Multiplier', key: 'volume_multiplier', val: ind.volume_multiplier ?? 1.5, min: 1, max: 3, step: 0.1 },
            ].map(item => (
              <div key={item.key}>
                <label className="text-[10px] text-slate-500 uppercase tracking-widest block mb-1.5">{item.label}</label>
                <NumberInput value={item.val} onChange={v => update(['indicators', item.key], v)} min={item.min} max={item.max} step={item.step} />
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Notifications */}
      {notif && (
        <Section title="Notifications">
          <Field label="Email Alerts" desc="Send trade entry/exit alerts and daily summary by email">
            <button
              onClick={() => update(['notifications', 'email_enabled'], !notif.email_enabled)}
              className={`relative w-11 h-6 rounded-full transition-colors ${notif.email_enabled ? 'bg-blue-600' : 'bg-[#1e2d45]'}`}
            >
              <div className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${notif.email_enabled ? 'translate-x-5' : 'translate-x-0'}`} />
            </button>
          </Field>
          <Field label="Telegram Alerts" desc="Instant trade alerts on your phone via Telegram">
            <button
              onClick={() => update(['notifications', 'telegram_enabled'], !notif.telegram_enabled)}
              className={`relative w-11 h-6 rounded-full transition-colors ${notif.telegram_enabled ? 'bg-blue-600' : 'bg-[#1e2d45]'}`}
            >
              <div className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${notif.telegram_enabled ? 'translate-x-5' : 'translate-x-0'}`} />
            </button>
          </Field>
        </Section>
      )}
    </div>
  )
}
