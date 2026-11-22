import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import type { ColumnDef } from '@tanstack/react-table'
import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DataTable } from '@/components/kit/data-table'
import { Delta } from '@/components/kit/delta'
import { DocumentView } from '@/components/kit/document-view'
import { FilterBar } from '@/components/kit/filter-bar'
import { NewItemsPill } from '@/components/kit/new-items-pill'
import { PriorityBadge } from '@/components/kit/priority-badge'
import { RelativeTime } from '@/components/kit/relative-time'
import { SplitView } from '@/components/kit/split-view'
import { Stat } from '@/components/kit/stat'
import { TickerChip } from '@/components/kit/ticker-chip'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { useHeldItems } from '@/hooks/use-held-items'
import { livePollInterval, useLivePoll } from '@/hooks/use-live-poll'
import i18n from '@/i18n'
import { getColorBlind, toggleColorBlind } from '@/lib/color-blind'
import { formatMoney, formatNum, formatPercent, relativeParts } from '@/lib/format'

beforeEach(async () => {
  await i18n.changeLanguage('tr')
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.documentElement.removeAttribute('data-cb')
})

describe('format', () => {
  it('formats Turkish and English numbers', () => {
    expect(formatMoney(1234.56, { lang: 'tr' })).toBe('1.234,56 ₺')
    expect(formatMoney(1234.56, { lang: 'en' })).toBe('₺1,234.56')
    expect(formatMoney(-5, { lang: 'en' })).toBe('-₺5.00')
    expect(formatNum(1234567.891, { decimals: 0, lang: 'tr' })).toBe('1.234.568')
    expect(formatPercent(4.2, { signed: true, lang: 'tr' })).toBe('%+4,20')
    expect(formatPercent(4.2, { signed: true, lang: 'en' })).toBe('+4.20%')
    expect(formatNum(null)).toBe('–')
  })

  it('follows the active i18n language by default', async () => {
    expect(formatNum(1234.5)).toBe('1.234,50')
    await i18n.changeLanguage('en')
    expect(formatNum(1234.5)).toBe('1,234.50')
  })

  it('splits ages into relative units', () => {
    expect(relativeParts(3)).toEqual({ unit: 'now', n: 0 })
    expect(relativeParts(180)).toEqual({ unit: 'm', n: 3 })
    expect(relativeParts(7200)).toEqual({ unit: 'h', n: 2 })
    expect(relativeParts(3 * 86400)).toEqual({ unit: 'd', n: 3 })
  })
})

