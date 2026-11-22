import { Link, useNavigate } from '@tanstack/react-router'
import { Bot } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { RelativeTime } from '@/components/kit/relative-time'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { Panel } from '@/components/ui/panel'
import { formatDuration, formatNum } from '@/lib/format'
import { PanelError, SkeletonLines } from '../shared/states'
import { useDocumentTitle } from '../shared/use-document-title'
import { type AgentDef, type AgentRunRow, RUNS_PAGE, type RunFilters, useAgentDefs, useAgentRuns } from './api'
import { RunAgentDialog } from './run-dialog'
import { RunStatusBadge } from './status'

const STATUSES = ['active', 'ok', 'failed', 'incomplete', 'deferred', 'stale'] as const

function AgentCard({ a, onRun }: { a: AgentDef; onRun: (name: string) => void }) {
  const { t, i18n } = useTranslation()
  return (
    <article className="flex flex-col gap-2 rounded-md border border-border bg-card p-3" data-testid="agent-card">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="font-mono text-sm font-semibold">{a.name}</h3>
          <p className="mt-0.5 text-sm text-muted-foreground">{a.description}</p>
        </div>
        <Button size="sm" variant="outline" onClick={() => onRun(a.name)} aria-label={`${t('agents.run')}: ${a.name}`}>
          <Bot /> {t('agents.run')}
        </Button>
      </header>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs sm:grid-cols-4">
        <div>
          <dt className="text-muted-foreground">{t('agents.model')}</dt>
          <dd className="font-mono">{a.model}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t('agents.budget')}</dt>
          <dd className="num">{t('agents.budgetValue', { usd: formatNum(a.max_budget_usd, { decimals: 2, lang: i18n.language }) })}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t('agents.turns')}</dt>
          <dd className="num">{t('agents.turnsValue', { n: a.max_turns })}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t('agents.timeout')}</dt>
          <dd className="num">{formatDuration(a.timeout_s)}</dd>
        </div>
      </dl>
      <div className="text-xs">
        <span className="text-muted-foreground">{t('agents.skills')}: </span>
        {a.skills.map((s) => (
          <span key={s.name} className="mr-2 font-mono" title={s.description}>
            {s.name}
          </span>
        ))}
        <span className="font-mono text-muted-foreground">
          {t('agents.version')} {a.version}
        </span>
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-muted-foreground">
          {t('agents.tools')} ({a.tools.length + a.builtin_tools.length})
        </summary>
        <div className="mt-1 flex flex-wrap gap-1">
          {a.tools.length + a.builtin_tools.length === 0 && <span className="text-muted-foreground">{t('agents.noTools')}</span>}
          {a.tools.map((x) => (
            <Badge key={x} variant="outline" className="font-mono">
              {x}
            </Badge>
          ))}
          {a.builtin_tools.map((x) => (
            <Badge key={x} variant="brand" className="font-mono" title={t('agents.builtin')}>
              {x}
            </Badge>
          ))}
        </div>
      </details>
    </article>
  )
}

