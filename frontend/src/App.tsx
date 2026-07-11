import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import Layout from './components/Layout'

// Lazy load all pages — instant nav, only load when needed
const Overview     = lazy(() => import('./pages/Overview'))
const Scanner      = lazy(() => import('./pages/Scanner'))
const TradeHistory = lazy(() => import('./pages/TradeHistory'))
const LivePremiums = lazy(() => import('./pages/LivePremiums'))
const PaperTrading = lazy(() => import('./pages/PaperTrading'))
const Backtest     = lazy(() => import('./pages/Backtest'))
const Performance  = lazy(() => import('./pages/Performance'))
const ApiTest      = lazy(() => import('./pages/ApiTest'))
const Settings     = lazy(() => import('./pages/Settings'))

function PageSkeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      <div className="h-8 bg-[#141b2d] rounded-xl w-1/3" />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {[1,2,3,4].map(i => (
          <div key={i} className="h-20 bg-[#141b2d] border border-[#1e2d45] rounded-xl" />
        ))}
      </div>
      <div className="h-48 bg-[#141b2d] border border-[#1e2d45] rounded-xl" />
      <div className="h-32 bg-[#141b2d] border border-[#1e2d45] rounded-xl" />
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={
            <Suspense fallback={<PageSkeleton />}><Overview /></Suspense>
          } />
          <Route path="/scanner" element={
            <Suspense fallback={<PageSkeleton />}><Scanner /></Suspense>
          } />
          <Route path="/trade-history" element={
            <Suspense fallback={<PageSkeleton />}><TradeHistory /></Suspense>
          } />
          <Route path="/live-premiums" element={
            <Suspense fallback={<PageSkeleton />}><LivePremiums /></Suspense>
          } />
          <Route path="/paper-trading" element={
            <Suspense fallback={<PageSkeleton />}><PaperTrading /></Suspense>
          } />
          <Route path="/backtest" element={
            <Suspense fallback={<PageSkeleton />}><Backtest /></Suspense>
          } />
          <Route path="/performance" element={
            <Suspense fallback={<PageSkeleton />}><Performance /></Suspense>
          } />
          <Route path="/api-test" element={
            <Suspense fallback={<PageSkeleton />}><ApiTest /></Suspense>
          } />
          <Route path="/settings" element={
            <Suspense fallback={<PageSkeleton />}><Settings /></Suspense>
          } />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
