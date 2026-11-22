import { useTranslation } from 'react-i18next'
import { useNow } from '@/hooks/use-now'
import { formatDateTime, relativeParts } from '@/lib/format'
import { cn } from '@/lib/utils'

/** "3 dk önce" / "3m ago", refreshed automatically; absolute Europe/Istanbul time in the hover title. */
export function RelativeTime({
  iso,
  now,
  className,
}: {
  iso: string | null | undefined
  /** Override "now" (ms), e.g. the server clock or a test. Disables auto-refresh effects on the output. */
  now?: number
  className?: string
}) {
  const { t, i18n } = useTranslation()
  const tick = useNow()
  if (!iso) return <span className={className}>{'–'}</span>
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return <span className={className}>{'–'}</span>
  const { unit, n } = relativeParts(((now ?? tick) - ts) / 1000)
  return (
    <time dateTime={iso} title={formatDateTime(ts, { lang: i18n.language })} className={cn('whitespace-nowrap', className)}>
      {t(`kit.time.${unit}`, { n })}
    </time>
  )
}
