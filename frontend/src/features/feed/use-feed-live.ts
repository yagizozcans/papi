import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLivePoll } from '@/hooks/use-live-poll'
import { fetchFeed, POLL_LIMIT } from './api'
import { dedupe, type FeedItem, sortKey } from './lib'
import { type FeedSearch, feedQueryParams } from './search'

const FLASH_MS = 2500

interface Options {
  search: FeedSearch
  /** Items loaded through paging (newest first). */
  base: readonly FeedItem[]
  /** Poll only after the first page has loaded and only for newest-first sorting. */
  enabled: boolean
  /** True when new items may be inserted straight away (list scrolled to the top, nothing selected). */
  auto: boolean
}

/**
 * Polls `GET /v1/feed?after=<newest published_key>` every 5 s (while the tab is visible). New items are held
 * behind a pill (`pending`) unless `auto` is true, in which case they are inserted at once and flash once.
 * Remount (key) to reset when the filters change.
 */
export function useFeedLive({ search, base, enabled, auto }: Options) {
  const params = feedQueryParams(search)
  const [live, setLive] = useState<readonly FeedItem[]>([])
  const [revealed, setRevealed] = useState<ReadonlySet<string>>(new Set())
  const [flash, setFlash] = useState<ReadonlySet<string>>(new Set())

  const known = useRef(new Set<string>())
  const newest = useRef('')
  const autoRef = useRef(auto)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    autoRef.current = auto
    for (const it of base) known.current.add(it.id)
    const keys = [...base, ...live].map((i) => i.published_key).filter(Boolean)
    newest.current = keys.reduce((a, b) => (b > a ? b : a), '')
  }, [auto, base, live])

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const onPoll = useCallback((items: readonly FeedItem[]) => {
    const fresh = items.filter((i) => !known.current.has(i.id))
    if (fresh.length === 0) return
    for (const it of fresh) known.current.add(it.id)
    setLive((prev) => [...fresh, ...prev].sort((a, b) => (sortKey(a) < sortKey(b) ? 1 : -1)))
    if (autoRef.current) {
      const ids = fresh.map((i) => i.id)
      setRevealed((prev) => new Set([...prev, ...ids]))
      setFlash(new Set(ids))
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => setFlash(new Set()), FLASH_MS)
    }
  }, [])

  useLivePoll({
    queryKey: ['feed-live', params],
    queryFn: async ({ signal }) => {
      const out = await fetchFeed({ ...params, limit: POLL_LIMIT, after: newest.current || undefined }, signal)
      onPoll(out.items)
      return out.items.length
    },
    enabled,
    retry: false,
    gcTime: 0,
  })

  const items = useMemo(() => dedupe(live.filter((i) => revealed.has(i.id)), base), [live, revealed, base])
  const baseIds = useMemo(() => new Set(base.map((i) => i.id)), [base])
  const pending = live.filter((i) => !revealed.has(i.id) && !baseIds.has(i.id)).length
  const reveal = useCallback(() => setRevealed(new Set(live.map((i) => i.id))), [live])

  return { items, pending, reveal, flash }
}
