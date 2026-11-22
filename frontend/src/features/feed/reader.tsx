import { Link } from '@tanstack/react-router'
import { Bot, ExternalLink } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Skeleton } from '@/components/ui/skeleton'
import { formatDateTime, formatDuration, lagTone, toneClass } from '@/lib/format'
import { cn } from '@/lib/utils'
import { RunAgentDialog } from '../agents/run-dialog'
import { type EventDetail, useEventDetail } from './api'
import { Documents } from './documents'
import { Markers, Materiality, Prio, Stage, Tickers } from './item-bits'
import { headlineOf } from './lib'
import { Analyses, Feedback, ForMe, Forecasts, History, KeyFacts, PayloadAndRules, Summary } from './reader-sections'

function ReaderSkeleton() {
  return (
    <div className="flex flex-col gap-4 p-4" aria-busy="true" data-testid="reader-skeleton">
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="h-6 w-full" />
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

function Meta({ label, children, title }: { label: string; children: React.ReactNode; title?: string }) {
  return (
    <span className="flex items-baseline gap-1" title={title}>
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="num">{children}</span>
    </span>
  )
}

function ReaderBody({ detail }: { detail: EventDetail }) {
  const { t, i18n } = useTranslation()
  const { item, event } = detail
  const lang = i18n.language
  const headline = headlineOf(item)
  const [docTab, setDocTab] = useState(detail.documents[0]?.id ?? '')
  const [highlight, setHighlight] = useState<string | null>(null)
  const [runOpen, setRunOpen] = useState(false)

  const cite = (anchor: string) => {
    const doc = detail.documents.find((d) => d.spans.some((s) => s.anchor === anchor)) ?? detail.documents.find((d) => d.html.includes(`id="${anchor}"`))
    if (doc) setDocTab(doc.id)
    // Clear first so clicking the same citation again re-triggers the flash.
    setHighlight(null)
    requestAnimationFrame(() => setHighlight(anchor))
  }

  return (
    <article className="flex flex-col gap-3 p-3 md:p-4" aria-labelledby="reader-heading">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Prio priority={item.priority} />
          <Tickers item={item} />
          <span className="text-sm text-muted-foreground" title={`${event.code}${event.taxonomy_version ? ` (${event.taxonomy_version})` : ''}`}>
            {item.code_label}
          </span>
          <Materiality value={item.materiality} band={item.materiality_band} />
          <Stage stage={item.stage} />
          <Markers item={item} />
          <span className="ml-auto flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={!item.source_ref} onClick={() => setRunOpen(true)} data-testid="run-agent">
              <Bot /> {t('feed.reader.runAgent')}
            </Button>
            {item.source_ref && <RunAgentDialog open={runOpen} onOpenChange={setRunOpen} agent="kap-analyst" lockAgent refValue={item.source_ref} />}
            {item.kap_url && (
              <Button asChild variant="outline" size="sm">
                <a href={item.kap_url} target="_blank" rel="noopener noreferrer">
                  <ExternalLink /> {t('feed.reader.openKap')}
                </a>
              </Button>
            )}
          </span>
        </div>
        <h1 id="reader-heading" tabIndex={-1} className="text-xl font-semibold leading-snug tracking-tight">
          {headline}
        </h1>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
          {item.issuers.length > 0 && <span className="text-muted-foreground">{item.issuers.map((i) => i.name).join(', ')}</span>}
          <Meta label={t('feed.reader.published')}>{formatDateTime(item.published_at ?? item.announced_at, { lang })}</Meta>
          <Meta label={t('feed.reader.firstSeen')}>{item.first_seen_at ? formatDateTime(item.first_seen_at, { lang }) : '–'}</Meta>
          <Meta label={t('feed.reader.latency')} title={t('feed.reader.latencyTitle')}>
            <span className={cn(toneClass[item.latency_s == null ? 'muted' : lagTone(item.latency_s)])}>{item.latency_s != null ? formatDuration(item.latency_s) : '–'}</span>
          </Meta>
          <span className="font-mono text-xs text-muted-foreground">{event.code}</span>
        </div>
        {event.superseded_by && (
          <p className="rounded-md border border-warning/40 bg-warning/10 px-3 py-1.5 text-sm">
            {t('feed.reader.supersededBy')}{' '}
            <Link to="/feed/$eventId" params={{ eventId: event.superseded_by }} search={(prev) => prev} className="text-brand hover:underline">
              {t('feed.reader.currentEvent')}
            </Link>
          </p>
        )}
      </header>

      <ForMe detail={detail} />
      <Summary detail={detail} lang={lang} onCite={cite} />
      <KeyFacts detail={detail} onCite={cite} />
      <Analyses detail={detail} onCite={cite} />
      <Forecasts detail={detail} />
      <Documents detail={detail} value={docTab} onValueChange={setDocTab} highlight={highlight} />
      <PayloadAndRules detail={detail} />
      <History detail={detail} />
      <Feedback detail={detail} />
    </article>
  )
}

/** Reader pane for one event (`/feed/$eventId`). */
export function Reader({ eventId, onClose }: { eventId: string; onClose?: () => void }) {
  const { t } = useTranslation()
  const q = useEventDetail(eventId)
  let content: React.ReactNode
  if (q.isPending) content = <ReaderSkeleton />
  else if (q.isError) {
    const notFound = q.error instanceof ApiError && q.error.status === 404
    content = notFound ? (
      <EmptyState
        title={t('feed.reader.notFound')}
        description={t('feed.reader.notFoundDesc')}
        action={
          onClose && (
            <Button variant="outline" size="sm" onClick={onClose}>
              {t('feed.reader.backToFeed')}
            </Button>
          )
        }
      />
    ) : (
      <ErrorState description={q.error.message} onRetry={() => void q.refetch()} />
    )
  } else content = <ReaderBody key={eventId} detail={q.data} />

  return (
    <section role="region" aria-label={t('feed.reader.region')} className="min-h-full">
      {q.isPending || q.isError ? <h1 className="sr-only">{t('feed.reader.region')}</h1> : null}
      {content}
    </section>
  )
}

