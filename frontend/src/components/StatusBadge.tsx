interface StatusBadgeProps {
  status: 'ACTIVE' | 'PAUSED' | 'STOPPED' | 'DISCONNECTED'
}

const colorMap: Record<string, string> = {
  ACTIVE: 'bg-emerald-500 text-emerald-50',
  PAUSED: 'bg-amber-500 text-amber-50',
  STOPPED: 'bg-red-500 text-red-50',
  DISCONNECTED: 'bg-slate-500 text-slate-50',
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.22em] ${colorMap[status] ?? colorMap.DISCONNECTED}`}>
      {status}
    </span>
  )
}
