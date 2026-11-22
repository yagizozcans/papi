import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { baseStatus, renderApp } from './helpers'

function item(n: number, over: Record<string, unknown> = {}) {
  const id = `ev-${n}`
  return {
    id,
    code: 'x.code',
    code_label: `Tür ${n}`,
    family: 'x',
    materiality: 50,
    materiality_band: 'medium',
    priority: 'P2',
    status: 'final',
    stage: 'rules',
    announced_at: `2026-11-20T1${n}:00:00Z`,
    published_key: `2026-11-20T1${n}:00:00.000000Z`,
    published_at: `2026-11-20T1${n}:00:00Z`,
    first_seen_at: `2026-11-20T1${n}:00:30Z`,
    latency_s: 30,
    catchup: false,
    is_late: false,
    revision_kind: null,
    revision_label: null,
    headline: `Başlık ${n}`,
    headline_en: null,
    tickers: [{ instrument_id: null, ticker: `TK${n}`, badge: n === 1 ? 'HOLDING' : null }],
    holding: n === 1,
    watch: false,
    issuers: [{ id: 'i', name: 'Şirket A.Ş.' }],
    disclosure_id: `d${n}`,
    source_ref: `${n}`,
    kap_url: `https://www.kap.org.tr/tr/Bildirim/${n}`,
    ...over,
  }
}

const detail = (n: number) => ({
  item: item(n),
  event: { id: `ev-${n}`, code: 'x.code', taxonomy_version: 'v1', status: 'final', superseded_by: null, payload: { a: { b: 1 }, list: ['x', 'y'] }, payload_source: null, payload_schema: 's', rules: { why: 'kural' }, created_at: '2026-11-20T10:00:00Z' },
  disclosures: [],
  revision_chain: [],
  superseded: [],
  summaries: { tr: { headline: 'Özet başlık', body: 'Özet metni [S1]', created_at: '2026-11-20T10:00:00Z' }, en: null },
  analyses: [
    { id: 'a1', agent: 'analyst', backend: 'x', model_ids: ['m'], prompt_version: null, created_at: '2026-11-20T10:00:00Z', review_state: 'auto', confidence: 0.8, latency_ms: 1, cost_usd_est: 0, output: null,
      claims: [{ id: 'c1', kind: 'fact', text_tr: 'İddia metni [S1]', text_en: null, value: null, unit: null, check_status: null, verify_status: null, citations: [{ target_type: 'span', target_id: 's', quote: 'alıntı', anchor: 'd1s1' }] }] },
  ],
  key_facts: [{ path: 'tutar', value: 12, anchors: ['d1s1'], provenance: null }],
  documents: [{ index: 0, id: 'doc1', kind: 'disclosure_body', mime: 'text/html', filename: null, byte_size: 10, raw_sha256: 'abc', raw_url: null, download_url: '/raw/abc', template_id: null, text_length: 5, html: '<p><a id="d1s1">metin</a></p>', spans: [], unplaced_span_ids: [] }],
  forecasts: [],
  for_me: { holding: true, positions: [{ ticker: 'TK1', quantity: 10, avg_cost: 5, last_close: 6, last_close_date: null, market_value: 60, weight: 0.1 }], impacts: [], reason: null },
  feedback_counts: { helpful: 1 },
})

const feed = (items: unknown[], next: string | null = null) => ({ items, next_cursor: next, facets: { families: [{ code: 'x', label: 'Aile X' }] } })

