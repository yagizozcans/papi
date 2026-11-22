import { type QueryKey, useQuery, type UseQueryOptions } from '@tanstack/react-query'

export const LIVE_POLL_MS = 5000

/** `refetchInterval` value for TanStack Query: poll every `ms`, but only while the tab is visible. */
export function livePollInterval(ms = LIVE_POLL_MS) {
  return () => (typeof document === 'undefined' || document.visibilityState === 'visible' ? ms : false)
}

/**
 * `useQuery` that polls every 5 s (configurable) while the tab is visible and pauses when hidden.
 * TanStack's window-focus refetch brings the data up to date when the tab becomes visible again.
 */
export function useLivePoll<
  TQueryFnData = unknown,
  TError = Error,
  TData = TQueryFnData,
  TKey extends QueryKey = QueryKey,
>(options: Omit<UseQueryOptions<TQueryFnData, TError, TData, TKey>, 'refetchInterval'> & { interval?: number }) {
  const { interval, ...rest } = options
  return useQuery<TQueryFnData, TError, TData, TKey>({
    ...rest,
    refetchInterval: livePollInterval(interval),
    refetchIntervalInBackground: false,
  })
}
