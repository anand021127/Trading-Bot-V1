import { useEffect, useState } from 'react'
import { fetchDiagnostics, fetchHealth, fetchSettings } from '../api/endpoints'
import type { DiagnosticsResponse, HealthStatus, AppSettingsResponse } from '../types'

export default function ApiTest() {
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResponse | null>(null)
  const [settings, setSettings] = useState<AppSettingsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = async () => {
      try {
        const [healthData, diagnosticsData, settingsData] = await Promise.all([
          fetchHealth(),
          fetchDiagnostics(),
          fetchSettings(),
        ])
        setHealth(healthData)
        setDiagnostics(diagnosticsData)
        setSettings(settingsData)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to load API diagnostics')
      }
    }

    load()
  }, [])

  return (
    <div className="space-y-8">
      <section className="rounded-[2rem] border border-white/10 bg-card p-8 shadow-glow">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.22em] text-white/50">API test</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">Backend diagnostics</h2>
          </div>
          <p className="max-w-2xl text-sm text-white/70">Verify backend health and inspect available configuration values.</p>
        </div>
      </section>

      {error ? (
        <div className="rounded-[2rem] border border-red-500/20 bg-red-950/40 p-8 text-red-200">{error}</div>
      ) : (
        <div className="grid gap-6 lg:grid-cols-3">
          <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
            <h3 className="text-xl font-semibold text-white">Health</h3>
            <pre className="mt-4 overflow-auto rounded-3xl bg-white/5 p-4 text-sm text-white/70">{health ? JSON.stringify(health, null, 2) : 'Loading...'}</pre>
          </div>
          <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
            <h3 className="text-xl font-semibold text-white">Diagnostics</h3>
            <pre className="mt-4 overflow-auto rounded-3xl bg-white/5 p-4 text-sm text-white/70">{diagnostics ? JSON.stringify(diagnostics, null, 2) : 'Loading...'}</pre>
          </div>
          <div className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
            <h3 className="text-xl font-semibold text-white">Settings</h3>
            <pre className="mt-4 overflow-auto rounded-3xl bg-white/5 p-4 text-sm text-white/70">{settings ? JSON.stringify(settings, null, 2) : 'Loading...'}</pre>
          </div>
        </div>
      )}
    </div>
  )
}
