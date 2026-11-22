import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'

type Variant = 'up' | 'down' | 'brand' | 'warning' | 'outline'

const VARIANT: Record<string, Variant> = {
  ok: 'up',
  failed: 'down',
  deferred: 'warning',
  incomplete: 'warning',
  stale: 'warning',
  starting: 'brand',
  running: 'brand',
  resuming: 'brand',
}

export function RunStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation()
  return (
    <Badge variant={VARIANT[status] ?? 'outline'} data-testid="run-status" data-status={status}>
      {t(`agents.status.${status}`, { defaultValue: status })}
    </Badge>
  )
}
