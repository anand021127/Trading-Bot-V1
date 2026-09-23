import { useEffect, useRef } from 'react'

/**
 * Poll `fn` every `intervalMs`. Fires immediately on mount (and whenever
 * `enabled` flips true) in addition to on each interval tick — the
 * previous version only started the interval timer, so every page using
 * this hook (Scanner, Overview, Paper Trading, Live Premiums, Trade
 * History, Performance) showed a blank/loading state for the full
 * interval before any data appeared, which reads as "not real-time" even
 * once data does arrive.
 */
export function usePolling(fn: () => void, intervalMs: number, enabled = true) {
  const savedFn = useRef(fn)
  useEffect(() => { savedFn.current = fn }, [fn])

  useEffect(() => {
    if (!enabled) return
    savedFn.current()  // immediate first call — don't wait a full interval
    const id = setInterval(() => savedFn.current(), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, enabled])
}
