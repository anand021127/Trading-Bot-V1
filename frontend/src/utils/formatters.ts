export const formatCurrency = (value: number | null | undefined): string => {
  if (value == null) return '—'
  const abs = Math.abs(value)
  return `${value < 0 ? '-' : ''}₹${abs.toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

export const formatPercent = (value: number | null | undefined): string => {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

export const formatR = (value: number | null | undefined): string => {
  if (value == null) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}R`
}

export const formatDate = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric',
  })
}

export const formatTime = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata',
  })
}

export const formatDateTime = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  return `${formatDate(iso)} ${formatTime(iso)}`
}

export const formatDuration = (minutes: number | null | undefined): string => {
  if (minutes == null) return '—'
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

export const formatVolume = (v: number): string => {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(0)}K`
  return `${v}`
}

// ── Common trade metadata model display helpers ────────────────────────────
// Absent historical metadata renders as N/A — never an invented value.

export const HISTORICAL_NOTE = 'N/A / Historical metadata unavailable'

export const formatStrike = (value: number | string | null | undefined): string => {
  if (value == null || value === '') return 'N/A'
  const n = Number(value)
  if (Number.isNaN(n)) return 'N/A'
  return n.toLocaleString('en-IN', { maximumFractionDigits: 2 })
}

export const formatContractLabel = (
  strike: number | string | null | undefined,
  optionType: string | null | undefined,
): string => {
  const hasStrike = strike != null && strike !== ''
  const hasType = !!optionType
  if (!hasStrike && !hasType) return HISTORICAL_NOTE
  if (!hasStrike) return String(optionType)
  return `${formatStrike(strike)} ${optionType ?? ''}`.trim()
}

export const formatQty = (value: number | null | undefined): string => {
  if (value == null) return 'N/A'
  return `${value} qty`
}

export const pnlColor = (value: number | null | undefined): string => {
  if (value == null) return 'text-slate-400'
  return value >= 0 ? 'text-emerald-400' : 'text-red-400'
}

export const pnlBg = (value: number | null | undefined): string => {
  if (value == null) return ''
  return value >= 0 ? 'bg-emerald-950/20' : 'bg-red-950/20'
}

export function classNames(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(' ')
}
