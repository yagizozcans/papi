import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

export type AgentDef = components['schemas']['AgentDefOut']
export type AgentRunRow = components['schemas']['AgentRunRow']
export type AgentRunDetail = components['schemas']['AgentRunDetail']
export type RunCall = components['schemas']['RunCall']
export type StartRunOut = components['schemas']['StartRunOut']

export interface RunFilters {
  agent?: string
  status?: string
  ref?: string
}

export const RUNS_PAGE = 25
const POLL_MS = 2000

export function useAgentDefs() {
  return useQuery({
    queryKey: ['agents', 'defs'],
    queryFn: ({ signal }) => unwrap(api.GET('/v1/agents', { signal })).then((r) => r.agents),
    staleTime: 5 * 60_000,
  })
}

/** Runs list; refreshes every 5 s while a run on the page is still active. */
export function useAgentRuns(filters: RunFilters, offset: number) {
  return useQuery({
    queryKey: ['agents', 'runs', filters, offset],
    queryFn: ({ signal }) =>
      unwrap(
        api.GET('/v1/agent-runs', {
          params: {
            query: {
              agent: filters.agent || undefined,
              status: filters.status || undefined,
              ref: filters.ref || undefined,
              limit: RUNS_PAGE,
              offset,
            },
          },
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.items.some((r) => r.active) ? 5000 : 30_000),
  })
}

/** One run; polls every 2 s while it is active (the transcript grows with every tool call). */
export function useAgentRun(runId: string | undefined) {
  return useQuery({
    queryKey: ['agents', 'run', runId],
    enabled: !!runId,
    queryFn: ({ signal }): Promise<AgentRunDetail> =>
      unwrap(api.GET('/v1/agent-runs/{run_id}', { params: { path: { run_id: runId ?? '' } }, signal })),
    refetchInterval: (q) => (q.state.data?.active ? POLL_MS : false),
    retry: (count, error) => !(error instanceof Error && 'status' in error && error.status === 404) && count < 1,
  })
}

export function useStartRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { agent: string; ref: string }): Promise<StartRunOut> =>
      unwrap(api.POST('/v1/agent-runs', { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['agents', 'runs'] })
      void qc.invalidateQueries({ queryKey: ['theses'] })
      void qc.invalidateQueries({ queryKey: ['today'] })
    },
  })
}
