import { useNavigate, useParams, useSearch } from '@tanstack/react-router'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { NewItemsPill } from '@/components/kit/new-items-pill'
import { SplitView } from '@/components/kit/split-view'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { useFeedPages } from './api'
import { FeedList } from './feed-list'
import { FeedToolbar } from './feed-toolbar'
import { buildRows, dedupe, type FeedItem } from './lib'
import { Reader } from './reader'
import { type FeedSearch, hasActiveFilters, parsePriorities } from './search'
import { useFeedLive } from './use-feed-live'

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}

interface TapeProps {
  search: FeedSearch
  selectedId?: string
  onSelect: (item: FeedItem) => void
  onChangeSearch: (next: FeedSearch) => void
  onFamilies: (f: { code: string; label: string }[]) => void
}

/** The tape: paged + live feed. Keyed by the filters so paging/live state resets when they change. */
function Tape({ search, selectedId, onSelect, onChangeSearch, onFamilies }: TapeProps) {
  const { t } = useTranslation()
  const pages = useFeedPages(search)
  const [atTop, setAtTop] = useState(true)
  const [topSignal, setTopSignal] = useState(0)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())

  const base = useMemo(() => (pages.data ? dedupe(...pages.data.pages.map((p) => p.items)) : []), [pages.data])
  const families = pages.data?.pages[0]?.facets.families
  useEffect(() => {
    if (families) onFamilies(families)
  }, [families, onFamilies])

  const live = useFeedLive({
    search,
    base,
    enabled: !!pages.data && (search.sort ?? 'time_desc') === 'time_desc',
    auto: atTop && !selectedId,
  })

  const items = useMemo(() => {
    const wanted = parsePriorities(search.priority)
    return wanted.length ? live.items.filter((i) => wanted.some((p) => p === i.priority)) : live.items
  }, [live.items, search.priority])

  const grouping = (search.sort ?? 'time_desc') !== 'materiality'
  const rows = useMemo(() => buildRows(items, grouping, expanded, selectedId), [items, grouping, expanded, selectedId])

  const toggleBurst = useCallback(
    (key: string) =>
      setExpanded((prev) => {
        const next = new Set(prev)
        if (!next.delete(key)) next.add(key)
        return next
      }),
    [],
  )

  // j/k move the selection (and open the reader), o opens the KAP original.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || isTyping(e.target)) return
      if (document.querySelector('[role="dialog"]')) return
      if (e.key === 'j' || e.key === 'k') {
        if (items.length === 0) return
        const cur = items.findIndex((i) => i.id === selectedId)
        const next = e.key === 'j' ? (cur < 0 ? 0 : Math.min(cur + 1, items.length - 1)) : cur < 0 ? 0 : Math.max(cur - 1, 0)
        const target = items[next]
        if (target) onSelect(target)
      } else if (e.key === 'o') {
        const url = items.find((i) => i.id === selectedId)?.kap_url
        if (url) window.open(url, '_blank', 'noopener,noreferrer')
      } else return
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [items, selectedId, onSelect])

  const empty = hasActiveFilters(search) ? (
    <EmptyState
      title={t('feed.empty.filteredTitle')}
      description={t('feed.empty.filteredDesc')}
      action={
        <Button variant="outline" size="sm" onClick={() => onChangeSearch({ scope: search.scope, sort: search.sort })}>
          {t('feed.empty.clear')}
        </Button>
      }
    />
  ) : (
    <EmptyState
      title={t('feed.empty.title')}
      description={t('feed.empty.desc')}
      action={
        search.scope && search.scope !== 'all' ? (
          <Button variant="outline" size="sm" onClick={() => onChangeSearch({ ...search, scope: 'all' })}>
            {t('feed.empty.showAll')}
          </Button>
        ) : undefined
      }
    />
  )

  return (
    <div className="relative h-full">
      <FeedList
        rows={rows}
        selectedId={selectedId}
        onSelect={onSelect}
        onToggleBurst={toggleBurst}
        loading={pages.isPending}
        error={pages.isError ? pages.error : undefined}
        onRetry={() => void pages.refetch()}
        empty={empty}
        flashIds={live.flash}
        hasMore={pages.hasNextPage}
        loadingMore={pages.isFetchingNextPage}
        onLoadMore={() => void pages.fetchNextPage()}
        onAtTopChange={setAtTop}
        scrollTopSignal={topSignal}
        ariaLabel={t('feed.list')}
      />
      <div className="pointer-events-none absolute inset-x-0 top-2 z-20 flex justify-center">
        <NewItemsPill
          count={live.pending}
          className="pointer-events-auto"
          onClick={() => {
            live.reveal()
            setTopSignal((n) => n + 1)
          }}
        />
      </div>
    </div>
  )
}

export function FeedPage() {
  const { t } = useTranslation()
  const search = useSearch({ from: '/feed' })
  const { eventId } = useParams({ strict: false })
  const navigate = useNavigate({ from: '/feed' })
  const [families, setFamilies] = useState<{ code: string; label: string }[]>([])

  const go = useCallback(
    (id: string | undefined, next: FeedSearch | ((prev: FeedSearch) => FeedSearch)) => {
      if (id) void navigate({ to: '/feed/$eventId', params: { eventId: id }, search: next, replace: true })
      else void navigate({ to: '/feed', search: next, replace: true })
    },
    [navigate],
  )
  const select = useCallback((item: FeedItem) => go(item.id, (prev) => prev), [go])
  const close = useCallback(() => go(undefined, (prev) => prev), [go])
  const changeSearch = useCallback((next: FeedSearch) => go(eventId, next), [go, eventId])

  // Escape closes the reader even when focus is not inside the split view.
  useEffect(() => {
    if (!eventId) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || isTyping(e.target) || document.querySelector('[role="dialog"]')) return
      close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [eventId, close])

  const tapeKey = JSON.stringify(search)

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="feed-page">
      <h1 className="sr-only">{t('feed.title')}</h1>
      <FeedToolbar search={search} onChange={changeSearch} families={families} />
      <div className="min-h-0 flex-1">
        <SplitView
          storageKey="feed"
          hasDetail={!!eventId}
          onCloseDetail={close}
          detailTitle={t('feed.reader.region')}
          emptyDetail={<div className="flex h-full items-center justify-center p-6 text-center text-muted-foreground">{t('feed.reader.empty')}</div>}
          list={<Tape key={tapeKey} search={search} selectedId={eventId} onSelect={select} onChangeSearch={changeSearch} onFamilies={setFamilies} />}
          detail={eventId ? <Reader eventId={eventId} onClose={close} /> : null}
        />
      </div>
    </div>
  )
}
