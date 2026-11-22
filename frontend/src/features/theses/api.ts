import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

export type ThesisRow = components['schemas']['ThesisRow']
export type ThesisDetail = components['schemas']['ThesisDetail']
export type ThesisCheck = components['schemas']['ThesisCheckOut']
export type ThesisReviewRow = components['schemas']['ThesisReviewRow']
export type StartRunOut = components['schemas']['StartRunOut']

const POLL_MS = 2000

export function useTheses() {
  return useQuery({
    queryKey: ['theses', 'list'],
    queryFn: ({ signal }) => unwrap(api.GET('/v1/theses', { signal })),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.items.some((r) => r.active) ? POLL_MS : 30_000),
  })
}

/** One thesis; polls every 2 s while an upload is being read. */
export function useThesis(name: string) {
  return useQuery({
    queryKey: ['theses', 'detail', name],
    queryFn: ({ signal }): Promise<ThesisDetail> => unwrap(api.GET('/v1/theses/{name}', { params: { path: { name } }, signal })),
    refetchInterval: (q) => (q.state.data?.active ? POLL_MS : false),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
  })
}

/** Strategy-analyst runs of a thesis, newest first; polls while one is active. */
export function useThesisReviews(name: string) {
  return useQuery({
    queryKey: ['theses', 'reviews', name],
    queryFn: ({ signal }) => unwrap(api.GET('/v1/theses/{name}/reviews', { params: { path: { name } }, signal })),
    refetchInterval: (q) => (q.state.data?.some((r) => ['starting', 'running', 'resuming'].includes(r.status)) ? 3000 : false),
  })
}

export function useSaveThesis(name: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (content: string): Promise<ThesisDetail> =>
      unwrap(api.PUT('/v1/theses/{name}/text', { params: { path: { name } }, body: { content } })),
    onSuccess: (data) => {
      qc.setQueryData(['theses', 'detail', name], data)
      void qc.invalidateQueries({ queryKey: ['theses', 'list'] })
    },
  })
}

export function useCheckThesis(name: string) {
  return useMutation({
    mutationFn: (content: string | null): Promise<ThesisCheck> =>
      unwrap(api.POST('/v1/theses/{name}/check', { params: { path: { name } }, body: { content } })),
  })
}

export function useNewThesis() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (ticker: string): Promise<ThesisDetail> => unwrap(api.POST('/v1/theses', { body: { ticker } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['theses', 'list'] }),
  })
}

export function useStartReview(name: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (): Promise<StartRunOut> => unwrap(api.POST('/v1/theses/{name}/review', { params: { path: { name } } })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['theses'] })
      void qc.invalidateQueries({ queryKey: ['agents', 'runs'] })
    },
  })
}

export interface UploadInput {
  files: File[]
  ticker: string
  direction: string
  horizon: string
  run: boolean
}

/** Multipart upload (files in order). Plain fetch: openapi-fetch cannot serialise FormData bodies. */
export function useUploadThesis() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: UploadInput): Promise<components['schemas']['UploadThesisOut']> => {
      const form = new FormData()
      for (const f of input.files) form.append('files', f, f.name)
      form.append('ticker', input.ticker)
      form.append('direction', input.direction)
      form.append('horizon', input.horizon)
      form.append('run', input.run ? 'true' : 'false')
      const res = await fetch('/v1/theses/upload', { method: 'POST', body: form, credentials: 'same-origin' })
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: unknown }
        throw new ApiError(res.status, typeof body.detail === 'string' ? body.detail : undefined)
      }
      return (await res.json()) as components['schemas']['UploadThesisOut']
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['theses', 'list'] }),
  })
}
