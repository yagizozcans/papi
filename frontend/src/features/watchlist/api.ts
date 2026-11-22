import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

export type WatchOut = components['schemas']['WatchOut']
export type UnwatchOut = components['schemas']['UnwatchOut']

export const DEFAULT_WATCHLIST = 'Ana'

function useInvalidate() {
  const qc = useQueryClient()
  return () => Promise.all(['portfolio', 'instrument', 'today', 'search'].map((k) => qc.invalidateQueries({ queryKey: [k] })))
}

/** Add a ticker, `XU030` / `endeks:XU030` or `sektor:<kod>` to a watchlist. */
export function useWatch() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({ ref, watchlist }: { ref: string; watchlist?: string }): Promise<WatchOut> =>
      unwrap(api.POST('/v1/watchlist', { body: { ref, watchlist: watchlist ?? DEFAULT_WATCHLIST } })),
    onSuccess: invalidate,
  })
}

export function useUnwatch() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: ({ ref, watchlist }: { ref: string; watchlist?: string }): Promise<UnwatchOut> =>
      unwrap(api.DELETE('/v1/watchlist', { params: { query: { ref, watchlist: watchlist ?? DEFAULT_WATCHLIST } } })),
    onSuccess: invalidate,
  })
}
