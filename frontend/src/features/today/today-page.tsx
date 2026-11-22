import { Link } from '@tanstack/react-router'
import { Check, CheckCheck, Cpu, ListChecks } from 'lucide-react'
import { type ReactNode, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Panel } from '@/components/ui/panel'
import { Skeleton } from '@/components/ui/skeleton'
import { NewItemsPill } from '@/components/kit/new-items-pill'
import { PriorityBadge } from '@/components/kit/priority-badge'
import { RelativeTime } from '@/components/kit/relative-time'
import { Stat } from '@/components/kit/stat'
import { TickerChip } from '@/components/kit/ticker-chip'
import { useHeldItems } from '@/hooks/use-held-items'
import { formatMoney, formatNum } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { AgentRunBrief, FeedItem } from '../shared/api'
import { DisclosureLink } from '../shared/disclosure-link'
import { toPriority } from '../shared/priority'
import { errorText, SkeletonLines } from '../shared/states'
import { useDocumentTitle } from '../shared/use-document-title'
import { type TodayOut, useMonthPerformance, useToday } from './api'
import { useSeenAttention } from './seen'

const DASH = '–'

export function TodayPage() {
  const { t } = useTranslation()
  useDocumentTitle(t('nav.today'))
  const today = useToday()
  const perf = useMonthPerformance()

  if (today.isError && !today.data) {
    return (
      <div className="mx-auto max-w-6xl p-4 md:p-6">
        <ErrorState description={errorText(today.error)} onRetry={() => void today.refetch()} />
      </div>
    )
  }
  const d = today.data
  const loading = !d

  return (
    <div className="mx-auto grid max-w-6xl grid-cols-[minmax(0,1fr)] gap-3 p-3 md:p-4 lg:grid-cols-2 lg:grid-rows-[auto_auto_1fr]">
      <div className="order-2 lg:order-none lg:col-span-2 lg:row-start-1">
        <MarketStrip
          data={d}
          benchmark={perf.data?.benchmark.map((b) => b.value)}
          returns={perf.data?.points.map((p) => p.return_index)}
        />
      </div>
      <div className="order-1 lg:order-none lg:col-start-1 lg:row-start-2">
        <AttentionPanel data={d} loading={loading} />
      </div>
      <div className="order-3 lg:order-none lg:col-start-2 lg:row-span-2 lg:row-start-2">
        <TapePanel data={d} loading={loading} />
      </div>
      <div className="order-4 lg:order-none lg:col-start-1 lg:row-start-3">
        <AgentRunsPanel runs={d?.agent_runs} loading={loading} />
      </div>
    </div>
  )
}

function MarketStrip({
  data,
  benchmark,
  returns,
}: {
  data?: TodayOut
  benchmark?: number[]
  returns?: number[]
}) {
  const { t } = useTranslation()
  const loadingValue = <Skeleton className="mt-1 h-5 w-24" />
  const p = data?.portfolio
  const hasPortfolio = !!p?.available && p.value != null
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="market-strip">
      <Stat
        label={data?.xu100?.label ?? 'XU100'}
        value={data ? (data.xu100 ? formatNum(data.xu100.last) : DASH) : loadingValue}
        delta={data ? (data.xu100?.change_pct ?? null) : undefined}
        sparkline={benchmark}
      />
      <Stat
        label={data?.usdtry?.label ?? 'USDTRY'}
        value={data ? (data.usdtry ? formatNum(data.usdtry.last, { decimals: 4 }) : DASH) : loadingValue}
        delta={data ? (data.usdtry?.change_pct ?? null) : undefined}
      />
      <Stat
        label={t('today.portfolioValue')}
        value={data ? (hasPortfolio ? formatMoney(p.value, { decimals: 0 }) : DASH) : loadingValue}
        hint={hasPortfolio ? t('today.positions', { count: p.positions }) : undefined}
        sparkline={returns}
      />
      <Stat
        label={t('today.dayPnl')}
        value={data ? (hasPortfolio ? formatMoney(p.day_change, { decimals: 0, signed: true }) : DASH) : loadingValue}
        delta={data ? (hasPortfolio ? (p.day_change_pct ?? null) : null) : undefined}
      />
    </div>
  )
}

