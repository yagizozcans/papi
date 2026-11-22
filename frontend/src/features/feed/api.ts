import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError, api } from '@/api/client'
import type { components } from '@/api/schema'
import i18n from '@/i18n'
import { type FeedSearch, feedQueryParams } from './search'

export type FeedOut = components['schemas']['FeedOut']
export type EventDetail = components['schemas']['EventDetailOut']
export type FeedbackKind = components['schemas']['FeedbackIn']['kind']

export const PAGE_SIZE = 50
export const POLL_LIMIT = 200

function detailOf(error: unknown): string | undefined {
  const d = (error as { detail?: unknown } | undefined)?.detail
  return typeof d === 'string' ? d : undefined
}

type FeedQuery = ReturnType<typeof feedQueryParams> & { limit: number; before?: string; after?: string }

export async function fetchFeed(query: FeedQuery, signal?: AbortSignal): Promise<FeedOut> {
  const { data, error, response } = await api.GET('/v1/feed', { params: { query }, signal })
  if (!data) throw new ApiError(response.status, detailOf(error))
  return data
}

/** Paged feed (older pages via `next_cursor`). Never auto-refetches: live items come from `fetchFeed(after=...)`. */
export function useFeedPages(search: FeedSearch) {
  const params = feedQueryParams(search)
  return useInfiniteQuery({
    queryKey: ['feed', params],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => fetchFeed({ ...params, limit: PAGE_SIZE, before: pageParam }, signal),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })
}

export const eventKey = (id: string) => ['event', id] as const

export function useEventDetail(id: string) {
  return useQuery({
    queryKey: eventKey(id),
    queryFn: async ({ signal }): Promise<EventDetail> => {
      const { data, error, response } = await api.GET('/v1/events/{event_id}/detail', { params: { path: { event_id: id } }, signal })
      if (!data) throw new ApiError(response.status, detailOf(error))
      return data
    },
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  })
}

/** POST feedback with an optimistic count bump (rolled back on error). */
export function useFeedback(eventId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ kind, comment }: { kind: FeedbackKind; comment?: string }) => {
      const { data, error, response } = await api.POST('/v1/events/{event_id}/feedback', {
        params: { path: { event_id: eventId } },
        body: { kind, comment: comment || null, target_type: 'event' },
      })
      if (!data) throw new ApiError(response.status, detailOf(error))
      return data
    },
    onMutate: async ({ kind }) => {
      await qc.cancelQueries({ queryKey: eventKey(eventId) })
      const prev = qc.getQueryData<EventDetail>(eventKey(eventId))
      if (prev) {
        qc.setQueryData<EventDetail>(eventKey(eventId), {
          ...prev,
          feedback_counts: { ...prev.feedback_counts, [kind]: (prev.feedback_counts[kind] ?? 0) + 1 },
        })
      }
      return { prev }
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(eventKey(eventId), ctx.prev)
      toast.error(i18n.t('feed.feedback.error'))
    },
    onSuccess: (out) => {
      const cur = qc.getQueryData<EventDetail>(eventKey(eventId))
      if (cur) qc.setQueryData<EventDetail>(eventKey(eventId), { ...cur, feedback_counts: out.feedback_counts })
      toast.success(i18n.t('feed.feedback.thanks'))
    },
  })
}
