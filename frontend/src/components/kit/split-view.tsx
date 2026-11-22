import { ArrowLeft } from 'lucide-react'
import { type KeyboardEvent, type PointerEvent, type ReactNode, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { useIsDesktop } from '@/hooks/use-media-query'
import { readStorage, writeStorage } from '@/lib/storage'
import { cn } from '@/lib/utils'

const MIN = 280
const MAX = 720
const STEP = 16

const clamp = (n: number) => Math.min(MAX, Math.max(MIN, n))

export interface SplitViewProps {
  list: ReactNode
  detail: ReactNode
  /** True when an item is selected. On mobile this switches from the list to the detail panel. */
  hasDetail: boolean
  /** Escape key / mobile back button. Typically navigates back to the list route. */
  onCloseDetail: () => void
  /** Shown in the detail pane on desktop when nothing is selected. */
  emptyDetail?: ReactNode
  /** Title in the mobile detail header. */
  detailTitle?: ReactNode
  /** localStorage suffix for the persisted list width. */
  storageKey?: string
  defaultWidth?: number
  className?: string
}

/**
 * Master/detail layout. Desktop: list left, detail right, draggable divider (width persisted).
 * Mobile (< md): only the list; the detail opens as a full-screen panel with a back button.
 * Fills its parent (give the parent a height). Escape closes the detail and returns focus to the list.
 */
export function SplitView({
  list,
  detail,
  hasDetail,
  onCloseDetail,
  emptyDetail,
  detailTitle,
  storageKey = 'default',
  defaultWidth = 440,
  className,
}: SplitViewProps) {
  const { t } = useTranslation()
  const desktop = useIsDesktop()
  const key = `papi-split-${storageKey}`
  const [width, setWidth] = useState(() => {
    const saved = Number(readStorage(key))
    return Number.isFinite(saved) && saved > 0 ? clamp(saved) : clamp(defaultWidth)
  })
  const [dragging, setDragging] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const detailRef = useRef<HTMLDivElement>(null)
  const startRef = useRef({ x: 0, w: 0 })

  const commit = (w: number) => {
    const next = clamp(w)
    setWidth(next)
    writeStorage(key, String(Math.round(next)))
  }

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    startRef.current = { x: e.clientX, w: width }
    setDragging(true)
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!dragging) return
    setWidth(clamp(startRef.current.w + e.clientX - startRef.current.x))
  }
  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!dragging) return
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    setDragging(false)
    commit(startRef.current.w + e.clientX - startRef.current.x)
  }
  const onDividerKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? STEP * 4 : STEP
    if (e.key === 'ArrowLeft') commit(width - step)
    else if (e.key === 'ArrowRight') commit(width + step)
    else if (e.key === 'Home') commit(MIN)
    else if (e.key === 'End') commit(MAX)
    else return
    e.preventDefault()
  }

  const close = () => {
    onCloseDetail()
    // Hand focus back to the list so j/k keeps working.
    requestAnimationFrame(() => (listRef.current?.querySelector<HTMLElement>('[tabindex="0"]') ?? listRef.current)?.focus())
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape' && hasDetail && !e.defaultPrevented) {
      e.stopPropagation()
      close()
    }
  }

  // Move focus into the detail panel when it opens as a full-screen mobile panel.
  useEffect(() => {
    if (!desktop && hasDetail) detailRef.current?.focus()
  }, [desktop, hasDetail])

  if (!desktop) {
    return (
      <div className={cn('relative h-full min-h-0', className)} onKeyDown={onKeyDown}>
        <div ref={listRef} className="h-full min-h-0 overflow-hidden" inert={hasDetail || undefined}>
          {list}
        </div>
        {hasDetail && (
          <div ref={detailRef} tabIndex={-1} className="absolute inset-0 z-20 flex flex-col bg-background outline-none" data-testid="detail-panel">
            <div className="flex h-10 shrink-0 items-center gap-1 border-b border-border px-1">
              <Button variant="ghost" size="sm" onClick={close} aria-label={t('kit.split.back')}>
                <ArrowLeft /> {t('kit.split.back')}
              </Button>
              {detailTitle && <span className="truncate font-medium">{detailTitle}</span>}
            </div>
            <div className="min-h-0 flex-1 overflow-auto">{detail}</div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className={cn('flex h-full min-h-0', dragging && 'select-none', className)} onKeyDown={onKeyDown}>
      <div ref={listRef} className="min-h-0 shrink-0 overflow-hidden" style={{ width }}>
        {list}
      </div>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label={t('kit.split.resize')}
        aria-valuenow={Math.round(width)}
        aria-valuemin={MIN}
        aria-valuemax={MAX}
        tabIndex={0}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onKeyDown={onDividerKey}
        onDoubleClick={() => commit(defaultWidth)}
        className={cn(
          'group relative w-px shrink-0 cursor-col-resize touch-none bg-border outline-none',
          'before:absolute before:inset-y-0 before:-left-1.5 before:-right-1.5 before:content-[""]',
          'hover:bg-brand focus-visible:bg-brand',
          dragging && 'bg-brand',
        )}
      />
      <div ref={detailRef} tabIndex={-1} className="min-h-0 min-w-0 flex-1 overflow-auto outline-none" data-testid="detail-pane">
        {hasDetail ? (
          detail
        ) : (
          (emptyDetail ?? (
            <div className="flex h-full items-center justify-center text-muted-foreground">{t('kit.split.selectItem')}</div>
          ))
        )}
      </div>
    </div>
  )
}
