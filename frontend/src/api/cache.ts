/**
 * Lightweight API cache with stale-while-revalidate.
 * - Returns cached data immediately while fetching fresh data in background.
 * - Deduplicates in-flight requests (same key = same promise).
 * - Each entry has a TTL (time-to-live in ms).
 */

interface CacheEntry<T> {
  data: T
  fetchedAt: number
  ttl: number
}

const _store = new Map<string, CacheEntry<unknown>>()
const _inflight = new Map<string, Promise<unknown>>()

function isStale(entry: CacheEntry<unknown>): boolean {
  return Date.now() - entry.fetchedAt > entry.ttl
}

/**
 * Fetch with cache.
 * @param key    Unique cache key
 * @param fetcher Function that fetches fresh data
 * @param ttlMs  Time before data is considered stale (ms)
 */
export async function cachedFetch<T>(
  key: string,
  fetcher: () => Promise<T>,
  ttlMs = 5000,
): Promise<T> {
  const existing = _store.get(key) as CacheEntry<T> | undefined

  // Fresh cache hit — return immediately
  if (existing && !isStale(existing)) {
    return existing.data
  }

  // Deduplicate in-flight requests
  if (_inflight.has(key)) {
    return _inflight.get(key) as Promise<T>
  }

  const promise = fetcher()
    .then((data) => {
      _store.set(key, { data, fetchedAt: Date.now(), ttl: ttlMs })
      _inflight.delete(key)
      return data
    })
    .catch((err) => {
      _inflight.delete(key)
      throw err
    })

  _inflight.set(key, promise)

  // Stale-while-revalidate: if we have stale data, return it immediately
  // and let the background fetch update the cache
  if (existing) {
    return existing.data
  }

  return promise
}

/** Invalidate a specific cache key */
export function invalidateCache(key: string): void {
  _store.delete(key)
}

/** Invalidate all keys matching a prefix */
export function invalidateCachePrefix(prefix: string): void {
  for (const key of _store.keys()) {
    if (key.startsWith(prefix)) _store.delete(key)
  }
}

/** Cache TTLs in ms */
export const TTL = {
  PRICES:      5_000,   // 5s  — live prices
  OVERVIEW:    10_000,  // 10s — dashboard overview
  TRADES:      30_000,  // 30s — trade history
  PERFORMANCE: 60_000,  // 1m  — analytics
  PAPER:       60_000,  // 1m  — paper status
  SETTINGS:    300_000, // 5m  — settings
  INSTRUMENTS: 600_000, // 10m — instrument list
} as const
