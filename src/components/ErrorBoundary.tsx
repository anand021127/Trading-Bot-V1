import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

/**
 * Catches uncaught render errors anywhere in the tree below it and shows a
 * recoverable screen instead of a blank page.
 *
 * This is the fix for a real production bug: selecting Options mode in
 * Settings triggered a render that read a field missing from an API
 * response (`universe.valid_option_indices.map(...)` on undefined). With
 * no error boundary anywhere in the app, that uncaught exception unmounted
 * the entire React tree — the whole app, not just the Settings page, went
 * to a blank screen with no way to recover except a manual refresh.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Uncaught render error:', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-[#0a0e1a] flex items-center justify-center p-6">
          <div className="max-w-md w-full bg-[#141b2d] border border-red-800/40 rounded-xl p-6 text-center">
            <AlertTriangle size={32} className="mx-auto mb-3 text-red-400" />
            <h1 className="text-white font-semibold text-lg mb-2">Something went wrong</h1>
            <p className="text-slate-400 text-sm mb-4">
              A page hit an unexpected error and had to stop. Your bot's backend keeps running
              independently of this — reload to get back to a working dashboard.
            </p>
            {this.state.error && (
              <pre className="text-[10px] text-slate-600 bg-[#0f1628] rounded p-2 mb-4 overflow-x-auto text-left">
                {this.state.error.message}
              </pre>
            )}
            <button
              onClick={() => window.location.reload()}
              className="flex items-center gap-2 mx-auto px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium rounded-lg transition-colors"
            >
              <RefreshCw size={14} /> Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
