import { Link } from '@tanstack/react-router'
import { ArrowLeft, Check, ExternalLink, Loader2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Panel } from '@/components/ui/panel'
import { EmptyState } from '@/components/ui/empty-state'
import { RelativeTime } from '@/components/kit/relative-time'
import { formatDateTime, formatDuration, formatNum } from '@/lib/format'
import { cn } from '@/lib/utils'
import { isNotFound } from '../shared/api'
import { PanelError, SkeletonLines } from '../shared/states'
import { useDocumentTitle } from '../shared/use-document-title'
import { ReviewView } from '../theses/review-view'
import { type AgentRunDetail, type RunCall, useAgentRun } from './api'
import { RunAnswer } from './run-output'
import { RunStatusBadge } from './status'

function Bar({ value, max, tone = 'brand' }: { value: number; max: number | null; tone?: 'brand' | 'warning' }) {
  const pct = max && max > 0 ? Math.min(100, (value / max) * 100) : 0
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="presentation">
      <div className={cn('h-full rounded-full', tone === 'warning' ? 'bg-warning' : 'bg-brand')} style={{ width: `${pct}%` }} />
    </div>
  )
}

function Meter({ run }: { run: AgentRunDetail }) {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const u = run.usage
  const cost = run.cost_usd ?? 0
  const nearBudget = run.max_budget_usd != null && cost > run.max_budget_usd * 0.85
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-testid="run-meter">
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">{t('agents.meter.cost')}</span>
        <span className="num text-lg font-semibold">
          {run.cost_usd != null ? formatNum(run.cost_usd, { decimals: 4, lang }) : '–'}
          {run.max_budget_usd != null && <span className="text-sm font-normal text-muted-foreground"> / {formatNum(run.max_budget_usd, { decimals: 2, lang })} USD</span>}
        </span>
        <Bar value={cost} max={run.max_budget_usd} tone={nearBudget ? 'warning' : 'brand'} />
      </div>
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">{t('agents.meter.turns')}</span>
        <span className="num text-lg font-semibold">
          {run.turns ?? '–'}
          {run.max_turns != null && <span className="text-sm font-normal text-muted-foreground"> / {run.max_turns}</span>}
        </span>
        <Bar value={run.turns ?? 0} max={run.max_turns} />
      </div>
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">{t('agents.meter.calls')}</span>
        <span className="num text-lg font-semibold">{run.calls.length}</span>
      </div>
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">{t('agents.meter.tokens')}</span>
        <span className="num text-xs text-muted-foreground" data-testid="run-tokens">
          {t('agents.meter.cacheRead')} {formatNum(u.cache_read_input_tokens, { decimals: 0, lang })} · {t('agents.meter.cacheWrite')}{' '}
          {formatNum(u.cache_creation_input_tokens, { decimals: 0, lang })}
          <br />
          {t('agents.meter.input')} {formatNum(u.input_tokens, { decimals: 0, lang })} · {t('agents.meter.output')} {formatNum(u.output_tokens, { decimals: 0, lang })}
        </span>
      </div>
    </div>
  )
}

