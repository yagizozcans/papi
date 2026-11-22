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

import { baseStatus, renderApp } from './helpers'

const item = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  code: 'ozel',
  code_label: 'Özel durum',
  family: 'ozel',
  materiality: 80,
  materiality_band: 'high',
  priority: 'P1',
  status: 'final',
  stage: 'rules',
  announced_at: '2026-11-22T09:00:00Z',
  published_key: '2026-11-22T09:00:00.000000Z',
  published_at: '2026-11-22T09:00:00Z',
  first_seen_at: null,
  latency_s: null,
  catchup: false,
  is_late: false,
  revision_kind: null,
  revision_label: null,
  headline: `Headline ${id}`,
  headline_en: null,
  tickers: [{ instrument_id: 'i1', ticker: 'THYAO', badge: 'HOLDING' }],
  holding: true,
  watch: false,
  issuers: [],
  disclosure_id: null,
  source_ref: null,
  kap_url: null,
  ...over,
})

const today = {
  generated_at: '2026-11-22T10:00:00Z',
  xu100: { code: 'XU100', label: 'BIST 100', last: 11131.32, prev: 11400, change: -268, change_pct: -2.35, as_of: '2026-11-20' },
  usdtry: null,
  portfolio: { available: true, value: 560018.25, cost: 495386.81, pnl: 64631.44, day_change: -2717.67, day_change_pct: -0.48, positions: 4 },
  attention: { window_hours: 48, items: [item('e1'), item('e2')] },
  pending_actions: { count: 0, items: [] },
  jobs: {
    dead_total: 3,
    lanes: [{ lane: 'io', dead: 3, queued: 0, running: 0, last_error: 'boom', last_error_at: null, last_error_kind: null }],
    analyses_waiting: 0,
  },
  feed: [item('e1')],
  agent_runs: [],
}

const position = {
  instrument_id: 'i1',
  ticker: 'THYAO',
  name: 'TÜRK HAVA YOLLARI A.O.',
  kind: 'stock',
  quantity: 462,
  avg_cost: 277.28,
  cost_total: 128103.36,
  last_close: 329,
  last_close_date: '2026-11-20',
  prev_close: 328.24,
  prev_close_date: '2026-11-19',
  market_value: 151998,
  weight: 0.271,
  pnl: 23894.64,
  pnl_pct: 18.65,
  day_change: 351.12,
  day_change_pct: 0.23,
  badge: 'HOLDING',
}

const overview = {
  available: true,
  positions: [position],
  totals: { value: 560018.25, cost: 495386.81, pnl: 64631.44, pnl_pct: 13.05, day_change: -2717.67, day_change_pct: -0.48, positions: 1, unvalued: ['XYZ'] },
  allocation: {
    by_instrument: [{ key: 'THYAO', label: 'THYAO', value: 151998, weight: 1 }],
    by_kind: [{ key: 'stock', label: 'stock', value: 151998, weight: 1 }],
    by_sector: [],
  },
  transactions: [],
  pending_actions: [],
  watchlists: [{ id: 'w1', name: 'Bankalar', members: [{ instrument_id: 'i2', ticker: 'GARAN', name: 'Garanti', badge: 'WATCH' }], sectors: [], indices: [] }],
}

const instrument = {
  ticker: 'THYAO',
  name: 'TÜRK HAVA YOLLARI A.O.',
  kind: 'stock',
  instrument: { id: 'i1', type: 'equity', currency: 'TRY', status: 'active' },
  issuer: null,
  badge: 'HOLDING',
  watch: false,
  watchlists: [],
  indices: ['XU100'],
  identifiers: [],
  share_capital: {
    paid_in_capital: null, registered_capital_ceiling: null, paid_in_capital_as_of: null, shares_outstanding: null,
    shares_as_of: null, free_float_shares: null, free_float_pct: null, market_cap: null,
  },
  last_close: { date: '2026-11-20', close: 329, prev_close: 328.24, day_change: 0.76, day_change_pct: 0.23 },
  position,
  disclosures: [item('e1', { headline: 'THYAO: İçeriden işlem' })],
  theses: [],
  agent_runs: [],
}

const prices = {
  ticker: 'THYAO',
  from: '2025-11-22',
  to: '2026-11-22',
  primary_source: 'x',
  bars: [{ date: '2026-11-20', open: null, high: null, low: null, close: 329, volume: null, source: 'x' }],
  markers_disclosures: [],
  markers_txns: [],
}

const search = {
  q: 'thy',
  instruments: [{ instrument_id: 'i1', ticker: 'THYAO', name: 'TÜRK HAVA YOLLARI A.O.', kind: 'stock', badge: 'HOLDING', score: 90 }],
  disclosures: [{ disclosure_id: 'd1', event_id: 'e1', headline: 'THYAO bildirimi', snippet: '', source_ref: null, kap_url: null, published_at: null, priority: 'P1', tickers: ['THYAO'] }],
}

