import { ExternalLink } from 'lucide-react'
import { type ReactNode, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { type ChartEvent, type ChartTransaction, PriceChart } from '@/components/kit/price-chart'
import { Delta } from '@/components/kit/delta'
import { type Priority, PriorityBadge } from '@/components/kit/priority-badge'
import { RelativeTime } from '@/components/kit/relative-time'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Panel } from '@/components/ui/panel'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { formatMoney, formatNum, formatPercent } from '@/lib/format'
import { DisclosureLink } from '../shared/disclosure-link'
import { toPriority } from '../shared/priority'
import { isNotFound } from '../shared/api'
import { errorText, PanelError, SkeletonLines } from '../shared/states'
import { useDocumentTitle } from '../shared/use-document-title'
import { AgentRunRow } from '../today/today-page'
import { formatQty } from '../portfolio/portfolio-page'
import { type InstrumentOut, useInstrument, usePrices } from './api'

const DASH = '–'

export function InstrumentPage({ ticker: raw }: { ticker: string }) {
  const { t } = useTranslation()
  const ticker = raw.toUpperCase()
  const q = useInstrument(ticker)
  useDocumentTitle(q.data ? `${q.data.ticker}${q.data.name ? ` · ${q.data.name}` : ''}` : ticker)

  if (q.isError && !q.data) {
    return (
      <div className="mx-auto max-w-6xl p-4 md:p-6">
        {isNotFound(q.error) ? (
          <EmptyState title={t('instrument.notFound', { ticker })} description={t('instrument.notFoundHint')} />
        ) : (
          <ErrorState description={errorText(q.error)} onRetry={() => void q.refetch()} />
        )}
      </div>
    )
  }
  const d = q.data

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-3 p-3 md:p-4">
      <Header d={d} ticker={ticker} />
      <Panel>
        <Chart d={d} ticker={ticker} />
      </Panel>
      {d ? <InstrumentTabs d={d} /> : <SkeletonLines rows={6} className="flex flex-col gap-2 rounded-md border border-border bg-card p-3" />}
    </div>
  )
}

function Header({ d, ticker }: { d?: InstrumentOut; ticker: string }) {
  const { t } = useTranslation()
  const lc = d?.last_close
  const kapUrl = d?.disclosures.find((x) => x.kap_url)?.kap_url
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2" data-testid="instrument-header">
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="font-mono text-2xl font-semibold">{ticker}</span>
        {d ? (
          <span className="truncate text-muted-foreground">{d.name ?? d.issuer?.legal_name}</span>
        ) : (
          <Skeleton className="h-4 w-40" />
        )}
      </div>
      {d && (
        <>
          <Badge variant="outline">{t(`portfolio.kind.${d.kind}`, { defaultValue: d.kind })}</Badge>
          {d.badge === 'HOLDING' && <Badge variant="brand">{t('instrument.holding')}</Badge>}
          {d.badge === 'WATCH' && <Badge variant="default">{t('instrument.watch')}</Badge>}
        </>
      )}
      <div className="ml-auto flex items-baseline gap-3">
        {lc ? (
          <>
            <span className="num text-2xl font-semibold" data-testid="last-close">{formatNum(lc.close)}</span>
            <Delta value={lc.day_change_pct} percent />
            <span className="num text-xs text-muted-foreground">{lc.date}</span>
          </>
        ) : d ? (
          <span className="text-muted-foreground">{t('instrument.noPrice')}</span>
        ) : (
          <Skeleton className="h-6 w-28" />
        )}
        {kapUrl && (
          <a href={kapUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-brand">
            KAP <ExternalLink className="size-3" />
          </a>
        )}
      </div>
    </div>
  )
}

