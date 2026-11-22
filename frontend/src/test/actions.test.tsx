import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('lightweight-charts', () => {
  const series = () => ({ setData: vi.fn(), applyOptions: vi.fn(), priceScale: () => ({ applyOptions: vi.fn() }) })
  const timeScale = { setVisibleRange: vi.fn(), fitContent: vi.fn() }
  const chart = {
    addSeries: vi.fn(() => series()),
    removeSeries: vi.fn(),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    timeScale: vi.fn(() => timeScale),
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
  }
  return {
    createChart: vi.fn(() => chart),
    createSeriesMarkers: vi.fn(() => ({ detach: vi.fn(), setMarkers: vi.fn() })),
    LineSeries: 'Line',
    CandlestickSeries: 'Candlestick',
    HistogramSeries: 'Histogram',
    ColorType: { Solid: 'solid' },
    CrosshairMode: { Normal: 0 },
    LineStyle: { Dashed: 2 },
  }
})

import { parseDecimal } from '@/lib/parse-number'
import { baseStatus, renderApp } from './helpers'

const snap = (quantity: number, avg: number | null, weight: number | null) => ({ quantity, avg_cost: avg, cost_total: quantity * (avg ?? 0), market_value: null, weight })

const txn = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  instrument_id: 'i1',
  ticker: 'THYAO',
  type: 'buy',
  trade_date: '2026-11-20',
  quantity: 10,
  price: 300,
  fees: 0,
  tax: 0,
  currency: 'TRY',
  source: 'manual',
  note: null,
  reverses_id: null,
  reversed: false,
  ...over,
})

const pendingAction = {
  id: 'pa1',
  instrument_id: 'i1',
  ticker: 'THYAO',
  kind: 'bonus',
  status: 'expected',
  ex_date: '2026-11-19',
  proposal: { txn_type: 'bonus_shares', quantity: '462', text_tr: 'THYAO bedelsiz %100: 462 → 924 pay.' },
  corporate_action_id: 'ca1',
  created_at: '2026-11-19T09:00:00Z',
}

const overview = {
  available: true,
  positions: [],
  totals: { value: 0, cost: 0, pnl: 0, pnl_pct: null, day_change: null, day_change_pct: null, positions: 0, unvalued: [] },
  allocation: { by_instrument: [], by_kind: [], by_sector: [] },
  transactions: [txn('t-new'), txn('t-rev', { reverses_id: 't-old', quantity: -5 }), txn('t-old', { reversed: true, quantity: 5 })],
  pending_actions: [pendingAction],
  watchlists: [{ id: 'w1', name: 'Ana', members: [{ instrument_id: 'i2', ticker: 'GARAN', name: 'Garanti', badge: 'WATCH' }], sectors: [], indices: ['XU030'] }],
}

const llm = {
  paused: false,
  paused_until: null,
  pause_reason: null,
  used_5h: 0.5,
  cap_5h: 2,
  used_chat_5h: 0.1,
  cap_chat_5h: 1,
  used_week: 3,
  cap_week: 20,
  calibration_mode: false,
  focus: null,
  night_window_active: false,
  concurrency: 2,
  subscription_bars: null,
  by_task: [],
  queue: { deferred: {}, deferred_total: 0 },
}

const rule = { id: 'r1', name: 'Portföy', filter: 'scope = "holdings"', channels: ['telegram'], quiet_hours: null, enabled: true, created_at: '2026-11-01T00:00:00Z', updated_at: null }

const instrument = {
  ticker: 'THYAO',
  name: 'TÜRK HAVA YOLLARI A.O.',
  kind: 'stock',
  instrument: { id: 'i1', type: 'equity', currency: 'TRY', status: 'active' },
  issuer: null,
  badge: null,
  watch: false,
  watchlists: [],
  indices: [],
  identifiers: [],
  share_capital: { paid_in_capital: null, registered_capital_ceiling: null, paid_in_capital_as_of: null, shares_outstanding: null, shares_as_of: null, free_float_shares: null, free_float_pct: null, market_cap: null },
  last_close: null,
  position: null,
  disclosures: [],
  theses: [],
  agent_runs: [],
}

interface Call {
  method: string
  path: string
  query: string
  body: unknown
}
let calls: Call[]

