import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import Layout from './components/Layout'
import Overview from './pages/Overview'
import TradeHistory from './pages/TradeHistory'
import LivePremiums from './pages/LivePremiums'
import PaperTrading from './pages/PaperTrading'
import Backtest from './pages/Backtest'
import Performance from './pages/Performance'
import ApiTest from './pages/ApiTest'
import Settings from './pages/Settings'

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/trade-history" element={<TradeHistory />} />
          <Route path="/live-premiums" element={<LivePremiums />} />
          <Route path="/paper-trading" element={<PaperTrading />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/api-test" element={<ApiTest />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