function Chart({ d, ticker }: { d?: InstrumentOut; ticker: string }) {
  const { t } = useTranslation()
  const prices = usePrices(ticker, !!d)
  const p = prices.data
  const data = useMemo(
    () => (p?.bars ?? []).flatMap((b) => (b.close == null ? [] : [{ time: b.date, close: b.close, open: b.open, high: b.high, low: b.low, volume: b.volume }])),
    [p],
  )
  // One marker per day (the most urgent priority wins); market-wide notices would otherwise stack dozens per bar.
  const events = useMemo<ChartEvent[]>(() => {
    const byDay = new Map<string, { priority: Priority; label: string; n: number }>()
    for (const m of p?.markers_disclosures ?? []) {
      const priority = toPriority(m.priority)
      if (!priority) continue
      const cur = byDay.get(m.date)
      if (!cur) byDay.set(m.date, { priority, label: `${m.code_label}: ${m.headline}`, n: 1 })
      else {
        cur.n += 1
        if (priority < cur.priority) {
          cur.priority = priority
          cur.label = `${m.code_label}: ${m.headline}`
        }
      }
    }
    return [...byDay].map(([time, v]) => ({ time, priority: v.priority, label: v.n > 1 ? `${v.label} (+${v.n - 1})` : v.label }))
  }, [p])
  const txns = useMemo<ChartTransaction[]>(
    () => (p?.markers_txns ?? []).map((m) => ({ time: m.date, side: m.side, quantity: m.quantity, price: m.price ?? undefined })),
    [p],
  )
  if (prices.isError) return <PanelError error={prices.error} onRetry={() => void prices.refetch()} />
  if (!d || prices.isPending) return <Skeleton className="h-[352px] w-full rounded-none" aria-label={t('kit.common.loading')} />
  return <PriceChart data={data} events={events} transactions={txns} defaultMode="line" showVolume={data.some((x) => x.volume != null)} defaultRange="1y" height={320} />
}

