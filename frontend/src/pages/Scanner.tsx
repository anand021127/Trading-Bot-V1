import { useState, useCallback } from 'react'
import { Activity, CheckCircle, XCircle, MinusCircle, RefreshCw, AlertTriangle } from 'lucide-react'
import { fetchScannerStatus, triggerScanNow } from '../api/endpoints'
import { usePolling } from '../hooks/usePolling'
import { formatCurrency } from '../utils/formatters'
import type { ScannerStatus, ScannerEntry } from '../types'
import toast from 'react-hot-toast'

function StatusPill({ value }: { value: string }) {
  if (value === 'PASS') {
    return <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-emerald-400"><CheckCircle size={11} /> PASS</span>
  }
  if (value === 'FAILED') {
    return <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-red-400"><XCircle size={11} /> FAILED</span>
  }
  return <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-slate-500"><MinusCircle size={11} /> N/A</span>
}

function ScannerRow({ entry }: { entry: ScannerEntry }) {
  const [expanded, setExpanded] = useState(false)
  const isBuy = entry.signal === 'BUY'

  return (
    <div className={`border rounded-lg ${isBuy ? 'border-emerald-800/50 bg-emerald-950/10' : 'border-[#1e2d45] bg-[#141b2d]'}`}>
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <div className="flex items-center gap-3 min-w-0">
          <div className="text-sm font-semibold text-white w-20 flex-shrink-0">{entry.symbol}</div>
          <div className="text-xs text-slate-400 w-20 flex-shrink-0">{entry.ltp != null ? formatCurrency(entry.ltp) : '—'}</div>
          <div className="flex items-center gap-3 flex-shrink-0">
            <div className="flex flex-col items-center gap-0.5">
              <span className="text-[9px] text-slate-600 uppercase">EMA</span>
              <StatusPill value={entry.ema_status} />
            </div>
            <div className="flex flex-col items-center gap-0.5">
              <span className="text-[9px] text-slate-600 uppercase">RSI {entry.rsi_value?.toFixed(0) ?? '—'}</span>
              <StatusPill value={entry.rsi_status} />
            </div>
            <div className="flex flex-col items-center gap-0.5">
              <span className="text-[9px] text-slate-600 uppercase">Volume</span>
              <StatusPill value={entry.volume_status} />
            </div>
          </div>
        </div>
        <div className={`text-xs font-medium truncate ml-2 ${isBuy ? 'text-emerald-400' : entry.error ? 'text-red-400' : 'text-slate-500'}`}>
          {entry.error ? `ERROR — ${entry.error}` : entry.decision}
        </div>
      </button>

      {expanded && (
        <div className="border-t border-[#1e2d45] px-4 py-3 space-y-2">
          {entry.strategy_breakdown.map(s => (
            <div key={s.strategy_name} className="text-xs">
              <div className="flex items-center justify-between">
                <span className="font-medium text-slate-300">{s.strategy_name}</span>
                <span className="text-slate-500">{s.conditions_passed}/{s.conditions_total} conditions · {s.confidence.toFixed(0)}% confidence</span>
              </div>
              {s.rejected_reasons.length > 0 && (
                <ul className="mt-1 space-y-0.5 text-[11px] text-amber-400/80 list-disc list-inside">
                  {s.rejected_reasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Scanner() {
  const [data, setData] = useState<ScannerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)

  const load = useCallback(async () => {
    try {
      const d = await fetchScannerStatus()
      setData(d)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load scanner status')
    } finally {
      setLoading(false)
    }
  }, [])

  usePolling(load, 4000)

  const scanNow = async () => {
    setScanning(true)
    try {
      await triggerScanNow()
      await load()
      toast.success('Scan complete')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Scan failed')
    } finally {
      setScanning(false)
    }
  }

  const results = data?.results ?? []
  const buySignals = results.filter(r => r.signal === 'BUY').length

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Live Scanner</h1>
          <p className="text-xs text-slate-500 mt-0.5">What the bot is analyzing right now — every symbol, every reason</p>
        </div>
        <button
          onClick={scanNow}
          disabled={scanning}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-blue-700/50 bg-blue-950/30 text-blue-400 text-xs font-medium hover:bg-blue-950/50 disabled:opacity-50"
        >
          <RefreshCw size={12} className={scanning ? 'animate-spin' : ''} />
          Scan Now
        </button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {[
          { label: 'Status', val: data?.is_running ? 'Running' : 'Stopped', ok: data?.is_running ?? false },
          { label: 'Watching', val: `${data?.watching_count ?? 0} symbols`, ok: (data?.watching_count ?? 0) > 0 },
          { label: 'Currently Analyzing', val: data?.currently_scanning ?? 'Idle', ok: !!data?.currently_scanning },
          { label: 'Buy Signals', val: String(buySignals), ok: buySignals > 0 },
        ].map(item => (
          <div key={item.label} className="bg-[#141b2d] border border-[#1e2d45] rounded-lg px-3 py-2 flex items-center gap-2">
            <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${item.ok ? 'bg-emerald-400' : 'bg-slate-600'}`} />
            <div className="min-w-0">
              <div className="text-[10px] text-slate-500">{item.label}</div>
              <div className="text-xs font-medium text-slate-200 truncate">{item.val}</div>
            </div>
          </div>
        ))}
      </div>

      {loading ? (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-8 text-center text-slate-500 text-sm">Loading scanner status...</div>
      ) : error ? (
        <div className="bg-amber-950/30 border border-amber-800/50 rounded-xl p-5">
          <div className="flex items-start gap-2 text-amber-300">
            <AlertTriangle size={14} className="flex-shrink-0 mt-0.5" />
            <div className="text-xs font-medium">{error}</div>
          </div>
        </div>
      ) : results.length === 0 ? (
        <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-8 text-center text-slate-500 text-sm">
          <Activity className="mx-auto mb-2 text-slate-600" size={24} />
          No scan results yet. The scanner runs continuously in the background — check back shortly, or click "Scan Now".
        </div>
      ) : (
        <div className="space-y-2">
          {results.map(entry => <ScannerRow key={entry.symbol} entry={entry} />)}
        </div>
      )}
    </div>
  )
}