describe('primitives', () => {
  it('Delta shows sign, direction and percent', () => {
    const { container } = render(<Delta value={-1.5} percent />)
    expect(container.firstElementChild).toHaveAttribute('data-dir', 'down')
    expect(container).toHaveTextContent('%-1,50')
    const up = render(<Delta value={2} />)
    expect(up.container.firstElementChild).toHaveAttribute('data-dir', 'up')
    expect(up.container).toHaveTextContent('+2,00')
  })

  it('toggles colour-blind mode via data-cb on <html>', () => {
    expect(getColorBlind()).toBe(false)
    toggleColorBlind()
    expect(document.documentElement).toHaveAttribute('data-cb', '1')
    expect(localStorage.getItem('papi-cb')).toBe('1')
    toggleColorBlind()
    expect(document.documentElement).not.toHaveAttribute('data-cb')
  })

  it('PriorityBadge renders its label', () => {
    render(<PriorityBadge priority="P2" />)
    expect(screen.getByText('P2')).toHaveAttribute('data-priority', 'P2')
  })

  it('RelativeTime prints Turkish / English text with an Istanbul absolute title', async () => {
    const iso = '2026-11-22T10:00:00Z'
    const now = Date.parse(iso) + 180_000
    const { rerender } = render(<RelativeTime iso={iso} now={now} />)
    const el = screen.getByText('3 dk önce')
    expect(el).toHaveAttribute('title', expect.stringContaining('13:00'))
    await act(() => i18n.changeLanguage('en'))
    rerender(<RelativeTime iso={iso} now={now} />)
    expect(screen.getByText('3m ago')).toBeInTheDocument()
  })

  it('Stat renders value, delta and sparkline', () => {
    const { container } = render(<Stat label="XU100" value="10.000" delta={1.2} sparkline={[1, 2, 3]} />)
    expect(screen.getByText('XU100')).toBeInTheDocument()
    expect(container.querySelector('svg path')).toBeTruthy()
  })

  it('EmptyState and ErrorState (retry)', async () => {
    const onRetry = vi.fn()
    render(
      <>
        <EmptyState />
        <ErrorState onRetry={onRetry} />
      </>,
    )
    expect(screen.getByText('Veri yok')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Yeniden dene' }))
    expect(onRetry).toHaveBeenCalled()
  })

  it('TickerChip links to the instrument page and shows its marker', async () => {
    const root = createRootRoute({ component: () => <TickerChip ticker="THYAO" mark="holding" /> })
    const inst = createRoute({ getParentRoute: () => root, path: '/i/$ticker' })
    const router = createRouter({ routeTree: root.addChildren([inst]), history: createMemoryHistory({ initialEntries: ['/'] }) })
    render(<RouterProvider router={router} />)
    const link = await screen.findByRole('link', { name: /THYAO/ })
    expect(link).toHaveAttribute('href', '/i/THYAO')
    expect(within(link).getByText('Portföyde')).toBeInTheDocument()
  })
})

interface Row {
  id: string
  name: string
  qty: number
}
const columns: ColumnDef<Row>[] = [
  { accessorKey: 'name', header: 'Name', size: 160, meta: { grow: true } },
  { accessorKey: 'qty', header: 'Qty', size: 80, meta: { numeric: true } },
]
const makeRows = (n: number): Row[] => Array.from({ length: n }, (_, i) => ({ id: `r${i}`, name: `Item ${i}`, qty: (i * 7) % 13 }))

describe('DataTable', () => {
  it('virtualises 1k+ rows', () => {
    render(<DataTable data={makeRows(2000)} columns={columns} getRowId={(r) => r.id} />)
    const n = screen.getAllByRole('row').length
    expect(n).toBeGreaterThan(5)
    expect(n).toBeLessThan(80)
  })

  it('aligns numeric columns right in mono', () => {
    render(<DataTable data={makeRows(3)} columns={columns} getRowId={(r) => r.id} />)
    const cell = screen.getAllByRole('cell').find((c) => c.className.includes('num'))
    expect(cell).toHaveClass('text-right')
  })

  it('supports keyboard selection and activation', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const onActivate = vi.fn()
    const { rerender } = render(<DataTable data={makeRows(5)} columns={columns} getRowId={(r) => r.id} onSelect={onSelect} onActivate={onActivate} selectedId="r1" />)
    screen.getByRole('grid').focus()
    await user.keyboard('j')
    expect(onSelect).toHaveBeenLastCalledWith('r2', expect.objectContaining({ id: 'r2' }))
    await user.keyboard('k')
    expect(onSelect).toHaveBeenLastCalledWith('r0', expect.objectContaining({ id: 'r0' }))
    await user.keyboard('{ArrowDown}')
    expect(onSelect).toHaveBeenLastCalledWith('r2', expect.anything())
    await user.keyboard('{Enter}')
    expect(onActivate).toHaveBeenLastCalledWith('r1', expect.anything())
    rerender(<DataTable data={makeRows(5)} columns={columns} getRowId={(r) => r.id} selectedId="r1" />)
    expect(screen.getByText('Item 1').closest('[role=row]')).toHaveAttribute('aria-selected', 'true')
  })

  it('selects on click and sorts by header', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(<DataTable data={makeRows(6)} columns={columns} getRowId={(r) => r.id} onSelect={onSelect} />)
    await user.click(screen.getByText('Item 3'))
    expect(onSelect).toHaveBeenCalledWith('r3', expect.anything())
    await user.click(screen.getByRole('button', { name: 'Qty' }))
    expect(screen.getByRole('columnheader', { name: 'Qty' })).toHaveAttribute('aria-sort', 'ascending')
    const first = screen.getAllByRole('row')[1]!
    expect(within(first).getByText('0')).toBeInTheDocument()
  })

  it('shows loading, error (retry) and empty states', async () => {
    const onRetry = vi.fn()
    const { rerender } = render(<DataTable data={[]} columns={columns} getRowId={(r) => r.id} loading />)
    expect(screen.getByTestId('table-skeleton')).toBeInTheDocument()
    rerender(<DataTable data={[]} columns={columns} getRowId={(r) => r.id} error="HTTP 500" onRetry={onRetry} />)
    expect(screen.getByText('HTTP 500')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Yeniden dene' }))
    expect(onRetry).toHaveBeenCalled()
    rerender(<DataTable data={[]} columns={columns} getRowId={(r) => r.id} />)
    expect(screen.getByText('Veri yok')).toBeInTheDocument()
  })

  it('hides columns through the visibility prop', () => {
    render(<DataTable data={makeRows(2)} columns={columns} getRowId={(r) => r.id} columnVisibility={{ qty: false }} />)
    expect(screen.queryByRole('columnheader', { name: 'Qty' })).not.toBeInTheDocument()
  })

  it('renders cards with mobileRender in mobile mode', () => {
    render(<DataTable data={makeRows(3)} columns={columns} getRowId={(r) => r.id} mobile mobileRender={(r) => <b>card {r.name}</b>} />)
    expect(screen.getByText('card Item 1')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader')).not.toBeInTheDocument()
  })
})

describe('SplitView', () => {
  it('desktop: shows list and detail, divider resizes by keyboard and persists', async () => {
    const user = userEvent.setup()
    render(<SplitView storageKey="t" list={<div>LIST</div>} detail={<div>DETAIL</div>} hasDetail onCloseDetail={() => {}} defaultWidth={400} />)
    expect(screen.getByText('LIST')).toBeInTheDocument()
    expect(screen.getByText('DETAIL')).toBeInTheDocument()
    const sep = screen.getByRole('separator')
    sep.focus()
    await user.keyboard('{ArrowRight}')
    expect(sep).toHaveAttribute('aria-valuenow', '416')
    expect(localStorage.getItem('papi-split-t')).toBe('416')
  })

  it('restores the persisted width', () => {
    localStorage.setItem('papi-split-t2', '500')
    render(<SplitView storageKey="t2" list={<div>L</div>} detail={<div>D</div>} hasDetail onCloseDetail={() => {}} />)
    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuenow', '500')
  })

  it('Escape closes the detail', async () => {
    const onClose = vi.fn()
    render(<SplitView list={<button>L</button>} detail={<button>D</button>} hasDetail onCloseDetail={onClose} />)
    screen.getByText('D').focus()
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })

  it('mobile: list only, then a full-screen detail with a back button', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }))
    const onClose = vi.fn()
    const { rerender } = render(<SplitView list={<div>LIST</div>} detail={<div>DETAIL</div>} hasDetail={false} onCloseDetail={onClose} />)
    expect(screen.queryByText('DETAIL')).not.toBeInTheDocument()
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
    rerender(<SplitView list={<div>LIST</div>} detail={<div>DETAIL</div>} hasDetail onCloseDetail={onClose} />)
    expect(screen.getByTestId('detail-panel')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Geri' }))
    expect(onClose).toHaveBeenCalled()
  })
})

