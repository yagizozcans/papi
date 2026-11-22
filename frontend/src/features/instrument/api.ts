import { useQuery } from '@tanstack/react-query'
import { ApiError, api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

export type InstrumentOut = components['schemas']['InstrumentOut']
export type PricesOut = components['schemas']['PricesOut']

export function useInstrument(ticker: string) {
  return useQuery({
    queryKey: ['instrument', ticker],
    queryFn: ({ signal }): Promise<InstrumentOut> =>
      unwrap(api.GET('/v1/instruments/{ticker}', { params: { path: { ticker } }, signal })),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
    refetchInterval: 15_000,
  })
}

/** Daily bars from ten years back (the chart's range switcher picks the visible window). */
export function usePrices(ticker: string, enabled: boolean) {
  return useQuery({
    queryKey: ['instrument', ticker, 'prices'],
    enabled,
    queryFn: ({ signal }): Promise<PricesOut> => {
      const d = new Date()
      d.setFullYear(d.getFullYear() - 10)
      return unwrap(
        api.GET('/v1/instruments/{ticker}/prices', {
          params: { path: { ticker }, query: { from: d.toISOString().slice(0, 10) } },
          signal,
        }),
      )
    },
    staleTime: 60_000,
  })
}