function mockApi(extra: Record<string, () => Response> = {}) {
  calls = []
  const fetchMock = vi.fn(async (req: Request) => {
    const url = new URL(req.url)
    const body = req.method === 'GET' ? undefined : await req.clone().json().catch(() => undefined)
    const key = `${req.method} ${url.pathname}`
    if (req.method !== 'GET') calls.push({ method: req.method, path: url.pathname, query: url.search, body })
    if (extra[key]) return extra[key]()
    switch (key) {
      case 'GET /v1/status':
        return Response.json(baseStatus)
      case 'GET /v1/portfolio/overview':
        return Response.json(overview)
      case 'GET /v1/portfolio/performance':
        return Response.json({ range: '3m', method: 'transactions_x_closes', start: null, end: null, points: [], benchmark: [], unpriced: [] })
      case 'GET /v1/search':
        return Response.json({ q: 'thy', instruments: [{ instrument_id: 'i1', ticker: 'THYAO', name: 'TÜRK HAVA YOLLARI A.O.', kind: 'stock', badge: null, score: 90 }], disclosures: [] })
      case 'POST /v1/portfolio/transactions': {
        const b = body as { dry_run: boolean; type: string; ticker: string; quantity: number; price: number }
        return Response.json({
          committed: !b.dry_run,
          txn_id: b.dry_run ? null : 'new-id',
          type: b.type,
          ticker: b.ticker,
          instrument_id: 'i1',
          name: 'TÜRK HAVA YOLLARI A.O.',
          quantity: b.quantity,
          price: b.price,
          fees: 0,
          trade_date: '2026-11-22',
          gross: b.quantity * b.price,
          warnings: ['Fiyat son kapanıştan %12 farklı.'],
          before: snap(100, 250, 0.2),
          after: snap(150, 260, 0.3),
        })
      }
      case 'POST /v1/portfolio/transactions/undo':
        return Response.json({ undone: txn('t-new', { reversed: true }), reversal_id: 'rev' })
      case 'POST /v1/portfolio/pending/pa1/apply':
        return Response.json({ pending_id: 'pa1', txn_id: 'tx' })
      case 'POST /v1/portfolio/pending/pa1/dismiss':
        return Response.json({ pending_id: 'pa1' })
      case 'GET /v1/instruments/THYAO':
        return Response.json(instrument)
      case 'GET /v1/instruments/THYAO/prices':
        return Response.json({ ticker: 'THYAO', from: '2025-11-22', to: '2026-11-22', primary_source: 'x', bars: [], markers_disclosures: [], markers_txns: [] })
      case 'POST /v1/watchlist':
        return Response.json({ ref: 'THYAO', changed: true, watchlist: { id: 'w1', name: 'Ana', tickers: ['THYAO'], sectors: [], indices: [] } })
      case 'GET /v1/llm/status':
        return Response.json(llm)
      case 'POST /v1/llm/governor/pause':
        return Response.json({ ...llm, paused: true })
      case 'POST /v1/llm/governor/resume':
        return Response.json(llm)
      case 'GET /v1/alerts/rules':
        return Response.json({ rules: [rule], fields: ['scope', 'materiality'], telegram_ready: true })
      case 'POST /v1/alerts/rules':
        return Response.json({ ...rule, id: 'r2', name: (body as { name: string }).name })
      case 'PATCH /v1/alerts/rules/r1':
        return Response.json({ ...rule, enabled: false })
      case 'DELETE /v1/alerts/rules/r1':
        return Response.json({ id: 'r1' })
      case 'POST /v1/alerts/rules/validate':
        return Response.json({ ok: false, error: 'bilinmeyen alan' })
      case 'POST /v1/alerts/test':
        return Response.json({ ok: true, channel: 'telegram', detail: 'mesaj #1' })
    }
    return Response.json({})
  })
  vi.stubGlobal('fetch', fetchMock)
}

beforeEach(() => mockApi())
afterEach(() => vi.unstubAllGlobals())

const find = (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path)

describe('parseDecimal', () => {
  it('reads Turkish and English decimals', () => {
    expect(parseDecimal('285,40')).toBe(285.4)
    expect(parseDecimal('285.40')).toBe(285.4)
    expect(parseDecimal('1.234,56')).toBe(1234.56)
    expect(parseDecimal('1,234.56')).toBe(1234.56)
    expect(parseDecimal('1.234.567')).toBe(1234567)
    expect(parseDecimal('1.500')).toBe(1500)
    expect(parseDecimal('1.5')).toBe(1.5)
    expect(parseDecimal(' 100 ')).toBe(100)
    expect(parseDecimal('-3,5')).toBe(-3.5)
  })
  it('rejects non-numbers', () => {
    expect(parseDecimal('')).toBeNull()
    expect(parseDecimal('abc')).toBeNull()
    expect(parseDecimal('1,2,3')).toBeNull()
    expect(parseDecimal('12x')).toBeNull()
  })
})

