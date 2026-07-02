import { useEffect, useState } from 'react'
import { fetchSettings } from '../api/endpoints'
import type { AppSettingsResponse } from '../types'

export default function Settings() {
  const [settings, setSettings] = useState<AppSettingsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const loadSettings = async () => {
      try {
        const data = await fetchSettings()
        setSettings(data)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load settings')
      }
    }

    loadSettings()
  }, [])

  return (
    <div className="space-y-8">
      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">Settings</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">Application configuration</h2>
          </div>
          <p className="max-w-2xl text-sm text-white/70">View the current runtime configuration that supports the trading bot.</p>
        </div>
      </section>

      {error ? (
        <div className="rounded-[2rem] border border-red-500/20 bg-red-950/40 p-8 text-red-200">{error}</div>
      ) : settings ? (
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
            <h3 className="text-xl font-semibold text-white">Runtime mode</h3>
            <div className="mt-4 space-y-3 text-sm text-white/70">
              <div className="flex justify-between border-b border-white/10 pb-3">
                <span>Mode</span>
                <span>{settings.mode}</span>
              </div>
              <div className="flex justify-between border-b border-white/10 pb-3 pt-3">
                <span>API host</span>
                <span>{settings.api_host}</span>
              </div>
              <div className="flex justify-between border-b border-white/10 pb-3 pt-3">
                <span>API port</span>
                <span>{settings.api_port}</span>
              </div>
            </div>
          </div>

          <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
            <h3 className="text-xl font-semibold text-white">Notifications</h3>
            <div className="mt-4 space-y-3 text-sm text-white/70">
              <div className="flex justify-between border-b border-white/10 pb-3">
                <span>Email enabled</span>
                <span>{settings.notifications.email_enabled ? 'Yes' : 'No'}</span>
              </div>
              <div className="flex justify-between pt-3">
                <span>Telegram enabled</span>
                <span>{settings.notifications.telegram_enabled ? 'Yes' : 'No'}</span>
              </div>
            </div>
          </div>

          <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
            <h3 className="text-xl font-semibold text-white">Integration URLs</h3>
            <div className="mt-4 space-y-3 text-sm text-white/70">
              <div className="flex justify-between border-b border-white/10 pb-3">
                <span>Broker</span>
                <span>{settings.broker_base_url}</span>
              </div>
              <div className="flex justify-between border-b border-white/10 pb-3 pt-3">
                <span>WebSocket</span>
                <span>{settings.websocket_url}</span>
              </div>
              <div className="flex justify-between pt-3">
                <span>Frontend</span>
                <span>{settings.frontend_url}</span>
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-[2rem] border border-white/10 bg-card p-8 text-center text-white/70">Loading settings...</div>
      )}
    </div>
  )
}