function InstrumentTabs({ d }: { d: InstrumentOut }) {
  const { t } = useTranslation()
  const held = d.position != null
  return (
    <Tabs defaultValue="disclosures">
      <TabsList>
        <TabsTrigger value="disclosures">{t('instrument.tabs.disclosures')} <Count n={d.disclosures.length} /></TabsTrigger>
        <TabsTrigger value="position">{t('instrument.tabs.position')}</TabsTrigger>
        <TabsTrigger value="company">{t('instrument.tabs.company')}</TabsTrigger>
        <TabsTrigger value="theses">{t('instrument.tabs.theses')} <Count n={d.theses.length} /></TabsTrigger>
        <TabsTrigger value="agents">{t('instrument.tabs.agents')} <Count n={d.agent_runs.length} /></TabsTrigger>
        {(['financials', 'impact', 'risk'] as const).map((k) => (
          <TabsTrigger key={k} value={k} className="opacity-60">{t(`instrument.tabs.${k}`)}</TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="disclosures" className="pt-3"><Disclosures d={d} /></TabsContent>
      <TabsContent value="position" className="pt-3">{held ? <PositionTab d={d} /> : <EmptyState title={t('instrument.position.none')} description={d.watch ? t('instrument.position.watching') : undefined} />}</TabsContent>
      <TabsContent value="company" className="pt-3"><CompanyTab d={d} /></TabsContent>
      <TabsContent value="theses" className="pt-3"><Theses d={d} /></TabsContent>
      <TabsContent value="agents" className="pt-3"><Agents d={d} /></TabsContent>
      {(['financials', 'impact', 'risk'] as const).map((k) => (
        <TabsContent key={k} value={k} className="pt-3">
          <Panel><EmptyState title={t('instrument.soon')} description={t(`instrument.soonHint.${k}`)} /></Panel>
        </TabsContent>
      ))}
    </Tabs>
  )
}

function Count({ n }: { n: number }) {
  return n > 0 ? <span className="num text-xs text-muted-foreground">{n}</span> : null
}

function Disclosures({ d }: { d: InstrumentOut }) {
  const { t } = useTranslation()
  if (d.disclosures.length === 0) return <Panel><EmptyState title={t('instrument.disclosures.empty')} /></Panel>
  return (
    <Panel>
      <ul data-testid="disclosure-list">
        {d.disclosures.map((i) => {
          const p = toPriority(i.priority)
          return (
            <li key={i.id} className="flex items-start gap-2 border-b border-border px-3 py-2 last:border-b-0">
              {p ? <PriorityBadge priority={p} className="mt-0.5" /> : <span className="w-[26px]" />}
              <div className="min-w-0 flex-1">
                <DisclosureLink eventId={i.id} className="line-clamp-2 break-words hover:text-brand">{i.headline}</DisclosureLink>
                <div className="mt-0.5 text-xs text-muted-foreground">{i.code_label}</div>
              </div>
              <RelativeTime iso={i.published_at ?? i.announced_at} className="text-xs text-muted-foreground" />
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}

function KV({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border px-3 py-1.5 last:border-b-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="num min-w-0 truncate text-right">{children}</dd>
    </div>
  )
}

function PositionTab({ d }: { d: InstrumentOut }) {
  const { t } = useTranslation()
  const p = d.position!
  return (
    <Panel title={t('instrument.position.title')}>
      <dl data-testid="position-tab">
        <KV label={t('portfolio.holdings.qty')}>{formatQty(p.quantity)}</KV>
        <KV label={t('portfolio.holdings.avgCost')}>{formatNum(p.avg_cost)}</KV>
        <KV label={t('portfolio.totals.cost')}>{formatMoney(p.cost_total)}</KV>
        <KV label={t('portfolio.holdings.value')}>{formatMoney(p.market_value)}</KV>
        <KV label={t('portfolio.holdings.weight')}>{p.weight != null ? formatPercent(p.weight * 100, { decimals: 1 }) : DASH}</KV>
        <KV label={t('portfolio.holdings.pnl')}><Delta value={p.pnl} arrow={false} /> <Delta value={p.pnl_pct} percent /></KV>
        <KV label={t('portfolio.totals.day')}><Delta value={p.day_change} arrow={false} /> <Delta value={p.day_change_pct} percent /></KV>
      </dl>
      <p className="px-3 py-2 text-xs text-muted-foreground">{t('instrument.position.txnsOnChart')}</p>
    </Panel>
  )
}

function CompanyTab({ d }: { d: InstrumentOut }) {
  const { t } = useTranslation()
  const sc = d.share_capital
  const hasCapital = Object.values(sc).some((v) => v != null)
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Panel title={t('instrument.company.issuer')}>
        <dl>
          <KV label={t('instrument.company.legalName')}>{d.issuer?.legal_name ?? DASH}</KV>
          <KV label={t('instrument.company.sector')}>{d.issuer?.sector_code ?? DASH}</KV>
          <KV label={t('instrument.company.type')}>{d.instrument.type} · {d.instrument.currency}</KV>
          {d.identifiers.map((i) => (
            <KV key={`${i.scheme}-${i.value}`} label={i.scheme}>{i.value}</KV>
          ))}
        </dl>
      </Panel>
      <div className="flex flex-col gap-3">
        <Panel title={t('instrument.company.capital')}>
          {hasCapital ? (
            <dl>
              <KV label={t('instrument.company.paidIn')}>{formatMoney(sc.paid_in_capital, { decimals: 0 })}</KV>
              <KV label={t('instrument.company.ceiling')}>{formatMoney(sc.registered_capital_ceiling, { decimals: 0 })}</KV>
              <KV label={t('instrument.company.shares')}>{formatNum(sc.shares_outstanding, { decimals: 0 })}</KV>
              <KV label={t('instrument.company.freeFloat')}>{sc.free_float_pct != null ? formatPercent(sc.free_float_pct, { decimals: 1 }) : DASH}</KV>
              <KV label={t('instrument.company.marketCap')}>{formatMoney(sc.market_cap, { decimals: 0, compact: true })}</KV>
            </dl>
          ) : (
            <EmptyState title={t('instrument.company.noCapital')} className="py-6" />
          )}
        </Panel>
        <Panel title={t('instrument.company.membership')} bodyClassName="flex flex-col gap-2 p-3">
          <ChipRow label={t('instrument.company.indices')} items={d.indices} />
          <ChipRow label={t('instrument.company.watchlists')} items={d.watchlists} />
        </Panel>
      </div>
    </div>
  )
}

function ChipRow({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mr-1 text-muted-foreground">{label}</span>
      {items.length === 0 ? <span className="text-muted-foreground">{DASH}</span> : items.map((i) => <Badge key={i} variant="outline">{i}</Badge>)}
    </div>
  )
}

function Theses({ d }: { d: InstrumentOut }) {
  const { t } = useTranslation()
  if (d.theses.length === 0) return <Panel><EmptyState title={t('instrument.theses.empty')} /></Panel>
  return (
    <Panel>
      <ul>
        {d.theses.map((th) => (
          <li key={th.name} className="flex items-center gap-3 border-b border-border px-3 py-2 last:border-b-0">
            <div className="min-w-0 flex-1">
              <div className="truncate font-medium">{th.name}</div>
              <div className="text-xs text-muted-foreground">
                {[th.direction, th.horizon, th.date].filter(Boolean).join(' · ')}
              </div>
            </div>
            {th.last_verdict && <Badge variant={th.last_verdict === 'confirmed' ? 'up' : th.last_verdict === 'broken' ? 'down' : 'outline'}>{th.last_verdict}</Badge>}
            <span className="num text-xs text-muted-foreground">{t('instrument.theses.runs', { count: th.runs })}</span>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function Agents({ d }: { d: InstrumentOut }) {
  const { t } = useTranslation()
  if (d.agent_runs.length === 0) return <Panel><EmptyState title={t('instrument.agents.empty')} /></Panel>
  return (
    <Panel>
      <ul>{d.agent_runs.map((r) => <AgentRunRow key={r.run_id} run={r} />)}</ul>
    </Panel>
  )
}

