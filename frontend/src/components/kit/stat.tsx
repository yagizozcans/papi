import type * as React from 'react'
import { cn } from '@/lib/utils'
import { Delta } from './delta'

/** Tiny inline SVG sparkline; colour follows the overall trend (up/down) unless `tone` is given. */
export function Sparkline({
  values,
  width = 72,
  height = 24,
  tone,
  className,
}: {
  values: number[]
  width?: number
  height?: number
  tone?: 'up' | 'down' | 'muted'
  className?: string
}) {
  const pts = values.filter((v) => Number.isFinite(v))
  if (pts.length < 2) return null
  const min = Math.min(...pts)
  const max = Math.max(...pts)
  const span = max - min || 1
  const pad = 2
  const d = pts
    .map((v, i) => {
      const x = (i / (pts.length - 1)) * width
      const y = pad + (1 - (v - min) / span) * (height - pad * 2)
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)} ${y.toFixed(1)}`
    })
    .join(' ')
  const trend = tone ?? (pts[pts.length - 1]! >= pts[0]! ? 'up' : 'down')
  return (
    <svg
      role="img"
      aria-hidden="true"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className={cn('shrink-0', trend === 'up' ? 'text-up' : trend === 'down' ? 'text-down' : 'text-muted-foreground', className)}
    >
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

/** KPI tile: small uppercase label, large mono value, optional delta and sparkline. */
export function Stat({
  label,
  value,
  delta,
  deltaPercent = true,
  sparkline,
  hint,
  className,
}: {
  label: React.ReactNode
  value: React.ReactNode
  delta?: number | null
  /** Format `delta` as a percentage (default) instead of a plain number. */
  deltaPercent?: boolean
  sparkline?: number[]
  hint?: React.ReactNode
  className?: string
}) {
  return (
    <div data-slot="stat" className={cn('flex min-w-0 items-end justify-between gap-2 rounded-md border border-border bg-card px-3 py-2', className)}>
      <div className="min-w-0">
        <div className="truncate text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className="num mt-0.5 truncate text-xl font-semibold leading-6">{value}</div>
        {(delta !== undefined || hint) && (
          <div className="mt-0.5 flex items-center gap-2 text-sm">
            {delta !== undefined && <Delta value={delta} percent={deltaPercent} />}
            {hint && <span className="text-muted-foreground">{hint}</span>}
          </div>
        )}
      </div>
      {sparkline && <Sparkline values={sparkline} />}
    </div>
  )
}
