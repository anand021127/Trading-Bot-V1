import { NavLink } from 'react-router-dom'

const links = [
  { label: 'Overview', path: '/' },
  { label: 'Trade History', path: '/trade-history' },
  { label: 'Live Premiums', path: '/live-premiums' },
  { label: 'Paper Trading', path: '/paper-trading' },
  { label: 'Backtest', path: '/backtest' },
  { label: 'Performance', path: '/performance' },
  { label: 'API Test', path: '/api-test' },
  { label: 'Settings', path: '/settings' },
]

interface LayoutProps {
  children: React.ReactNode
}

export default function Layout({ children }: LayoutProps) {
  return (
    <div className="min-h-screen bg-primary text-white">
      <div className="mx-auto flex min-h-screen max-w-[1440px] flex-col gap-6 px-4 py-6 sm:px-6 lg:px-10">
        <header className="rounded-[2rem] border border-white/10 bg-card p-6 shadow-glow">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm uppercase tracking-[0.22em] text-white/50">Upstox trading bot</p>
              <h1 className="mt-2 text-3xl font-semibold text-white">Live trade operations</h1>
            </div>
            <div className="text-sm text-white/70">Professional bot operations with live status and analytics.</div>
          </div>
        </header>

        <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
          <aside className="rounded-[2rem] border border-white/10 bg-sidebar p-6 shadow-glow">
            <div className="mb-6 space-y-3">
              <p className="text-xs uppercase tracking-[0.24em] text-white/50">Navigation</p>
              <h2 className="text-xl font-semibold text-white">Workbench</h2>
            </div>
            <nav className="space-y-2">
              {links.map((link) => (
                <NavLink
                  key={link.path}
                  to={link.path}
                  className={({ isActive }) =>
                    `block rounded-3xl border border-white/10 px-4 py-3 text-sm font-medium transition ${
                      isActive ? 'bg-white/10 text-white' : 'text-white/70 hover:bg-white/5 hover:text-white'
                    }`
                  }
                >
                  {link.label}
                </NavLink>
              ))}
            </nav>
          </aside>

          <main className="space-y-6">{children}</main>
        </div>
      </div>
    </div>
  )
}
