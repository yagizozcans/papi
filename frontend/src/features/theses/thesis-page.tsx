import { Link } from '@tanstack/react-router'
import { ArrowLeft, Download, Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { EmptyState } from '@/components/ui/empty-state'
import { Badge } from '@/components/ui/badge'
import { Panel } from '@/components/ui/panel'
import { isNotFound } from '../shared/api'
import { PanelError, SkeletonLines } from '../shared/states'
import { useDocumentTitle } from '../shared/use-document-title'
import { type ThesisDetail, useThesis } from './api'
import { ReviewPanel } from './review-panel'
import { ThesisEditor } from './thesis-editor'
import { ThesisStatusBadge } from './theses-page'

function Progress({ d }: { d: ThesisDetail }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col gap-1 rounded-md border border-brand/40 bg-brand/10 px-3 py-2 text-sm" role="status" data-testid="import-progress">
      <span className="flex items-center gap-2">
        <Loader2 className="size-4 animate-spin" />
        <ThesisStatusBadge status={d.status} />
        {d.source_files.length > 0 && <span className="num">{t('theses.page.progress', { done: d.files_done ?? 0, total: d.source_files.length, pages: d.pages_done ?? 0 })}</span>}
        {d.status === 'analysing' && d.run_id && (
          <span>
            {t('theses.page.analysing')}:{' '}
            <Link to="/research/agents/runs/$runId" params={{ runId: d.run_id }} className="text-brand hover:underline">
              {t('theses.page.watchLive')}
            </Link>
          </span>
        )}
      </span>
      {d.status === 'extracting' && <span className="text-xs text-muted-foreground">{t('theses.page.extractingHint')}</span>}
    </div>
  )
}

function Pages({ d }: { d: ThesisDetail }) {
  const { t } = useTranslation()
  if (d.pages.length === 0) return null
  return (
    <Panel title={t('theses.page.pages', { count: d.pages.length })}>
      <div className="divide-y divide-border">
        {d.pages.map((p, i) => (
          <details key={i} className="px-3 py-2">
            <summary className="cursor-pointer text-sm">
              {t('theses.page.page', { n: i + 1 })} · <span className="font-mono text-xs">{p.file}</span> s.{p.number}{' '}
              <span className="text-xs text-muted-foreground">({t(`theses.page.method.${p.method}`, { defaultValue: p.method })})</span>
              {p.uncertain.length > 0 && <Badge variant="warning" className="ml-2">{p.uncertain.length}</Badge>}
            </summary>
            {p.uncertain.length > 0 && (
              <p className="mt-1 text-xs text-muted-foreground">
                {t('theses.page.uncertain')}: {p.uncertain.map((w) => <span key={w} className="mr-1 rounded border border-border px-1">{w}</span>)}
              </p>
            )}
            <pre className="mt-1 whitespace-pre-wrap font-sans text-sm">{p.text}</pre>
          </details>
        ))}
      </div>
    </Panel>
  )
}

export function ThesisPage({ name }: { name: string }) {
  const { t } = useTranslation()
  useDocumentTitle(name)
  const q = useThesis(name)
  const [dirty, setDirty] = useState(false)
  const d = q.data

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-3 p-3 md:p-4" data-testid="thesis-page">
      <Link to="/research/theses" className="inline-flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-3.5" /> {t('theses.page.back')}
      </Link>
      {q.isLoading ? (
        <SkeletonLines rows={8} />
      ) : q.isError || !d ? (
        isNotFound(q.error) ? <EmptyState title={t('theses.page.notFound')} /> : <PanelError error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <>
          <header className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-mono text-xl font-semibold tracking-tight">{d.name}</h1>
              <ThesisStatusBadge status={d.status} />
              <span className="text-sm text-muted-foreground">{[d.direction, d.horizon, d.date].filter(Boolean).join(' · ')}</span>
            </div>
            {d.source_files.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>{t('theses.page.files')}:</span>
                {d.source_files.map((f) => (
                  <a key={f.name} href={f.url} download className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-foreground hover:bg-accent">
                    <Download className="size-3" /> {f.name}
                  </a>
                ))}
              </div>
            )}
            {d.active && <Progress d={d} />}
            {d.status === 'failed' && d.status_error && <p role="alert" className="rounded-md border border-down/40 bg-down/10 px-3 py-2 text-sm text-down">{d.status_error}</p>}
            {d.status === 'stale' && <p className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">{t('theses.page.stale')}</p>}
            {d.warnings.length > 0 && (
              <ul className="list-disc space-y-0.5 pl-5 text-sm text-warning">
                {d.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            )}
          </header>
          <div className="grid items-start gap-3 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
            {d.text == null ? (
              <Panel>
                <p className="p-3 text-sm text-muted-foreground">{t('theses.page.waiting')}</p>
              </Panel>
            ) : (
              <ThesisEditor key={d.name} detail={d} onDirtyChange={setDirty} />
            )}
            <ReviewPanel detail={d} dirty={dirty} />
          </div>
          <Pages d={d} />
        </>
      )}
    </div>
  )
}
