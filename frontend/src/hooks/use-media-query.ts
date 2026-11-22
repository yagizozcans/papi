import { useSyncExternalStore } from 'react'

/** Reactive `matchMedia`. Falls back to `fallback` when matchMedia is unavailable (SSR, jsdom). */
export function useMediaQuery(query: string, fallback = false): boolean {
  return useSyncExternalStore(
    (cb) => {
      if (typeof window.matchMedia !== 'function') return () => {}
      const mq = window.matchMedia(query)
      mq.addEventListener('change', cb)
      return () => mq.removeEventListener('change', cb)
    },
    () => (typeof window.matchMedia === 'function' ? window.matchMedia(query).matches : fallback),
    () => fallback,
  )
}

/** Tailwind `md` breakpoint and up. */
export function useIsDesktop(): boolean {
  return useMediaQuery('(min-width: 768px)', true)
}
