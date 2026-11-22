import { TriangleAlert } from 'lucide-react'
import type * as React from 'react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import { Button } from './button'

function ErrorState({
  title,
  description,
  onRetry,
  className,
}: {
  title?: React.ReactNode
  description?: React.ReactNode
  onRetry?: () => void
  className?: string
}) {
  const { t } = useTranslation()
  return (
    <div role="alert" className={cn('flex flex-col items-center justify-center gap-2 px-4 py-10 text-center', className)}>
      <TriangleAlert className="size-6 text-error" strokeWidth={1.5} />
      <p className="font-medium">{title ?? t('kit.common.error')}</p>
      {description && <p className="max-w-sm break-words text-sm text-muted-foreground">{description}</p>}
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t('kit.common.retry')}
        </Button>
      )}
    </div>
  )
}

export { ErrorState }