describe('DocumentView', () => {
  const html = '<p id="a1">Merhaba</p><table><tr><td>Toplam</td><td>1.234,56</td></tr></table>'

  it('renders trusted html and marks numeric cells', () => {
    render(<DocumentView html={html} />)
    expect(screen.getByText('Merhaba')).toBeInTheDocument()
    expect(screen.getByText('1.234,56')).toHaveAttribute('data-num')
    expect(screen.getByText('Toplam')).not.toHaveAttribute('data-num')
  })

  it('scrolls to and flashes the highlighted anchor', () => {
    const spy = vi.spyOn(Element.prototype, 'scrollIntoView')
    render(<DocumentView html={html} highlightAnchor="a1" />)
    expect(spy).toHaveBeenCalled()
    expect(screen.getByText('Merhaba')).toHaveClass('doc-flash')
    spy.mockRestore()
  })
})

describe('FilterBar', () => {
  it('toggles priorities and clears', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<FilterBar value={{ priorities: ['P1'] }} onChange={onChange} scopeOptions={[{ value: 'portfolio', label: 'Portföy' }]} />)
    await user.click(screen.getByRole('button', { name: 'P2' }))
    expect(onChange).toHaveBeenLastCalledWith({ priorities: ['P1', 'P2'] })
    await user.click(screen.getByRole('button', { name: 'P1' }))
    expect(onChange).toHaveBeenLastCalledWith({ priorities: [] })
    await user.selectOptions(screen.getByLabelText('Kapsam'), 'portfolio')
    expect(onChange).toHaveBeenLastCalledWith({ priorities: ['P1'], scope: 'portfolio' })
    await user.click(screen.getByRole('button', { name: /Temizle/ }))
    expect(onChange).toHaveBeenLastCalledWith({})
  })

  it('commits the ticker upper-cased after typing', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    render(<FilterBar value={{}} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('Kod'), { target: { value: 'thy' } })
    await act(() => vi.advanceTimersByTimeAsync(300))
    expect(onChange).toHaveBeenLastCalledWith({ ticker: 'THY' })
    vi.useRealTimers()
  })
})

describe('live helpers', () => {
  it('NewItemsPill shows the count and reveals on click', async () => {
    const onClick = vi.fn()
    const { rerender } = render(<NewItemsPill count={0} onClick={onClick} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    rerender(<NewItemsPill count={3} onClick={onClick} />)
    await userEvent.click(screen.getByRole('button', { name: '3 yeni bildirim' }))
    expect(onClick).toHaveBeenCalled()
  })

  it('useHeldItems holds back items that arrive after the first load', () => {
    const a = { id: 'a' }
    const b = { id: 'b' }
    const { result, rerender } = renderHook(({ items }) => useHeldItems(items, (x) => x.id), { initialProps: { items: [a] } })
    expect(result.current.visible).toEqual([a])
    rerender({ items: [b, a] })
    expect(result.current.visible).toEqual([a])
    expect(result.current.pending).toBe(1)
    act(() => result.current.reveal())
    expect(result.current.visible).toEqual([b, a])
    expect(result.current.pending).toBe(0)
  })

  it('livePollInterval polls every 5 s only while the tab is visible', () => {
    const interval = livePollInterval()
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    expect(interval()).toBe(5000)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    expect(interval()).toBe(false)
    vi.restoreAllMocks()
  })

  it('useLivePoll re-fetches on the interval', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fn = vi.fn(async () => 1)
    const qc = new QueryClient()
    renderHook(() => useLivePoll({ queryKey: ['x'], queryFn: fn }), {
      wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
    })
    await act(() => vi.advanceTimersByTimeAsync(100))
    expect(fn).toHaveBeenCalledTimes(1)
    await act(() => vi.advanceTimersByTimeAsync(5100))
    expect(fn.mock.calls.length).toBeGreaterThanOrEqual(2)
    vi.useRealTimers()
  })
})
