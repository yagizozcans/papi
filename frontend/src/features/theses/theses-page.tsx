import { Link, useNavigate } from '@tanstack/react-router'
import { FilePlus2 } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { RelativeTime } from '@/components/kit/relative-time'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { EmptyState } from '@/components/ui/empty-state'
import { Panel } from '@/components/ui/panel'
import { RunStatusBadge } from '../agents/status'
import { errorText, PanelError, SkeletonLines } from '../shared/states'
import { useDocumentTitle } from '../shared/use-document-title'
import { type ThesisRow, useNewThesis, useTheses } from './api'
import { UploadPanel } from './upload-panel'

const TICKER = /^[A-Za-z0-9]{3,6}$/

export function VerdictBadge({ verdict }: { verdict: string | null | undefined }) {
  const { t } = useTranslation()
  if (!verdict) return null
  const variant = verdict === 'supported' ? 'up' : verdict === 'refuted' || verdict === 'weak' ? 'down' : 'outline'
  return <Badge variant={variant}>{t(`theses.verdict.${verdict}`, { defaultValue: verdict })}</Badge>
}

export function ThesisStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation()
  const variant = status === 'failed' || status === 'needs_fix' ? 'down' : status === 'stale' ? 'warning' : ['starting', 'extracting', 'analysing'].includes(status) ? 'brand' : status === 'manual' ? 'outline' : 'up'
  return <Badge variant={variant}>{t(`theses.status.${status}`, { defaultValue: status })}</Badge>
}

function CheckCell({ row }: { row: ThesisRow }) {
  const { t } = useTranslation()
  if (row.error_count == null) return <span className="text-muted-foreground">{t('theses.check.none')}</span>
  if (row.error_count > 0) return <span className="text-down">{t('theses.check.errors', { count: row.error_count })}</span>
  if ((row.gap_count ?? 0) > 0) return <span>{t('theses.check.gaps', { count: row.gap_count })}</span>
  return <span className="text-up">{t('theses.check.ok')}</span>
}

function NewThesisDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const create = useNewThesis()
  const [ticker, setTicker] = useState('')
  const valid = TICKER.test(ticker.trim())
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <div className="grid gap-1">
          <DialogTitle>{t('theses.newDialog.title')}</DialogTitle>
          <DialogDescription>{t('theses.newDialog.desc')}</DialogDescription>
        </div>
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            if (!valid) return
            void create.mutateAsync(ticker.trim().toUpperCase()).then((d) => {
              onOpenChange(false)
              void navigate({ to: '/research/theses/$name', params: { name: d.name } })
            })
          }}
        >
          <label className="grid gap-1 text-sm">
            <span className="text-muted-foreground">{t('theses.newDialog.ticker')}</span>
            <input className="h-8 rounded-md border border-border bg-background px-2 font-mono uppercase" value={ticker} maxLength={6} placeholder={t('theses.newDialog.tickerPlaceholder')} onChange={(e) => setTicker(e.target.value)} autoFocus />
          </label>
          {ticker !== '' && !valid && <span className="text-xs text-error">{t('theses.newDialog.invalid')}</span>}
          {create.isError && <p role="alert" className="text-sm text-error">{errorText(create.error)}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              {t('theses.newDialog.cancel')}
            </Button>
            <Button type="submit" disabled={!valid || create.isPending}>
              {t('theses.newDialog.create')}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function ThesesPage() {
  const { t } = useTranslation()
  useDocumentTitle(`${t('nav.research')} · ${t('theses.title')}`)
  const q = useTheses()
  const [creating, setCreating] = useState(false)
  const items = q.data?.items ?? []

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-3 p-3 md:p-4" data-testid="theses-page">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">{t('theses.title')}</h1>
        <Button onClick={() => setCreating(true)}>
          <FilePlus2 /> {t('theses.new')}
        </Button>
      </div>
      <UploadPanel accept={q.data?.accept ?? ['.pdf', '.jpg', '.jpeg', '.png', '.heic', '.docx', '.txt']} />
      <Panel>
        {q.isLoading ? (
          <SkeletonLines rows={5} />
        ) : q.isError && !q.data ? (
          <PanelError error={q.error} onRetry={() => void q.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState description={t('theses.empty')} className="py-8" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="theses-table">
              <thead className="text-xs text-muted-foreground">
                <tr className="h-8 border-b border-border text-left">
                  <th className="px-3 font-medium">{t('theses.cols.name')}</th>
                  <th className="px-3 font-medium">{t('theses.cols.direction')}</th>
                  <th className="px-3 font-medium">{t('theses.cols.status')}</th>
                  <th className="px-3 font-medium">{t('theses.cols.check')}</th>
                  <th className="px-3 font-medium">{t('theses.cols.review')}</th>
                  <th className="px-3 text-right font-medium">{t('theses.cols.runs')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <tr key={r.name} className="h-row border-b border-border last:border-b-0 hover:bg-accent/50">
                    <td className="px-3">
                      <Link to="/research/theses/$name" params={{ name: r.name }} className="font-medium text-brand hover:underline">
                        {r.name}
                      </Link>
                    </td>
                    <td className="px-3 text-muted-foreground">{[r.direction, r.horizon].filter(Boolean).join(' · ')}</td>
                    <td className="px-3">
                      <ThesisStatusBadge status={r.status} />
                    </td>
                    <td className="px-3">
                      <CheckCell row={r} />
                    </td>
                    <td className="px-3">
                      {r.last_review ? (
                        <Link to="/research/agents/runs/$runId" params={{ runId: r.last_review.run_id }} className="inline-flex items-center gap-2 hover:underline">
                          {r.last_review.verdict ? <VerdictBadge verdict={r.last_review.verdict} /> : <RunStatusBadge status={r.last_review.status} />}
                          {r.last_review.started_at && <RelativeTime iso={r.last_review.started_at} className="text-xs text-muted-foreground" />}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">–</span>
                      )}
                    </td>
                    <td className="num px-3 text-right">{r.reviews}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      <NewThesisDialog open={creating} onOpenChange={setCreating} />
    </div>
  )
}
