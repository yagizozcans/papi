import { ArrowUp } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'

/** "3 yeni bildirim": floating pill for live lists. Renders nothing when `count` is 0. */
export function NewItemsPill({ count, onClick, className }: { count: number; onClick: () => void; className?: string }) {
  const { t } = useTranslation()
  if (count <= 0) return null
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'inline-flex h-7 items-center gap-1.5 rounded-full border border-brand/50 bg-card px-3 text-sm font-medium text-brand shadow-md hover:bg-brand hover:text-primary-foreground',
        className,
      )}
    >
      <ArrowUp className="size-3.5" />
      {t('kit.live.newItems', { count })}
    </button>
  )
}
