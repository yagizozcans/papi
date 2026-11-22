import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { useLivePoll } from '@/hooks/use-live-poll'
import { unwrap } from '../shared/api'

export type TodayOut = components['schemas']['TodayOut']

export const TODAY_KEY = ['today'] as const

/** Today dashboard, polled every 5 s while the tab is visible. */
export function useToday() {
  return useLivePoll({
    queryKey: TODAY_KEY,
    queryFn: ({ signal }): Promise<TodayOut> => unwrap(api.GET('/v1/today', { params: { query: { feed_limit: 15 } }, signal })),
  })
}

/** One month of portfolio performance, only used for the sparklines of the market strip. */
export function useMonthPerformance() {
  return useQuery({
    queryKey: ['portfolio', 'performance', '1m'],
    queryFn: ({ signal }) => unwrap(api.GET('/v1/portfolio/performance', { params: { query: { range: '1m' } }, signal })),
    staleTime: 60_000,
    retry: false,
  })
}
