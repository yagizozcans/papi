import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  LineSeries,
  LineStyle,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useColorBlind } from '@/lib/color-blind'
import { formatPercent } from '@/lib/format'
import { useTheme } from '@/lib/theme'
import { cn } from '@/lib/utils'
import { PERF_RANGES, type Performance, type PerfRange } from './api'

const toSec = (d: string) => (Date.parse(`${d}T00:00:00Z`) / 1000) as UTCTimestamp

/** Portfolio price-return index vs the XU100 benchmark (both rebased to 100), with a range switcher. */
export function PerformanceChart({
  data,
  range,
  onRangeChange,
  height = 280,
}: {
  data?: Performance
  range: PerfRange
  onRangeChange: (r: PerfRange) => void
  height?: number
}) {
  const { t, i18n } = useTranslation()
  const theme = useTheme()
  const colorBlind = useColorBlind()
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const portfolioRef = useRef<ISeriesApi<'Line'> | null>(null)
  const benchRef = useRef<ISeriesApi<'Line'> | null>(null)

  const points = useMemo(() => data?.points ?? [], [data])
  const bench = useMemo(() => data?.benchmark ?? [], [data])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: { attributionLogo: true, fontFamily: 'JetBrains Mono, ui-monospace, monospace', fontSize: 11 },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { rightOffset: 2 },
    })
    chartRef.current = chart
    portfolioRef.current = chart.addSeries(LineSeries, { lineWidth: 2, priceLineVisible: false })
    benchRef.current = chart.addSeries(LineSeries, { lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false })
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? el.clientWidth
      if (w > 0) chart.applyOptions({ width: Math.floor(w) })
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      chart.remove()
      chartRef.current = portfolioRef.current = benchRef.current = null
    }
    // height changes are applied below; the chart is created once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    chartRef.current?.applyOptions({ height })
  }, [height])

  useEffect(() => {
    const el = ref.current
    const chart = chartRef.current
    if (!el || !chart) return
    const cs = getComputedStyle(el)
    const v = (n: string, f: string) => cs.getPropertyValue(n).trim() || f
    const border = v('--border', '#27272a')
    const text = v('--muted-foreground', '#a1a1aa')
    chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: v('--card', '#0e0e11') }, textColor: text, attributionLogo: true },
      grid: { vertLines: { color: border }, horzLines: { color: border } },
      rightPriceScale: { borderColor: border },
      timeScale: { borderColor: border },
      localization: { locale: i18n.language === 'en' ? 'en-US' : 'tr-TR' },
    })
    portfolioRef.current?.applyOptions({ color: v('--brand', '#3b82f6') })
    benchRef.current?.applyOptions({ color: text })
  }, [theme, colorBlind, i18n.language])

  useEffect(() => {
    portfolioRef.current?.setData(points.map((p) => ({ time: toSec(p.date), value: p.return_index })))
    benchRef.current?.setData(bench.map((p) => ({ time: toSec(p.date), value: p.value })))
    chartRef.current?.timeScale().fitContent()
  }, [points, bench])

  const lastP = points.at(-1)?.return_index
  const lastB = bench.at(-1)?.value

  return (
    <div className="flex min-w-0 flex-col" data-testid="performance-chart">
      <div className="flex min-h-8 flex-wrap items-center justify-between gap-2 border-b border-border px-2">
        <div role="group" aria-label={t('portfolio.performance.range')} className="flex items-center gap-0.5">
          {PERF_RANGES.map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={range === r}
              onClick={() => onRangeChange(r)}
              className={cn(
                'h-6 rounded-sm px-2 font-mono text-xs text-muted-foreground hover:bg-accent hover:text-foreground',
                range === r && 'bg-accent text-foreground',
              )}
            >
              {t(`portfolio.performance.ranges.${r}`)}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 font-mono text-xs">
          <span className="flex items-center gap-1.5">
            <span aria-hidden="true" className="h-0.5 w-3 bg-brand" />
            {t('portfolio.performance.portfolio')}
            <b className="font-medium">{lastP != null ? formatPercent(lastP - 100, { signed: true }) : '–'}</b>
          </span>
          <span className="flex items-center gap-1.5 text-muted-foreground">
            <span aria-hidden="true" className="h-0 w-3 border-t border-dashed border-muted-foreground" />
            XU100
            <b className="font-medium">{lastB != null ? formatPercent(lastB - 100, { signed: true }) : '–'}</b>
          </span>
        </div>
      </div>
      <div className="relative" style={{ height }}>
        <div ref={ref} className="absolute inset-0" />
        {points.length === 0 && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-muted-foreground">{t('portfolio.performance.noData')}</div>
        )}
      </div>
    </div>
  )
}
