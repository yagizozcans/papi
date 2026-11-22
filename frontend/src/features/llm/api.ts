import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { STATUS_KEY } from '@/api/status'
import { livePollInterval } from '@/hooks/use-live-poll'
import { unwrap } from '../shared/api'

export type LlmStatus = components['schemas']['LlmStatusOut']

export const LLM_KEY = ['llm', 'status'] as const

/** Pause durations offered in the UI (`null` = until resumed). */
export const PAUSE_OPTIONS = [
  { key: '1h', minutes: 60 },
  { key: '3h', minutes: 180 },
  { key: 'until', minutes: null },
] as const
export type PauseOption = (typeof PAUSE_OPTIONS)[number]

export function useLlmStatus() {
  return useQuery({
    queryKey: LLM_KEY,
    queryFn: ({ signal }): Promise<LlmStatus> => unwrap(api.GET('/v1/llm/status', { signal })),
    refetchInterval: livePollInterval(5000),
  })
}

function useRefresh() {
  const qc = useQueryClient()
  return async () => {
    await Promise.all([qc.invalidateQueries({ queryKey: LLM_KEY }), qc.invalidateQueries({ queryKey: STATUS_KEY })])
  }
}

export function usePauseLlm() {
  const refresh = useRefresh()
  return useMutation({
    mutationFn: ({ minutes }: { minutes: number | null }): Promise<LlmStatus> =>
      unwrap(api.POST('/v1/llm/governor/pause', { body: { minutes, reason: 'manual' } })),
    onSuccess: refresh,
  })
}

export function useResumeLlm() {
  const refresh = useRefresh()
  return useMutation({
    mutationFn: (): Promise<LlmStatus> => unwrap(api.POST('/v1/llm/governor/resume')),
    onSuccess: refresh,
  })
}
