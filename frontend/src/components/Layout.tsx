import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, ClipboardList, TrendingUp, FileText,
  BarChart2, PieChart, Wrench, Settings, Menu, X, Wifi, WifiOff,
} from 'lucide-react'
import { useState } from 'react'
import { Toaster } from 'react-hot-toast'
import { useWebSocket } from '../hooks/useWebSocket'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Overview' },
  { to: '/trade-history', icon: ClipboardList, label: 'Trade History' },
  { to: '/live-premiums', icon: TrendingUp, label: 'Live Premiums' },
  { to: '/paper-trading', icon: FileText, label: 'Paper Trading' },
  { to: '/backtest', icon: BarChart2, label: 'Backtest' },
  { to: '/performance', icon: PieChart, label: 'Performance' },
  { to: '/api-test', icon: Wrench, label: 'API Test' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

export default function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const { status } = useWebSocket()
  const location = useLocation()
  const currentPage = navItems.find((n) =>
    n.to === '/' ? location.pathname === '/' : location.pathname.startsWith(n.to)
  )?.label ?? 'Dashboard'

  const connected = status === 'connected'

  return (
    <div className="flex h-screen overflow-hidden bg-[#0a0e1a] text-slate-200">
      {/* Desktop sidebar */}
      <aside className="hidden md:flex flex-col w-56 bg-[#0d1424] border-r border-[#1e2d45] flex-shrink-0">
        <div className="px-4 py-5 border-b border-[#1e2d45]">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-sm flex-shrink-0">U</div>
            <div>
              <div className="text-sm font-semibold text-white leading-tight">Upstox Bot</div>
              <div className="text-[10px] text-slate-500 uppercase tracking-widest">Trading System</div>
            </div>
          </div>
        </div>

        <nav className="flex-1 py-3 px-2 space-y-0.5 overflow-y-auto">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm transition-all ${
                  isActive
                    ? 'bg-blue-600/20 text-blue-400 font-medium border border-blue-600/25'
                    : 'text-slate-400 hover:bg-[#141b2d] hover:text-slate-200'
                }`
              }
            >
              <Icon size={15} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="px-4 py-3 border-t border-[#1e2d45]">
          <div className="flex items-center gap-2 text-xs">
            {connected ? (
              <><Wifi size={11} className="text-emerald-400" /><span className="text-emerald-400">Live</span></>
            ) : (
              <><WifiOff size={11} className="text-red-400" /><span className="text-red-400">Offline</span></>
            )}
          </div>
        </div>
      </aside>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 flex md:hidden">
          <div className="absolute inset-0 bg-black/70" onClick={() => setMobileOpen(false)} />
          <aside className="relative w-64 bg-[#0d1424] border-r border-[#1e2d45] flex flex-col z-10">
            <div className="px-4 py-4 border-b border-[#1e2d45] flex items-center justify-between">
              <span className="font-semibold text-white text-sm">Menu</span>
              <button onClick={() => setMobileOpen(false)} className="text-slate-400 hover:text-white p-1">
                <X size={18} />
              </button>
            </div>
            <nav className="flex-1 py-3 px-2 space-y-0.5 overflow-y-auto">
              {navItems.map(({ to, icon: Icon, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={to === '/'}
                  onClick={() => setMobileOpen(false)}
                  className={({ isActive }) =>
                    `flex items-center gap-2.5 px-3 py-3 rounded-lg text-sm transition-all ${
                      isActive ? 'bg-blue-600/20 text-blue-400 font-medium' : 'text-slate-400 hover:bg-[#141b2d] hover:text-slate-200'
                    }`
                  }
                >
                  <Icon size={15} />
                  <span>{label}</span>
                </NavLink>
              ))}
            </nav>
          </aside>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Topbar */}
        <header className="flex items-center gap-3 px-4 py-3 bg-[#0d1424] border-b border-[#1e2d45] flex-shrink-0">
          <button
            onClick={() => setMobileOpen(true)}
            className="md:hidden text-slate-400 hover:text-white p-1 min-w-[44px] min-h-[44px] flex items-center justify-center"
          >
            <Menu size={20} />
          </button>

          <div className="flex items-center gap-1.5 text-sm min-w-0">
            <span className="text-slate-500 hidden sm:inline">Bot</span>
            <span className="text-slate-600 hidden sm:inline">/</span>
            <span className="text-slate-200 font-medium truncate">{currentPage}</span>
          </div>

          <div className="flex-1" />

          <span className="px-2 py-1 rounded text-[10px] font-medium bg-amber-500/15 text-amber-400 border border-amber-500/25 uppercase tracking-wide">
            PAPER
          </span>

          <div className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full ${connected ? 'bg-emerald-950/50 text-emerald-400' : 'bg-red-950/50 text-red-400'}`}>
            <div className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400 animate-pulse' : 'bg-red-400'}`} />
            <span className="hidden sm:inline">{connected ? 'Live' : 'Offline'}</span>
          </div>

          <span className="text-xs text-slate-500 hidden sm:block tabular-nums">
            {new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST
          </span>
        </header>

        <main className="flex-1 overflow-y-auto p-4 md:p-6">
          <Outlet />
        </main>
      </div>

      <Toaster
        position="top-right"
        toastOptions={{
          style: { background: '#141b2d', color: '#e2e8f0', border: '1px solid #1e2d45', fontSize: '13px' },
          success: { iconTheme: { primary: '#10b981', secondary: '#141b2d' } },
          error: { iconTheme: { primary: '#ef4444', secondary: '#141b2d' } },
        }}
      />
    </div>
  )
}
