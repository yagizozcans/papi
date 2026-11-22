import { useCallback, useSyncExternalStore } from 'react'
import { readStorage, writeStorage } from '@/lib/storage'

const KEY = 'papi-seen-attention'
const MAX = 500
const listeners = new Set<() => void>()
let cache: { raw: string | null; set: ReadonlySet<string> } | null = null

function read(): ReadonlySet<string> {
  const raw = readStorage(KEY)
  if (cache && cache.raw === raw) return cache.set
  let ids: string[] = []
  try {
    const parsed: unknown = raw ? JSON.parse(raw) : []
    if (Array.isArray(parsed)) ids = parsed.filter((x): x is string => typeof x === 'string')
  } catch {
    /* corrupt value: start over */
  }
  cache = { raw, set: new Set(ids) }
  return cache.set
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** Attention events the user has acknowledged (papi keeps no ack state server-side), persisted in localStorage. */
export function useSeenAttention() {
  const seen = useSyncExternalStore(subscribe, read, read)
  const markSeen = useCallback((ids: readonly string[]) => {
    const next = [...read(), ...ids]
    writeStorage(KEY, JSON.stringify(next.slice(-MAX)))
    cache = null
    listeners.forEach((l) => l())
  }, [])
  return { seen, markSeen }
}
