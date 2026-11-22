import { createFileRoute, Navigate } from '@tanstack/react-router'
import { type ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import { DataTable } from '@/components/kit/data-table'
import { Delta } from '@/components/kit/delta'
import { DocumentView } from '@/components/kit/document-view'
import { type FilterState, FilterBar } from '@/components/kit/filter-bar'
import { NewItemsPill } from '@/components/kit/new-items-pill'
import { Panel, Section } from '@/components/ui/panel'
import { type Priority, PriorityBadge } from '@/components/kit/priority-badge'
import { type ChartEvent, type ChartTransaction, type PricePoint, PriceChart } from '@/components/kit/price-chart'
import { RelativeTime } from '@/components/kit/relative-time'
import { SplitView } from '@/components/kit/split-view'
import { Stat } from '@/components/kit/stat'
import { TickerChip } from '@/components/kit/ticker-chip'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Kbd } from '@/components/ui/kbd'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip } from '@/components/ui/tooltip'
import { useHeldItems } from '@/hooks/use-held-items'
import { toggleColorBlind } from '@/lib/color-blind'
import { formatMoney, formatNum, formatPercent } from '@/lib/format'
import { toggleTheme } from '@/lib/theme'
import { toggleLanguage } from '@/i18n'

export const Route = createFileRoute('/_kitchen')({
  staticData: { titleKey: 'nav.lab' },
  component: Kitchen,
})

/* ---- deterministic fake data ---- */
const THREE_MIN_AGO = new Date(Date.now() - 180_000).toISOString()

function rng(seed: number) {
  let s = seed
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296
    return s / 4294967296
  }
}

function makePrices(days: number): PricePoint[] {
  const r = rng(7)
  const out: PricePoint[] = []
  let p = 100
  const start = Date.UTC(2025, 5, 1)
  for (let i = 0; i < days; i++) {
    const t = start + i * 86400000
    const wd = new Date(t).getUTCDay()
    if (wd === 0 || wd === 6) continue
    const open = p
    p = Math.max(5, p * (1 + (r() - 0.48) * 0.03))
    const high = Math.max(open, p) * (1 + r() * 0.01)
    const low = Math.min(open, p) * (1 - r() * 0.01)
    out.push({ time: new Date(t).toISOString().slice(0, 10), open, high, low, close: p, volume: Math.round(1e6 * (0.5 + r())) })
  }
  return out
}

interface Row {
  id: string
  ticker: string
  title: string
  priority: Priority
  at: string
  price: number
  change: number
}

const TITLES = ['Özel Durum Açıklaması (Genel)', 'Finansal Rapor', 'Pay Alım Satım Bildirimi', 'Genel Kurul İşlemlerine İlişkin Bildirim', 'Sermaye Artırımı', 'Kar Payı Dağıtım İşlemlerine İlişkin Bildirim']
const TICKERS = ['THYAO', 'ASELS', 'GARAN', 'EREGL', 'AYGAZ', 'FROTO', 'KCHOL', 'SISE']

function makeRows(n: number): Row[] {
  const r = rng(11)
  return Array.from({ length: n }, (_, i) => ({
    id: `r${i}`,
    ticker: TICKERS[i % TICKERS.length]!,
    title: TITLES[Math.floor(r() * TITLES.length)]!,
    priority: (['P1', 'P2', 'P3'] as const)[Math.floor(r() * 3)]!,
    at: new Date(Date.now() - i * 137_000).toISOString(),
    price: 20 + r() * 400,
    change: (r() - 0.5) * 8,
  }))
}

const DOC_HTML = `
<h2 id="ozet">Özet</h2>
<p>Şirketimizin 2026 yılı üçüncü çeyrek bilançosu kamuya açıklanmıştır. Konsolide net dönem kârı <strong>1.234.567.890 TL</strong> olarak gerçekleşmiştir.</p>
<p id="anchor-risk">Bu bölüm vurgulanan çapa örneğidir: riskler ve belirsizlikler, ilgili mevzuat çerçevesinde yatırımcıların bilgisine sunulmaktadır.</p>
<h3>Finansal göstergeler</h3>
<table><thead><tr><th>Kalem</th><th>2026/9</th><th>2025/9</th><th>Değişim</th><th>Açıklama</th><th>Not</th><th>Daha uzun bir başlık sütunu</th></tr></thead>
<tbody>
<tr><td>Hasılat</td><td>12.345.678</td><td>9.876.543</td><td>%25,00</td><td>Satışlardaki artış kaynaklıdır, ihracat gelirleri dahildir</td><td>1</td><td>Ayrıntılar dipnotlarda</td></tr>
<tr><td>Brüt kâr</td><td>3.456.789</td><td>2.345.678</td><td>%47,37</td><td>Maliyet kontrolü</td><td>2</td><td>-</td></tr>
<tr><td>Net kâr</td><td>(123.456)</td><td>234.567</td><td>-152,63%</td><td>Kur farkı etkisi</td><td>3</td><td>-</td></tr>
</tbody></table>
<ul><li>Birinci madde</li><li>İkinci madde</li></ul>
<blockquote>Bu açıklama yatırım tavsiyesi niteliği taşımamaktadır.</blockquote>`

