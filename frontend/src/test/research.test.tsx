import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { baseStatus, renderApp } from './helpers'

type Handler = (url: URL, req: Request) => Response | Promise<Response | undefined> | undefined

function setup(handler: Handler) {
  const calls: { url: URL; method: string; req: Request; body?: BodyInit | null }[] = []
  const fetchMock = vi.fn(async (input: Request | string, init?: RequestInit) => {
    // A plain `fetch(url, { body: FormData })` is kept as is: jsdom's FormData does not round-trip through Request.
    const req = typeof input === 'string' ? new Request(new URL(input, 'http://localhost'), { method: init?.method }) : input
    const url = new URL(req.url)
    calls.push({ url, method: req.method, req, body: init?.body })
    if (url.pathname === '/v1/status') return Response.json(baseStatus)
    return (await handler(url, req)) ?? Response.json({ detail: 'yok' }, { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

const AGENTS = [
  {
    name: 'kap-analyst', description: 'KAP bildirimini analiz eder', version: 'v1abc', model: 'sonnet', max_turns: 14, max_budget_usd: 0.6, timeout_s: 600, priority: 'P2',
    skills: [{ name: 'kap-disclosure-analysis', description: 'd', tools: ['read_kap'] }], tools: ['read_kap', 'search_kap'], builtin_tools: ['WebSearch'], ref_kind: 'disclosure',
  },
  {
    name: 'strategy-analyst', description: 'Tezi sınar', version: 'v2', model: 'sonnet', max_turns: 40, max_budget_usd: 1.5, timeout_s: 1200, priority: 'P2',
    skills: [{ name: 'thesis-review', description: 'd', tools: [] }], tools: [], builtin_tools: [], ref_kind: 'thesis',
  },
]

const runRow = (id: string, over: Record<string, unknown> = {}) => ({
  run_id: id, agent: 'kap-analyst', subject: '1691891', status: 'ok', active: false, started_at: '2026-11-22T09:00:00Z', finished_at: '2026-11-22T09:01:00Z',
  elapsed_s: 60, cost_usd: 0.1234, max_budget_usd: 0.6, turns: 6, error: null, verdict: null, ...over,
})

const runDetail = (id: string, over: Record<string, unknown> = {}) => ({
  ...runRow(id),
  agent_version: 'abc123', max_turns: 14, gate: null, resumed: false,
  usage: { input_tokens: 10, output_tokens: 20, cache_read_input_tokens: 12345, cache_creation_input_tokens: 678 },
  calls: [
    { at: '2026-11-22T09:00:05Z', tool: 'use_skill', args: { name: 'kap' }, ms: 3, ok: true, blocking: null, result_excerpt: "{'success': True}" },
    { at: '2026-11-22T09:00:09Z', tool: 'read_kap_text', args: { no: '1691891', offset: 0 }, ms: 410, ok: false, blocking: 'metnin kalanını oku', result_excerpt: "{'success': False, 'text': '<500 karakter>'}" },
  ],
  output: {
    headline_tr: 'Şirket temettü dağıtacak', event_codes: ['dividend'], materiality: 70, fundamental_direction: 'positive', confidence: 0.82,
    summary_tr: 'Özet metni', summary_en: 'Summary text',
    positives: [{ text_tr: 'Nakit temettü artıyor', span_ordinals: [2] }], negatives: [{ text_tr: 'Pay başına kâr düşük', span_ordinals: [5] }],
    materiality_rationale: 'Gerekçe', watch_next: ['Genel kurul tarihi'], claims: [{ kind: 'fact', text_tr: 'Brüt temettü 2 TL', value: 2, unit: 'TL', span_ordinals: [2], citation_ids: [] }],
  },
  text: null,
  ...over,
})

describe('agents', () => {
  it('lists the catalogue and runs, and filters by agent', async () => {
    const { calls } = setup((url) => {
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
      if (url.pathname === '/v1/agent-runs') return Response.json({ items: [runRow('r-1'), runRow('r-2', { status: 'failed', error: 'boom' })], total: 2, limit: 25, offset: 0 })
    })
    await renderApp('/research/agents')
    const cards = await screen.findAllByTestId('agent-card')
    expect(cards).toHaveLength(2)
    expect(within(cards[0]!).getByText('kap-analyst')).toBeInTheDocument()
    expect(within(cards[0]!).getByText('en fazla 0,60 USD')).toBeInTheDocument()
    const table = await screen.findByTestId('runs-table')
    expect(within(table).getAllByTestId('run-status').map((e) => e.getAttribute('data-status'))).toEqual(['ok', 'failed'])
    await userEvent.selectOptions(screen.getByLabelText('Ajan'), 'strategy-analyst')
    await waitFor(() => expect(calls.some((c) => c.url.pathname === '/v1/agent-runs' && c.url.searchParams.get('agent') === 'strategy-analyst')).toBe(true))
  })

  it('renders a run with tool calls, meter and the green/red takeaways', async () => {
    setup((url) => {
      if (url.pathname === '/v1/agent-runs/r-1') return Response.json(runDetail('r-1'))
    })
    await renderApp('/research/agents/runs/r-1')
    const calls = await screen.findAllByTestId('run-call')
    expect(calls).toHaveLength(2)
    expect(within(screen.getByTestId('run-page')).getByRole('heading', { level: 1 })).toHaveTextContent('kap-analyst')
    expect(within(calls[0]!).getByText('use_skill')).toBeInTheDocument()
    expect(within(calls[1]!).getByText('read_kap_text')).toBeInTheDocument()
    expect(within(calls[1]!).getByText(/metnin kalanını oku/)).toBeInTheDocument()
    const meter = screen.getByTestId('run-meter')
    expect(within(meter).getByText(/0,1234/)).toBeInTheDocument()
    expect(screen.getByTestId('run-tokens')).toHaveTextContent('cache okuma 12.345')
    expect(within(screen.getByTestId('takeaways-positive')).getByText('Nakit temettü artıyor')).toBeInTheDocument()
    expect(within(screen.getByTestId('takeaways-negative')).getByText('Pay başına kâr düşük')).toBeInTheDocument()
    expect(screen.getByText('Genel kurul tarihi')).toBeInTheDocument()
    expect(screen.getByText('Brüt temettü 2 TL')).toBeInTheDocument()
  })

  it('polls an active run every 2 seconds until it finishes', async () => {
    let n = 0
    const { calls } = setup((url) => {
      if (url.pathname !== '/v1/agent-runs/r-live') return undefined
      n++
      return Response.json(n === 1 ? runDetail('r-live', { status: 'running', active: true, calls: [], output: null, cost_usd: null }) : runDetail('r-live'))
    })
    await renderApp('/research/agents/runs/r-live')
    expect(await screen.findByText('Henüz araç çağrısı yok; ajan başlıyor…')).toBeInTheDocument()
    expect(await screen.findAllByTestId('run-call', {}, { timeout: 5000 })).toHaveLength(2)
    expect(calls.filter((c) => c.url.pathname === '/v1/agent-runs/r-live').length).toBe(2)
    await new Promise((r) => setTimeout(r, 2300))
    expect(calls.filter((c) => c.url.pathname === '/v1/agent-runs/r-live').length).toBe(2) // finished: polling stopped
  }, 15_000)

  it('shows an error notice for a failed run and 404 for an unknown run', async () => {
    setup((url) => {
      if (url.pathname === '/v1/agent-runs/r-bad') return Response.json(runDetail('r-bad', { status: 'failed', error: 'Ertelendi: limit', output: null, calls: [] }))
    })
    await renderApp('/research/agents/runs/r-bad')
    expect(await screen.findByText('Ertelendi: limit')).toBeInTheDocument()
    await renderApp('/research/agents/runs/nope')
    expect(await screen.findByText('Ajan çalışması bulunamadı')).toBeInTheDocument()
  })

  it('run dialog POSTs agent + ref, shows the budget and opens the run page', async () => {
    const { calls } = setup((url, req) => {
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
      if (url.pathname === '/v1/agent-runs' && req.method === 'POST') return Response.json({ run_id: 'new-run', agent: 'kap-analyst', ref: '1691891', max_budget_usd: 0.6 }, { status: 202 })
      if (url.pathname === '/v1/agent-runs/new-run') return Response.json(runDetail('new-run', { status: 'starting', active: true, calls: [], output: null }))
      if (url.pathname === '/v1/agent-runs') return Response.json({ items: [], total: 0, limit: 25, offset: 0 })
    })
    const { router } = await renderApp('/research/agents')
    await userEvent.click(await screen.findByRole('button', { name: 'Ajan çalıştır: kap-analyst' }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByTestId('run-budget')).toHaveTextContent('En fazla 0,60 USD · en çok 14 tur')
    const start = within(dialog).getByRole('button', { name: 'Başlat' })
    expect(start).toBeDisabled() // no subject yet
    await userEvent.type(within(dialog).getByRole('textbox'), '1691891')
    await userEvent.click(start)
    await waitFor(() => expect(router.state.location.pathname).toBe('/research/agents/runs/new-run'))
    const post = calls.find((c) => c.method === 'POST')!
    expect(await post.req.clone().json()).toEqual({ agent: 'kap-analyst', ref: '1691891' })
  })
})

const GOOD = '---\nticker: THYAO\n---\n\n## Fikir\nmetin\n'
const thesisRow = (over: Record<string, unknown> = {}) => ({
  name: 'THYAO-2026-11-06', ticker: 'THYAO', date: '2026-11-06', direction: 'long', horizon: '6-12 ay', status: 'manual', active: false,
  error_count: 0, gap_count: 3, reviews: 1, last_review: { run_id: 'sr-1', status: 'ok', verdict: 'mixed', confidence: 0.55, started_at: '2026-11-21T10:00:00Z', cost_usd: 0.9 }, files: 0, ...over,
})
const thesisDetail = (over: Record<string, unknown> = {}) => ({
  ...thesisRow(),
  text: GOOD,
  check: { ok: true, errors: [], gaps: ["'Konsensüs' bölümü boş veya yok"], sections_filled: 1, sections_total: 10, triggers: 0, kill_signals: 0 },
  outline: [{ line: 5, title: 'Fikir', key: 'fikir', required: true, items: 1, empty: false }],
  warnings: [], status_error: null, files_done: null, pages_done: null, run_id: null, source_files: [], pages: [], ...over,
})

describe('theses', () => {
  it('lists theses with parse state and the last review verdict', async () => {
    setup((url) => {
      if (url.pathname === '/v1/theses') return Response.json({ items: [thesisRow(), thesisRow({ name: 'AKBNK-2026-11-01', error_count: 2, gap_count: 0, last_review: null, reviews: 0 })], accept: ['.pdf', '.txt'] })
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
    })
    await renderApp('/research/theses')
    const table = await screen.findByTestId('theses-table')
    expect(within(table).getByText('THYAO-2026-11-06')).toBeInTheDocument()
    expect(within(table).getByText('3 eksik')).toBeInTheDocument()
    expect(within(table).getByText('2 hata')).toBeInTheDocument()
    expect(within(table).getByText('karışık')).toBeInTheDocument()
  })

  it('upload sends a multipart POST with the files in order', async () => {
    const { calls } = setup((url, req) => {
      if (url.pathname === '/v1/theses/upload' && req.method === 'POST') return Response.json({ name: 'THYAO-2026-11-22', files: ['01-a.txt', '02-b.txt'] }, { status: 202 })
      if (url.pathname === '/v1/theses/THYAO-2026-11-22') return Response.json(thesisDetail({ name: 'THYAO-2026-11-22', status: 'extracting', active: true, text: null, check: null, outline: [], files_done: 1, pages_done: 2, source_files: [{ name: '01-a.txt', size: 3, url: '/v1/theses/x/files/01-a.txt' }, { name: '02-b.txt', size: 3, url: '/v1/theses/x/files/02-b.txt' }] }))
      if (url.pathname === '/v1/theses') return Response.json({ items: [], accept: ['.pdf', '.txt'] })
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
      if (url.pathname.endsWith('/reviews')) return Response.json([])
    })
    const { router } = await renderApp('/research/theses')
    const input = await screen.findByTestId('upload-input')
    await userEvent.upload(input, [new File(['a'], 'a.txt', { type: 'text/plain' }), new File(['b'], 'b.txt', { type: 'text/plain' })])
    expect(await screen.findAllByText(/\.txt$/)).toHaveLength(2)
    await userEvent.type(screen.getByPlaceholderText('THYAO'), 'thyao')
    await userEvent.click(screen.getByRole('button', { name: 'Yükle' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/research/theses/THYAO-2026-11-22'))
    const post = calls.find((c) => c.method === 'POST' && c.url.pathname === '/v1/theses/upload')!
    const form = post.body as FormData
    expect(form).toBeInstanceOf(FormData)
    expect((form.getAll('files') as File[]).map((f) => f.name)).toEqual(['a.txt', 'b.txt'])
    expect(form.get('ticker')).toBe('thyao')
    expect(form.get('run')).toBe('false')
    expect(await screen.findByTestId('import-progress')).toHaveTextContent('1/2 dosya · 2 sayfa okundu')
  })

  it('rejects an unsupported file type before uploading', async () => {
    const { calls } = setup((url) => {
      if (url.pathname === '/v1/theses') return Response.json({ items: [], accept: ['.pdf', '.txt'] })
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
    })
    await renderApp('/research/theses')
    await userEvent.upload(await screen.findByTestId('upload-input'), new File(['x'], 'a.exe'), { applyAccept: false })
    expect(await screen.findByRole('alert')).toHaveTextContent('a.exe: desteklenmeyen tür')
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('editor saves the text and shows the parse errors with their lines', async () => {
    const { calls } = setup((url, req) => {
      if (url.pathname === '/v1/theses/THYAO-2026-11-06' && req.method === 'GET') return Response.json(thesisDetail())
      if (url.pathname === '/v1/theses/THYAO-2026-11-06/text' && req.method === 'PUT')
        return Response.json(thesisDetail({ text: 'x', error_count: 2, gap_count: 0, check: { ok: false, errors: [{ message: 'direction: long | short | izle olmalı', line: 3 }, { message: "bilinmeyen başlık '## Bilinmeyen'", line: 6 }], gaps: [], sections_filled: 0, sections_total: 10, triggers: 0, kill_signals: 0 }, outline: [{ line: 6, title: 'Bilinmeyen', key: null, required: false, items: null, empty: false }] }))
      if (url.pathname === '/v1/theses/THYAO-2026-11-06/reviews') return Response.json([])
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
    })
    await renderApp('/research/theses/THYAO-2026-11-06')
    const area = await screen.findByRole('textbox', { name: 'Tez metni' })
    expect(await screen.findByText("'Konsensüs' bölümü boş veya yok")).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Kaydet' })
    expect(save).toBeDisabled()
    await userEvent.clear(area)
    await userEvent.type(area, 'x')
    expect(screen.getByText('kaydedilmemiş değişiklik')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'İncele' })).toBeDisabled() // unsaved text cannot be reviewed
    await userEvent.click(save)
    const put = await waitFor(() => {
      const c = calls.find((x) => x.method === 'PUT')
      expect(c).toBeDefined()
      return c!
    })
    expect(await put.req.clone().json()).toEqual({ content: 'x' })
    const check = await screen.findByTestId('thesis-check')
    expect(within(check).getByText("bilinmeyen başlık '## Bilinmeyen'")).toBeInTheDocument()
    expect(within(check).getByRole('button', { name: 'satır 6' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'İncele' })).toBeDisabled() // errors block the review
    expect(screen.getByText('bilinmeyen başlık', { selector: 'span' })).toBeInTheDocument()
  })

  it('review button asks for the budget, POSTs, and renders the latest review output', async () => {
    const { calls } = setup((url, req) => {
      if (url.pathname === '/v1/theses/THYAO-2026-11-06' && req.method === 'GET') return Response.json(thesisDetail())
      if (url.pathname === '/v1/theses/THYAO-2026-11-06/reviews') return Response.json([{ run_id: 'sr-1', status: 'ok', verdict: 'mixed', confidence: 0.55, started_at: '2026-11-21T10:00:00Z', cost_usd: 0.9 }])
      if (url.pathname === '/v1/theses/THYAO-2026-11-06/review' && req.method === 'POST') return Response.json({ run_id: 'sr-2', agent: 'strategy-analyst', ref: 'THYAO-2026-11-06', max_budget_usd: 1.5 }, { status: 202 })
      if (url.pathname === '/v1/agent-runs/sr-1')
        return Response.json(runDetail('sr-1', { agent: 'strategy-analyst', subject: 'THYAO-2026-11-06', output: {
          headline_tr: 'Tez kısmen ayakta', summary_tr: 'özet', verdict: 'mixed', confidence: 0.55, as_of: '2026-11-20', edge: { category: 'analytical', stated_in_thesis: true, accessible_to_individual: 'yes', rationale_tr: 'neden' },
          scenarios: [{ name: 'bull', probability: 0.3, description_tr: 'iyi gider', drivers_tr: [], signpost_tr: 'işaret' }], triggers: [], claims: [], consensus: { implied_by_price_tr: 'fiyat büyümeyi varsayıyor', evidence: [], thesis_section_is_consensus: true, variant_differs: 'yes' },
          counterparty: { who_tr: 'kurumlar', plausible: 'yes', rationale_tr: 'çünkü' }, kill_signals: [{ signal_tr: 'doluluk düşer', metric: 'doluluk', threshold: '%80', current_value: '%85', status: 'clear' }], pre_mortem: [{ reason_tr: 'savaş', likelihood: 'low' }],
          structure: [{ section: 'fikir', status: 'ok', note_tr: 'iyi' }], data_gaps: ['rakip verisi yok'],
        } }))
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
    })
    await renderApp('/research/theses/THYAO-2026-11-06')
    const view = await screen.findByTestId('review-view')
    expect(within(view).getByText('Tez kısmen ayakta')).toBeInTheDocument()
    expect(within(view).getByTestId('review-verdict')).toHaveTextContent('karışık')
    expect(within(view).getByText('fiyat büyümeyi varsayıyor')).toBeInTheDocument()
    expect(within(view).getByText('rakip verisi yok')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'İncele' }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByTestId('review-budget')).toHaveTextContent('En fazla 1,50 USD · en çok 40 tur')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Çalıştır' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.pathname === '/v1/theses/THYAO-2026-11-06/review')).toBe(true))
  })

  it('creates a new thesis from the template and opens it', async () => {
    const { calls } = setup((url, req) => {
      if (url.pathname === '/v1/theses' && req.method === 'POST') return Response.json(thesisDetail({ name: 'GARAN-2026-11-22' }), { status: 201 })
      if (url.pathname === '/v1/theses') return Response.json({ items: [], accept: ['.pdf'] })
      if (url.pathname === '/v1/theses/GARAN-2026-11-22') return Response.json(thesisDetail({ name: 'GARAN-2026-11-22' }))
      if (url.pathname.endsWith('/reviews')) return Response.json([])
      if (url.pathname === '/v1/agents') return Response.json({ agents: AGENTS })
    })
    const { router } = await renderApp('/research/theses')
    await userEvent.click(await screen.findByRole('button', { name: 'Yeni tez' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.type(within(dialog).getByRole('textbox'), 'garan')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Oluştur' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/research/theses/GARAN-2026-11-22'))
    const post = calls.find((c) => c.method === 'POST')!
    expect(await post.req.clone().json()).toEqual({ ticker: 'GARAN' })
  })
})

describe('research index', () => {
  it('shows the three overview cards', async () => {
    setup((url) => {
      if (url.pathname === '/v1/theses') return Response.json({ items: [thesisRow()], accept: [] })
      if (url.pathname === '/v1/agent-runs') return Response.json({ items: [runRow('r-1', { active: true, status: 'running' })], total: 7, limit: 25, offset: 0 })
    })
    await renderApp('/research')
    await waitFor(() => expect(screen.getByTestId('research-card-theses')).toHaveTextContent('1 tez'))
    await waitFor(() => expect(screen.getByTestId('research-card-agents')).toHaveTextContent('7 çalışma'))
    expect(screen.getByTestId('research-card-agents')).toHaveTextContent('1 çalışıyor')
    expect(screen.getByTestId('research-card-kb')).toHaveTextContent('Yakında (P3)')
  })
})
