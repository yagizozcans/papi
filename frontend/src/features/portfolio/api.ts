import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

export type Overview = components['schemas']['PortfolioOverviewOut']
export type Position = components['schemas']['PositionOut']
export type Performance = components['schemas']['PerformanceOut']
export type PerfRange = Performance['range']
export type Txn = components['schemas']['TxnOut']
export type AllocationSlice = components['schemas']['AllocationSlice']

export const PERF_RANGES: PerfRange[] = ['1m', '3m', '6m', '1y', 'ytd', 'max']

export function useOverview() {
  return useQuery({
    queryKey: ['portfolio', 'overview'],
    queryFn: ({ signal }): Promise<Overview> => unwrap(api.GET('/v1/portfolio/overview', { signal })),
    refetchInterval: 15_000,
  })
}

export function usePerformance(range: PerfRange) {
  return useQuery({
    queryKey: ['portfolio', 'performance', range],
    queryFn: ({ signal }): Promise<Performance> => unwrap(api.GET('/v1/portfolio/performance', { params: { query: { range } }, signal })),
    placeholderData: keepPreviousData,
  })
}
