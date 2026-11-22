import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type OnChangeFn,
  type Row,
  type SortingState,
  useReactTable,
  type VisibilityState,
} from '@tanstack/react-table'
import { observeElementRect, useVirtualizer } from '@tanstack/react-virtual'
import { ChevronDown, ChevronUp, Columns3 } from 'lucide-react'
import { DropdownMenu } from 'radix-ui'
import { type KeyboardEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Skeleton } from '@/components/ui/skeleton'
import { useMediaQuery } from '@/hooks/use-media-query'
import { cn } from '@/lib/utils'

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData, TValue> {
    /** Right-aligned mono tabular numbers. */
    numeric?: boolean
    /** Column takes the remaining width (min width = `size`). */
    grow?: boolean
    /** Extra classes for body cells. */
    cellClassName?: string
    /** Label used in the column-visibility menu (defaults to a string header or the column id). */
    label?: string
  }
}

const ROW_H = 32
const ROW_H_COMPACT = 28
const CARD_H = 76

export interface DataTableProps<T> {
  data: readonly T[]
  columns: ColumnDef<T, any>[] // eslint-disable-line @typescript-eslint/no-explicit-any
  getRowId: (row: T) => string
  /** Selected row id (controlled). */
  selectedId?: string | null
  /** Called when the selection changes by click or keyboard (j/k, arrows, Home/End). */
  onSelect?: (id: string, row: T) => void
  /** Called on Enter or double click. */
  onActivate?: (id: string, row: T) => void
  loading?: boolean
  /** Truthy shows the error state (string/Error message is displayed). */
  error?: unknown
  onRetry?: () => void
  /** Custom empty state. */
  empty?: ReactNode
  /** 28px rows instead of 32px. */
  compact?: boolean
  /** Controlled / initial sorting. */
  sorting?: SortingState
  onSortingChange?: OnChangeFn<SortingState>
  initialSorting?: SortingState
  /** Show the column visibility menu. */
  columnMenu?: boolean
  columnVisibility?: VisibilityState
  onColumnVisibilityChange?: OnChangeFn<VisibilityState>
  /** Render rows as cards below the md breakpoint (or when `mobile` is true). */
  mobileRender?: (row: T) => ReactNode
  /** Force mobile / desktop mode instead of reading the viewport. */
  mobile?: boolean
  ariaLabel?: string
  className?: string
}

function errorMessage(error: unknown): string | undefined {
  if (typeof error === 'string') return error
  if (error instanceof Error) return error.message
  return undefined
}

/**
 * Dense, virtualised, keyboard-driven table. Fills its parent (give the parent a height).
 * Keyboard (when focused): j/ArrowDown, k/ArrowUp, Home/End move the selection, Enter activates.
 */