function mockApi(overrides: Record<string, () => Response> = {}) {
  const fetchMock = vi.fn(async (req: Request | string) => {
    const url = new URL(typeof req === 'string' ? req : req.url)
    const path = url.pathname
    for (const [prefix, fn] of Object.entries(overrides)) if (path === prefix) return fn()
    if (path === '/v1/status') return Response.json(baseStatus)
    if (path === '/v1/today') return Response.json(today)
    if (path === '/v1/portfolio/overview') return Response.json(overview)
    if (path === '/v1/portfolio/performance') return Response.json({ range: '1m', method: 'transactions_x_closes', start: null, end: null, points: [], benchmark: [], unpriced: [] })
    if (path === '/v1/instruments/THYAO') return Response.json(instrument)
    if (path === '/v1/instruments/THYAO/prices') return Response.json(prices)
    if (path.startsWith('/v1/instruments/')) return Response.json({ detail: 'unknown ticker' }, { status: 404 })
    if (path === '/v1/search') return Response.json(search)
    return Response.json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  mockApi()
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Today', () => {
  it('renders the attention list and persists mark-as-seen', async () => {
    const user = userEvent.setup()
    const first = await renderApp('/')
    expect(await screen.findByTestId('attention-unseen')).toHaveTextContent('2')
    expect(screen.getByTestId('market-strip')).toHaveTextContent('11.131,32')
    expect(screen.getByTestId('dead-lane')).toHaveTextContent('boom')

    const list = screen.getByRole('list', { name: 'Dikkat gerektiren bildirimler' })
    const [firstItem] = within(list).getAllByRole('listitem')
    await user.click(within(firstItem!).getByRole('button', { name: 'Okundu yap' }))
    expect(screen.getByTestId('attention-unseen')).toHaveTextContent('1')
    expect(JSON.parse(localStorage.getItem('papi-seen-attention')!)).toEqual(['e1'])

    first.unmount()
    await renderApp('/')
    expect(await screen.findByTestId('attention-unseen')).toHaveTextContent('1')

    await user.click(screen.getByRole('button', { name: /Hepsini okundu yap/ }))
    await waitFor(() => expect(screen.queryByTestId('attention-unseen')).not.toBeInTheDocument())
    expect(JSON.parse(localStorage.getItem('papi-seen-attention')!)).toEqual(['e1', 'e2'])
  })

  it('shows an error with retry', async () => {
    vi.unstubAllGlobals()
    mockApi({ '/v1/today': () => new Response('{}', { status: 500 }) })
    await renderApp('/')
    expect(await screen.findByRole('button', { name: 'Yeniden dene' })).toBeInTheDocument()
  })
})

describe('Portfolio', () => {
  it('renders totals and a holdings row with Turkish number formatting', async () => {
    await renderApp('/portfolio')
    const totals = await screen.findByTestId('portfolio-totals')
    await waitFor(() => expect(totals).toHaveTextContent('560.018,25'))
    expect(totals).toHaveTextContent('495.386,81')
    const holdings = screen.getByTestId('holdings')
    const row = await within(holdings).findByText('TÜRK HAVA YOLLARI A.O.')
    const line = row.closest('[role="row"]')!
    expect(line).toHaveTextContent('462')
    expect(line).toHaveTextContent('277,28')
    expect(line).toHaveTextContent('151.998,00')
    expect(line).toHaveTextContent('27,1')
    expect(screen.getByTestId('unpriced-note')).toHaveTextContent('XYZ')
    expect(screen.getByText('Bankalar')).toBeInTheDocument()
    expect(document.title).toBe('Portföy · papi')
  })
})

describe('Instrument', () => {
  it('renders the header and the disclosures tab', async () => {
    await renderApp('/i/THYAO')
    const header = await screen.findByTestId('instrument-header')
    await waitFor(() => expect(header).toHaveTextContent('TÜRK HAVA YOLLARI A.O.'))
    expect(header).toHaveTextContent('PORTFÖYDE')
    expect(screen.getByTestId('last-close')).toHaveTextContent('329,00')
    const list = await screen.findByTestId('disclosure-list')
    expect(within(list).getByText('THYAO: İçeriden işlem')).toBeInTheDocument()
  })

  it('shows a 404 state for an unknown ticker', async () => {
    await renderApp('/i/NOPE')
    expect(await screen.findByText('NOPE bulunamadı')).toBeInTheDocument()
  })
})

describe('Command palette search', () => {
  it('shows instrument results and navigates on Enter', async () => {
    const user = userEvent.setup()
    const { router } = await renderApp('/')
    await screen.findByTestId('page-title')
    await user.keyboard('{Control>}k{/Control}')
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByPlaceholderText('Bir komut veya sayfa yaz…'), 'thy')
    const hit = await within(dialog).findByText('TÜRK HAVA YOLLARI A.O.')
    expect(hit).toBeInTheDocument()
    expect(within(dialog).getByText('THYAO bildirimi')).toBeInTheDocument()
    await user.click(hit)
    await waitFor(() => expect(router.state.location.pathname).toBe('/i/THYAO'))
  })
})
