import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, ClipboardList, FileText,
  BarChart2, PieChart, Wrench, Settings, Menu, X, Wifi, WifiOff, Radar, Table2,
  AlertTriangle, KeyRound, Moon, RefreshCw, Download, Sparkles, Smartphone, Zap,
} from 'lucide-react'
import { useState } from 'react'
import { Toaster } from 'react-hot-toast'
import { useConnection, type ConnectionState } from '../context/ConnectionContext'
import { usePWA } from '../hooks/usePWA'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Overview' },
  { to: '/live-premiums', icon: Zap, label: 'Live Premiums' },
  { to: '/option-chain', icon: Table2, label: 'Option Chain' },
  { to: '/scanner', icon: Radar, label: 'Option Scanner' },
  { to: '/trade-history', icon: ClipboardList, label: 'Trade History' },
  { to: '/paper-trading', icon: FileText, label: 'Paper Trading' },
  { to: '/backtest', icon: BarChart2, label: 'Backtest' },
  { to: '/performance', icon: PieChart, label: 'Performance' },
  { to: '/api-test', icon: Wrench, label: 'API Test' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

const bottomBarItems = [
  { to: '/option-chain', icon: Table2, label: 'Options' },
  { to: '/', icon: LayoutDashboard, label: 'Overview' },
  { to: '/scanner', icon: Radar, label: 'Scanner' },
  { to: '/paper-trading', icon: FileText, label: 'Trades' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

function StatusBadge({ status, isMarketOpen, onFixAuth }: { status: ConnectionState; isMarketOpen: boolean; onFixAuth?: () => void }) {
  switch (status) {
    case 'CONNECTED':
      return (
        <div className="flex items-center gap-1.5 text-[11px] sm:text-xs px-2 sm:px-2.5 py-1 rounded-full bg-emerald-950/60 text-emerald-400 border border-emerald-800/40 whitespace-nowrap">
          <div className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
          <span className="font-medium">Live</span>
        </div>
      )
    case 'MARKET_CLOSED':
      return (
        <div className="flex items-center gap-1.5 text-[11px] sm:text-xs px-2 sm:px-2.5 py-1 rounded-full bg-slate-800/80 text-slate-300 border border-slate-700/60 whitespace-nowrap">
          <Moon size={11} className="text-blue-400" />
          <span className="font-medium">Market Closed</span>
        </div>
      )
    case 'AUTH_EXPIRED':
      return (
        <button
          onClick={onFixAuth}
          className="flex items-center gap-1.5 text-[11px] sm:text-xs px-2 sm:px-2.5 py-1 rounded-full bg-amber-950/80 text-amber-300 border border-amber-600/50 hover:bg-amber-900/60 transition-colors whitespace-nowrap"
          title="Click to refresh Upstox token in Settings"
        >
          <KeyRound size={11} className="text-amber-400" />
          <span className="font-medium">Auth Expired</span>
        </button>
      )
    case 'DEGRADED':
      return (
        <div className="flex items-center gap-1.5 text-[11px] sm:text-xs px-2 sm:px-2.5 py-1 rounded-full bg-amber-950/60 text-amber-400 border border-amber-800/40 whitespace-nowrap">
          <AlertTriangle size={11} />
          <span className="font-medium">Polling Mode</span>
        </div>
      )
    case 'RECONNECTING':
      return (
        <div className="flex items-center gap-1.5 text-[11px] sm:text-xs px-2 sm:px-2.5 py-1 rounded-full bg-blue-950/60 text-blue-400 border border-blue-800/40 whitespace-nowrap">
          <RefreshCw size={11} className="animate-spin" />
          <span className="font-medium">Reconnecting</span>
        </div>
      )
    case 'BACKEND_UNAVAILABLE':
      return (
        <div className="flex items-center gap-1.5 text-[11px] sm:text-xs px-2 sm:px-2.5 py-1 rounded-full bg-red-950/60 text-red-400 border border-red-800/40 whitespace-nowrap">
          <WifiOff size={11} />
          <span className="font-medium">Backend Offline</span>
        </div>
      )
    case 'OFFLINE':
    default:
      return (
        <div className="flex items-center gap-1.5 text-[11px] sm:text-xs px-2 sm:px-2.5 py-1 rounded-full bg-red-950/60 text-red-400 border border-red-800/40 whitespace-nowrap">
          <WifiOff size={11} />
          <span className="font-medium">Offline</span>
        </div>
      )
  }
}

export default function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [showIOSModal, setShowIOSModal] = useState(false)
  const { status, isMarketOpen } = useConnection()
  const { isInstallable, isInstalled, isUpdateAvailable, isIOS, installApp, applyUpdate } = usePWA()
  const location = useLocation()
  const navigate = useNavigate()

  const currentPage = navItems.find((n) =>
    n.to === '/' ? location.pathname === '/' : location.pathname.startsWith(n.to)
  )?.label ?? 'Dashboard'

  const handleInstallClick = async () => {
    if (isIOS) {
      setShowIOSModal(true)
    } else {
      await installApp()
    }
  }

  return (
    <div className="flex h-screen overflow-hidden bg-[#0a0e1a] text-slate-200">
      {/* Desktop sidebar */}
      <aside className="hidden md:flex flex-col w-56 bg-[#0d1424] border-r border-[#1e2d45] flex-shrink-0">
        <div className="px-4 py-5 border-b border-[#1e2d45]">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-600 to-cyan-600 flex items-center justify-center text-white font-bold text-sm flex-shrink-0 shadow-sm shadow-blue-500/20">
              <Table2 size={16} />
            </div>
            <div>
              <div className="text-sm font-semibold text-white leading-tight">Upstox Bot</div>
              <div className="text-[10px] text-slate-500 uppercase tracking-widest font-mono">Options V8-D</div>
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
                `flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all ${
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

        {/* Install PWA Button on Desktop */}
        {(isInstallable || (!isInstalled && !isIOS)) && (
          <div className="p-3 border-t border-[#1e2d45]/60">
            <button
              onClick={handleInstallClick}
              className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-blue-600/15 hover:bg-blue-600/25 text-blue-300 border border-blue-500/30 text-xs font-medium transition-colors"
            >
              <Download size={14} />
              <span>Install Desktop App</span>
            </button>
          </div>
        )}

        <div className="px-4 py-3 border-t border-[#1e2d45]">
          <div className="flex items-center justify-between text-xs">
            <StatusBadge status={status} isMarketOpen={isMarketOpen} onFixAuth={() => navigate('/settings')} />
          </div>
        </div>
      </aside>

      {/* Mobile drawer overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 flex md:hidden">
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={() => setMobileOpen(false)} />
          <aside className="relative w-64 bg-[#0d1424] border-r border-[#1e2d45] flex flex-col z-10">
            <div className="px-4 py-4 border-b border-[#1e2d45] flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="w-6 h-6 rounded bg-blue-600 flex items-center justify-center text-white text-xs font-bold">
                  U
                </div>
                <span className="font-semibold text-white text-sm">Upstox Options Bot</span>
              </div>
              <button
                onClick={() => setMobileOpen(false)}
                className="text-slate-400 hover:text-white p-2 min-w-[44px] min-h-[44px] flex items-center justify-center"
              >
                <X size={18} />
              </button>
            </div>
            <nav className="flex-1 py-3 px-2 space-y-1 overflow-y-auto">
              {navItems.map(({ to, icon: Icon, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={to === '/'}
                  onClick={() => setMobileOpen(false)}
                  className={({ isActive }) =>
                    `flex items-center gap-3 px-3 py-3 rounded-lg text-sm transition-all ${
                      isActive ? 'bg-blue-600/20 text-blue-400 font-medium' : 'text-slate-400 hover:bg-[#141b2d] hover:text-slate-200'
                    }`
                  }
                >
                  <Icon size={17} />
                  <span>{label}</span>
                </NavLink>
              ))}
            </nav>

            {(isInstallable || isIOS) && !isInstalled && (
              <div className="p-3 border-t border-[#1e2d45]">
                <button
                  onClick={() => {
                    setMobileOpen(false)
                    handleInstallClick()
                  }}
                  className="w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg bg-blue-600 text-white text-xs font-semibold shadow-sm"
                >
                  <Smartphone size={15} />
                  <span>Install Mobile App</span>
                </button>
              </div>
            )}
          </aside>
        </div>
      )}

      {/* Main content area */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* PWA Update Banner */}
        {isUpdateAvailable && (
          <div className="bg-gradient-to-r from-blue-600 to-cyan-600 px-4 py-2 text-white flex items-center justify-between text-xs sm:text-sm z-30 shadow-md flex-shrink-0">
            <div className="flex items-center gap-2">
              <Sparkles size={16} className="animate-bounce" />
              <span>A new version of Options Bot is available!</span>
            </div>
            <button
              onClick={applyUpdate}
              className="px-3 py-1 bg-white text-blue-900 rounded font-semibold text-xs hover:bg-slate-100 transition-colors"
            >
              Update Now
            </button>
          </div>
        )}

        {/* Topbar */}
        <header className="flex items-center gap-2 sm:gap-3 px-3 sm:px-4 py-2.5 bg-[#0d1424] border-b border-[#1e2d45] flex-shrink-0">
          <button
            onClick={() => setMobileOpen(true)}
            className="md:hidden text-slate-400 hover:text-white p-1 min-w-[40px] min-h-[40px] flex items-center justify-center rounded-lg hover:bg-slate-800"
            aria-label="Open Navigation Drawer"
          >
            <Menu size={20} />
          </button>

          <div className="flex items-center gap-1.5 text-sm min-w-0">
            <span className="text-slate-400 font-semibold truncate text-sm sm:text-base">{currentPage}</span>
          </div>

          <div className="flex-1" />

          {/* Quick Install Button for Mobile Header if not installed */}
          {(isInstallable || isIOS) && !isInstalled && (
            <button
              onClick={handleInstallClick}
              className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 border border-blue-500/40 text-xs font-medium transition-colors"
              title="Install Upstox Options Bot to home screen"
            >
              <Download size={12} />
              <span className="hidden sm:inline">Install App</span>
            </button>
          )}

          <span className="px-2 py-0.5 sm:py-1 rounded text-[10px] font-bold bg-amber-500/15 text-amber-400 border border-amber-500/30 uppercase tracking-wider font-mono">
            PAPER
          </span>

          <StatusBadge status={status} isMarketOpen={isMarketOpen} onFixAuth={() => navigate('/settings')} />

          <span className="text-[11px] text-slate-500 hidden lg:block tabular-nums font-mono">
            {new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST
          </span>
        </header>

        {/* Scrollable Main Content (adds bottom padding on mobile for bottom bar) */}
        <main className="flex-1 overflow-y-auto p-3 sm:p-4 md:p-6 pb-20 md:pb-6">
          <Outlet />
        </main>

        {/* Mobile Bottom Navigation Bar (iOS / Android touch-friendly) */}
        <nav className="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-[#0d1424]/95 backdrop-blur-md border-t border-[#1e2d45] px-2 py-1 flex items-center justify-around">
          {bottomBarItems.map(({ to, icon: Icon, label }) => {
            const isActive = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
            return (
              <NavLink
                key={to}
                to={to}
                className={`flex flex-col items-center justify-center py-1.5 px-3 min-w-[56px] min-h-[48px] rounded-lg transition-colors ${
                  isActive ? 'text-blue-400 font-semibold' : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <Icon size={18} className={isActive ? 'stroke-[2.5]' : 'stroke-[1.75]'} />
                <span className="text-[10px] mt-0.5">{label}</span>
              </NavLink>
            )
          })}
        </nav>
      </div>

      {/* iOS Safari Installation Guide Modal */}
      {showIOSModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm">
          <div className="bg-[#0d1424] border border-[#1e2d45] rounded-xl p-5 max-w-sm w-full shadow-2xl">
            <div className="flex items-center justify-between pb-3 border-b border-[#1e2d45]">
              <div className="flex items-center gap-2">
                <Smartphone size={18} className="text-blue-400" />
                <h3 className="font-semibold text-white text-sm">Install on iPhone / iPad</h3>
              </div>
              <button onClick={() => setShowIOSModal(false)} className="text-slate-400 hover:text-white p-1">
                <X size={18} />
              </button>
            </div>
            <div className="py-4 space-y-3 text-xs text-slate-300">
              <p>To install <strong>Upstox Options Bot</strong> as a native app on iOS:</p>
              <ol className="list-decimal list-inside space-y-2 text-slate-300">
                <li>Tap the <strong>Share</strong> icon at the bottom of Safari (the box with an upward arrow).</li>
                <li>Scroll down and tap <strong>Add to Home Screen</strong>.</li>
                <li>Tap <strong>Add</strong> in the top right corner.</li>
              </ol>
              <div className="p-2.5 rounded bg-blue-950/40 border border-blue-800/40 text-blue-300 text-[11px]">
                Once added, launch from your Home Screen for full-screen trading and instant market updates.
              </div>
            </div>
            <button
              onClick={() => setShowIOSModal(false)}
              className="w-full py-2.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold"
            >
              Got it
            </button>
          </div>
        </div>
      )}

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

