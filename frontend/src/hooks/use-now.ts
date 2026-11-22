import { useSyncExternalStore } from 'react'

/** One shared timer for every RelativeTime on the page; ticks every 15 s while the tab is visible. */
const listeners = new Set<() => void>()
let timer: number | undefined
let snapshot = Date.now()

function tick() {
  snapshot = Date.now()
  listeners.forEach((l) => l())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  if (listeners.size === 1) {
    snapshot = Date.now()
    timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') tick()
    }, 15_000)
    document.addEventListener('visibilitychange', tick)
  }
  return () => {
    listeners.delete(cb)
    if (listeners.size === 0) {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', tick)
    }
  }
}

/** Current time in ms, refreshed periodically. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot)
}
