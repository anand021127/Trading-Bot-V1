import { useEffect } from 'react'

export function usePolling(callback: () => Promise<void> | void, intervalMs: number) {
  useEffect(() => {
    let mounted = true

    const execute = async () => {
      if (!mounted) {
        return
      }
      await callback()
    }

    const id = window.setInterval(execute, intervalMs)
    execute()

    return () => {
      mounted = false
      window.clearInterval(id)
    }
  }, [callback, intervalMs])
}