function AttentionPanel({ data, loading }: { data?: TodayOut; loading: boolean }) {
  const { t } = useTranslation()
  const { seen, markSeen } = useSeenAttention()

  const events = useMemo(() => {
    const items = data?.attention.items ?? []
    const rank = (i: FeedItem) => (seen.has(i.id) ? 1 : 0)
    return [...items].sort((a, b) => rank(a) - rank(b))
  }, [data, seen])
  const unseen = events.filter((e) => !seen.has(e.id))

  const deadLanes = data?.jobs.lanes.filter((l) => l.dead > 0) ?? []
  const pending = data?.pending_actions
  const waiting = data?.jobs.analyses_waiting ?? 0
  const nothing = !loading && events.length === 0 && !pending?.count && deadLanes.length === 0 && waiting === 0

  return (
    <Panel
      title={
        <span className="flex items-center gap-2">
          {t('today.attention.title')}
          {unseen.length > 0 && (
            <Badge variant="brand" data-testid="attention-unseen">
              {unseen.length}
            </Badge>
          )}
        </span>
      }
      actions={
        unseen.length > 0 && (
          <Button variant="ghost" size="sm" onClick={() => markSeen(unseen.map((e) => e.id))}>
            <CheckCheck />
            {t('today.attention.markAll')}
          </Button>
        )
      }
    >
      {loading ? (
        <SkeletonLines rows={5} />
      ) : nothing ? (
        <EmptyState icon={<ListChecks className="size-6" strokeWidth={1.5} />} title={t('today.attention.clear')} className="py-8" />
      ) : (
        <div className="flex flex-col">
          {events.length > 0 && (
            <ul aria-label={t('today.attention.events')} className="max-h-80 overflow-y-auto">
              {events.map((e) => {
                const isSeen = seen.has(e.id)
                return (
                  <li
                    key={e.id}
                    data-seen={isSeen || undefined}
                    className={cn('flex items-start gap-2 border-b border-border px-3 py-2 last:border-b-0', isSeen && 'opacity-55')}
                  >
                    {toPriority(e.priority) && <PriorityBadge priority={toPriority(e.priority)!} className="mt-0.5" />}
                    <div className="min-w-0 flex-1">
                      <DisclosureLink eventId={e.id} className="line-clamp-2 break-words hover:text-brand">
                        {e.headline}
                      </DisclosureLink>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                        {e.tickers.map((tk) => (
                          <TickerChip key={tk.ticker} ticker={tk.ticker} mark={tk.badge === 'HOLDING' ? 'holding' : tk.badge === 'WATCH' ? 'watch' : null} />
                        ))}
                        <span>{e.code_label}</span>
                        <RelativeTime iso={e.published_at ?? e.announced_at} />
                      </div>
                    </div>
                    {!isSeen && (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-6"
                        aria-label={t('today.attention.markSeen')}
                        title={t('today.attention.markSeen')}
                        onClick={() => markSeen([e.id])}
                      >
                        <Check className="size-3.5" />
                      </Button>
                    )}
                  </li>
                )
              })}
            </ul>
          )}

          {!!pending?.count && (
            <AttentionGroup label={t('today.attention.pending', { count: pending.count })} to="/portfolio">
              {pending.items.map((a) => (
                <div key={a.id} className="flex flex-wrap items-center gap-2 text-sm">
                  {a.ticker && <TickerChip ticker={a.ticker} />}
                  <span>{a.kind}</span>
                  {a.ex_date && <span className="num text-muted-foreground">{a.ex_date}</span>}
                </div>
              ))}
            </AttentionGroup>
          )}

          {deadLanes.length > 0 && (
            <AttentionGroup label={t('today.attention.deadJobs', { count: data!.jobs.dead_total })} to="/system" tone="bad">
              {deadLanes.map((l) => (
                <div key={l.lane} className="flex items-baseline gap-2 text-sm" data-testid="dead-lane">
                  <span className="w-16 shrink-0 font-mono text-xs">{l.lane}</span>
                  <Badge variant="down" className="num">
                    {l.dead}
                  </Badge>
                  {l.last_error && (
                    <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={l.last_error}>
                      {l.last_error}
                    </span>
                  )}
                </div>
              ))}
            </AttentionGroup>
          )}

          {waiting > 0 && (
            <AttentionGroup label={t('today.attention.analysesWaiting', { count: waiting })} to="/system" />
          )}
        </div>
      )}
    </Panel>
  )
}

