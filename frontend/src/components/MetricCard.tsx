interface MetricCardProps {
  label: string
  value: string
  delta?: string
  description?: string
}

export default function MetricCard({ label, value, delta, description }: MetricCardProps) {
  return (
    <div className="rounded-3xl border border-white/10 bg-card p-6 shadow-glow transition hover:border-white/20 hover:bg-card-hover">
      <div className="text-sm uppercase tracking-[0.24em] text-white/60">{label}</div>
      <div className="mt-4 flex items-baseline gap-2">
        <span className="text-3xl font-semibold text-white">{value}</span>
        {delta ? <span className="rounded-full bg-white/10 px-3 py-1 text-xs uppercase tracking-[0.18em] text-white/80">{delta}</span> : null}
      </div>
      {description ? <p className="mt-3 text-sm text-white/60">{description}</p> : null}
    </div>
  )
}
