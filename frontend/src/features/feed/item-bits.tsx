import { ExternalLink } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { PriorityBadge } from '@/components/kit/priority-badge'
import { TickerChip } from '@/components/kit/ticker-chip'
import { Badge } from '@/components/ui/badge'
import { formatDuration } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { FeedItem } from './lib'

/** P1..P3 via the kit badge; P4 (lowest) and missing priorities get a quiet text badge. */
export function Prio({ priority }: { priority: string | null }) {
  if (priority === 'P1' || priority === 'P2' || priority === 'P3') return <PriorityBadge priority={priority} />
  return (
    <span className="inline-flex h-[18px] min-w-[26px] shrink-0 items-center justify-center rounded-sm border border-border px-1 font-mono text-xs leading-none text-muted-foreground">
      {priority ?? '–'}
    </span>
  )
}

const bandTone: Record<string, string> = {
  critical: 'text-error',
  high: 'text-warning',
  medium: 'text-foreground',
  low: 'text-muted-foreground',
}

/** Materiality score (0-100) coloured by band; the band name is in the tooltip. */
export function Materiality({ value, band }: { value: number | null; band: string | null }) {
  const { t } = useTranslation()
  if (value == null) return null
  return (
    <span
      title={t('feed.row.materiality', { value, band: band ? t(`feed.band.${band}`, band) : '–' })}
      className={cn('num inline-flex h-[18px] min-w-6 items-center justify-center rounded-sm border border-border px-1 text-xs leading-none', band && bandTone[band])}
    >
      {value}
    </span>
  )
}

export function Stage({ stage }: { stage: FeedItem['stage'] }) {
  const { t } = useTranslation()
  return (
    <Badge variant={stage === 'analysed' ? 'brand' : 'outline'} title={t(`feed.stageTitle.${stage}`)}>
      {t(`feed.stage.${stage}`)}
    </Badge>
  )
}

/** Revision / catch-up / late / provisional markers. */
export function Markers({ item }: { item: FeedItem }) {
  const { t } = useTranslation()
  return (
    <>
      {item.revision_label && <Badge variant="warning" title={t('feed.markers.revision')}>{item.revision_label}</Badge>}
      {item.catchup && (
        <Badge variant="warning" title={t('feed.markers.catchupTitle', { latency: item.latency_s != null ? formatDuration(item.latency_s) : '–' })}>
          {t('feed.markers.catchup')}
        </Badge>
      )}
      {item.is_late && <Badge variant="down" title={t('feed.markers.lateTitle')}>{t('feed.markers.late')}</Badge>}
      {item.status === 'provisional' && <Badge variant="outline" title={t('feed.markers.provisionalTitle')}>{t('feed.markers.provisional')}</Badge>}
    </>
  )
}

export function Tickers({ item, max }: { item: FeedItem; max?: number }) {
  const shown = max ? item.tickers.slice(0, max) : item.tickers
  const rest = item.tickers.length - shown.length
  return (
    <>
      {shown.map((tk) => (
        <TickerChip key={tk.ticker} ticker={tk.ticker} mark={tk.badge === 'HOLDING' ? 'holding' : tk.badge === 'WATCH' ? 'watch' : null} />
      ))}
      {rest > 0 && <span className="num text-xs text-muted-foreground">+{rest}</span>}
    </>
  )
}

export function KapLink({ url, className }: { url: string | null; className?: string }) {
  const { t } = useTranslation()
  if (!url) return null
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={t('feed.row.openKap')}
      aria-label={t('feed.row.openKap')}
      onClick={(e) => e.stopPropagation()}
      className={cn('inline-flex size-5 items-center justify-center rounded-sm text-muted-foreground hover:bg-accent hover:text-brand', className)}
    >
      <ExternalLink className="size-3.5" />
    </a>
  )
}