function Kitchen() {
  if (!import.meta.env.DEV) return <Navigate to="/" />
  return <KitchenInner />
}

function KitchenInner() {
  const prices = useMemo(() => makePrices(520), [])
  const events: ChartEvent[] = useMemo(
    () => [
      { time: prices[60]!.time, priority: 'P1', label: 'Finansal Rapor' },
      { time: prices[140]!.time, priority: 'P2', label: 'Pay Alım Satım Bildirimi' },
      { time: prices[200]!.time, priority: 'P3', label: 'Genel Kurul' },
      { time: prices[300]!.time, priority: 'P1', label: 'Sermaye Artırımı' },
    ],
    [prices],
  )
  const txns: ChartTransaction[] = useMemo(
    () => [
      { time: prices[90]!.time, side: 'buy', quantity: 100, price: prices[90]!.close },
      { time: prices[250]!.time, side: 'sell', quantity: 40, price: prices[250]!.close },
    ],
    [prices],
  )
  const noOhlc = useMemo(() => prices.map(({ time, close }) => ({ time, close })), [prices])

  const rows = useMemo(() => makeRows(1500), [])
  const [selected, setSelected] = useState<string | null>('r2')
  const [state, setState] = useState<'ok' | 'loading' | 'error' | 'empty'>('ok')
  const [compact, setCompact] = useState(false)
  const [filters, setFilters] = useState<FilterState>({ priorities: ['P1'] })
  const [live, setLive] = useState(() => rows.slice(0, 5))
  const held = useHeldItems(live, (r) => r.id)
  const [hl, setHl] = useState<string | null>(null)

  const columns = useMemo<ColumnDef<Row>[]>(
    () => [
      { accessorKey: 'priority', header: 'Pri', size: 56, cell: (c) => <PriorityBadge priority={c.getValue<Priority>()} />, meta: { label: 'Öncelik' } },
      { accessorKey: 'at', header: 'Zaman', size: 96, cell: (c) => <RelativeTime iso={c.getValue<string>()} className="text-muted-foreground" />, enableSorting: true },
      { accessorKey: 'ticker', header: 'Kod', size: 88, cell: (c) => <TickerChip ticker={c.getValue<string>()} mark={c.row.index % 3 === 0 ? 'holding' : c.row.index % 3 === 1 ? 'watch' : null} /> },
      { accessorKey: 'title', header: 'Başlık', size: 260, meta: { grow: true } },
      { accessorKey: 'price', header: 'Fiyat', size: 110, meta: { numeric: true }, cell: (c) => formatMoney(c.getValue<number>()) },
      { accessorKey: 'change', header: 'Değişim', size: 100, meta: { numeric: true }, cell: (c) => <Delta value={c.getValue<number>()} percent /> },
    ],
    [],
  )

  const tableProps = {
    data: state === 'empty' || state === 'error' ? [] : rows,
    columns,
    getRowId: (r: Row) => r.id,
    selectedId: selected,
    onSelect: (id: string) => setSelected(id),
    loading: state === 'loading',
    error: state === 'error' ? 'HTTP 500' : undefined,
    onRetry: () => setState('ok'),
    compact,
    columnMenu: true,
    ariaLabel: 'Demo table',
    mobileRender: (r: Row) => (
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <PriorityBadge priority={r.priority} />
          <TickerChip ticker={r.ticker} />
          <RelativeTime iso={r.at} className="ml-auto text-xs text-muted-foreground" />
        </div>
        <div className="truncate">{r.title}</div>
      </div>
    ),
  }

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 md:p-6">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Kitchen sink</h1>
        <Badge variant="warning">dev only</Badge>
        <div className="ml-auto flex gap-2">
          <Button variant="outline" size="sm" onClick={toggleTheme}>Theme</Button>
          <Button variant="outline" size="sm" onClick={toggleColorBlind}>Colour-blind</Button>
          <Button variant="outline" size="sm" onClick={toggleLanguage}>TR / EN</Button>
        </div>
      </div>

      <Section title="Primitives">
        <div className="flex flex-wrap items-center gap-3">
          <PriorityBadge priority="P1" /> <PriorityBadge priority="P2" /> <PriorityBadge priority="P3" />
          <TickerChip ticker="THYAO" mark="holding" /> <TickerChip ticker="ASELS" mark="watch" /> <TickerChip ticker="GARAN" />
          <Delta value={4.2} percent /> <Delta value={-1.37} percent /> <Delta value={0} percent /> <Delta value={null} />
          <span className="num">{formatMoney(1234.56)}</span> <span className="num">{formatMoney(1234.56, { lang: 'en' })}</span>
          <span className="num">{formatNum(1234567.891, { decimals: 0 })}</span> <span className="num">{formatPercent(12.5)}</span>
          <RelativeTime iso={THREE_MIN_AGO} />
          <Badge>default</Badge> <Badge variant="brand">brand</Badge> <Badge variant="up">up</Badge> <Badge variant="down">down</Badge> <Badge variant="outline">outline</Badge>
          <Kbd>j</Kbd> <Kbd>k</Kbd> <Kbd>Enter</Kbd>
          <Tooltip content="Tooltip text"><Button variant="outline" size="sm">Hover me</Button></Tooltip>
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="XU100" value="10.234,56" delta={1.24} sparkline={[3, 4, 3.5, 5, 6, 5.5, 7]} />
          <Stat label="USDTRY" value="43,2150" delta={-0.12} sparkline={[7, 6, 6.5, 5, 4, 4.5, 3]} />
          <Stat label="Portföy K/Z" value={formatMoney(12345.6)} delta={2.3} hint="bugün" />
          <Stat label="Yükleniyor" value={<Skeleton className="mt-1 h-5 w-24" />} />
        </div>
        <Tabs defaultValue="a">
          <TabsList>
            <TabsTrigger value="a">Bildirimler</TabsTrigger>
            <TabsTrigger value="b">Pozisyonlar</TabsTrigger>
            <TabsTrigger value="c" disabled>Yakında</TabsTrigger>
          </TabsList>
          <TabsContent value="a" className="pt-2 text-muted-foreground">Tab A içeriği</TabsContent>
          <TabsContent value="b" className="pt-2 text-muted-foreground">Tab B içeriği</TabsContent>
        </Tabs>
        <div className="grid gap-3 md:grid-cols-3">
          <Panel title="Panel" actions={<Button size="sm" variant="ghost">Aksiyon</Button>} bodyClassName="p-3">Panel içeriği</Panel>
          <Panel title="Boş"><EmptyState description="Bu filtreyle sonuç yok." /></Panel>
          <Panel title="Hata"><ErrorState description="HTTP 500" onRetry={() => {}} /></Panel>
        </div>
        <ScrollArea className="h-20 rounded-md border border-border">
          <div className="space-y-1 p-2">{Array.from({ length: 12 }, (_, i) => <div key={i} className="num text-muted-foreground">Satır {i + 1}</div>)}</div>
        </ScrollArea>
      </Section>

      <Section title="FilterBar + live list">
        <FilterBar
          value={filters}
          onChange={setFilters}
          scopeOptions={[{ value: 'portfolio', label: 'Portföy' }, { value: 'watch', label: 'İzleme' }]}
          familyOptions={[{ value: 'fr', label: 'Finansal rapor' }, { value: 'ozel', label: 'Özel durum' }]}
        />
        <pre className="num rounded-md border border-border bg-muted p-2 text-xs">{JSON.stringify(filters)}</pre>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => setLive((l) => [rows[l.length + 10]!, ...l])}>Yeni öğe ekle (simüle)</Button>
          <NewItemsPill count={held.pending} onClick={held.reveal} />
          <span className="num text-muted-foreground">{held.visible.length} görünür</span>
        </div>
      </Section>

      <Section
        title="DataTable (1500 satır)"
        actions={
          <>
            {(['ok', 'loading', 'error', 'empty'] as const).map((s) => (
              <Button key={s} size="sm" variant={state === s ? 'secondary' : 'ghost'} onClick={() => setState(s)}>{s}</Button>
            ))}
            <Button size="sm" variant={compact ? 'secondary' : 'ghost'} onClick={() => setCompact((c) => !c)}>compact</Button>
          </>
        }
      >
        <div className="h-96 overflow-hidden rounded-md border border-border bg-card">
          <DataTable {...tableProps} />
        </div>
        <p className="text-muted-foreground">Mobile cards: <Kbd>mobile</Kbd></p>
        <div className="h-64 max-w-sm overflow-hidden rounded-md border border-border bg-card">
          <DataTable {...tableProps} mobile />
        </div>
      </Section>

      <Section title="PriceChart">
        <Panel>
          <PriceChart data={prices} events={events} transactions={txns} defaultMode="candles" showVolume />
        </Panel>
        <Panel title="Line only (no OHLC)">
          <PriceChart data={noOhlc} events={events} defaultRange="6m" height={220} />
        </Panel>
      </Section>

      <Section title="SplitView">
        <div className="h-72 overflow-hidden rounded-md border border-border bg-card">
          <SplitView
            storageKey="kitchen"
            hasDetail={selected != null}
            onCloseDetail={() => setSelected(null)}
            detailTitle={selected}
            list={<DataTable {...tableProps} loading={false} error={undefined} data={rows.slice(0, 50)} columnMenu={false} />}
            detail={<div className="p-4">Detay: <span className="num">{selected}</span></div>}
          />
        </div>
      </Section>

      <Section title="DocumentView" actions={<Button size="sm" variant="outline" onClick={() => setHl(hl === 'anchor-risk' ? 'ozet' : 'anchor-risk')}>Vurgula ({hl ?? 'yok'})</Button>}>
        <Panel bodyClassName="p-4 md:p-6">
          <DocumentView html={DOC_HTML} highlightAnchor={hl} />
        </Panel>
      </Section>
    </div>
  )
}
