import { ApiError } from '@/api/client'
import type { components } from '@/api/schema'

export type FeedItem = components['schemas']['FeedItem']
export type AgentRunBrief = components['schemas']['AgentRunBrief']
export type PendingActionOut = components['schemas']['PendingActionOut']

/** Unwraps an openapi-fetch result: returns `data` or throws an ApiError carrying the HTTP status and detail. */
export async function unwrap<T>(
  promise: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await promise
  if (data === undefined) {
    const detail = (error as { detail?: unknown } | undefined)?.detail
    throw new ApiError(response.status, typeof detail === 'string' ? detail : undefined)
  }
  return data
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}