function RunRows({ rows }: { rows: AgentRunRow[] }) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  return (
    <table className="w-full text-sm" data-testid="runs-table">
      <thead className="text-xs text-muted-foreground">
        <tr className="h-8 border-b border-border text-left">
          <th className="px-3 font-medium">{t('agents.cols.started')}</th>
          <th className="px-3 font-medium">{t('agents.cols.agent')}</th>
          <th className="px-3 font-medium">{t('agents.cols.subject')}</th>
          <th className="px-3 font-medium">{t('agents.cols.status')}</th>
          <th className="px-3 text-right font-medium">{t('agents.cols.turns')}</th>
          <th className="px-3 text-right font-medium">{t('agents.cols.cost')}</th>
          <th className="px-3 text-right font-medium">{t('agents.cols.duration')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr
            key={r.run_id}
            className="h-row cursor-pointer border-b border-border last:border-b-0 hover:bg-accent/50"
            onClick={() => void navigate({ to: '/research/agents/runs/$runId', params: { runId: r.run_id } })}
          >
            <td className="px-3 whitespace-nowrap text-muted-foreground">{r.started_at ? <RelativeTime iso={r.started_at} /> : '–'}</td>
            <td className="px-3 font-mono text-xs">{r.agent}</td>
            <td className="px-3">
              <Link
                to="/research/agents/runs/$runId"
                params={{ runId: r.run_id }}
                className="text-brand hover:underline"
                onClick={(e) => e.stopPropagation()}
              >
                {r.subject ?? r.run_id.slice(0, 8)}
              </Link>
              {r.verdict && (
                <Badge variant="outline" className="ml-2">
                  {t(`theses.verdict.${r.verdict}`, { defaultValue: r.verdict })}
                </Badge>
              )}
            </td>
            <td className="px-3">
              <RunStatusBadge status={r.status} />
            </td>
            <td className="num px-3 text-right">{r.turns ?? '–'}</td>
            <td className="num px-3 text-right">{r.cost_usd != null ? formatNum(r.cost_usd, { decimals: 4, lang: i18n.language }) : '–'}</td>
            <td className="num px-3 text-right">{r.elapsed_s != null ? formatDuration(r.elapsed_s) : '–'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function AgentsPage() {
  const { t } = useTranslation()
  useDocumentTitle(`${t('nav.research')} · ${t('agents.title')}`)
  const defs = useAgentDefs()
  const [filters, setFilters] = useState<RunFilters>({})
  const [offset, setOffset] = useState(0)
  const runs = useAgentRuns(filters, offset)
  const [dialog, setDialog] = useState<{ open: boolean; agent?: string }>({ open: false })
  const filtered = !!(filters.agent || filters.status || filters.ref)
  const set = (patch: RunFilters) => {
    setFilters((f) => ({ ...f, ...patch }))
    setOffset(0)
  }
  const total = runs.data?.total ?? 0

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-3 p-3 md:p-4" data-testid="agents-page">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">{t('agents.title')}</h1>
        <Button onClick={() => setDialog({ open: true })}>
          <Bot /> {t('agents.run')}
        </Button>
      </div>

      <section aria-label={t('agents.catalogue')} className="grid gap-3 md:grid-cols-2">
        {defs.isLoading ? <SkeletonLines rows={4} /> : defs.isError ? <PanelError error={defs.error} onRetry={() => void defs.refetch()} /> : defs.data?.map((a) => <AgentCard key={a.name} a={a} onRun={(agent) => setDialog({ open: true, agent })} />)}
      </section>

      <Panel
        title={t('agents.runs')}
        actions={
          <div className="flex flex-wrap items-center gap-1">
            <select className="h-6 rounded-md border border-border bg-background px-1 text-xs" aria-label={t('agents.filters.agent')} value={filters.agent ?? ''} onChange={(e) => set({ agent: e.target.value || undefined })}>
              <option value="">{t('agents.filters.allAgents')}</option>
              {defs.data?.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
            <select className="h-6 rounded-md border border-border bg-background px-1 text-xs" aria-label={t('agents.filters.status')} value={filters.status ?? ''} onChange={(e) => set({ status: e.target.value || undefined })}>
              <option value="">{t('agents.filters.allStatuses')}</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`agents.status.${s}`)}
                </option>
              ))}
            </select>
            <input
              className="h-6 w-60 rounded-md border border-border bg-background px-1.5 font-mono text-xs"
              aria-label={t('agents.filters.ref')}
              placeholder={t('agents.filters.refPlaceholder')}
              value={filters.ref ?? ''}
              onChange={(e) => set({ ref: e.target.value.trim() || undefined })}
            />
          </div>
        }
      >
        {runs.isLoading ? (
          <SkeletonLines rows={6} />
        ) : runs.isError && !runs.data ? (
          <PanelError error={runs.error} onRetry={() => void runs.refetch()} />
        ) : runs.data && runs.data.items.length > 0 ? (
          <div className="overflow-x-auto">
            <RunRows rows={runs.data.items} />
          </div>
        ) : (
          <EmptyState description={filtered ? t('agents.empty') : t('agents.emptyAll')} className="py-8" />
        )}
        {total > RUNS_PAGE && (
          <div className="flex items-center justify-end gap-2 border-t border-border px-3 py-1.5 text-xs text-muted-foreground">
            <span className="num">{t('agents.page', { from: offset + 1, to: Math.min(offset + RUNS_PAGE, total), total })}</span>
            <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - RUNS_PAGE))}>
              {t('agents.prev')}
            </Button>
            <Button size="sm" variant="ghost" disabled={offset + RUNS_PAGE >= total} onClick={() => setOffset(offset + RUNS_PAGE)}>
              {t('agents.next')}
            </Button>
          </div>
        )}
      </Panel>

      <RunAgentDialog key={dialog.agent ?? 'none'} open={dialog.open} onOpenChange={(open) => setDialog((d) => ({ ...d, open }))} agent={dialog.agent} />
    </div>
  )
}
