import { observeElementRect, useVirtualizer } from '@tanstack/react-virtual'
import type { TFunction } from 'i18next'
import { ChevronRight } from 'lucide-react'
import { type KeyboardEvent, type ReactNode, useEffect, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Skeleton } from '@/components/ui/skeleton'
import { relativeParts } from '@/lib/format'
import { cn } from '@/lib/utils'
import { KapLink, Markers, Materiality, Prio, Stage, Tickers } from './item-bits'
import { formatDM, formatHM, type FeedItem, headlineOf, type TapeRow } from './lib'

const ITEM_H = 56
const BURST_H = 32

export interface FeedListProps {
  rows: TapeRow[]
  selectedId?: string | null
  onSelect: (item: FeedItem) => void
  onToggleBurst: (key: string) => void
  loading?: boolean
  error?: unknown
  onRetry?: () => void
  empty?: ReactNode
  flashIds?: ReadonlySet<string>
  hasMore?: boolean
  loadingMore?: boolean
  onLoadMore?: () => void
  /** Reports whether the list is scrolled to its very top. */
  onAtTopChange?: (atTop: boolean) => void
  /** Increment to scroll back to the top. */
  scrollTopSignal?: number
  ariaLabel: string
}

const rowDomId = (key: string) => `feed-row-${key}`

function ageTitle(item: FeedItem, t: TFunction): string {
  const { unit, n } = relativeParts((Date.now() - Date.parse(item.announced_at)) / 1000)
  return t(`kit.time.${unit}`, { n })
}

function ItemRow({ item, nested }: { item: FeedItem; nested: boolean }) {
  const { t } = useTranslation()
  const headline = headlineOf(item)
  return (
    <div className={cn('grid h-full grid-cols-[44px_minmax(0,1fr)] items-center gap-x-2 px-2', nested && 'pl-6')}>
      <div className="flex flex-col items-end leading-tight" title={`${ageTitle(item, t)} · ${formatDM(item.announced_at)} ${formatHM(item.announced_at)}`}>
        <time dateTime={item.announced_at} className="num text-sm font-medium">
          {formatHM(item.announced_at)}
        </time>
        <span className="num text-xs text-muted-foreground">{formatDM(item.announced_at)}</span>
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex min-w-0 items-center gap-1.5 overflow-hidden">
          <Prio priority={item.priority} />
          <Tickers item={item} max={3} />
          <span className="min-w-0 truncate text-xs text-muted-foreground" title={item.code}>
            {item.code_label}
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-1">
            <Materiality value={item.materiality} band={item.materiality_band} />
            <Stage stage={item.stage} />
            <Markers item={item} />
            <KapLink url={item.kap_url} />
          </span>
        </div>
        <div className={cn('truncate', item.holding && 'font-medium')} title={headline}>
          {headline}
        </div>
      </div>
    </div>
  )
}