export function DataTable<T>({
  data,
  columns,
  getRowId,
  selectedId,
  onSelect,
  onActivate,
  loading,
  error,
  onRetry,
  empty,
  compact,
  sorting: sortingProp,
  onSortingChange,
  initialSorting,
  columnMenu,
  columnVisibility: visibilityProp,
  onColumnVisibilityChange,
  mobileRender,
  mobile,
  ariaLabel,
  className,
}: DataTableProps<T>) {
  const { t } = useTranslation()
  const isNarrow = useMediaQuery('(max-width: 767px)')
  const asCards = !!mobileRender && (mobile ?? isNarrow)

  const [sortingState, setSortingState] = useState<SortingState>(initialSorting ?? [])
  const [visibilityState, setVisibilityState] = useState<VisibilityState>({})

  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable<T>({
    data: data as T[],
    columns: columns as ColumnDef<T, unknown>[],
    getRowId,
    state: {
      sorting: sortingProp ?? sortingState,
      columnVisibility: visibilityProp ?? visibilityState,
    },
    onSortingChange: onSortingChange ?? setSortingState,
    onColumnVisibilityChange: onColumnVisibilityChange ?? setVisibilityState,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    enableSortingRemoval: true,
    sortDescFirst: false,
  })

  const rows = table.getRowModel().rows
  const rowH = compact ? ROW_H_COMPACT : ROW_H
  const leaf = table.getVisibleLeafColumns()

  const gridTemplate = useMemo(
    () => leaf.map((c) => (c.columnDef.meta?.grow ? `minmax(${c.getSize()}px, 1fr)` : `${c.getSize()}px`)).join(' '),
    [leaf],
  )
  const minWidth = leaf.reduce((sum, c) => sum + c.getSize(), 0)

  const scrollRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => (asCards ? CARD_H : rowH),
    overscan: 12,
    getItemKey: (i) => rows[i]?.id ?? i,
    // Gives a sensible first render before the scroll element is measured, and keeps rows rendered while
    // the element has no layout (hidden tab, jsdom).
    initialRect: { width: 800, height: 800 },
    observeElementRect: (instance, cb) =>
      observeElementRect(instance, (rect) => cb(rect.height === 0 ? { width: 800, height: 800 } : rect)),
  })
  useEffect(() => {
    virtualizer.measure()
  }, [virtualizer, asCards, rowH])

  const selectedIndex = selectedId == null ? -1 : rows.findIndex((r) => r.id === selectedId)

  const select = (index: number) => {
    const row = rows[index]
    if (!row) return
    onSelect?.(row.id, row.original)
    virtualizer.scrollToIndex(index, { align: 'auto' })
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return
    switch (e.key) {
      case 'j':
      case 'ArrowDown':
        e.preventDefault()
        select(selectedIndex < 0 ? 0 : Math.min(selectedIndex + 1, rows.length - 1))
        break
      case 'k':
      case 'ArrowUp':
        e.preventDefault()
        select(selectedIndex < 0 ? 0 : Math.max(selectedIndex - 1, 0))
        break
      case 'Home':
        e.preventDefault()
        select(0)
        break
      case 'End':
        e.preventDefault()
        select(rows.length - 1)
        break
      case 'Enter': {
        const row = rows[selectedIndex]
        if (row) {
          e.preventDefault()
          onActivate?.(row.id, row.original)
        }
        break
      }
    }
  }

  const rowId = (id: string) => `dt-row-${id}`

  const body = (): ReactNode => {
    if (loading) return <SkeletonRows columns={leaf.map((c) => !!c.columnDef.meta?.numeric)} template={gridTemplate} minWidth={minWidth} rowH={rowH} cards={asCards} />
    if (error) {
      return <ErrorState description={errorMessage(error)} onRetry={onRetry} />
    }
    if (rows.length === 0) return empty ?? <EmptyState />
    return null
  }
  const state = body()

  const renderRow = (row: Row<T>, index: number, start: number) => {
    const selected = row.id === selectedId
    const common = {
      id: rowId(row.id),
      role: 'row' as const,
      'aria-selected': selected,
      'aria-rowindex': index + 2,
      'data-index': index,
      'data-selected': selected || undefined,
      onClick: () => {
        scrollRef.current?.focus({ preventScroll: true })
        onSelect?.(row.id, row.original)
      },
      onDoubleClick: () => onActivate?.(row.id, row.original),
    }
    if (asCards) {
      return (
        <div
          key={row.id}
          {...common}
          ref={virtualizer.measureElement}
          className={cn(
            'absolute left-0 top-0 w-full cursor-default border-b border-border px-3 py-2 text-left hover:bg-accent/60',
            selected && 'bg-accent',
          )}
          style={{ transform: `translateY(${start}px)` }}
        >
          {mobileRender!(row.original)}
        </div>
      )
    }
    return (
      <div
        key={row.id}
        {...common}
        className={cn(
          'absolute left-0 top-0 grid w-full cursor-default items-center border-b border-border hover:bg-accent/60',
          selected && 'bg-accent shadow-[inset_2px_0_0_var(--brand)]',
        )}
        style={{ height: rowH, transform: `translateY(${start}px)`, gridTemplateColumns: gridTemplate }}
      >
        {row.getVisibleCells().map((cell) => {
          const meta = cell.column.columnDef.meta
          return (
            <div
              key={cell.id}
              role="cell"
              className={cn('min-w-0 truncate px-3', meta?.numeric && 'num text-right', meta?.cellClassName)}
            >
              {flexRender(cell.column.columnDef.cell, cell.getContext())}
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <div className={cn('flex h-full min-h-0 flex-col', className)} data-slot="data-table">
      {columnMenu && !asCards && (
        <div className="flex h-8 shrink-0 items-center justify-end border-b border-border px-2">
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <Button variant="ghost" size="sm" className="text-muted-foreground">
                <Columns3 /> {t('kit.table.columns')}
              </Button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                align="end"
                sideOffset={4}
                className="z-50 min-w-40 rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md"
              >
                {table
                  .getAllLeafColumns()
                  .filter((c) => c.getCanHide())
                  .map((c) => (
                    <DropdownMenu.CheckboxItem
                      key={c.id}
                      checked={c.getIsVisible()}
                      onCheckedChange={(v) => c.toggleVisibility(!!v)}
                      onSelect={(e) => e.preventDefault()}
                      className="flex h-7 cursor-default items-center gap-2 rounded-sm px-2 outline-none data-[highlighted]:bg-accent"
                    >
                      <span className="flex size-3.5 items-center justify-center rounded-sm border border-input">
                        <DropdownMenu.ItemIndicator>
                          <span className="size-2 rounded-[1px] bg-brand" />
                        </DropdownMenu.ItemIndicator>
                      </span>
                      {c.columnDef.meta?.label ?? (typeof c.columnDef.header === 'string' ? c.columnDef.header : c.id)}
                    </DropdownMenu.CheckboxItem>
                  ))}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        </div>
      )}

      <div
        ref={scrollRef}
        role={asCards ? 'listbox' : 'grid'}
        aria-label={ariaLabel}
        aria-rowcount={rows.length + 1}
        aria-activedescendant={selectedIndex >= 0 ? rowId(rows[selectedIndex]!.id) : undefined}
        tabIndex={0}
        onKeyDown={onKeyDown}
        className="relative min-h-0 flex-1 overflow-auto outline-none focus-visible:outline-offset-[-2px]"
      >
        <div style={{ minWidth: asCards ? undefined : minWidth }}>
          {!asCards && (
            <div
              role="row"
              className="sticky top-0 z-10 grid items-center border-b border-border bg-card text-xs uppercase tracking-wide text-muted-foreground"
              style={{ height: rowH, gridTemplateColumns: gridTemplate }}
            >
              {table.getHeaderGroups()[0]?.headers.map((h) => {
                const meta = h.column.columnDef.meta
                const sorted = h.column.getIsSorted()
                const canSort = h.column.getCanSort()
                return (
                  <div
                    key={h.id}
                    role="columnheader"
                    aria-sort={sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : canSort ? 'none' : undefined}
                    className={cn('min-w-0 truncate px-3', meta?.numeric && 'text-right')}
                  >
                    {canSort ? (
                      <button
                        type="button"
                        onClick={h.column.getToggleSortingHandler()}
                        className={cn(
                          'inline-flex max-w-full items-center gap-1 uppercase hover:text-foreground',
                          sorted && 'text-foreground',
                          meta?.numeric && 'flex-row-reverse',
                        )}
                      >
                        <span className="truncate">{flexRender(h.column.columnDef.header, h.getContext())}</span>
                        {sorted === 'asc' ? <ChevronUp className="size-3" /> : sorted === 'desc' ? <ChevronDown className="size-3" /> : null}
                      </button>
                    ) : (
                      flexRender(h.column.columnDef.header, h.getContext())
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {state ?? (
            <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
              {virtualizer.getVirtualItems().map((v) => {
                const row = rows[v.index]
                return row ? renderRow(row, v.index, v.start) : null
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function SkeletonRows({
  columns,
  template,
  minWidth,
  rowH,
  cards,
}: {
  columns: boolean[]
  template: string
  minWidth: number
  rowH: number
  cards: boolean
}) {
  const widths = ['w-3/4', 'w-1/2', 'w-5/6', 'w-2/3']
  return (
    <div data-testid="table-skeleton" aria-busy="true" style={{ minWidth: cards ? undefined : minWidth }}>
      {Array.from({ length: 8 }, (_, i) =>
        cards ? (
          <div key={i} className="flex flex-col gap-2 border-b border-border px-3 py-2.5" style={{ height: CARD_H }}>
            <Skeleton className="h-3 w-1/3" />
            <Skeleton className="h-3 w-5/6" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        ) : (
          <div key={i} className="grid items-center border-b border-border" style={{ height: rowH, gridTemplateColumns: template }}>
            {columns.map((numeric, j) => (
              <div key={j} className={cn('px-3', numeric && 'flex justify-end')}>
                <Skeleton className={cn('h-2.5', numeric ? 'w-12' : widths[(i + j) % widths.length])} />
              </div>
            ))}
          </div>
        ),
      )}
    </div>
  )
}