describe('Trade dialog', () => {
  it('parses a Turkish decimal, previews with dry_run, then commits', async () => {
    const user = userEvent.setup()
    await renderApp('/portfolio')
    await user.click((await screen.findAllByTestId('add-trade'))[0]!)
    const dialog = await screen.findByRole('dialog', { name: 'İşlem ekle' })

    await user.click(within(dialog).getByRole('button', { name: 'Önizle' }))
    expect(await within(dialog).findByText('Kod gerekli')).toBeInTheDocument()
    expect(find('POST', '/v1/portfolio/transactions')).toHaveLength(0)

    await user.type(within(dialog).getByLabelText('Hisse / fon kodu'), 'thy')
    await user.click(await within(dialog).findByRole('option', { name: /THYAO/ }))
    await user.type(within(dialog).getByLabelText('Adet'), '50')
    await user.type(within(dialog).getByLabelText('Fiyat'), '285,40')
    await user.click(within(dialog).getByRole('button', { name: 'Önizle' }))

    const preview = await within(dialog).findByTestId('trade-preview')
    expect(find('POST', '/v1/portfolio/transactions')[0]!.body).toMatchObject({ dry_run: true, type: 'buy', ticker: 'THYAO', quantity: 50, price: 285.4, fees: 0 })
    expect(within(preview).getByTestId('snap-before-qty')).toHaveTextContent('100')
    expect(within(preview).getByTestId('snap-after-qty')).toHaveTextContent('150')
    expect(within(preview).getByTestId('trade-warnings')).toHaveTextContent('%12')
    expect(find('POST', '/v1/portfolio/transactions')).toHaveLength(1) // preview only

    await user.click(within(dialog).getByRole('button', { name: 'Onayla ve kaydet' }))
    await waitFor(() => expect(find('POST', '/v1/portfolio/transactions')).toHaveLength(2))
    expect(find('POST', '/v1/portfolio/transactions')[1]!.body).toMatchObject({ dry_run: false, quantity: 50, price: 285.4 })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'İşlem ekle' })).not.toBeInTheDocument())
  })

  it('shows a server validation error from the preview', async () => {
    vi.unstubAllGlobals()
    mockApi({ 'POST /v1/portfolio/transactions': () => Response.json({ detail: 'Bilinmeyen hisse kodu: ZZZ.' }, { status: 422 }) })
    const user = userEvent.setup()
    await renderApp('/portfolio')
    await user.click((await screen.findAllByTestId('add-trade'))[0]!)
    const dialog = await screen.findByRole('dialog', { name: 'İşlem ekle' })
    await user.type(within(dialog).getByLabelText('Hisse / fon kodu'), 'ZZZ')
    await user.type(within(dialog).getByLabelText('Adet'), '1')
    await user.type(within(dialog).getByLabelText('Fiyat'), '2')
    await user.click(within(dialog).getByRole('button', { name: 'Önizle' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Bilinmeyen hisse kodu: ZZZ.')
  })

  it('opens from the instrument header with that ticker', async () => {
    const user = userEvent.setup()
    await renderApp('/i/THYAO')
    await user.click(await screen.findByTestId('add-trade'))
    const dialog = await screen.findByRole('dialog', { name: 'İşlem ekle' })
    expect(within(dialog).getByLabelText('Hisse / fon kodu')).toHaveValue('THYAO')
  })
})

describe('Undo', () => {
  it('offers undo on the last live transaction only, behind a confirm dialog', async () => {
    const user = userEvent.setup()
    await renderApp('/portfolio')
    const button = await screen.findByTestId('undo-last')
    expect(screen.getAllByTestId('undo-last')).toHaveLength(1)
    await user.click(button)
    const dialog = await screen.findByRole('dialog', { name: 'Son işlem geri alınsın mı?' })
    expect(within(dialog).getByTestId('undo-target')).toHaveTextContent('THYAO')
    expect(find('POST', '/v1/portfolio/transactions/undo')).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Geri al' }))
    await waitFor(() => expect(find('POST', '/v1/portfolio/transactions/undo')).toHaveLength(1))
    expect(find('POST', '/v1/portfolio/transactions/undo')[0]!.body).toEqual({ txn_id: 't-new' })
  })
})

describe('Pending corporate actions', () => {
  it('applies after confirming the proposal', async () => {
    const user = userEvent.setup()
    await renderApp('/portfolio')
    const row = await screen.findByTestId('pending-action')
    await user.click(within(row).getByRole('button', { name: 'Uygula' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByText(/462 → 924 pay/).length).toBeGreaterThan(0)
    await user.click(within(dialog).getByRole('button', { name: 'Uygula' }))
    await waitFor(() => expect(find('POST', '/v1/portfolio/pending/pa1/apply')).toHaveLength(1))
    expect(find('POST', '/v1/portfolio/pending/pa1/apply')[0]!.body).toEqual({ quantity_override: null })
  })

  it('sends an overridden quantity and can dismiss', async () => {
    const user = userEvent.setup()
    await renderApp('/portfolio')
    const row = await screen.findByTestId('pending-action')
    await user.click(within(row).getByRole('button', { name: 'Uygula' }))
    let dialog = await screen.findByRole('dialog')
    const qty = within(dialog).getByLabelText('Adet')
    await user.clear(qty)
    await user.type(qty, '417')
    await user.click(within(dialog).getByRole('button', { name: 'Uygula' }))
    await waitFor(() => expect(find('POST', '/v1/portfolio/pending/pa1/apply')[0]!.body).toEqual({ quantity_override: 417 }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(within(row).getByRole('button', { name: 'Yoksay' }))
    dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Yoksay' }))
    await waitFor(() => expect(find('POST', '/v1/portfolio/pending/pa1/dismiss')).toHaveLength(1))
  })
})

describe('Watchlist', () => {
  it('toggles watching from the instrument header', async () => {
    const user = userEvent.setup()
    await renderApp('/i/THYAO')
    await user.click(await screen.findByTestId('watch-toggle'))
    await waitFor(() => expect(find('POST', '/v1/watchlist')).toHaveLength(1))
    expect(find('POST', '/v1/watchlist')[0]!.body).toEqual({ ref: 'THYAO', watchlist: 'Ana' })
  })

  it('adds and removes from the portfolio watchlist panel', async () => {
    const user = userEvent.setup()
    await renderApp('/portfolio')
    const list = await screen.findByTestId('watchlist')
    await user.type(screen.getByRole('combobox', { name: /THYAO, XU030/ }), 'XU050')
    await user.click(screen.getByRole('button', { name: 'Ekle' }))
    await waitFor(() => expect(find('POST', '/v1/watchlist')).toHaveLength(1))
    expect(find('POST', '/v1/watchlist')[0]!.body).toEqual({ ref: 'XU050', watchlist: 'Ana' })

    await user.click(within(list).getByRole('button', { name: 'GARAN izlemeden çıkar' }))
    await waitFor(() => expect(find('DELETE', '/v1/watchlist')).toHaveLength(1))
    expect(find('DELETE', '/v1/watchlist')[0]!.query).toBe('?ref=GARAN&watchlist=Ana')
    await user.click(within(list).getByRole('button', { name: 'XU030 izlemeden çıkar' }))
    await waitFor(() => expect(find('DELETE', '/v1/watchlist')).toHaveLength(2))
    expect(find('DELETE', '/v1/watchlist')[1]!.query).toBe('?ref=endeks%3AXU030&watchlist=Ana')
  })
})

describe('System: LLM panel', () => {
  it('shows usage and pauses for a chosen duration, then resumes', async () => {
    const user = userEvent.setup()
    await renderApp('/system')
    const panel = await screen.findByTestId('llm-panel')
    expect(within(panel).getByTestId('llm-state')).toHaveTextContent('Aktif')
    expect(within(panel).getByTestId('usage-5h')).toHaveTextContent('%25')
    await user.click(screen.getByRole('button', { name: /Duraklat/ }))
    await user.click(await screen.findByRole('menuitem', { name: '3 saat' }))
    await waitFor(() => expect(find('POST', '/v1/llm/governor/pause')).toHaveLength(1))
    expect(find('POST', '/v1/llm/governor/pause')[0]!.body).toEqual({ minutes: 180, reason: 'manual' })
  })

  it('offers resume while paused', async () => {
    vi.unstubAllGlobals()
    mockApi({ 'GET /v1/llm/status': () => Response.json({ ...llm, paused: true, pause_reason: 'manual' }) })
    const user = userEvent.setup()
    await renderApp('/system')
    expect(await screen.findByTestId('llm-state')).toHaveTextContent('Duraklatıldı')
    await user.click(screen.getByRole('button', { name: 'Sürdür' }))
    await waitFor(() => expect(find('POST', '/v1/llm/governor/resume')).toHaveLength(1))
  })

  it('palette pause entry takes a duration', async () => {
    const user = userEvent.setup()
    await renderApp('/system')
    await screen.findByTestId('llm-panel')
    await user.keyboard('{Control>}k{/Control}')
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByText("LLM'i duraklat: 1 saat"))
    await waitFor(() => expect(find('POST', '/v1/llm/governor/pause')[0]!.body).toEqual({ minutes: 60, reason: 'manual' }))
  })
})

describe('System: alert rules', () => {
  it('lists rules, toggles one and creates a new one through the editor', async () => {
    const user = userEvent.setup()
    await renderApp('/system')
    const rows = await screen.findAllByTestId('rule-row')
    expect(rows[0]).toHaveTextContent('scope = "holdings"')

    await user.click(within(rows[0]!).getByRole('checkbox', { name: 'Portföy kuralını aç/kapat' }))
    await waitFor(() => expect(find('PATCH', '/v1/alerts/rules/r1')).toHaveLength(1))
    expect(find('PATCH', '/v1/alerts/rules/r1')[0]!.body).toEqual({ enabled: false })

    await user.click(screen.getByRole('button', { name: 'Yeni kural' }))
    const dialog = await screen.findByRole('dialog', { name: 'Yeni uyarı kuralı' })
    await user.click(within(dialog).getByRole('button', { name: 'Kaydet' }))
    expect(await within(dialog).findByText('Ad gerekli')).toBeInTheDocument()
    expect(within(dialog).getByText('Filtre gerekli')).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('Ad'), 'Savunma')
    await user.type(within(dialog).getByLabelText('Filtre'), 'sector = "savunma"')
    await user.click(within(dialog).getByLabelText('Kurala özel sessiz saat'))
    await user.click(within(dialog).getByRole('button', { name: 'Kaydet' }))
    await waitFor(() => expect(find('POST', '/v1/alerts/rules')).toHaveLength(1))
    expect(find('POST', '/v1/alerts/rules')[0]!.body).toEqual({
      name: 'Savunma',
      filter: 'sector = "savunma"',
      channels: ['telegram', 'macos'],
      quiet_hours: { start: '23:00', end: '07:30' },
      enabled: true,
    })
  })

  it('shows the filter validation message and deletes after confirming', async () => {
    const user = userEvent.setup()
    await renderApp('/system')
    await screen.findAllByTestId('rule-row')
    await user.click(screen.getByRole('button', { name: 'Portföy kuralını düzenle' }))
    const dialog = await screen.findByRole('dialog', { name: 'Kuralı düzenle' })
    await user.click(within(dialog).getByRole('button', { name: 'Filtreyi doğrula' }))
    expect(await within(dialog).findByText('bilinmeyen alan')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Vazgeç' }))

    await user.click(screen.getByRole('button', { name: 'Portföy kuralını sil' }))
    const confirm = await screen.findByRole('dialog', { name: '"Portföy" silinsin mi?' })
    expect(find('DELETE', '/v1/alerts/rules/r1')).toHaveLength(0)
    await user.click(within(confirm).getByRole('button', { name: 'Sil' }))
    await waitFor(() => expect(find('DELETE', '/v1/alerts/rules/r1')).toHaveLength(1))
  })

  it('sends a test notification', async () => {
    const user = userEvent.setup()
    await renderApp('/system')
    await user.click(await screen.findByRole('button', { name: /Test bildirimi/ }))
    await user.click(await screen.findByRole('menuitem', { name: 'Telegram' }))
    await waitFor(() => expect(find('POST', '/v1/alerts/test')).toHaveLength(1))
    expect(find('POST', '/v1/alerts/test')[0]!.body).toEqual({ channel: 'telegram' })
  })
})