/** Virtualised, keyboard-navigable tape (listbox). Fills its parent. */
export function FeedList({
  rows,
  selectedId,
  onSelect,
  onToggleBurst,
  loading,
  error,
  onRetry,
  empty,
  flashIds,
  hasMore,
  loadingMore,
  onLoadMore,
  onAtTopChange,
  scrollTopSignal,
  ariaLabel,
}: FeedListProps) {
  const { t } = useTranslation()
  const scrollRef = useRef<HTMLDivElement>(null)
  const itemRows = useMemo(() => rows.filter((r) => r.type === 'item'), [rows])

  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (i) => (rows[i]?.type === 'burst' ? BURST_H : ITEM_H),
    overscan: 10,
    getItemKey: (i) => rows[i]?.key ?? i,
    initialRect: { width: 600, height: 800 },
    observeElementRect: (instance, cb) =>
      observeElementRect(instance, (rect) => cb(rect.height === 0 ? { width: 600, height: 800 } : rect)),
  })
  useEffect(() => {
    virtualizer.measure()
  }, [virtualizer, rows])

  const selectedIndex = rows.findIndex((r) => r.type === 'item' && r.item.id === selectedId)
  useEffect(() => {
    if (selectedIndex >= 0) virtualizer.scrollToIndex(selectedIndex, { align: 'auto' })
    // Only when the selection changes, not when rows are added above it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  useEffect(() => {
    if (scrollTopSignal && scrollRef.current) scrollRef.current.scrollTop = 0
  }, [scrollTopSignal])

  const virtualItems = virtualizer.getVirtualItems()
  const lastVisible = virtualItems.length ? virtualItems[virtualItems.length - 1]!.index : -1
  useEffect(() => {
    if (hasMore && !loadingMore && onLoadMore && rows.length > 0 && lastVisible >= rows.length - 8) onLoadMore()
  }, [hasMore, loadingMore, onLoadMore, rows.length, lastVisible])

  const move = (delta: number | 'first' | 'last') => {
    if (itemRows.length === 0) return
    const cur = itemRows.findIndex((r) => r.item.id === selectedId)
    const next = delta === 'first' ? 0 : delta === 'last' ? itemRows.length - 1 : cur < 0 ? 0 : Math.min(Math.max(cur + delta, 0), itemRows.length - 1)
    onSelect(itemRows[next]!.item)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return
    if (e.key === 'ArrowDown') move(1)
    else if (e.key === 'ArrowUp') move(-1)
    else if (e.key === 'Home') move('first')
    else if (e.key === 'End') move('last')
    else if (e.key === 'Enter') {
      const cur = itemRows.find((r) => r.item.id === selectedId)
      if (cur?.item.kap_url) window.open(cur.item.kap_url, '_blank', 'noopener,noreferrer')
    } else return
    e.preventDefault()
  }

  let body: ReactNode = null
  if (loading) {
    body = (
      <div aria-busy="true" data-testid="feed-skeleton">
        {Array.from({ length: 9 }, (_, i) => (
          <div key={i} className="flex items-center gap-2 border-b border-border px-2" style={{ height: ITEM_H }}>
            <Skeleton className="h-6 w-10" />
            <div className="flex flex-1 flex-col gap-1.5">
              <Skeleton className="h-3 w-2/5" />
              <Skeleton className="h-3 w-4/5" />
            </div>
          </div>
        ))}
      </div>
    )
  } else if (error) {
    body = <ErrorState description={error instanceof Error ? error.message : undefined} onRetry={onRetry} />
  } else if (rows.length === 0) {
    body = empty ?? <EmptyState />
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-slot="feed-list">
      <div
        ref={scrollRef}
        role="listbox"
        aria-label={ariaLabel}
        aria-activedescendant={selectedIndex >= 0 ? rowDomId(rows[selectedIndex]!.key) : undefined}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onScroll={(e) => onAtTopChange?.(e.currentTarget.scrollTop < 8)}
        className="relative min-h-0 flex-1 overflow-auto outline-none focus-visible:outline-offset-[-2px]"
      >
        {body ?? (
          <>
            <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
              {virtualItems.map((v) => {
                const row = rows[v.index]
                if (!row) return null
                const style = { height: row.type === 'burst' ? BURST_H : ITEM_H, transform: `translateY(${v.start}px)` }
                if (row.type === 'burst') {
                  const first = row.items[row.items.length - 1]!
                  const last = row.items[0]!
                  return (
                    <div
                      key={row.key}
                      id={rowDomId(row.key)}
                      role="option"
                      aria-selected={false}
                      aria-expanded={row.open}
                      onClick={() => onToggleBurst(row.key)}
                      className="absolute left-0 top-0 flex w-full cursor-default items-center gap-2 border-b border-border bg-muted/40 px-2 text-sm text-muted-foreground hover:bg-accent/60"
                      style={style}
                    >
                      <ChevronRight className={cn('size-3.5 transition-transform', row.open && 'rotate-90')} />
                      <span className="num font-medium text-foreground">{t('feed.row.burst', { count: row.items.length })}</span>
                      <span className="truncate">{last.code_label}</span>
                      <span className="num ml-auto shrink-0">
                        {formatHM(first.announced_at)}–{formatHM(last.announced_at)}
                      </span>
                    </div>
                  )
                }
                const selected = row.item.id === selectedId
                return (
                  <div
                    key={row.key}
                    id={rowDomId(row.key)}
                    role="option"
                    aria-selected={selected}
                    data-selected={selected || undefined}
                    onClick={() => {
                      scrollRef.current?.focus({ preventScroll: true })
                      onSelect(row.item)
                    }}
                    onDoubleClick={() => row.item.kap_url && window.open(row.item.kap_url, '_blank', 'noopener,noreferrer')}
                    className={cn(
                      'absolute left-0 top-0 w-full cursor-default border-b border-border hover:bg-accent/60',
                      selected && 'bg-accent shadow-[inset_2px_0_0_var(--brand)]',
                      flashIds?.has(row.item.id) && 'doc-flash',
                    )}
                    style={style}
                  >
                    <ItemRow item={row.item} nested={row.nested} />
                  </div>
                )
              })}
            </div>
            <div className="flex h-10 items-center justify-center text-xs text-muted-foreground">
              {loadingMore ? (
                <span role="status">{t('feed.loadingMore')}</span>
              ) : hasMore ? (
                <Button variant="ghost" size="sm" onClick={onLoadMore}>
                  {t('feed.loadMore')}
                </Button>
              ) : (
                <span>{t('feed.endOfList')}</span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
