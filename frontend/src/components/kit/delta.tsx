import { ArrowDown, ArrowUp, Minus } from 'lucide-react'
import { formatNum, formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'

/**
 * Signed change with colour + arrow (the arrow keeps it readable without colour).
 * Up/down colours come from --up/--down, which switch to blue/orange when `html[data-cb]` is set
 * (see lib/color-blind.ts).
 */
export function Delta({
  value,
  percent = false,
  decimals = 2,
  arrow = true,
  className,
}: {
  value: number | null | undefined
  /** `value` is already a percentage (4.2 means 4.2 %). */
  percent?: boolean
  decimals?: number
  arrow?: boolean
  className?: string
}) {
  const dir = value == null || !Number.isFinite(value) || value === 0 ? 0 : value > 0 ? 1 : -1
  const text = percent ? formatPercent(value, { decimals, signed: true }) : formatNum(value, { decimals, signed: true })
  const Icon = dir > 0 ? ArrowUp : dir < 0 ? ArrowDown : Minus
  return (
    <span
      data-dir={dir > 0 ? 'up' : dir < 0 ? 'down' : 'flat'}
      className={cn(
        'num inline-flex items-center gap-0.5 whitespace-nowrap',
        dir > 0 ? 'text-up' : dir < 0 ? 'text-down' : 'text-muted-foreground',
        className,
      )}
    >
      {arrow && <Icon aria-hidden="true" className="size-3" strokeWidth={2.5} />}
      {text}
    </span>
  )
}
