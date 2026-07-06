import { useEffect, useRef } from 'react'

export function usePolling(fn: () => void, intervalMs: number, enabled = true) {
  const savedFn = useRef(fn)
  useEffect(() => { savedFn.current = fn }, [fn])

  useEffect(() => {
    if (!enabled) return
    const id = setInterval(() => savedFn.current(), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, enabled])
}
