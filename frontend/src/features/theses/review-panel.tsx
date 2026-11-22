import { Link } from '@tanstack/react-router'
import { Loader2, Play } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { RelativeTime } from '@/components/kit/relative-time'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Panel } from '@/components/ui/panel'
import { formatNum } from '@/lib/format'
import { useAgentDefs, useAgentRun } from '../agents/api'
import { RunStatusBadge } from '../agents/status'
import { errorText } from '../shared/states'
import { type ThesisDetail, useStartReview, useThesisReviews } from './api'
import { ReviewView } from './review-view'
import { VerdictBadge } from './theses-page'

const ACTIVE = ['starting', 'running', 'resuming']

/** Strategy-analyst review: latest output rendered, history of runs, "İncele" with budget confirm. */
export function ReviewPanel({ detail, dirty }: { detail: ThesisDetail; dirty: boolean }) {
  const { t, i18n } = useTranslation()
  const reviews = useThesisReviews(detail.name)
  const defs = useAgentDefs()
  const start = useStartReview(detail.name)
  const [confirm, setConfirm] = useState(false)
  const list = reviews.data ?? []
  const active = list.find((r) => ACTIVE.includes(r.status))
  const latestDone = list.find((r) => r.verdict)
  const latest = useAgentRun(latestDone?.run_id)
  const agent = defs.data?.find((a) => a.name === 'strategy-analyst')
  const hasErrors = (detail.error_count ?? 0) > 0
  const blocked = detail.active || hasErrors || dirty || !!active || detail.text == null
  const reason = hasErrors ? t('theses.reviewPanel.needFix') : dirty ? t('theses.reviewPanel.needSave') : undefined

  return (
    <div className="flex flex-col gap-3" data-testid="review-panel">
      <Panel
        title={t('theses.reviewPanel.title')}
        actions={
          <Button size="sm" disabled={blocked} title={reason} onClick={() => setConfirm(true)}>
            {active ? <Loader2 className="animate-spin" /> : <Play />} {active ? t('theses.reviewPanel.running') : t('theses.reviewPanel.run')}
          </Button>
        }
      >
        {reason && !active && <p className="px-3 pt-2 text-xs text-muted-foreground">{reason}</p>}
        {active && (
          <p className="flex items-center gap-2 px-3 pt-2 text-sm">
            <Loader2 className="size-4 animate-spin text-brand" />
            {t('theses.reviewPanel.running')} ·{' '}
            <Link to="/research/agents/runs/$runId" params={{ runId: active.run_id }} className="text-brand hover:underline">
              {t('theses.reviewPanel.live')}
            </Link>
          </p>
        )}
        {list.length === 0 ? (
          <p className="p-3 text-sm text-muted-foreground">{t('theses.reviewPanel.empty')}</p>
        ) : (
          <div className="p-3">
            <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('theses.reviewPanel.history')}</h3>
            <ul className="divide-y divide-border rounded-md border border-border" data-testid="review-history">
              {list.map((r) => (
                <li key={r.run_id} className="flex items-center gap-2 px-2 py-1.5 text-sm">
                  <RunStatusBadge status={r.status} />
                  <VerdictBadge verdict={r.verdict} />
                  {r.confidence != null && <span className="num text-xs text-muted-foreground">{formatNum(r.confidence, { decimals: 2, lang: i18n.language })}</span>}
                  <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                    {r.cost_usd != null && <span className="num">{t('theses.reviewPanel.cost', { usd: formatNum(r.cost_usd, { decimals: 3, lang: i18n.language }) })}</span>}
                    {r.started_at && <RelativeTime iso={r.started_at} />}
                    <Link to="/research/agents/runs/$runId" params={{ runId: r.run_id }} className="text-brand hover:underline">
                      {t('theses.reviewPanel.open')}
                    </Link>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Panel>
      {latest.data?.output && <ReviewView output={latest.data.output} />}

      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent>
          <div className="grid gap-1">
            <DialogTitle>{t('theses.reviewPanel.confirmTitle')}</DialogTitle>
            <DialogDescription>{t('theses.reviewPanel.confirmDesc')}</DialogDescription>
          </div>
          {agent && (
            <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm" data-testid="review-budget">
              {t('theses.reviewPanel.confirmBudget', { usd: formatNum(agent.max_budget_usd, { decimals: 2, lang: i18n.language }), turns: agent.max_turns })}
            </p>
          )}
          {start.isError && <p role="alert" className="text-sm text-error">{errorText(start.error)}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirm(false)}>
              {t('theses.reviewPanel.cancel')}
            </Button>
            <Button disabled={start.isPending} onClick={() => void start.mutateAsync().then(() => setConfirm(false))}>
              {t('theses.reviewPanel.confirm')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
