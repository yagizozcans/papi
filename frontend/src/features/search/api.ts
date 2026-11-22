import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { unwrap } from '../shared/api'

export type SearchOut = components['schemas']['SearchOut']

/** Value that follows `value` after `ms` of quiet. */
export function useDebounced<T>(value: T, ms = 150): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return debounced
}

/** Instruments + disclosures matching `q` (debounced, min 1 char). */
export function useSearch(q: string, enabled = true) {
  const term = useDebounced(q.trim(), 150)
  const query = useQuery({
    queryKey: ['search', term],
    enabled: enabled && term.length >= 1,
    queryFn: ({ signal }): Promise<SearchOut> => unwrap(api.GET('/v1/search', { params: { query: { q: term } }, signal })),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    retry: false,
  })
  return { ...query, term, pending: q.trim() !== term || query.isFetching }
}