function Call({ c }: { c: RunCall }) {
  const { t, i18n } = useTranslation()
  const args = JSON.stringify(c.args)
  return (
    <li className="flex gap-2 border-b border-border px-3 py-2 last:border-b-0" data-testid="run-call">
      <span className={cn('mt-0.5 shrink-0', c.ok ? 'text-up' : 'text-down')} aria-label={c.ok ? 'ok' : t('agents.timeline.failed')}>
        {c.ok ? <Check className="size-3.5" /> : <X className="size-3.5" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-sm font-semibold">{c.tool}</span>
          <span className="num text-xs text-muted-foreground">
            {c.at ? formatDateTime(c.at, { lang: i18n.language }).slice(-8) : ''}
            {c.ms != null && ` · ${c.ms} ms`}
          </span>
        </div>
        {args !== '{}' && <div className="truncate font-mono text-xs text-muted-foreground" title={args}>{args}</div>}
        {c.result_excerpt && (
          <details className="mt-0.5">
            <summary className={cn('cursor-pointer truncate font-mono text-xs', c.ok ? 'text-muted-foreground' : 'text-down')}>{c.result_excerpt.slice(0, 120)}</summary>
            <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-md bg-muted/50 p-2 font-mono text-xs">{c.result_excerpt}</pre>
          </details>
        )}
        {c.blocking && <div className="text-xs text-down">{t('agents.timeline.blocking')}: {c.blocking}</div>}
      </div>
    </li>
  )
}

function Timeline({ run }: { run: AgentRunDetail }) {
  const { t } = useTranslation()
  return (
    <Panel title={`${t('agents.timeline.title')} (${run.calls.length})`}>
      {run.calls.length === 0 ? (
        <p className="p-3 text-sm text-muted-foreground">{run.active ? t('agents.timeline.starting') : t('agents.timeline.empty')}</p>
      ) : (
        <ol>
          {run.calls.map((c, i) => (
            <Call key={i} c={c} />
          ))}
        </ol>
      )}
      {run.active && run.calls.length > 0 && (
        <p className="flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" /> {t('agents.timeline.waiting')}
        </p>
      )}
    </Panel>
  )
}

function Notice({ tone, children }: { tone: 'warn' | 'error'; children: React.ReactNode }) {
  return (
    <p role="status" className={cn('rounded-md border px-3 py-2 text-sm', tone === 'error' ? 'border-down/40 bg-down/10 text-down' : 'border-warning/40 bg-warning/10 text-warning')}>
      {children}
    </p>
  )
}

function Subject({ run }: { run: AgentRunDetail }) {
  if (!run.subject) return null
  if (/^\d+$/.test(run.subject)) {
    return (
      <a href={`https://www.kap.org.tr/tr/Bildirim/${run.subject}`} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline">
        {run.subject} <ExternalLink className="size-3" />
      </a>
    )
  }
  if (run.agent === 'strategy-analyst') {
    return (
      <Link to="/research/theses/$name" params={{ name: run.subject }} className="text-brand hover:underline">
        {run.subject}
      </Link>
    )
  }
  return <span>{run.subject}</span>
}

export function RunPage({ runId }: { runId: string }) {
  const { t, i18n } = useTranslation()
  const q = useAgentRun(runId)
  const run = q.data
  useDocumentTitle(run ? `${run.agent} · ${run.subject ?? ''}` : t('agents.title'))

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-3 p-3 md:p-4" data-testid="run-page">
      <Link to="/research/agents" className="inline-flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-3.5" /> {t('agents.runPage.back')}
      </Link>
      {q.isLoading ? (
        <SkeletonLines rows={6} />
      ) : q.isError || !run ? (
        isNotFound(q.error) ? <EmptyState title={t('agents.runPage.notFound')} /> : <PanelError error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <>
          <Panel>
            <div className="flex flex-col gap-3 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-xl font-semibold tracking-tight">
                  <span className="font-mono">{run.agent}</span> · <Subject run={run} />
                </h1>
                <RunStatusBadge status={run.status} />
                {run.active && <Loader2 className="size-4 animate-spin text-brand" aria-label={t('agents.status.running')} />}
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>
                  {t('agents.runPage.started')}: {run.started_at ? <><RelativeTime iso={run.started_at} /> ({formatDateTime(run.started_at, { lang: i18n.language })})</> : '–'}
                </span>
                <span>{t('agents.runPage.duration')}: {run.elapsed_s != null ? formatDuration(run.elapsed_s) : '–'}</span>
                {run.agent_version && <span className="font-mono">{t('agents.runPage.version')} {run.agent_version}</span>}
              </div>
              <Meter run={run} />
              {run.status === 'resuming' && <Notice tone="warn">{t('agents.notice.resuming')} {run.gate}</Notice>}
              {run.status === 'incomplete' && <Notice tone="error">{t('agents.notice.incomplete')} {run.gate}</Notice>}
              {(run.status === 'failed' || run.status === 'deferred') && <Notice tone="error">{run.error ?? t('agents.notice.failed')}</Notice>}
              {run.status === 'stale' && <Notice tone="warn">{t('agents.notice.stale')}</Notice>}
            </div>
          </Panel>
          <RunAnswer agent={run.agent} output={run.output} text={run.text} review={(o) => <ReviewView output={o} />} />
          <Timeline run={run} />
        </>
      )}
    </div>
  )
}
