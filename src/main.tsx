import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'
import { registerServiceWorker } from './utils/pwa'
import './index.css'

const rootEl = document.getElementById('root')
if (!rootEl) throw new Error('Root element not found')

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)

// Register PWA service worker with update callback
registerServiceWorker(() => {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('pwa:update_available'))
  }
})