function AttentionGroup({
  label,
  to,
  tone,
  children,
}: {
  label: string
  to: '/portfolio' | '/system'
  tone?: 'bad'
  children?: ReactNode
}) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col gap-1.5 border-b border-border px-3 py-2 last:border-b-0">
      <div className="flex items-center justify-between gap-2">
        <span className={cn('text-sm font-medium', tone === 'bad' && 'text-error')}>{label}</span>
        <Link to={to} className="text-xs text-muted-foreground hover:text-brand">
          {t('today.attention.open')}
        </Link>
      </div>
      {children}
    </div>
  )
}

function TapePanel({ data, loading }: { data?: TodayOut; loading: boolean }) {
  const { t } = useTranslation()
  const items = data?.feed ?? []
  const held = useHeldItems(items, (i) => i.id, !loading)
  return (
    <Panel
      title={t('today.tape.title')}
      actions={<Link to="/feed" className="text-xs text-muted-foreground hover:text-brand">{t('today.tape.all')}</Link>}
      className="h-full"
      bodyClassName="relative"
    >
      {held.pending > 0 && (
        <div className="absolute inset-x-0 top-2 z-10 flex justify-center">
          <NewItemsPill count={held.pending} onClick={held.reveal} />
        </div>
      )}
      {loading ? (
        <SkeletonLines rows={8} />
      ) : held.visible.length === 0 ? (
        <EmptyState description={t('today.tape.empty')} />
      ) : (
        <ul className="max-h-[32rem] overflow-y-auto lg:max-h-none" data-testid="tape">
          {held.visible.map((i) => {
            const p = toPriority(i.priority)
            return (
              <li key={i.id} className="flex items-start gap-2 border-b border-border px-3 py-1.5 last:border-b-0">
                {p ? <PriorityBadge priority={p} className="mt-0.5" /> : <span className="w-[26px]" />}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    {i.tickers.slice(0, 3).map((tk) => (
                      <TickerChip key={tk.ticker} ticker={tk.ticker} mark={tk.badge === 'HOLDING' ? 'holding' : tk.badge === 'WATCH' ? 'watch' : null} />
                    ))}
                    <RelativeTime iso={i.published_at ?? i.announced_at} className="ml-auto text-xs text-muted-foreground" />
                  </div>
                  <DisclosureLink eventId={i.id} className="mt-0.5 line-clamp-2 break-words text-sm hover:text-brand">
                    {i.headline}
                  </DisclosureLink>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}

const RUN_VARIANT: Record<string, 'up' | 'down' | 'brand' | 'outline'> = {
  ok: 'up',
  completed: 'up',
  succeeded: 'up',
  done: 'up',
  failed: 'down',
  error: 'down',
  running: 'brand',
}

export function AgentRunRow({ run }: { run: AgentRunBrief }) {
  const { t } = useTranslation()
  return (
    <li className="flex items-center gap-2 border-b border-border px-3 py-2 last:border-b-0" data-testid="agent-run">
      <Cpu className="size-3.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs">{run.agent}</span>
          {run.subject && <span className="truncate text-sm text-muted-foreground">{run.subject}</span>}
        </div>
        {run.error && <div className="truncate text-xs text-error" title={run.error}>{run.error}</div>}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-0.5 text-xs text-muted-foreground">
        <Badge variant={RUN_VARIANT[run.status] ?? 'outline'}>{run.status}</Badge>
        <span className="num">
          {run.started_at && <RelativeTime iso={run.started_at} />}
          {run.cost_usd != null && ` · ${t('today.runs.cost', { value: formatNum(run.cost_usd, { decimals: 3 }) })}`}
        </span>
      </div>
    </li>
  )
}

function AgentRunsPanel({ runs, loading }: { runs?: AgentRunBrief[]; loading: boolean }) {
  const { t } = useTranslation()
  return (
    <Panel title={t('today.runs.title')}>
      {loading ? (
        <SkeletonLines rows={3} />
      ) : !runs || runs.length === 0 ? (
        <EmptyState description={t('today.runs.empty')} className="py-6" />
      ) : (
        <ul>
          {runs.map((r) => (
            <AgentRunRow key={r.run_id} run={r} />
          ))}
        </ul>
      )}
    </Panel>
  )
}

