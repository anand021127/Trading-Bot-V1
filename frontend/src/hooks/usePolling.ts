import { useEffect, useRef } from 'react'

/**
 * Poll `fn` every `intervalMs`. Fires once immediately on mount/enable so the
 * first paint isn't blank for a full interval (previously the initial spinner
 * lingered up to `intervalMs`).
 */
export function usePolling(fn: () => void, intervalMs: number, enabled = true) {
  const savedFn = useRef(fn)
  useEffect(() => { savedFn.current = fn }, [fn])

  useEffect(() => {
    if (!enabled) return
    savedFn.current()                                  // immediate first run
    const id = setInterval(() => savedFn.current(), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, enabled])
}
