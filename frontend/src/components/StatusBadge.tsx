const STATUS_MAP: Record<string, string> = {
  ACTIVE:    'bg-emerald-950/50 text-emerald-400 border-emerald-800/50',
  PAUSED:    'bg-amber-950/50 text-amber-400 border-amber-800/50',
  STOPPED:   'bg-red-950/50 text-red-400 border-red-800/50',
  BULLISH:   'bg-emerald-950/50 text-emerald-400 border-emerald-800/50',
  NEUTRAL:   'bg-slate-800/50 text-slate-400 border-slate-700/50',
  BEARISH:   'bg-red-950/50 text-red-400 border-red-800/50',
  PAPER:     'bg-amber-950/50 text-amber-400 border-amber-800/50',
  LIVE:      'bg-red-950/50 text-red-400 border-red-800/50',
  PASS:      'bg-emerald-950/50 text-emerald-400 border-emerald-800/50',
  FAIL:      'bg-red-950/50 text-red-400 border-red-800/50',
  RUNNING:   'bg-blue-950/50 text-blue-400 border-blue-800/50',
  PENDING:   'bg-slate-800/50 text-slate-400 border-slate-700/50',
  WATCHING:  'bg-blue-950/50 text-blue-400 border-blue-800/50',
  IN_TRADE:  'bg-emerald-950/50 text-emerald-400 border-emerald-800/50',
  SKIPPED:   'bg-slate-800/50 text-slate-500 border-slate-700/50',
  connected: 'bg-emerald-950/50 text-emerald-400 border-emerald-800/50',
  connecting:'bg-amber-950/50 text-amber-400 border-amber-800/50',
  error:     'bg-red-950/50 text-red-400 border-red-800/50',
  closed:    'bg-slate-800/50 text-slate-400 border-slate-700/50',
}

interface Props {
  status: string
  size?: 'sm' | 'md'
}

export default function StatusBadge({ status, size = 'sm' }: Props) {
  const cls = STATUS_MAP[status] ?? 'bg-slate-800/50 text-slate-400 border-slate-700/50'
  return (
    <span className={`inline-flex items-center font-medium rounded border uppercase tracking-widest ${size === 'sm' ? 'text-[9px] px-1.5 py-0.5' : 'text-[11px] px-2 py-1'} ${cls}`}>
      {status}
    </span>
  )
}
