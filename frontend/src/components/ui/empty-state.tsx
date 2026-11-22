import { Inbox } from 'lucide-react'
import type * as React from 'react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'

function EmptyState({
  title,
  description,
  icon,
  action,
  className,
}: {
  title?: React.ReactNode
  description?: React.ReactNode
  icon?: React.ReactNode
  action?: React.ReactNode
  className?: string
}) {
  const { t } = useTranslation()
  return (
    <div className={cn('flex flex-col items-center justify-center gap-2 px-4 py-10 text-center', className)}>
      <div className="text-muted-foreground/60">{icon ?? <Inbox className="size-6" strokeWidth={1.5} />}</div>
      <p className="font-medium">{title ?? t('kit.common.empty')}</p>
      {description && <p className="max-w-sm text-sm text-muted-foreground">{description}</p>}
      {action}
    </div>
  )
}

export { EmptyState }
