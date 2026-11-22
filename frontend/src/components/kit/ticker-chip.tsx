import { Link } from '@tanstack/react-router'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'

export type TickerMark = 'holding' | 'watch' | null | undefined

/** Mono ticker that links to the instrument page, with a small marker for holdings / watchlist. */
export function TickerChip({ ticker, mark, className }: { ticker: string; mark?: TickerMark; className?: string }) {
  const { t } = useTranslation()
  const label = mark ? t(`kit.${mark}`) : undefined
  return (
    <Link
      to="/i/$ticker"
      params={{ ticker }}
      title={label}
      onClick={(e) => e.stopPropagation()}
      className={cn(
        'inline-flex h-[18px] items-center gap-1 rounded-sm border border-border bg-muted px-1.5 font-mono text-xs font-medium leading-none text-foreground hover:border-brand/60 hover:text-brand',
        className,
      )}
    >
      {ticker}
      {mark && (
        <>
          <span
            aria-hidden="true"
            data-mark={mark}
            className={cn('size-1.5 shrink-0 rounded-full', mark === 'holding' ? 'bg-brand' : 'border border-muted-foreground')}
          />
          <span className="sr-only">{label}</span>
        </>
      )}
    </Link>
  )
}
