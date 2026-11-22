import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: ReactNode
  /** Extra body (e.g. the proposal being confirmed). */
  children?: ReactNode
  confirmLabel: string
  destructive?: boolean
  pending?: boolean
  error?: string | null
  onConfirm: () => void
}

/** Confirm / cancel dialog. Stays open (showing `error`) when the action fails. */
export function ConfirmDialog({ open, onOpenChange, title, description, children, confirmLabel, destructive, pending, error, onConfirm }: Props) {
  const { t } = useTranslation()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="top-[15%] max-w-md">
        <div className="flex flex-col gap-1">
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </div>
        {children}
        {error && (
          <p role="alert" className="rounded-md border border-error/40 bg-error/10 px-2 py-1.5 text-sm text-error">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={pending}>
            {t('portfolio.common.cancel')}
          </Button>
          <Button variant={destructive ? 'outline' : 'default'} className={destructive ? 'border-error/50 text-error hover:bg-error/10' : undefined} onClick={onConfirm} disabled={pending}>
            {confirmLabel}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
