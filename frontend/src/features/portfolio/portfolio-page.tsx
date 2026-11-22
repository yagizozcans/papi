import type { ColumnDef } from '@tanstack/react-table'
import { useNavigate } from '@tanstack/react-router'
import { TriangleAlert } from 'lucide-react'
import { type ReactNode, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { DataTable } from '@/components/kit/data-table'
import { Delta } from '@/components/kit/delta'
import { Stat } from '@/components/kit/stat'
import { TickerChip } from '@/components/kit/ticker-chip'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Panel } from '@/components/ui/panel'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip } from '@/components/ui/tooltip'
import { useIsDesktop } from '@/hooks/use-media-query'
import { formatMoney, formatNum, formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'
import { errorText, PanelError, SkeletonLines } from '../shared/states'
import { useDocumentTitle } from '../shared/use-document-title'
import { type AllocationSlice, type Overview, type PerfRange, type Position, type Txn, useOverview, usePerformance } from './api'
import { PerformanceChart } from './performance-chart'

const DASH = '–'
const mark = (b: Position['badge']) => (b === 'HOLDING' ? 'holding' : b === 'WATCH' ? 'watch' : null)

/** Whole numbers without decimals, fractional quantities (funds) with up to 4. */
export function formatQty(q: number): string {
  return formatNum(q, { decimals: Number.isInteger(q) ? 0 : 4 }).replace(/([.,]\d*?)0+$/, '$1').replace(/[.,]$/, '')
}

export function PortfolioPage() {
  const { t } = useTranslation()
  useDocumentTitle(t('nav.portfolio'))
  const overview = useOverview()
  const [range, setRange] = useState<PerfRange>('3m')
  const perf = usePerformance(range)

  if (overview.isError && !overview.data) {
    return (
      <div className="mx-auto max-w-6xl p-4 md:p-6">
        <ErrorState description={errorText(overview.error)} onRetry={() => void overview.refetch()} />
      </div>
    )
  }
  const o = overview.data
  if (o && !o.available) {
    return (
      <div className="mx-auto max-w-6xl p-4 md:p-6">
        <EmptyState title={t('portfolio.unavailable')} />
      </div>
    )
  }

  const unvalued = [...new Set([...(o?.totals.unvalued ?? []), ...(perf.data?.unpriced ?? [])])]

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-3 p-3 md:p-4">
      <Totals o={o} />

      {unvalued.length > 0 && (
        <div role="status" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning" data-testid="unpriced-note">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" />
          <span>
            {t('portfolio.unpriced')} <span className="font-mono">{unvalued.join(', ')}</span>
          </span>
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Panel title={t('portfolio.performance.title')}>
          {perf.isError && !perf.data ? (
            <PanelError error={perf.error} onRetry={() => void perf.refetch()} />
          ) : (
            <PerformanceChart data={perf.data} range={range} onRangeChange={setRange} />
          )}
        </Panel>
        <Panel title={t('portfolio.allocation.title')}>
          {!o ? <SkeletonLines rows={6} /> : <Allocation o={o} />}
        </Panel>
      </div>

      <Panel title={t('portfolio.holdings.title')} actions={o && <span className="num text-xs text-muted-foreground">{o.positions.length}</span>}>
        <Holdings o={o} loading={overview.isPending} />
      </Panel>

      <div className="grid gap-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Panel title={t('portfolio.txns.title')}>
          <Transactions rows={o?.transactions} loading={overview.isPending} />
        </Panel>
        <div className="flex flex-col gap-3">
          <Panel title={t('portfolio.pending.title')}>
            {!o ? <SkeletonLines rows={2} /> : <Pending actions={o.pending_actions} />}
          </Panel>
          <Panel title={t('portfolio.watchlists.title')}>
            {!o ? <SkeletonLines rows={2} /> : <Watchlists lists={o.watchlists} />}
          </Panel>
        </div>
      </div>
    </div>
  )
}

function Totals({ o }: { o?: Overview }) {
  const { t } = useTranslation()
  const tt = o?.totals
  const loading = <Skeleton className="mt-1 h-5 w-24" />
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="portfolio-totals">
      <Stat label={t('portfolio.totals.value')} value={tt ? formatMoney(tt.value) : loading} />
      <Stat label={t('portfolio.totals.cost')} value={tt ? formatMoney(tt.cost) : loading} />
      <Stat
        label={t('portfolio.totals.pnl')}
        value={tt ? formatMoney(tt.pnl, { signed: true }) : loading}
        delta={tt ? (tt.pnl_pct ?? null) : undefined}
      />
      <Stat
        label={t('portfolio.totals.day')}
        value={tt ? formatMoney(tt.day_change, { signed: true }) : loading}
        delta={tt ? (tt.day_change_pct ?? null) : undefined}
      />
    </div>
  )
}

function WeightBar({ weight }: { weight: number | null }) {
  if (weight == null) return <span className="text-muted-foreground">{DASH}</span>
  return (
    <span className="flex items-center justify-end gap-2">
      <span aria-hidden="true" className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
        <span className="block h-full rounded-full bg-brand" style={{ width: `${Math.min(100, weight * 100)}%` }} />
      </span>
      <span className="w-12 text-right">{formatPercent(weight * 100, { decimals: 1 })}</span>
    </span>
  )
}

function Holdings({ o, loading }: { o?: Overview; loading: boolean }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const desktop = useIsDesktop()

  const columns = useMemo<ColumnDef<Position>[]>(
    () => [
      { id: 'ticker', header: t('portfolio.holdings.ticker'), accessorFn: (r) => r.ticker, size: 100, cell: (c) => <TickerChip ticker={c.row.original.ticker} mark={mark(c.row.original.badge)} /> },
      { id: 'name', header: t('portfolio.holdings.name'), accessorFn: (r) => r.name ?? '', size: 180, meta: { grow: true }, cell: (c) => <span className="text-muted-foreground">{c.row.original.name ?? DASH}</span> },
      { id: 'kind', header: t('portfolio.holdings.kind'), accessorFn: (r) => r.kind, size: 80, cell: (c) => <Badge variant="outline">{t(`portfolio.kind.${c.row.original.kind}`, { defaultValue: c.row.original.kind })}</Badge> },
      { id: 'qty', header: t('portfolio.holdings.qty'), accessorFn: (r) => r.quantity, size: 90, meta: { numeric: true }, cell: (c) => formatQty(c.row.original.quantity) },
      { id: 'avg', header: t('portfolio.holdings.avgCost'), accessorFn: (r) => r.avg_cost, size: 120, meta: { numeric: true }, cell: (c) => formatNum(c.row.original.avg_cost) },
      { id: 'last', header: t('portfolio.holdings.last'), accessorFn: (r) => r.last_close, size: 100, meta: { numeric: true }, cell: (c) => formatNum(c.row.original.last_close) },
      { id: 'value', header: t('portfolio.holdings.value'), accessorFn: (r) => r.market_value, size: 120, meta: { numeric: true }, cell: (c) => formatMoney(c.row.original.market_value) },
      { id: 'weight', header: t('portfolio.holdings.weight'), accessorFn: (r) => r.weight, size: 140, meta: { numeric: true }, cell: (c) => <WeightBar weight={c.row.original.weight} /> },
      { id: 'pnl', header: t('portfolio.holdings.pnl'), accessorFn: (r) => r.pnl, size: 120, meta: { numeric: true }, cell: (c) => <Delta value={c.row.original.pnl} arrow={false} /> },
      { id: 'pnlPct', header: t('portfolio.holdings.pnlPct'), accessorFn: (r) => r.pnl_pct, size: 110, meta: { numeric: true }, cell: (c) => <Delta value={c.row.original.pnl_pct} percent /> },
      { id: 'day', header: t('portfolio.holdings.day'), accessorFn: (r) => r.day_change_pct, size: 100, meta: { numeric: true }, cell: (c) => <Delta value={c.row.original.day_change_pct} percent /> },
    ],
    [t],
  )

  const rows = o?.positions ?? []
  const height = desktop ? (Math.min(Math.max(rows.length, 3), 14) + 1) * 32 + 2 : Math.min(Math.max(rows.length, 2), 6) * 76 + 2
  return (
    <div style={{ height }} data-testid="holdings">
      <DataTable
        data={rows}
        columns={columns}
        getRowId={(r) => r.instrument_id}
        loading={loading}
        initialSorting={[{ id: 'weight', desc: true }]}
        onActivate={(_, r) => void navigate({ to: '/i/$ticker', params: { ticker: r.ticker } })}
        empty={<EmptyState title={t('portfolio.holdings.empty')} />}
        ariaLabel={t('portfolio.holdings.title')}
        mobileRender={(r) => (
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <TickerChip ticker={r.ticker} mark={mark(r.badge)} />
              <span className="min-w-0 flex-1 truncate text-muted-foreground">{r.name}</span>
              <span className="num">{formatMoney(r.market_value)}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="num text-muted-foreground">
                {formatQty(r.quantity)} × {formatNum(r.last_close)}
              </span>
              <span className="flex items-center gap-3">
                <Delta value={r.pnl_pct} percent />
                <Delta value={r.day_change_pct} percent />
              </span>
            </div>
          </div>
        )}
      />
    </div>
  )
}

function Allocation({ o }: { o: Overview }) {
  const { t } = useTranslation()
  const [tab, setTab] = useState('by_instrument')
  const groups = { by_instrument: o.allocation.by_instrument, by_kind: o.allocation.by_kind, by_sector: o.allocation.by_sector }
  return (
    <Tabs value={tab} onValueChange={setTab} className="px-3 pb-3 pt-1">
      <TabsList className="mb-2">
        {(Object.keys(groups) as (keyof typeof groups)[]).map((k) => (
          <TabsTrigger key={k} value={k}>
            {t(`portfolio.allocation.${k}`)}
          </TabsTrigger>
        ))}
      </TabsList>
      {(Object.keys(groups) as (keyof typeof groups)[]).map((k) => (
        <TabsContent key={k} value={k}>
          <Bars slices={groups[k]} kind={k === 'by_kind'} />
        </TabsContent>
      ))}
    </Tabs>
  )
}

function Bars({ slices, kind }: { slices: AllocationSlice[]; kind: boolean }) {
  const { t } = useTranslation()
  if (slices.length === 0) return <EmptyState className="py-6" />
  const max = Math.max(...slices.map((s) => s.weight), 0.0001)
  return (
    <ul className="flex flex-col gap-1.5" data-testid="allocation-bars">
      {slices.map((s) => (
        <li key={s.key} className="grid grid-cols-[minmax(0,8rem)_1fr_3.5rem] items-center gap-2 text-sm">
          <span className="truncate" title={s.label}>{kind ? t(`portfolio.kind.${s.key}`, { defaultValue: s.label }) : s.label}</span>
          <span aria-hidden="true" className="h-2 overflow-hidden rounded-sm bg-muted">
            <span className="block h-full rounded-sm bg-brand" style={{ width: `${(s.weight / max) * 100}%` }} />
          </span>
          <span className="num text-right" title={formatMoney(s.value)}>{formatPercent(s.weight * 100, { decimals: 1 })}</span>
        </li>
      ))}
    </ul>
  )
}

function Transactions({ rows, loading }: { rows?: Txn[]; loading: boolean }) {
  const { t } = useTranslation()
  const desktop = useIsDesktop()
  const typeLabel = (x: Txn) => t(`portfolio.txns.types.${x.type}`, { defaultValue: x.type })
  const isBuy = (x: Txn) => x.type.toLowerCase().startsWith('buy')
  const isSell = (x: Txn) => x.type.toLowerCase().startsWith('sell')
  const columns = useMemo<ColumnDef<Txn>[]>(
    () => [
      { id: 'date', header: t('portfolio.txns.date'), accessorFn: (r) => r.trade_date, size: 110, meta: { cellClassName: 'num' }, cell: (c) => c.row.original.trade_date },
      { id: 'type', header: t('portfolio.txns.type'), accessorFn: (r) => r.type, size: 140, cell: (c) => <span className={cn(isBuy(c.row.original) && 'text-up', isSell(c.row.original) && 'text-down', c.row.original.reversed && 'line-through opacity-60')}>{typeLabel(c.row.original)}</span> },
      { id: 'ticker', header: t('portfolio.txns.ticker'), accessorFn: (r) => r.ticker ?? '', size: 100, cell: (c) => (c.row.original.ticker ? <TickerChip ticker={c.row.original.ticker} /> : DASH) },
      { id: 'qty', header: t('portfolio.holdings.qty'), accessorFn: (r) => r.quantity, size: 90, meta: { numeric: true }, cell: (c) => formatQty(c.row.original.quantity) },
      { id: 'price', header: t('portfolio.txns.price'), accessorFn: (r) => r.price, size: 100, meta: { numeric: true }, cell: (c) => formatNum(c.row.original.price) },
      { id: 'fees', header: t('portfolio.txns.fees'), accessorFn: (r) => r.fees + r.tax, size: 80, meta: { numeric: true }, cell: (c) => formatNum(c.row.original.fees + c.row.original.tax) },
      { id: 'source', header: t('portfolio.txns.source'), accessorFn: (r) => r.source, size: 90, meta: { grow: true }, cell: (c) => <span className="text-muted-foreground">{c.row.original.source}</span> },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t],
  )
  const n = rows?.length ?? 0
  const height = desktop ? (Math.min(Math.max(n, 2), 10) + 1) * 32 + 2 : Math.min(Math.max(n, 2), 5) * 76 + 2
  return (
    <div style={{ height }}>
      <DataTable
        data={rows ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={loading}
        compact={false}
        empty={<EmptyState title={t('portfolio.txns.empty')} className="py-6" />}
        ariaLabel={t('portfolio.txns.title')}
        mobileRender={(r) => (
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              {r.ticker && <TickerChip ticker={r.ticker} />}
              <span className={cn(isBuy(r) && 'text-up', isSell(r) && 'text-down', r.reversed && 'line-through opacity-60')}>{typeLabel(r)}</span>
              <span className="num ml-auto text-muted-foreground">{r.trade_date}</span>
            </div>
            <div className="num text-sm text-muted-foreground">
              {formatQty(r.quantity)} × {formatNum(r.price)}
            </div>
          </div>
        )}
      />
    </div>
  )
}

function P2Button({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  return (
    <Tooltip content={t('portfolio.pending.p2')}>
      <span tabIndex={0} className="inline-flex">
        <Button size="sm" variant="outline" disabled>
          {children}
        </Button>
      </span>
    </Tooltip>
  )
}

function Pending({ actions }: { actions: Overview['pending_actions'] }) {
  const { t } = useTranslation()
  if (actions.length === 0) return <EmptyState title={t('portfolio.pending.empty')} className="py-6" />
  return (
    <ul>
      {actions.map((a) => (
        <li key={a.id} className="flex flex-col gap-1.5 border-b border-border px-3 py-2 last:border-b-0" data-testid="pending-action">
          <div className="flex items-center gap-2">
            {a.ticker && <TickerChip ticker={a.ticker} />}
            <span className="text-sm">{a.kind}</span>
            {a.ex_date && <span className="num ml-auto text-xs text-muted-foreground">{a.ex_date}</span>}
          </div>
          <div className="flex gap-2">
            <P2Button>{t('portfolio.pending.apply')}</P2Button>
            <P2Button>{t('portfolio.pending.dismiss')}</P2Button>
          </div>
        </li>
      ))}
    </ul>
  )
}

function Watchlists({ lists }: { lists: Overview['watchlists'] }) {
  const { t } = useTranslation()
  if (lists.length === 0) return <EmptyState title={t('portfolio.watchlists.empty')} className="py-6" />
  return (
    <ul>
      {lists.map((w) => (
        <li key={w.id} className="flex flex-col gap-1.5 border-b border-border px-3 py-2 last:border-b-0" data-testid="watchlist">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">{w.name}</span>
            <span className="num text-xs text-muted-foreground">{w.members.length}</span>
          </div>
          {w.members.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {w.members.map((m) => (
                <TickerChip key={m.instrument_id} ticker={m.ticker} mark={mark(m.badge)} />
              ))}
            </div>
          )}
          {(w.sectors.length > 0 || w.indices.length > 0) && (
            <div className="flex flex-wrap gap-1">
              {[...w.indices, ...w.sectors].map((x) => (
                <Badge key={x} variant="outline">{x}</Badge>
              ))}
            </div>
          )}
        </li>
      ))}
    </ul>
  )
}
