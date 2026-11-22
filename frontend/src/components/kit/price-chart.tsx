import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  LineSeries,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useColorBlind } from '@/lib/color-blind'
import { formatNum } from '@/lib/format'
import { useTheme } from '@/lib/theme'
import { cn } from '@/lib/utils'
import { PriorityBadge, type Priority } from './priority-badge'

/** Day string "YYYY-MM-DD" (UTC) or unix seconds. */
export type ChartTime = string | number

export interface PricePoint {
  time: ChartTime
  close: number
  open?: number | null
  high?: number | null
  low?: number | null
  volume?: number | null
}

export interface ChartEvent {
  time: ChartTime
  priority: Priority
  /** Shown in the crosshair legend. */
  label?: string
}

export interface ChartTransaction {
  time: ChartTime
  side: 'buy' | 'sell'
  quantity?: number
  price?: number
}

export type ChartRange = '1m' | '3m' | '6m' | '1y' | 'ytd' | 'all'
export const CHART_RANGES: ChartRange[] = ['1m', '3m', '6m', '1y', 'ytd', 'all']

export interface PriceChartProps {
  data: readonly PricePoint[]
  events?: readonly ChartEvent[]
  transactions?: readonly ChartTransaction[]
  /** Initial mode; the toolbar toggle appears when the data has OHLC. */
  defaultMode?: 'line' | 'candles'
  showVolume?: boolean
  /** Visible range (controlled) or initial range. */
  range?: ChartRange
  defaultRange?: ChartRange
  onRangeChange?: (range: ChartRange) => void
  /** Chart height in px (default 320). */
  height?: number
  className?: string
}

export function toSeconds(t: ChartTime): number {
  return typeof t === 'number' ? t : Date.parse(`${t.length === 10 ? t : t.slice(0, 10)}T00:00:00Z`) / 1000
}

/** Start of the visible window (unix seconds) for a range ending at `last`, or null for "all". */
export function rangeStart(range: ChartRange, last: number): number | null {
  if (range === 'all') return null
  const d = new Date(last * 1000)
  if (range === 'ytd') return Date.UTC(d.getUTCFullYear() - 1, 11, 31) / 1000
  const months = { '1m': 1, '3m': 3, '6m': 6, '1y': 12 }[range]
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - months, d.getUTCDate()) / 1000
}

interface Colors {
  card: string
  text: string
  border: string
  brand: string
  up: string
  down: string
  p: Record<Priority, string>
}

function readColors(el: HTMLElement): Colors {
  const cs = getComputedStyle(el)
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback
  return {
    card: v('--card', '#0e0e11'),
    text: v('--muted-foreground', '#a1a1aa'),
    border: v('--border', '#27272a'),
    brand: v('--brand', '#3b82f6'),
    up: v('--up', '#22c55e'),
    down: v('--down', '#f87171'),
    p: { P1: v('--p1', '#f87171'), P2: v('--p2', '#fbbf24'), P3: v('--p3', '#a1a1aa') },
  }
}

function withAlpha(color: string, alpha: string): string {
  return /^#[0-9a-f]{6}$/i.test(color) ? color + alpha : color
}

interface Legend {
  time: number
  point: PricePoint
  events: ChartEvent[]
  txns: ChartTransaction[]
}

function hasOhlc(p: PricePoint) {
  return p.open != null && p.high != null && p.low != null
}