function setup(handler: (url: URL, req: Request) => Response | Promise<Response | undefined> | undefined) {
  const calls: URL[] = []
  const fetchMock = vi.fn(async (input: Request | string) => {
    const req = typeof input === 'string' ? new Request(input) : input
    const url = new URL(req.url)
    calls.push(url)
    if (url.pathname === '/v1/status') return Response.json(baseStatus)
    return (await handler(url, req)) ?? Response.json({ detail: 'yok' }, { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('feed', () => {
  it('renders rows from FeedOut and keeps loading older pages by cursor', async () => {
    const { calls } = setup((url) => {
      if (url.pathname === '/v1/feed') return Response.json(url.searchParams.get('before') ? feed([item(3)]) : feed([item(2), item(1)], 'CUR'))
    })
    await renderApp('/feed')
    expect(await screen.findByText('Başlık 2')).toBeInTheDocument()
    expect(screen.getByText('Başlık 1')).toBeInTheDocument()
    expect(screen.getAllByRole('option').length).toBeGreaterThanOrEqual(2)
    await userEvent.click(await screen.findByRole('button', { name: 'Daha fazla yükle' }).catch(() => screen.getByText('Başlık 1')))
    await waitFor(() => expect(calls.some((u) => u.searchParams.get('before') === 'CUR')).toBe(true))
  })

  it('filter change updates search params and refetches', async () => {
    const { calls } = setup((url) => (url.pathname === '/v1/feed' ? Response.json(feed([item(1)])) : undefined))
    const { router } = await renderApp('/feed')
    await screen.findByText('Başlık 1')
    await userEvent.click(screen.getByRole('button', { name: 'P1' }))
    await waitFor(() => expect(router.state.location.search).toMatchObject({ priority: 'P1' }))
    await waitFor(() => expect(calls.some((u) => u.pathname === '/v1/feed' && u.searchParams.get('priority') === 'P1')).toBe(true))
  })

  it('selecting a row opens /feed/$id with the detail sections', async () => {
    setup((url) => {
      if (url.pathname === '/v1/feed') return Response.json(feed([item(1)]))
      if (url.pathname === '/v1/events/ev-1/detail') return Response.json(detail(1))
    })
    const { router } = await renderApp('/feed')
    await userEvent.click(await screen.findByText('Başlık 1'))
    await waitFor(() => expect(router.state.location.pathname).toBe('/feed/ev-1'))
    const reader = await screen.findByRole('region', { name: 'Bildirim okuyucu' })
    expect(await within(reader).findByRole('heading', { level: 1, name: 'Başlık 1' })).toBeInTheDocument()
    for (const name of ['Senin için', 'Özet', 'Temel bilgiler', 'Analiz', 'Belgeler']) {
      expect(within(reader).getByRole('region', { name })).toBeInTheDocument()
    }
    expect(within(reader).getByRole('button', { name: 'Ajan çalıştır' })).toBeEnabled()
    expect(within(reader).getByText('metin')).toBeInTheDocument()
  })

  it('reader "Ajan çalıştır" opens the run dialog prefilled with the disclosure and POSTs', async () => {
    const posts: unknown[] = []
    setup(async (url, req) => {
      if (url.pathname === '/v1/feed') return Response.json(feed([item(1)]))
      if (url.pathname === '/v1/events/ev-1/detail') return Response.json(detail(1))
      if (url.pathname === '/v1/agents')
        return Response.json({ agents: [{ name: 'kap-analyst', description: 'd', version: 'v', model: 'sonnet', max_turns: 14, max_budget_usd: 0.6, timeout_s: 600, priority: 'P2', skills: [], tools: [], builtin_tools: [], ref_kind: 'disclosure' }] })
      if (url.pathname === '/v1/agent-runs' && req.method === 'POST') {
        posts.push(await req.clone().json())
        return Response.json({ run_id: 'run-9', agent: 'kap-analyst', ref: '1', max_budget_usd: 0.6 }, { status: 202 })
      }
    })
    const { router } = await renderApp('/feed/ev-1')
    const reader = await screen.findByRole('region', { name: 'Bildirim okuyucu' })
    await userEvent.click(await within(reader).findByRole('button', { name: 'Ajan çalıştır' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('textbox')).toHaveValue('1')
    expect(within(dialog).getByRole('combobox')).toBeDisabled()
    await userEvent.click(await within(dialog).findByRole('button', { name: 'Başlat' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/research/agents/runs/run-9'))
    expect(posts).toEqual([{ agent: 'kap-analyst', ref: '1' }])
  })

  it('shows a 404 state for an unknown event', async () => {
    setup((url) => (url.pathname === '/v1/feed' ? Response.json(feed([item(1)])) : undefined))
    await renderApp('/feed/nope')
    expect(await screen.findByText('Bildirim bulunamadı')).toBeInTheDocument()
  })

  it('holds live items behind the pill while a row is selected', async () => {
    let polled = 0
    setup((url) => {
      if (url.pathname === '/v1/events/ev-1/detail') return Response.json(detail(1))
      if (url.pathname !== '/v1/feed') return undefined
      if (url.searchParams.get('after')) {
        polled++
        return Response.json(feed([item(5), item(1)]))
      }
      return Response.json(feed([item(1)]))
    })
    await renderApp('/feed/ev-1')
    await screen.findByText('Başlık 1')
    await waitFor(() => expect(polled).toBeGreaterThan(0), { timeout: 8000 })
    const pill = await screen.findByRole('button', { name: /1 yeni bildirim/ })
    expect(screen.queryByText('Başlık 5')).not.toBeInTheDocument()
    await act(async () => {
      await userEvent.click(pill)
    })
    expect(await screen.findByText('Başlık 5')).toBeInTheDocument()
  }, 15000)

  it('feedback POST bumps the count', async () => {
    let body: unknown
    setup(async (url, req) => {
      if (url.pathname === '/v1/feed') return Response.json(feed([item(1)]))
      if (url.pathname === '/v1/events/ev-1/detail') return Response.json(detail(1))
      if (url.pathname === '/v1/events/ev-1/feedback' && req.method === 'POST') {
        body = await req.json()
        return Response.json({ event_id: 'ev-1', feedback_counts: { helpful: 2 } })
      }
    })
    await renderApp('/feed/ev-1')
    const btn = await screen.findByRole('button', { name: /Faydalı/ })
    expect(btn).toHaveTextContent('(1)')
    await userEvent.click(btn)
    await waitFor(() => expect(body).toMatchObject({ kind: 'helpful' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Faydalı/ })).toHaveTextContent('(2)'))
  })
})
