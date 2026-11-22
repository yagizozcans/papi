import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'

const h = vi.hoisted(() => {
  const series = () => ({ setData: vi.fn(), applyOptions: vi.fn(), priceScale: () => ({ applyOptions: vi.fn() }) })
  const timeScale = { setVisibleRange: vi.fn(), fitContent: vi.fn() }
  const chart = {
    addSeries: vi.fn<(kind: unknown) => ReturnType<typeof series>>(() => series()),
    removeSeries: vi.fn(),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    timeScale: vi.fn(() => timeScale),
    
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
  }
  return { chart, timeScale, detach: vi.fn(), setMarkers: vi.fn() }
})

vi.mock('lightweight-charts', () => ({
  createChart: vi.fn(() => h.chart),
  createSeriesMarkers: vi.fn((_s: unknown, markers: unknown[]) => {
    h.setMarkers(markers)
    return { detach: h.detach, setMarkers: h.setMarkers }
  }),
  LineSeries: 'Line',
  CandlestickSeries: 'Candlestick',
  HistogramSeries: 'Histogram',
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
}))

import { createChart } from 'lightweight-charts'
import { PriceChart, rangeStart } from '@/components/kit/price-chart'

const data = [
  { time: '2026-01-02', close: 10, open: 9, high: 11, low: 8, volume: 100 },
  { time: '2026-01-05', close: 11, open: 10, high: 12, low: 9.5, volume: 200 },
  { time: '2026-01-06', close: 12, open: 11, high: 13, low: 10.5, volume: 150 },
]

beforeEach(async () => {
  vi.clearAllMocks()
  await i18n.changeLanguage('tr')
})

describe('PriceChart', () => {
  it('creates the chart with the TradingView attribution logo and removes it on unmount', () => {
    const { unmount } = render(<PriceChart data={data} />)
    expect(createChart).toHaveBeenCalledTimes(1)
    const opts = vi.mocked(createChart).mock.calls[0]![1]
    expect(opts?.layout?.attributionLogo).toBe(true)
    unmount()
    expect(h.chart.remove).toHaveBeenCalledTimes(1)
  })

  it('draws a line by default, candles when toggled, and a volume histogram when asked', async () => {
    render(<PriceChart data={data} showVolume />)
    const kinds = h.chart.addSeries.mock.calls.map((c) => c[0] as string)
    expect(kinds).toEqual(['Line', 'Histogram'])
    await userEvent.click(screen.getByRole('button', { name: 'Mum' }))
    expect(h.chart.addSeries.mock.calls.map((c) => c[0] as string)).toContain('Candlestick')
  })

  it('has no candle toggle without OHLC data', () => {
    render(<PriceChart data={data.map(({ time, close }) => ({ time, close }))} />)
    expect(screen.queryByRole('button', { name: 'Mum' })).not.toBeInTheDocument()
  })

  it('maps events and transactions to markers snapped to existing bars, coloured by priority', () => {
    render(
      <PriceChart
        data={data}
        events={[{ time: '2026-01-03', priority: 'P1', label: 'Rapor' }]}
        transactions={[{ time: '2026-01-06', side: 'buy' }, { time: '2025-12-01', side: 'sell' }]}
      />,
    )
    const markers = h.setMarkers.mock.calls.at(-1)![0] as { time: number; shape: string; id: string }[]
    expect(markers).toHaveLength(2) // the sell before the first bar is dropped
    expect(markers.map((m) => m.shape).sort()).toEqual(['arrowUp', 'circle'])
    expect(markers[0]!.time).toBe(Date.UTC(2026, 0, 2) / 1000)
  })

  it('range switcher applies the visible range (i18n labels)', async () => {
    const onRange = vi.fn()
    render(<PriceChart data={data} onRangeChange={onRange} />)
    expect(screen.getByRole('button', { name: 'YBİ' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Tümü' }))
    expect(onRange).toHaveBeenCalledWith('all')
    expect(h.timeScale.fitContent).toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: '1A' }))
    expect(h.timeScale.setVisibleRange).toHaveBeenCalled()
  })

  it('computes range starts', () => {
    const last = Date.UTC(2026, 10, 22) / 1000
    expect(rangeStart('all', last)).toBeNull()
    expect(rangeStart('1m', last)).toBe(Date.UTC(2026, 9, 22) / 1000)
    expect(rangeStart('ytd', last)).toBe(Date.UTC(2025, 11, 31) / 1000)
  })

  it('re-applies colours when the theme changes', async () => {
    const { toggleTheme } = await import('@/lib/theme')
    render(<PriceChart data={data} />)
    const before = h.chart.applyOptions.mock.calls.length
    const { act } = await import('@testing-library/react')
    act(() => toggleTheme())
    expect(h.chart.applyOptions.mock.calls.length).toBeGreaterThan(before)
  })

  it('shows the last point in the legend', () => {
    render(<PriceChart data={data} />)
    expect(screen.getByTestId('chart-legend')).toHaveTextContent('12,00')
  })
})
