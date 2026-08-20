interface MetricCardProps {
  title: string
  value: string
  sub?: string
  valueColor?: string
  icon?: React.ReactNode
  badge?: { label: string; cls: string }
}

export default function MetricCard({ title, value, sub, valueColor = 'text-white', icon, badge }: MetricCardProps) {
  return (
    <div className="bg-[#141b2d] border border-[#1e2d45] rounded-xl p-4 hover:border-[#243044] transition-colors">
      <div className="flex items-start justify-between mb-2">
        <span className="text-[10px] text-slate-500 uppercase tracking-widest font-medium">{title}</span>
        {icon && <span className="text-slate-600">{icon}</span>}
        {badge && (
          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${badge.cls}`}>{badge.label}</span>
        )}
      </div>
      <div className={`text-xl font-bold ${valueColor} mb-0.5`}>{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}
