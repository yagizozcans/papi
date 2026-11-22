import { useQuery } from '@tanstack/react-query'
import { ApiError, api } from './client'
import type { components } from './schema'

export type Status = components['schemas']['StatusOut']

export const STATUS_KEY = ['status'] as const

export function useStatus() {
  return useQuery({
    queryKey: STATUS_KEY,
    queryFn: async ({ signal }): Promise<Status> => {
      const { data, response } = await api.GET('/v1/status', { signal })
      if (!data) throw new ApiError(response.status)
      return data
    },
    refetchInterval: 5000,
    retry: (count, error) => !(error instanceof ApiError && error.status === 401) && count < 1,
  })
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}