export function PriceChart({
  data,
  events,
  transactions,
  defaultMode = 'line',
  showVolume = false,
  range: rangeProp,
  defaultRange = '1y',
  onRangeChange,
  height = 320,
  className,
}: PriceChartProps) {
  const { t, i18n } = useTranslation()
  const theme = useTheme()
  const colorBlind = useColorBlind()
  const themeKey = `${theme}-${colorBlind}`

  const [modeState, setMode] = useState(defaultMode)
  const [rangeState, setRange] = useState<ChartRange>(defaultRange)
  const range = rangeProp ?? rangeState
  const [hover, setHover] = useState<number | null>(null)

  const sorted = useMemo(
    () => [...data].map((p) => ({ ...p, time: toSeconds(p.time) })).sort((a, b) => (a.time as number) - (b.time as number)) as (PricePoint & { time: number })[],
    [data],
  )
  const ohlc = useMemo(() => sorted.some(hasOhlc), [sorted])
  const mode = modeState === 'candles' && ohlc ? 'candles' : 'line'
  const volume = showVolume && sorted.some((p) => p.volume != null)

  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const mainRef = useRef<ISeriesApi<'Line'> | ISeriesApi<'Candlestick'> | null>(null)
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null)

  // Chart lifetime: create once, resize with the container, remove on unmount.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: { attributionLogo: true, fontFamily: 'JetBrains Mono, ui-monospace, monospace', fontSize: 11 },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderVisible: true },
      timeScale: { borderVisible: true, rightOffset: 2 },
      handleScale: { mouseWheel: true },
    })
    chartRef.current = chart
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? el.clientWidth
      if (w > 0) chart.applyOptions({ width: Math.floor(w) })
    })
    ro.observe(el)
    const onMove = (p: MouseEventParams<Time>) => {
      setHover(typeof p.time === 'number' && p.point ? p.time : null)
    }
    chart.subscribeCrosshairMove(onMove)
    return () => {
      chart.unsubscribeCrosshairMove(onMove)
      ro.disconnect()
      chart.remove()
      chartRef.current = null
      mainRef.current = null
      volRef.current = null
    }
    // height is applied by the effect below; the chart itself must only be created once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    chartRef.current?.applyOptions({ height })
  }, [height])

  // Theme tokens (re-applied when theme or colour-blind mode changes).
  useEffect(() => {
    const el = containerRef.current
    const chart = chartRef.current
    if (!el || !chart) return
    const c = readColors(el)
    chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: c.card }, textColor: c.text, attributionLogo: true },
      grid: { vertLines: { color: withAlpha(c.border, '99') }, horzLines: { color: withAlpha(c.border, '99') } },
      crosshair: {
        vertLine: { color: c.text, labelBackgroundColor: c.border },
        horzLine: { color: c.text, labelBackgroundColor: c.border },
      },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border },
      localization: { locale: i18n.language === 'en' ? 'en-US' : 'tr-TR' },
    })
  }, [themeKey, i18n.language])

  // Main series (line or candles) and optional volume histogram.
  useEffect(() => {
    const chart = chartRef.current
    const el = containerRef.current
    if (!chart || !el) return
    const c = readColors(el)
    const main =
      mode === 'candles'
        ? chart.addSeries(CandlestickSeries, {
            upColor: c.up,
            downColor: c.down,
            borderUpColor: c.up,
            borderDownColor: c.down,
            wickUpColor: c.up,
            wickDownColor: c.down,
          })
        : chart.addSeries(LineSeries, { color: c.brand, lineWidth: 2, priceLineVisible: true, lastValueVisible: true })
    mainRef.current = main
    let vol: ISeriesApi<'Histogram'> | null = null
    if (volume) {
      vol = chart.addSeries(HistogramSeries, { priceScaleId: 'volume', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false })
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    }
    volRef.current = vol
    return () => {
      // On unmount the chart effect has already disposed the chart (its cleanup runs first).
      if (chartRef.current === chart) {
        chart.removeSeries(main)
        if (vol) chart.removeSeries(vol)
      }
      if (mainRef.current === main) mainRef.current = null
      volRef.current = null
    }
  }, [mode, volume])

  // Data (+ colours that depend on the data: candles, volume bars).
  useEffect(() => {
    const el = containerRef.current
    const main = mainRef.current
    if (!el || !main) return
    const c = readColors(el)
    if (mode === 'candles') {
      ;(main as ISeriesApi<'Candlestick'>).applyOptions({ upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down })
      ;(main as ISeriesApi<'Candlestick'>).setData(
        sorted.map((p) => ({
          time: p.time as UTCTimestamp,
          open: p.open ?? p.close,
          high: p.high ?? Math.max(p.open ?? p.close, p.close),
          low: p.low ?? Math.min(p.open ?? p.close, p.close),
          close: p.close,
        })),
      )
    } else {
      ;(main as ISeriesApi<'Line'>).applyOptions({ color: c.brand })
      ;(main as ISeriesApi<'Line'>).setData(sorted.map((p) => ({ time: p.time as UTCTimestamp, value: p.close })))
    }
    volRef.current?.setData(
      sorted
        .filter((p) => p.volume != null)
        .map((p, i, arr) => {
          const prev = i > 0 ? arr[i - 1]!.close : (p.open ?? p.close)
          return { time: p.time as UTCTimestamp, value: p.volume as number, color: withAlpha(p.close >= (p.open ?? prev) ? c.up : c.down, '66') }
        }),
    )
  }, [sorted, mode, volume, themeKey])

  // Markers: events (coloured by priority) and transactions (arrows), snapped to the nearest earlier bar.
  const markers = useMemo(() => {
    const times = sorted.map((p) => p.time)
    const snap = (t: ChartTime): number | null => {
      const s = toSeconds(t)
      let lo = 0
      let hi = times.length - 1
      let found = -1
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (times[mid]! <= s) {
          found = mid
          lo = mid + 1
        } else hi = mid - 1
      }
      return found < 0 ? null : times[found]!
    }
    return {
      events: (events ?? []).flatMap((e) => {
        const time = snap(e.time)
        return time == null ? [] : [{ ...e, time }]
      }),
      txns: (transactions ?? []).flatMap((x) => {
        const time = snap(x.time)
        return time == null ? [] : [{ ...x, time }]
      }),
    }
  }, [sorted, events, transactions])

  useEffect(() => {
    const el = containerRef.current
    const main = mainRef.current
    if (!el || !main) return
    const c = readColors(el)
    const list: SeriesMarker<Time>[] = [
      ...markers.events.map((e) => ({
        time: e.time as UTCTimestamp,
        position: 'aboveBar' as const,
        shape: 'circle' as const,
        color: c.p[e.priority],
        size: e.priority === 'P1' ? 1.4 : 1,
        id: `e-${e.priority}`,
      })),
      ...markers.txns.map((x) =>
        x.side === 'buy'
          ? { time: x.time as UTCTimestamp, position: 'belowBar' as const, shape: 'arrowUp' as const, color: c.up, id: 'buy' }
          : { time: x.time as UTCTimestamp, position: 'aboveBar' as const, shape: 'arrowDown' as const, color: c.down, id: 'sell' },
      ),
    ].sort((a, b) => (a.time as number) - (b.time as number))
    const plugin: ISeriesMarkersPluginApi<Time> = createSeriesMarkers(main, list)
    return () => {
      try {
        plugin.detach()
      } catch {
        /* series already removed together with its chart */
      }
    }
  }, [markers, mode, volume, themeKey])

  // Visible range.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || sorted.length === 0) return
    const last = sorted[sorted.length - 1]!.time
    const from = rangeStart(range, last)
    try {
      if (from == null) chart.timeScale().fitContent()
      else chart.timeScale().setVisibleRange({ from: from as UTCTimestamp, to: last as UTCTimestamp })
    } catch {
      chart.timeScale().fitContent()
    }
  }, [range, sorted, mode, volume])

  const pickRange = (r: ChartRange) => {
    setRange(r)
    onRangeChange?.(r)
  }

  const shown: Legend | null = useMemo(() => {
    const time = hover ?? sorted[sorted.length - 1]?.time
    if (time == null) return null
    const point = sorted.find((p) => p.time === time)
    if (!point) return null
    return {
      time,
      point,
      events: markers.events.filter((e) => e.time === time),
      txns: markers.txns.filter((x) => x.time === time),
    }
  }, [hover, sorted, markers])

  const loc = i18n.language === 'en' ? 'en-US' : 'tr-TR'

  return (
    <div className={cn('flex min-w-0 flex-col', className)} data-slot="price-chart">
      <div className="flex h-8 shrink-0 items-center justify-between gap-2 border-b border-border px-2">
        <div role="group" aria-label={t('kit.chart.label')} className="flex items-center gap-0.5">
          {CHART_RANGES.map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={range === r}
              onClick={() => pickRange(r)}
              className={cn(
                'h-6 rounded-sm px-2 font-mono text-xs text-muted-foreground hover:bg-accent hover:text-foreground',
                range === r && 'bg-accent text-foreground',
              )}
            >
              {t(`kit.chart.range.${r}`)}
            </button>
          ))}
        </div>
        {ohlc && (
          <div role="group" className="flex items-center gap-0.5">
            {(['line', 'candles'] as const).map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={mode === m}
                onClick={() => setMode(m)}
                className={cn(
                  'h-6 rounded-sm px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground',
                  mode === m && 'bg-accent text-foreground',
                )}
              >
                {t(`kit.chart.${m}`)}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="relative" style={{ height }}>
        <div ref={containerRef} className="absolute inset-0" data-testid="price-chart-canvas" />
        {sorted.length === 0 && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-muted-foreground">{t('kit.chart.noData')}</div>
        )}
        {shown && (
          <div
            data-testid="chart-legend"
            className="pointer-events-none absolute left-2 top-1.5 z-10 flex max-w-[calc(100%-5rem)] flex-col gap-0.5 rounded-sm bg-card/80 px-1.5 py-1 font-mono text-xs backdrop-blur-sm"
          >
            <div className="flex flex-wrap items-center gap-x-2 text-muted-foreground">
              <span>{new Date(shown.time * 1000).toLocaleDateString(loc, { timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric' })}</span>
              {mode === 'candles' && hasOhlc(shown.point) ? (
                <>
                  <span>{t('kit.chart.open')} <b className="font-medium text-foreground">{formatNum(shown.point.open)}</b></span>
                  <span>{t('kit.chart.high')} <b className="font-medium text-foreground">{formatNum(shown.point.high)}</b></span>
                  <span>{t('kit.chart.low')} <b className="font-medium text-foreground">{formatNum(shown.point.low)}</b></span>
                  <span>{t('kit.chart.close')} <b className="font-medium text-foreground">{formatNum(shown.point.close)}</b></span>
                </>
              ) : (
                <b className="font-medium text-foreground">{formatNum(shown.point.close)}</b>
              )}
              {volume && shown.point.volume != null && (
                <span>{t('kit.chart.volume')} <b className="font-medium text-foreground">{formatNum(shown.point.volume, { decimals: 0, compact: true })}</b></span>
              )}
            </div>
            {shown.events.map((e, i) => (
              <div key={`e${i}`} className="flex items-center gap-1.5 font-sans">
                <PriorityBadge priority={e.priority} />
                {e.label && <span className="truncate text-foreground">{e.label}</span>}
              </div>
            ))}
            {shown.txns.map((x, i) => (
              <div key={`x${i}`} className={cn('font-sans', x.side === 'buy' ? 'text-up' : 'text-down')}>
                {x.side === 'buy' ? '▲' : '▼'} {t(`kit.chart.${x.side}`)}
                {x.quantity != null && ` ${formatNum(x.quantity, { decimals: 0 })}`}
                {x.price != null && ` @ ${formatNum(x.price)}`}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
